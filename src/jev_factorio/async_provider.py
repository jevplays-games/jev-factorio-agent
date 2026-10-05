"""Bounded, explicit async HTTP transport for one logical Jev provider client.

This module owns only in-memory request admission and an HTTPX connection pool.
It does not retry, persist request identity, cancel remote work, or mutate a
game. Callers must treat a transport-entered cancellation/timeout as an
ambiguous provider outcome and keep their existing durable gates.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import httpx
import requests

from .provider_health import ProviderPayloadError


DeliveryPhase = str
NOT_SENT: DeliveryPhase = "not_sent"
MAY_HAVE_BEEN_SENT: DeliveryPhase = "may_have_been_sent"
RESPONSE_RECEIVED: DeliveryPhase = "response_received"


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Immutable caller-provided binding for one individual paid request."""

    session_id: str
    actor_id: str
    observation_id: str
    decision_id: str
    request_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        for name in ("session_id", "actor_id", "observation_id", "decision_id", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class AsyncProviderResult:
    """Immutable response metadata bound to the identity that produced it.

    ``request_payload_sha256`` fingerprints canonical request JSON; it is not
    a wire-byte digest or a provider idempotency key.
    """

    identity: RequestIdentity
    answers: Mapping[str, Any]
    usage: Any
    requested_model: str
    resolved_model: Any
    request_payload_sha256: str


class _RequestErrorContext:
    identity: RequestIdentity
    delivery_phase: DeliveryPhase

    def _set_context(self, identity: RequestIdentity, delivery_phase: DeliveryPhase) -> None:
        self.identity = identity
        self.delivery_phase = delivery_phase
        self.may_have_been_sent = delivery_phase != NOT_SENT


class AsyncProviderLocalError(RuntimeError, _RequestErrorContext):
    """Local admission/lifecycle failure known to have happened before send."""

    def __init__(self, message: str, identity: RequestIdentity):
        RuntimeError.__init__(self, message)
        self._set_context(identity, NOT_SENT)


class AsyncProviderQueueFull(AsyncProviderLocalError):
    """The bounded waiter queue is full; no provider request was started."""


class AsyncProviderClosed(AsyncProviderLocalError):
    """The client is closing or closed; no provider request was started."""


class AsyncProviderDeadlineExceeded(AsyncProviderLocalError):
    """The absolute deadline expired before the transport was entered."""


class AsyncProviderCancelled(requests.RequestException, _RequestErrorContext):
    """Typed provider outcome for a caller-cancelled request.

    This is deliberately not an ``asyncio.CancelledError``: Python 3.10 tasks
    normalize cancellation subclasses when they cross an ``await`` boundary,
    which discards the request identity and delivery phase. Awaiters receive
    this provider exception, so its task is done with an exception rather than
    reporting ``Task.cancelled()`` as true.
    """

    def __init__(self, identity: RequestIdentity, delivery_phase: DeliveryPhase):
        requests.RequestException.__init__(self, "provider request cancelled")
        self._set_context(identity, delivery_phase)


class AsyncProviderTimeout(requests.Timeout, _RequestErrorContext):
    """HTTPX timeout translated to the existing requests/provider taxonomy."""

    def __init__(self, identity: RequestIdentity, delivery_phase: DeliveryPhase):
        requests.Timeout.__init__(self, "provider request timed out")
        self._set_context(identity, delivery_phase)


class AsyncProviderConnectionError(requests.ConnectionError, _RequestErrorContext):
    """HTTPX connection failure translated to the existing error taxonomy."""

    def __init__(self, identity: RequestIdentity, delivery_phase: DeliveryPhase):
        requests.ConnectionError.__init__(self, "provider transport failed")
        self._set_context(identity, delivery_phase)


class AsyncProviderHTTPError(requests.HTTPError, _RequestErrorContext):
    """Sanitized HTTP status failure compatible with ProviderCircuit."""

    def __init__(self, status_code: int, headers: Mapping[str, str],
                 identity: RequestIdentity):
        safe_response = requests.Response()
        safe_response.status_code = status_code
        if "Retry-After" in headers:
            safe_response.headers["Retry-After"] = headers["Retry-After"]
        requests.HTTPError.__init__(self, f"provider returned HTTP {status_code}",
                                    response=safe_response)
        self._set_context(identity, RESPONSE_RECEIVED)


class AsyncProviderPayloadError(ProviderPayloadError, _RequestErrorContext):
    """Invalid provider response envelope with per-request delivery context."""

    def __init__(self, identity: RequestIdentity, message: str = "Invalid provider response envelope"):
        ProviderPayloadError.__init__(self, message)
        self._set_context(identity, RESPONSE_RECEIVED)


def snapshot_json(value: Any) -> Any:
    """Validate and detach JSON data before any admission await."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("provider request data must be finite JSON") from error


def request_payload_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, allow_nan=False,
                           sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def make_result(identity: RequestIdentity, answers: dict[str, Any], usage: Any,
                requested_model: str, resolved_model: Any,
                payload_sha256: str) -> AsyncProviderResult:
    if not isinstance(identity, RequestIdentity):
        raise TypeError("identity must be a RequestIdentity")
    if not isinstance(answers, dict):
        raise AsyncProviderPayloadError(identity)
    try:
        frozen_answers = _freeze_json(snapshot_json(answers))
        frozen_usage = _freeze_json(snapshot_json(usage)) if usage is not None else None
        frozen_model = (_freeze_json(snapshot_json(resolved_model))
                        if resolved_model is not None else None)
    except (TypeError, ValueError, OverflowError) as error:
        raise AsyncProviderPayloadError(identity) from error
    return AsyncProviderResult(identity=identity, answers=frozen_answers, usage=frozen_usage,
                               requested_model=requested_model, resolved_model=frozen_model,
                               request_payload_sha256=payload_sha256)


class AsyncProviderClient:
    """One reusable pooled client with bounded active and queued requests.

    ``deadline`` is an absolute ``time.monotonic()`` value and covers both
    queue admission and active transport. Cancellation after ``post`` is
    entered is conservatively ambiguous; this class never retries it.
    """

    def __init__(self, *, http_client: httpx.AsyncClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 owns_client: bool | None = None, max_concurrency: int = 4,
                 max_queue: int = 16, timeout: float = 10.0,
                 connect_timeout: float | None = None,
                 pool_timeout: float | None = None):
        if http_client is not None and transport is not None:
            raise ValueError("pass either http_client or transport, not both")
        if owns_client is not None and type(owns_client) is not bool:
            raise ValueError("owns_client must be a boolean")
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if type(max_queue) is not int or max_queue < 0:
            raise ValueError("max_queue must be a non-negative integer")
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a positive finite number")
        connect = min(float(timeout), 5.0) if connect_timeout is None else connect_timeout
        pool = min(float(timeout), 2.0) if pool_timeout is None else pool_timeout
        for name, value in (("connect_timeout", connect), ("pool_timeout", pool)):
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a positive finite number")

        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self.timeout = float(timeout)
        self._condition = asyncio.Condition()
        self._active = 0
        self._waiting = 0
        self._waiters: deque[object] = deque()
        self._closing = False
        self._close_lock = asyncio.Lock()
        self._client_closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        if http_client is None:
            self._owns_client = True
            limits = httpx.Limits(max_connections=max_concurrency,
                                  max_keepalive_connections=max_concurrency)
            http_client = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(self.timeout, connect=float(connect), pool=float(pool)),
                limits=limits,
            )
        else:
            # An injected client is borrowed unless ownership transfer is
            # explicit, so one wrapper cannot close a shared pool by accident.
            self._owns_client = owns_client is True
        self.http_client = http_client

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("an async provider client is bound to one event loop")

    async def __aenter__(self):
        self._bind_loop()
        if self._closing:
            raise RuntimeError("provider client is closed")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()

    async def _acquire(self, identity: RequestIdentity, deadline: float) -> None:
        self._bind_loop()
        async with self._condition:
            if self._closing:
                raise AsyncProviderClosed("provider client is closing", identity)
            if time.monotonic() >= deadline:
                raise AsyncProviderDeadlineExceeded("provider request deadline expired", identity)
            if self._active < self.max_concurrency and self._waiting == 0:
                self._active += 1
                return
            if self._waiting >= self.max_queue:
                raise AsyncProviderQueueFull("provider request queue is full", identity)
            waiter = object()
            self._waiters.append(waiter)
            self._waiting += 1
            queued = True
            try:
                while True:
                    if self._closing:
                        raise AsyncProviderClosed("provider client is closing", identity)
                    if time.monotonic() >= deadline:
                        raise AsyncProviderDeadlineExceeded("provider request deadline expired", identity)
                    if self._active < self.max_concurrency and self._waiters[0] is waiter:
                        self._waiters.popleft()
                        self._waiting -= 1
                        queued = False
                        self._active += 1
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AsyncProviderDeadlineExceeded(
                            "provider request deadline expired", identity
                        )
                    task = asyncio.current_task()
                    if task is None:
                        raise RuntimeError("provider admission requires an asyncio task")
                    timed_out = False

                    def cancel_at_deadline() -> None:
                        nonlocal timed_out
                        timed_out = True
                        task.cancel()

                    timeout_handle = asyncio.get_running_loop().call_later(
                        remaining, cancel_at_deadline
                    )
                    try:
                        # Await Condition.wait directly. asyncio.wait_for creates
                        # an inner task whose repeated cancellation can escape
                        # before Condition.wait reacquires this lock on Python 3.10.
                        await self._condition.wait()
                    except asyncio.CancelledError:
                        if timed_out:
                            raise AsyncProviderDeadlineExceeded(
                                "provider request deadline expired", identity
                            ) from None
                        raise
                    finally:
                        timeout_handle.cancel()
            finally:
                if queued:
                    self._waiters.remove(waiter)
                    self._waiting -= 1
                self._condition.notify_all()

    async def _release(self) -> asyncio.CancelledError | None:
        """Release one active slot before honoring cancellation during cleanup.

        Cancellation can arrive while this task waits to reacquire the
        admission condition after a request has already been classified. Keep
        waiting for the lock, then decrement exactly once; return the first
        deferred cancellation so ``post_json`` can preserve or classify it.
        """
        deferred_cancellation = None
        while True:
            try:
                async with self._condition:
                    self._active -= 1
                    self._condition.notify_all()
                return deferred_cancellation
            except asyncio.CancelledError as error:
                if deferred_cancellation is None:
                    deferred_cancellation = error

    async def post_json(self, *, identity: RequestIdentity, url: str,
                        headers: Mapping[str, str], payload: dict[str, Any],
                        deadline: float | None = None) -> tuple[dict[str, Any], str]:
        if not isinstance(identity, RequestIdentity):
            raise TypeError("identity must be a RequestIdentity")
        request_payload = snapshot_json(payload)
        if not isinstance(request_payload, dict):
            raise ValueError("provider request payload must be a JSON object")
        request_sha256 = request_payload_sha256(request_payload)
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        if (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                or not math.isfinite(deadline)):
            raise ValueError("deadline must be an absolute finite monotonic timestamp")

        acquired = False
        delivery_phase = NOT_SENT
        operation_failed = False
        try:
            try:
                await self._acquire(identity, float(deadline))
            except asyncio.CancelledError as error:
                raise AsyncProviderCancelled(identity, NOT_SENT) from error
            acquired = True
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                raise AsyncProviderDeadlineExceeded("provider request deadline expired", identity)

            try:
                # Entering HTTPX is the conservative ambiguity boundary. HTTPX
                # cannot prove whether a remote peer acted after later cancel.
                delivery_phase = MAY_HAVE_BEEN_SENT
                response = await asyncio.wait_for(
                    self.http_client.post(url, headers=dict(headers), json=request_payload,
                                          follow_redirects=True),
                    timeout=remaining,
                )
            except asyncio.CancelledError as error:
                raise AsyncProviderCancelled(identity, MAY_HAVE_BEEN_SENT) from error
            # Once HTTPX is entered, even a connect/pool failure can happen
            # after a redirect response from an earlier POST hop. Without
            # tracking each redirect response boundary, classify every
            # transport exception conservatively as possibly sent.
            except httpx.TimeoutException as error:
                raise AsyncProviderTimeout(identity, MAY_HAVE_BEEN_SENT) from error
            except asyncio.TimeoutError as error:
                raise AsyncProviderTimeout(identity, MAY_HAVE_BEEN_SENT) from error
            except httpx.ConnectError as error:
                raise AsyncProviderConnectionError(identity, MAY_HAVE_BEEN_SENT) from error
            except httpx.TransportError as error:
                raise AsyncProviderConnectionError(identity, MAY_HAVE_BEEN_SENT) from error

            delivery_phase = RESPONSE_RECEIVED
            if time.monotonic() > float(deadline):
                raise AsyncProviderTimeout(identity, MAY_HAVE_BEEN_SENT)
            if response.is_error:
                raise AsyncProviderHTTPError(response.status_code, response.headers, identity)
            try:
                body = response.json()
            except (ValueError, TypeError) as error:
                raise AsyncProviderPayloadError(identity) from error
            if not isinstance(body, dict):
                raise AsyncProviderPayloadError(identity)
            return body, request_sha256
        except BaseException:
            operation_failed = True
            raise
        finally:
            if acquired:
                deferred_cancellation = await self._release()
                if deferred_cancellation is not None and not operation_failed:
                    raise AsyncProviderCancelled(identity, delivery_phase) from deferred_cancellation

    async def aclose(self) -> None:
        self._bind_loop()
        async with self._condition:
            self._closing = True
            self._condition.notify_all()
            while self._active or self._waiting:
                await self._condition.wait()
        async with self._close_lock:
            if not self._client_closed:
                if self._owns_client:
                    await self.http_client.aclose()
                self._client_closed = True
