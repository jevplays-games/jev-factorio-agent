"""Opt-in binding between one async provider call, health gate, and decision WAL.

This module does not authorize a game action or consume a saved answer. The
controller integration stage must consume only after its selected plan is
durable. Credentials and HTTP headers are deliberately absent from this API.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .provider_decision_wal import (
    MAY_HAVE_BEEN_SENT,
    NOT_SENT,
    RESPONSE_RECEIVED,
    DecisionRecord,
    ProviderDecisionIdentity,
    ProviderDecisionWAL,
    WALNotFound,
    canonical_sha256,
)


def _copy_json(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":"))
    return json.loads(encoded)


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _retry_after(error, now: float) -> tuple[dict | None, int | None]:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None, status
    raw = headers.get("Retry-After")
    if raw is None:
        return None, status
    try:
        seconds = float(raw)
        if not math.isfinite(seconds):
            return None, status
        seconds = min(3600.0, max(0.0, seconds))
    except (ValueError, TypeError):
        try:
            retry_at = parsedate_to_datetime(raw).timestamp()
            seconds = min(3600.0, max(0.0, retry_at - now))
        except (ValueError, TypeError, OverflowError):
            return None, status
    return {"received_at": float(now), "retry_after_seconds": seconds}, status


@dataclass(frozen=True, slots=True)
class ProviderDecisionLease:
    """Immutable exact-request lease used by an explicitly WAL-bound call."""

    wal: ProviderDecisionWAL
    identity: ProviderDecisionIdentity
    request_identity: Any
    request_json: str
    wal_request_json: str
    request_sha256: str
    request_payload_sha256: str
    health_state_sha256: str
    wal_id: str
    provider_id: str
    model_id: str
    trace_binding: Any
    clock: Any

    @classmethod
    def prepare(cls, wal: ProviderDecisionWAL, client, state: dict,
                questions: dict, *, identity, clock,
                health_state_sha256: str) -> ProviderDecisionLease:
        if not isinstance(wal, ProviderDecisionWAL):
            raise TypeError("wal must be a ProviderDecisionWAL")
        prepare_payload = getattr(client, "prepare_decision_payload", None)
        provider_identity = getattr(client, "decision_provider_id", None)
        if not callable(prepare_payload) or not callable(provider_identity):
            raise TypeError("Provider client does not support decision leases")
        payload = _copy_json(prepare_payload(state, questions))
        if type(payload) is not dict:
            raise ValueError("Prepared provider payload must be a JSON object")
        model = getattr(client, "model", None)
        if type(model) is not str or not model.strip():
            raise ValueError("Decision lease requires a pinned provider model")
        provider_id = provider_identity()
        if type(provider_id) is not str:
            raise ValueError("Decision lease requires a sanitized provider identity")
        request_identity = identity
        binding = ProviderDecisionIdentity(
            session_id=identity.session_id,
            actor_id=identity.actor_id,
            observation_id=identity.observation_id,
            decision_id=identity.decision_id,
            request_id=identity.request_id,
            provider_id=provider_id,
            model_id=model,
        )
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        payload_digest = canonical_sha256(payload)
        trace_binding = None
        trace_prepare = getattr(client, "prepare_decision_trace_binding", None)
        if callable(trace_prepare):
            trace_binding = trace_prepare(identity)
            if trace_binding is not None and (
                    type(trace_binding) is not dict or set(trace_binding) != {
                    "session_id", "actor_id", "observation_id", "decision_id",
                    "model_call_id"}):
                raise ValueError("Causal trace binding is malformed")
            if (trace_binding is not None and any(
                    type(value) is not str or not value.strip()
                    for value in trace_binding.values())):
                raise ValueError("Causal trace binding identifiers must be non-empty")
        wal_request = {"provider_payload_sha256": payload_digest,
                       "trace_binding": trace_binding,
                       "health_state_sha256": health_state_sha256}
        wal_request_json = json.dumps(
            wal_request, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )
        request_digest = canonical_sha256(wal_request)
        wal_id = hashlib.sha256(str(Path(wal.path).resolve()).encode("utf-8")).hexdigest()
        if not callable(clock) or not math.isfinite(float(clock())):
            raise ValueError("Decision lease requires a finite wall-clock source")
        return cls(wal, binding, request_identity, encoded, wal_request_json,
                   request_digest, payload_digest, health_state_sha256, wal_id,
                   provider_id, model,
                   _freeze(trace_binding) if trace_binding is not None else None, clock)

    @property
    def payload(self) -> dict:
        return json.loads(self.request_json)

    @property
    def frozen_payload(self):
        return _freeze(self.payload)

    @property
    def wal_request(self) -> dict:
        return json.loads(self.wal_request_json)

    def binding(self) -> dict:
        return {
            "wal_id": self.wal_id,
            "identity": self.identity.to_dict(),
            "request_sha256": self.request_sha256,
            "health_state_sha256": self.health_state_sha256,
        }

    def assert_call(self, client, state: dict, questions: dict, identity) -> None:
        if identity != self.request_identity:
            raise ValueError("Decision lease request identity changed")
        prepare_payload = getattr(client, "prepare_decision_payload", None)
        provider_identity = getattr(client, "decision_provider_id", None)
        if not callable(prepare_payload) or not callable(provider_identity):
            raise TypeError("Provider client does not support decision leases")
        current_payload = _copy_json(prepare_payload(state, questions))
        model = getattr(client, "model", None)
        trace_binding = None
        trace_prepare = getattr(client, "prepare_decision_trace_binding", None)
        if callable(trace_prepare):
            trace_binding = trace_prepare(identity)
        if (current_payload != self.payload
                or canonical_sha256(current_payload) != self.request_payload_sha256
                or provider_identity() != self.provider_id or model != self.model_id
                or trace_binding != _thaw(self.trace_binding)):
            raise ValueError("Decision lease provider configuration or request changed")

    def assert_transport_payload(self, identity, payload: dict,
                                 payload_sha256: str) -> None:
        if (identity != self.request_identity or payload != self.payload
                or payload_sha256 != self.request_payload_sha256
                or payload_sha256 != canonical_sha256(payload)):
            raise ValueError("Provider transport payload differs from its decision lease")

    def reserve(self) -> DecisionRecord:
        return self.wal.reserve(self.identity, self.wal_request)

    def inspect(self) -> DecisionRecord:
        return self.wal.inspect(self.identity, self.wal_request)

    def inspect_optional(self) -> DecisionRecord | None:
        try:
            return self.inspect()
        except WALNotFound:
            return None

    def mark_may_have_been_sent(self) -> DecisionRecord:
        return self.wal.mark_may_have_been_sent(self.identity, self.wal_request)

    def save_response(self, result) -> DecisionRecord:
        if result.identity != self.request_identity:
            raise ValueError("Provider result identity changed before WAL save")
        if result.request_payload_sha256 != self.request_payload_sha256:
            raise ValueError("Provider result request digest changed before WAL save")
        value = {
            "answers": _thaw(result.answers),
            "usage": _thaw(result.usage),
            "requested_model": result.requested_model,
            "resolved_model": _thaw(result.resolved_model),
        }
        return self.wal.save_response(self.identity, self.wal_request, value)

    def recover_result(self, record: DecisionRecord | None = None):
        from .async_provider import make_result

        saved = self.wal.recover_response(self.identity, self.wal_request) if record is None else record
        if saved.state != "response_received" or saved.result is None:
            raise ValueError("Decision WAL does not contain an unconsumed response")
        result = _thaw(saved.result)
        return make_result(
            self.request_identity,
            result["answers"], result["usage"], result["requested_model"],
            result["resolved_model"], self.request_payload_sha256,
        )

    def record_failure(self, error: BaseException, phase: str) -> DecisionRecord:
        from .async_provider import (
            AsyncProviderCancelled,
            AsyncProviderConnectionError,
            AsyncProviderDeadlineExceeded,
            AsyncProviderLocalError,
            AsyncProviderPayloadError,
            AsyncProviderTimeout,
        )
        from .provider_health import category

        if phase not in {NOT_SENT, MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED}:
            raise ValueError("Unsupported provider delivery phase")
        http_status = None
        cooldown = None
        if isinstance(error, AsyncProviderLocalError) or phase == NOT_SENT:
            error_category = "local_admission"
        elif isinstance(error, AsyncProviderCancelled):
            error_category = "unknown_outcome" if phase != NOT_SENT else "local_admission"
        elif isinstance(error, AsyncProviderPayloadError):
            error_category = "application_schema"
        elif isinstance(error, (AsyncProviderTimeout, AsyncProviderConnectionError)):
            error_category = "service_network"
        else:
            error_category = category(error) or "unknown_outcome"
        if phase == RESPONSE_RECEIVED and getattr(error, "response", None) is not None:
            try:
                now = float(self.clock())
            except (TypeError, ValueError, OverflowError):
                now = float("nan")
            if math.isfinite(now):
                cooldown, http_status = _retry_after(error, now)
        return self.wal.record_error(
            self.identity, self.wal_request, error_category, phase,
            cooldown=cooldown, http_status=http_status,
        )
