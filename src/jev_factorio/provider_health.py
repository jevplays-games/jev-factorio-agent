"""Bounded, durable provider circuit; inference recovery never mutates the game."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
from copy import deepcopy
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

import requests

from .operational_safety import SafetyStateError, atomic_json, read_json


class ProviderPayloadError(ValueError):
    pass


class ProviderBlocked(RuntimeError):
    def __init__(self, state: dict, *, called: bool, identity=None,
                 delivery_phase: str | None = None):
        self.state, self.called = dict(state), called
        self.identity, self.delivery_phase = identity, delivery_phase
        super().__init__("Provider access blocked: " + state["category"])


def category(error: BaseException) -> str | None:
    if isinstance(error, ProviderPayloadError):
        return "application_schema"
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return "service_network"
    if isinstance(error, requests.HTTPError):
        status = error.response.status_code if error.response is not None else None
        if status in (401, 403):
            return "authentication_authorization"
        if status == 402:
            return "account_quota"
        if status == 429:
            return "rate_limit"
        if status == 408 or isinstance(status, int) and 500 <= status <= 599:
            return "service_network"
        return "application_schema"
    return None


class ProviderCircuit:
    """No sleeping inside evaluate: owned/background actions remain observable.

    Three denial/quota probes total, eight transient/rate-limit probes total,
    one schema failure. Exhaustion is persisted across process restarts. A new
    request is never a mutation and is never evidence that credentials changed.
    """
    def __init__(self, client, path: Path | None = None, *, clock=time.time):
        self.client, self.path, self.clock = client, path, clock
        identity = json.dumps([getattr(client, "base_url", getattr(client, "url", "")),
                               getattr(client, "model", "")])
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.state = {"schema": 1, "identity": self.identity, "phase": "healthy",
                      "category": None, "attempts": 0, "next_probe_at": 0,
                      "incident_id": None, "first_failure_at": None,
                      "last_recovery_at": None, "previous_incident": None,
                      "budget_category": None, "budget_limit": None,
                      "in_flight": None}
        # Async callers share the same durable one-flight gate. The lock keeps
        # concurrent tasks on this instance from reconciling one another's
        # still-active reservation as a crash.
        self._async_lock = None
        if path is not None:
            stored = read_json(path)
            if stored is not None:
                self._validate(stored)
                self.state = stored
                # Schema-1 sidecars created before request reservations remain
                # readable. Their persisted failure category seeds the budget.
                self.state.setdefault("in_flight", None)
                self.state.setdefault("budget_category", self.state.get("category"))
                self.state.setdefault("budget_limit", self._limit(self.state["category"])
                                      if self.state.get("category") else None)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _validate(self, value):
        if (value.get("schema") != 1 or value.get("identity") != self.identity
                or value.get("phase") not in {"healthy", "cooldown", "exhausted"}
                or type(value.get("attempts")) is not int or value["attempts"] < 0
                or type(value.get("next_probe_at")) not in (int, float)
                or not math.isfinite(value["next_probe_at"])
                or value.get("category") not in {None, "application_schema", "service_network",
                    "authentication_authorization", "account_quota", "rate_limit", "unknown_outcome"}
                or value.get("budget_category") not in {None, "application_schema", "service_network",
                    "authentication_authorization", "account_quota", "rate_limit", "unknown_outcome"}
                or (value.get("budget_limit") is not None
                    and (type(value["budget_limit"]) is not int or value["budget_limit"] < 1))):
            raise SafetyStateError("Provider circuit identity or schema differs")
        flight = value.get("in_flight")
        if flight is not None and (
                not isinstance(flight, dict)
                or not isinstance(flight.get("request_id"), str)
                or len(flight["request_id"]) != 36
                or any(c not in "0123456789abcdef-" for c in flight["request_id"])
                or type(flight.get("started_at")) not in (int, float)
                or not math.isfinite(flight["started_at"])
                or type(flight.get("healthy_start")) is not bool):
            raise SafetyStateError("Provider in-flight reservation is malformed")

    def _save(self):
        if self.path is not None:
            atomic_json(self.path, self.state)

    def _reconcile_in_flight(self, now):
        """Turn a stale reservation into a bounded unknown outcome, never a guessed status."""
        flight = self.state.get("in_flight")
        if flight is None:
            return False
        reservation = deepcopy(self.state)
        if flight["healthy_start"]:
            # No incident existed before this request. Conservatively charge one
            # unknown request, but do not claim a known provider failure/recovery.
            self.state.update(incident_id=str(uuid4()),
                              first_failure_at=flight["started_at"], attempts=1,
                              category="unknown_outcome", budget_category="unknown_outcome",
                              budget_limit=1, phase="exhausted",
                              next_probe_at=now + self._delay("unknown_outcome", 1))
        else:
            # The attempt was durably charged before sending. Keep the incident's
            # known budget category even though this particular result is unknown.
            budget = self.state.get("budget_category") or self.state.get("category")
            limit = self.state.get("budget_limit") or self._limit(budget)
            self.state.update(category="unknown_outcome", budget_category=budget,
                              budget_limit=limit,
                              phase="exhausted" if self.state["attempts"] >= limit else "cooldown")
        self.state["in_flight"] = None
        # If persistence fails, retain the reservation in memory too; no next call
        # may pass the recovery gate based on an unpersisted reconciliation.
        try:
            self._save()
        except BaseException:
            self.state = reservation
            raise
        return True

    def evaluate(self, state: dict, questions: dict) -> dict:
        if self._async_lock is not None and self._async_lock.locked():
            raise RuntimeError("provider health circuit has an active async evaluation")
        if inspect.iscoroutinefunction(getattr(self.client, "evaluate", None)):
            raise TypeError("synchronous evaluate cannot use an async provider client; use evaluate_async")
        self.client.last_usage = self.client.last_model = None
        now = self.clock()
        if self._reconcile_in_flight(now):
            raise ProviderBlocked(self.state, called=False)
        self._authorization()
        if self.state["phase"] == "exhausted" or now < self.state["next_probe_at"]:
            raise ProviderBlocked(self.state, called=False)
        healthy_start = self.state["phase"] == "healthy"
        if not healthy_start:
            # Charge unhealthy probes before HTTP. The original incident budget
            # remains stable even if a later result has another category.
            self.state["attempts"] += 1
            budget = self.state.get("budget_category") or self.state.get("category")
            maximum = self.state.get("budget_limit") or self._limit(budget)
            self.state["phase"] = "exhausted" if self.state["attempts"] >= maximum else "cooldown"
            self.state["budget_category"] = budget
            self.state["budget_limit"] = maximum
            self.state["next_probe_at"] = now + self._delay(budget, self.state["attempts"])
        self.state["in_flight"] = {"request_id": str(uuid4()), "started_at": now,
                                   "healthy_start": healthy_start}
        reservation = deepcopy(self.state)
        try:
            self._save()
        except BaseException:
            self.state = reservation
            raise
        try:
            answers = self.client.evaluate(state, questions)
            from .judgments import InvalidJudgment, validate_answers
            try:
                validate_answers(questions, answers, quantum=getattr(self.client, "answer_quantum", 0))
            except InvalidJudgment as error:
                raise ProviderPayloadError("Provider answer schema rejected") from error
        except (requests.RequestException, ProviderPayloadError) as error:
            kind = category(error)
            if kind is None:
                raise
            if healthy_start:
                attempts = 1
                budget_limit = self._limit(kind)
                budget_category = kind
                self.state.update(incident_id=str(uuid4()), first_failure_at=now,
                                  attempts=attempts, budget_category=budget_category,
                                  budget_limit=budget_limit)
            else:
                attempts = self.state["attempts"]
                budget_category = self.state.get("budget_category") or kind
                budget_limit = min(self.state.get("budget_limit") or self._limit(budget_category),
                                   self._limit(kind))
                self.state.update(budget_category=budget_category, budget_limit=budget_limit)
            delay = self._delay(budget_category, attempts)
            response = error.response if isinstance(error, requests.HTTPError) else None
            if response is not None:
                try:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    if math.isfinite(retry_after):
                        delay = max(delay, min(3600, max(0, retry_after)))
                except (ValueError, TypeError):
                    try:
                        retry_at = parsedate_to_datetime(response.headers.get("Retry-After", "")).timestamp()
                        delay = max(delay, min(3600, max(0, retry_at - now)))
                    except (ValueError, TypeError, OverflowError):
                        pass
            self.state.update(category=kind,
                              phase="exhausted" if attempts >= budget_limit else "cooldown",
                              next_probe_at=now + delay, in_flight=None)
            self.client.last_usage = self.client.last_model = None
            try:
                self._save()
            except BaseException:
                self.state = reservation
                raise
            raise ProviderBlocked(self.state, called=True) from None
        if not healthy_start:
            self.state.update(previous_incident={key: self.state[key] for key in
                              ("incident_id", "category", "attempts", "first_failure_at")},
                              phase="healthy", category=None, attempts=0, next_probe_at=0,
                              last_recovery_at=now, incident_id=None, first_failure_at=None,
                              budget_category=None, budget_limit=None, in_flight=None)
        else:
            # An ordinary healthy success is not recovery telemetry.
            self.state["in_flight"] = None
        try:
            self._save()
        except BaseException:
            self.state = reservation
            raise
        return answers

    async def evaluate_async(self, state: dict, questions: dict, *, identity,
                             deadline: float | None = None):
        """Run one explicit async request through the existing durable circuit.

        Async clients return immutable per-request results rather than the
        synchronous client's shared ``last_usage``/``last_model`` fields. This
        method preserves that result and identity, validates a detached copy of
        its answers, and uses the same persisted budgets and recovery gates.
        """
        from .async_provider import (
            AsyncProviderQueueFull,
            RequestIdentity,
            snapshot_json,
        )

        if not isinstance(identity, RequestIdentity):
            raise TypeError("identity must be a RequestIdentity")
        if (deadline is not None
                and (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                     or not math.isfinite(deadline))):
            raise ValueError("deadline must be an absolute finite monotonic timestamp")
        evaluate = getattr(self.client, "evaluate", None)
        if evaluate is None or not inspect.iscoroutinefunction(evaluate):
            raise TypeError("evaluate_async requires an explicit async provider client")
        if not isinstance(state, dict) or not isinstance(questions, dict):
            raise TypeError("async provider state and questions must be JSON objects")
        request = snapshot_json({"state": state, "questions": questions})
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        if self._async_lock.locked():
            raise AsyncProviderQueueFull("provider health circuit is already evaluating", identity)
        async with self._async_lock:
            return await self._evaluate_async_locked(
                request["state"], request["questions"], identity=identity,
                deadline=deadline)

    async def _evaluate_async_locked(self, state: dict, questions: dict, *, identity,
                                     deadline: float | None):
        from .async_provider import (
            AsyncProviderCancelled,
            AsyncProviderLocalError,
            AsyncProviderPayloadError,
            AsyncProviderResult,
            NOT_SENT,
        )

        now = self.clock()
        if self._reconcile_in_flight(now):
            raise ProviderBlocked(self.state, called=False, identity=identity,
                                  delivery_phase=NOT_SENT)
        self._authorization()
        if self.state["phase"] == "exhausted" or now < self.state["next_probe_at"]:
            raise ProviderBlocked(self.state, called=False, identity=identity,
                                  delivery_phase=NOT_SENT)

        before_attempt = deepcopy(self.state)
        healthy_start = self.state["phase"] == "healthy"
        if not healthy_start:
            self.state["attempts"] += 1
            budget = self.state.get("budget_category") or self.state.get("category")
            maximum = self.state.get("budget_limit") or self._limit(budget)
            self.state["phase"] = "exhausted" if self.state["attempts"] >= maximum else "cooldown"
            self.state["budget_category"] = budget
            self.state["budget_limit"] = maximum
            self.state["next_probe_at"] = now + self._delay(budget, self.state["attempts"])
        self.state["in_flight"] = {"request_id": str(uuid4()), "started_at": now,
                                   "healthy_start": healthy_start}
        reservation = deepcopy(self.state)
        try:
            self._save()
        except BaseException:
            self.state = reservation
            raise

        try:
            result = await self.client.evaluate(state, questions, identity=identity,
                                                deadline=deadline)
            if (not isinstance(result, AsyncProviderResult)
                    or result.identity != identity
                    or not isinstance(result.requested_model, str)
                    or not result.requested_model.strip()
                    or not isinstance(result.request_payload_sha256, str)
                    or len(result.request_payload_sha256) != 64
                    or any(char not in "0123456789abcdef" for char in
                           result.request_payload_sha256)):
                raise AsyncProviderPayloadError(identity, "Provider result identity or metadata rejected")
            from .judgments import InvalidJudgment, validate_answers

            def mutable(value):
                from collections.abc import Mapping

                if isinstance(value, Mapping):
                    return {key: mutable(item) for key, item in value.items()}
                if isinstance(value, (list, tuple)):
                    return [mutable(item) for item in value]
                return value

            try:
                validate_answers(questions, mutable(result.answers),
                                 quantum=getattr(self.client, "answer_quantum", 0))
            except InvalidJudgment as error:
                raise AsyncProviderPayloadError(identity, "Provider answer schema rejected") from error
        except BaseException as error:
            # The transport can prove that queue/deadline/cancellation stopped
            # before HTTPX entry. Release that reservation without charging a
            # provider-health attempt. If the release write fails, keep the
            # persisted in-flight reservation so recovery remains fail-closed.
            if (isinstance(error, (AsyncProviderLocalError, AsyncProviderCancelled))
                    and getattr(error, "delivery_phase", None) == NOT_SENT):
                self.state = before_attempt
                try:
                    self._save()
                except BaseException:
                    self.state = reservation
                    raise
                raise

            if isinstance(error, (requests.RequestException, ProviderPayloadError)):
                kind = category(error)
                if kind is not None:
                    if healthy_start:
                        attempts = 1
                        budget_limit = self._limit(kind)
                        budget_category = kind
                        self.state.update(incident_id=str(uuid4()), first_failure_at=now,
                                          attempts=attempts, budget_category=budget_category,
                                          budget_limit=budget_limit)
                    else:
                        attempts = self.state["attempts"]
                        budget_category = self.state.get("budget_category") or kind
                        budget_limit = min(self.state.get("budget_limit") or self._limit(budget_category),
                                           self._limit(kind))
                        self.state.update(budget_category=budget_category, budget_limit=budget_limit)
                    delay = self._delay(budget_category, attempts)
                    response = error.response if isinstance(error, requests.HTTPError) else None
                    if response is not None:
                        try:
                            retry_after = float(response.headers.get("Retry-After", "0"))
                            if math.isfinite(retry_after):
                                delay = max(delay, min(3600, max(0, retry_after)))
                        except (ValueError, TypeError):
                            try:
                                retry_at = parsedate_to_datetime(
                                    response.headers.get("Retry-After", "")).timestamp()
                                delay = max(delay, min(3600, max(0, retry_at - now)))
                            except (ValueError, TypeError, OverflowError):
                                pass
                    self.state.update(category=kind,
                                      phase="exhausted" if attempts >= budget_limit else "cooldown",
                                      next_probe_at=now + delay, in_flight=None)
                    try:
                        self._save()
                    except BaseException:
                        self.state = reservation
                        raise
                    raise ProviderBlocked(
                        self.state, called=True, identity=getattr(error, "identity", identity),
                        delivery_phase=getattr(error, "delivery_phase", None),
                    ) from None
            # A possibly-sent cancellation, an unclassified exception, or a
            # process interruption leaves the reservation in place. A later
            # call reconciles it as unknown and cannot silently replay it.
            raise

        if not healthy_start:
            self.state.update(previous_incident={key: self.state[key] for key in
                              ("incident_id", "category", "attempts", "first_failure_at")},
                              phase="healthy", category=None, attempts=0, next_probe_at=0,
                              last_recovery_at=now, incident_id=None, first_failure_at=None,
                              budget_category=None, budget_limit=None, in_flight=None)
        else:
            self.state["in_flight"] = None
        try:
            self._save()
        except BaseException:
            self.state = reservation
            raise
        return result

    def _authorization(self):
        if self.path is None or self.state["phase"] == "healthy":
            return
        request = read_json(self.path.parent / "provider-authorization.json")
        if request is None or request.get("request_id") == self.state.get("authorization_id"):
            return
        request_id, evidence = request.get("request_id"), request.get("evidence_sha256")
        if (request.get("incident_id") != self.state["incident_id"]
                or not isinstance(request_id, str) or len(request_id) != 36
                or any(c not in "0123456789abcdef-" for c in request_id)
                or not isinstance(evidence, str) or len(evidence) != 64
                or any(c not in "0123456789abcdef" for c in evidence)):
            raise SafetyStateError("Provider probe authorization does not match this incident")
        # Immutable history first, then an atomic new generation of probe budget.
        history_path = self.path.parent / ("provider-authorized-" + request_id + ".json")
        history = {"request": request, "previous_state": self.state}
        existing = read_json(history_path)
        if existing is not None and existing != history:
            raise SafetyStateError("Provider authorization history conflict")
        if existing is None:
            atomic_json(history_path, history)
        self.state = {**self.state, "phase": "cooldown", "attempts": 0,
                      "next_probe_at": 0, "authorization_id": request_id,
                      "budget_category": None, "budget_limit": None, "in_flight": None}
        self._save()

    @staticmethod
    def _limit(kind):
        return 1 if kind in {"application_schema", "unknown_outcome"} else 3 if kind in {
            "authentication_authorization", "account_quota"} else 8

    @staticmethod
    def _delay(kind, attempt):
        base = 60 if kind in {"authentication_authorization", "account_quota"} else 5
        return min(900, base * 2 ** min(attempt - 1, 8))
