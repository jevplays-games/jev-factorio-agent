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
        self._initial_state = deepcopy(self.state)
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

    def prepare_decision_lease(self, wal, state, questions, *, identity):
        """Prepare an explicit opt-in lease; ordinary provider calls stay unchanged."""
        if self.path is None:
            raise ValueError("Decision leases require a durable provider-health sidecar")
        if Path(wal.path).resolve() == Path(self.path).resolve():
            raise ValueError("Provider health and decision WAL must use separate files")
        from .decision_lease import ProviderDecisionLease

        with self._decision_health_writer_lock():
            self._refresh_decision_health_state()
            self._prepare_decision_authorization_locked(wal)
            health_state_sha256 = self._decision_health_baseline_sha256_locked(
                wal, identity,
            )
        return ProviderDecisionLease.prepare(
            wal, self.client, state, questions, identity=identity, clock=self.clock,
            health_state_sha256=health_state_sha256,
        )

    def acknowledge_decision_consumed(self, lease) -> None:
        """Durably bind health acknowledgment to an already-consumed exact WAL result.

        The controller calls this only after its selected-plan or no-action
        disposition is checkpointed and the WAL consume transition succeeds.
        Repeating the acknowledgment after a crash is idempotent.
        """
        from dataclasses import replace

        with self._decision_health_writer_lock():
            self._refresh_decision_health_state()
            record = lease.inspect()
            if record.state != "consumed":
                raise SafetyStateError("Provider result must be WAL-consumed before health acknowledgment")
            outcome = self.state.get("decision_outcome")
            if outcome is None:
                # Cancellation can land after the async client durably saved its
                # response but before this circuit finished its normal success
                # transition. The controller validates the exact archived
                # answers before calling us; reconstruct only that successful
                # health transition from the same consumed WAL receipt.
                flight = self.state.get("in_flight")
                if not self._flight_matches(lease, flight) or record.result is None:
                    raise SafetyStateError(
                        "Consumed provider result has no matching health reservation")
                from dataclasses import replace

                response_record = replace(record, state="response_received")
                result = lease.recover_result(response_record)
                if (result.identity != lease.request_identity
                        or result.requested_model != lease.model_id
                        or result.request_payload_sha256 != lease.request_payload_sha256):
                    raise SafetyStateError(
                        "Consumed provider result differs from its exact health reservation")
                self._finish_decision_success(
                    lease, response_record, flight, lease.request_identity)
                record = lease.inspect()
                if record.state != "consumed":
                    raise SafetyStateError("Consumed provider WAL receipt changed during recovery")
                outcome = self.state.get("decision_outcome")
            consumed = self._outcome_for(lease, record)
            if outcome == consumed:
                return
            response_record = replace(record, state="response_received")
            if outcome != self._outcome_for(lease, response_record):
                raise SafetyStateError("Consumed provider result differs from its durable health outcome")
            before = deepcopy(self.state)
            self.state["decision_outcome"] = consumed
            self._save_decision_state(before)

    def abandon_unstarted_decision(self, lease) -> None:
        """Close only a proven NOT_SENT reservation after stale local admission.

        This never changes MAY_HAVE_BEEN_SENT, ambiguous, or response-bearing
        WAL records. Those require their exact archived request for recovery.
        """
        from .async_provider import AsyncProviderLocalError, NOT_SENT

        with self._decision_health_writer_lock():
            self._refresh_decision_health_state()
            record = lease.inspect_optional()
            if record is None:
                return
            if record.state == "reserved":
                lease.record_failure(
                    AsyncProviderLocalError(
                        "The archived request became stale before transport admission",
                        lease.request_identity,
                    ),
                    NOT_SENT,
                )
                record = lease.inspect()
            if (record.state != "failed" or record.phase != NOT_SENT
                    or record.error_category != "local_admission"):
                raise SafetyStateError("Only a proven unstarted provider request can be abandoned")
            flight = self.state.get("in_flight")
            if flight is None:
                return
            if not self._flight_matches(lease, flight):
                raise SafetyStateError("A different provider decision owns the health reservation")
            self._restore_decision_before(lease, flight)

    def _prepare_decision_authorization_locked(self, wal):
        """Durably admit a pending operator probe before hashing a WAL baseline."""
        if self.state.get("in_flight") is not None:
            return
        outcome = self.state.get("decision_outcome")
        if outcome is not None:
            wal_id = hashlib.sha256(
                str(Path(wal.path).resolve()).encode("utf-8")).hexdigest()
            if outcome["wal_id"] != wal_id:
                raise SafetyStateError("Provider health sidecar is bound to another WAL")
            if outcome["state"] != "failed":
                # A response, ambiguous send, or consumed answer has its own
                # immutable decision binding. Authorization cannot supersede it.
                return
        pending = self._authorization_preview()
        if pending is None:
            return
        self._apply_authorization(pending, archive_terminal_decision=True)
        self._refresh_decision_health_state()

    def _decision_health_baseline_sha256(self, wal, identity):
        """Bind a WAL request to the exact health baseline that authorized it."""
        with self._decision_health_writer_lock():
            self._refresh_decision_health_state()
            return self._decision_health_baseline_sha256_locked(wal, identity)

    def _decision_health_baseline_sha256_locked(self, wal, identity):
        from .provider_decision_wal import canonical_sha256

        wal_id = hashlib.sha256(
            str(Path(wal.path).resolve()).encode("utf-8")).hexdigest()
        identity_fields = ("session_id", "actor_id", "observation_id", "decision_id",
                           "request_id")

        def same_request(bound):
            return (type(bound) is dict
                    and all(bound.get(field) == getattr(identity, field, None)
                            for field in identity_fields))

        flight = self.state.get("in_flight")
        if isinstance(flight, dict) and "decision_binding" in flight:
            binding = flight["decision_binding"]
            if binding["wal_id"] != wal_id:
                raise SafetyStateError("Provider health sidecar is bound to another WAL")
            bound_identity = binding["identity"]
            if bound_identity.get("request_id") == identity.request_id:
                if not same_request(bound_identity):
                    raise SafetyStateError("Provider decision identity changed during recovery")
                return binding["health_state_sha256"]

        outcome = self.state.get("decision_outcome")
        if isinstance(outcome, dict):
            if outcome["wal_id"] != wal_id:
                raise SafetyStateError("Provider health sidecar is bound to another WAL")
            bound_identity = outcome["identity"]
            if (outcome["state"] in {"response_received", "ambiguous"}
                    and bound_identity["request_id"] != identity.request_id):
                raise SafetyStateError(
                    "An unresolved provider decision must be replayed or consumed first")
            if bound_identity.get("request_id") == identity.request_id:
                if not same_request(bound_identity):
                    raise SafetyStateError("Provider decision identity changed after completion")
                return outcome["health_state_sha256"]

        return canonical_sha256(self.state)

    def _decision_health_writer_lock(self):
        from .provider_decision_wal import _writer_lock

        if self.path is None:
            raise SafetyStateError("Decision lease requires durable provider health")
        return _writer_lock(self.path)

    def _refresh_decision_health_state(self):
        current = read_json(self.path)
        if current is None:
            if self.state != self._initial_state:
                raise SafetyStateError("Provider health sidecar disappeared")
            self.state = deepcopy(self._initial_state)
            return
        self._validate(current)
        self.state = current

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
        outcome = value.get("decision_outcome")
        if outcome is not None:
            self._validate_decision_outcome(outcome)
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
        if outcome is not None and flight is None:
            from .provider_decision_wal import canonical_sha256
            completed_state = deepcopy(value)
            completed_state.pop("decision_outcome", None)
            if canonical_sha256(completed_state) != outcome["health_result_sha256"]:
                raise SafetyStateError("Provider decision completed health state changed")
        if isinstance(flight, dict) and "decision_binding" in flight:
            if set(flight) != {
                    "request_id", "started_at", "healthy_start", "decision_binding",
                    "decision_before", "authorization_sha256"}:
                raise SafetyStateError("Provider decision flight fields are malformed")
            self._validate_decision_binding(flight["decision_binding"])
            before = flight["decision_before"]
            if type(before) is not dict or before.get("in_flight") is not None:
                raise SafetyStateError("Provider decision rollback snapshot is malformed")
            self._validate(before)
            from .provider_decision_wal import canonical_sha256
            if canonical_sha256(before) != flight["decision_binding"]["health_state_sha256"]:
                raise SafetyStateError("Provider decision pre-attempt health digest differs")
            if (not isinstance(flight["authorization_sha256"], str)
                    or len(flight["authorization_sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in flight["authorization_sha256"])):
                raise SafetyStateError("Provider decision authorization binding is malformed")
            if flight["request_id"] != flight["decision_binding"]["identity"]["request_id"]:
                raise SafetyStateError("Provider decision request crosslink differs")
            if self._decision_reserved_state(before, flight) != value:
                raise SafetyStateError("Provider decision rollback snapshot conflicts with its reservation")

    @staticmethod
    def _validate_decision_binding(value):
        from .provider_decision_wal import ProviderDecisionIdentity

        if type(value) is not dict or set(value) != {
                "wal_id", "identity", "request_sha256", "health_state_sha256"}:
            raise SafetyStateError("Provider decision WAL binding is malformed")
        for name in ("wal_id", "request_sha256", "health_state_sha256"):
            digest = value[name]
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise SafetyStateError("Provider decision WAL fingerprint is malformed")
        if not isinstance(value["wal_id"], str):
            raise SafetyStateError("Provider decision WAL fingerprint is malformed")
        try:
            ProviderDecisionIdentity(**value["identity"])
        except (TypeError, ValueError) as error:
            raise SafetyStateError("Provider decision identity is malformed") from error

    @staticmethod
    def _validate_decision_outcome(value):
        from .provider_decision_wal import (
            MAY_HAVE_BEEN_SENT, NOT_SENT, RESPONSE_RECEIVED,
            ProviderDecisionIdentity,
        )

        fields = {"schema", "wal_id", "identity", "request_sha256",
                  "health_state_sha256", "health_result_sha256", "state",
                  "phase", "result_sha256", "error_category", "http_status"}
        if type(value) is not dict or set(value) != fields or value.get("schema") != 1:
            raise SafetyStateError("Provider decision outcome is malformed")
        try:
            ProviderDecisionIdentity(**value["identity"])
        except (TypeError, ValueError) as error:
            raise SafetyStateError("Provider decision outcome identity is malformed") from error
        for name in ("wal_id", "request_sha256", "health_state_sha256",
                     "health_result_sha256"):
            digest = value[name]
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise SafetyStateError("Provider decision outcome digest is malformed")
        if value["phase"] not in {NOT_SENT, MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED}:
            raise SafetyStateError("Provider decision outcome phase is malformed")
        if value["state"] == "response_received":
            digest = value["result_sha256"]
            if (value["phase"] != RESPONSE_RECEIVED or value["error_category"] is not None
                    or value["http_status"] is not None
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise SafetyStateError("Provider decision response outcome is malformed")
        elif value["state"] in {"failed", "ambiguous"}:
            if (value["result_sha256"] is not None
                    or not isinstance(value["error_category"], str)
                    or value["error_category"] not in {
                        "application_schema", "authentication_authorization", "account_quota",
                        "rate_limit", "service_network", "unknown_outcome", "local_admission",
                    }
                    or (value["state"] == "ambiguous"
                        and (value["phase"] != MAY_HAVE_BEEN_SENT
                             or value["error_category"] == "local_admission"
                             or value["http_status"] is not None))
                    or (value["state"] == "failed"
                        and (value["phase"] == MAY_HAVE_BEEN_SENT
                             or (value["phase"] == NOT_SENT
                                 and value["error_category"] != "local_admission")
                             or (value["phase"] == RESPONSE_RECEIVED
                                 and value["error_category"] == "local_admission"))
                        )):
                raise SafetyStateError("Provider decision error outcome is malformed")
        elif value["state"] == "consumed":
            digest = value["result_sha256"]
            if (value["phase"] != RESPONSE_RECEIVED or value["error_category"] is not None
                    or value["http_status"] is not None
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise SafetyStateError("Provider decision consumed outcome is malformed")
        else:
            raise SafetyStateError("Provider decision outcome state is unsupported")
        status = value["http_status"]
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise SafetyStateError("Provider decision HTTP status is malformed")

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
                             deadline: float | None = None, decision_lease=None):
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
        if decision_lease is not None:
            decision_lease.assert_call(
                self.client, request["state"], request["questions"], identity,
            )
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        if self._async_lock.locked():
            raise AsyncProviderQueueFull("provider health circuit is already evaluating", identity)
        async with self._async_lock:
            if decision_lease is not None:
                with self._decision_health_writer_lock():
                    self._refresh_decision_health_state()
                    return await self._evaluate_async_with_lease_locked(
                        request["state"], request["questions"], identity=identity,
                        deadline=deadline, lease=decision_lease,
                    )
            return await self._evaluate_async_locked(
                request["state"], request["questions"], identity=identity,
                deadline=deadline)

    def _decision_auth_digest(self) -> str:
        authorization = None
        if self.path is not None:
            authorization = read_json(self.path.parent / "provider-authorization.json")
        encoded = json.dumps(authorization, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _assert_durable_state(self, expected):
        if self.path is None:
            raise SafetyStateError("Decision lease requires durable provider health")
        current = read_json(self.path)
        if current is None:
            if expected == self._initial_state:
                return
            raise SafetyStateError("Provider health sidecar disappeared")
        self._validate(current)
        if current != expected:
            self.state = current
            raise SafetyStateError("Provider health sidecar changed during decision lease")

    def _save_decision_state(self, expected):
        proposed = deepcopy(self.state)
        self._assert_durable_state(expected)
        try:
            self._save()
        except BaseException:
            try:
                current = read_json(self.path)
                if current is not None:
                    self._validate(current)
                    self.state = current if current == proposed else expected
                else:
                    self.state = expected
            except BaseException:
                self.state = proposed
            raise

    def _outcome_for(self, lease, record):
        from .provider_decision_wal import canonical_sha256

        completed_state = deepcopy(self.state)
        completed_state.pop("decision_outcome", None)
        return {
            "schema": 1,
            "wal_id": lease.wal_id,
            "identity": lease.identity.to_dict(),
            "request_sha256": record.request_sha256,
            "health_state_sha256": lease.health_state_sha256,
            "health_result_sha256": canonical_sha256(completed_state),
            "state": record.state,
            "phase": record.phase,
            "result_sha256": record.result_sha256,
            "error_category": record.error_category,
            "http_status": record.http_status,
        }

    def _outcome_matches(self, lease, record, outcome):
        return outcome == self._outcome_for(lease, record)

    @staticmethod
    def _flight_matches(lease, flight):
        return (isinstance(flight, dict)
                and flight.get("decision_binding") == lease.binding())

    def _decision_reserved_state(self, before, flight):
        expected = deepcopy(before)
        healthy_start = expected.get("phase") == "healthy"
        if healthy_start != flight["healthy_start"]:
            raise SafetyStateError("Provider decision health baseline changed")
        if not healthy_start:
            expected["attempts"] += 1
            budget = expected.get("budget_category") or expected.get("category")
            maximum = expected.get("budget_limit") or self._limit(budget)
            expected["phase"] = "exhausted" if expected["attempts"] >= maximum else "cooldown"
            expected["budget_category"] = budget
            expected["budget_limit"] = maximum
            expected["next_probe_at"] = flight["started_at"] + self._delay(
                budget, expected["attempts"])
        expected["in_flight"] = {
            "request_id": flight["request_id"],
            "started_at": flight["started_at"],
            "healthy_start": healthy_start,
            "decision_binding": deepcopy(flight["decision_binding"]),
            "decision_before": deepcopy(before),
            "authorization_sha256": flight["authorization_sha256"],
        }
        return expected

    def _decision_blocked(self, identity, *, called=False, delivery_phase=None):
        raise ProviderBlocked(self.state, called=called, identity=identity,
                              delivery_phase=delivery_phase)

    def _restore_decision_before(self, lease, flight):
        expected = deepcopy(self.state)
        before = deepcopy(flight["decision_before"])
        if self._decision_reserved_state(before, flight) != expected:
            raise SafetyStateError("Provider incident or budget changed before rollback")
        if (self._decision_auth_digest() != flight["authorization_sha256"]
                or expected.get("incident_id") != flight["decision_before"].get("incident_id")
                or expected.get("authorization_id") != flight["decision_before"].get("authorization_id")):
            raise SafetyStateError("Provider authorization or incident changed before rollback")
        if not self._flight_matches(lease, expected.get("in_flight")):
            raise SafetyStateError("Provider decision rollback crosslink changed")
        self.state = before
        self._save_decision_state(expected)

    def _decision_failure_state(self, lease, record, flight):
        """Finish one already-durable provider error exactly once."""
        from .provider_decision_wal import MAY_HAVE_BEEN_SENT, NOT_SENT

        if not self._flight_matches(lease, flight):
            raise SafetyStateError("Provider decision error no longer owns the health reservation")
        if self._decision_auth_digest() != flight["authorization_sha256"]:
            raise SafetyStateError("Provider authorization changed during a decision")
        if record.phase == NOT_SENT and record.error_category == "local_admission":
            self._restore_decision_before(lease, flight)
            return
        if record.error_category == "unknown_outcome" and record.phase == MAY_HAVE_BEEN_SENT:
            now = self.clock()
            reservation = deepcopy(self.state)
            if flight["healthy_start"]:
                self.state.update(incident_id=str(uuid4()),
                                  first_failure_at=flight["started_at"], attempts=1,
                                  category="unknown_outcome", budget_category="unknown_outcome",
                                  budget_limit=1, phase="exhausted",
                                  next_probe_at=now + self._delay("unknown_outcome", 1))
            else:
                budget = self.state.get("budget_category") or self.state.get("category")
                limit = self.state.get("budget_limit") or self._limit(budget)
                self.state.update(category="unknown_outcome", budget_category=budget,
                                  budget_limit=limit,
                                  phase="exhausted" if self.state["attempts"] >= limit else "cooldown")
            self.state["in_flight"] = None
            self.state["decision_outcome"] = self._outcome_for(lease, record)
            self._save_decision_state(reservation)
            return

        kind = record.error_category
        if kind is None:
            raise SafetyStateError("Provider decision failure lacks a category")
        now = self.clock()
        reservation = deepcopy(self.state)
        if flight["healthy_start"]:
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
        cooldown = record.cooldown
        if cooldown is not None:
            cooldown_end = cooldown["received_at"] + cooldown["retry_after_seconds"]
            delay = max(delay, max(0.0, cooldown_end - now))
        next_probe = now + delay
        if cooldown is not None:
            next_probe = max(now + self._delay(budget_category, attempts),
                             cooldown["received_at"] + cooldown["retry_after_seconds"])
        self.state.update(category=kind,
                          phase="exhausted" if attempts >= budget_limit else "cooldown",
                          next_probe_at=next_probe, in_flight=None)
        self.state["decision_outcome"] = self._outcome_for(lease, record)
        self._save_decision_state(reservation)

    def _finish_decision_success(self, lease, record, flight, identity):
        if not self._flight_matches(lease, flight):
            raise SafetyStateError("Provider decision result no longer owns the health reservation")
        if self._decision_auth_digest() != flight["authorization_sha256"]:
            raise SafetyStateError("Provider authorization changed during a decision")
        now = self.clock()
        reservation = deepcopy(self.state)
        if not flight["healthy_start"]:
            self.state.update(previous_incident={key: self.state[key] for key in
                              ("incident_id", "category", "attempts", "first_failure_at")},
                              phase="healthy", category=None, attempts=0, next_probe_at=0,
                              last_recovery_at=now, incident_id=None, first_failure_at=None,
                              budget_category=None, budget_limit=None, in_flight=None)
        else:
            self.state["in_flight"] = None
        self.state["decision_outcome"] = self._outcome_for(lease, record)
        self._save_decision_state(reservation)
        return lease.recover_result(record)

    async def _evaluate_async_with_lease_locked(self, state, questions, *, identity,
                                                 deadline, lease):
        from .async_provider import (
            AsyncProviderLocalError,
            AsyncProviderPayloadError,
            AsyncProviderResult,
            NOT_SENT,
        )
        from .provider_decision_wal import (
            MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED,
            WALInvalidTransition, WALResponseConsumed,
        )
        from .judgments import InvalidJudgment, validate_answers

        now = self.clock()
        record = lease.inspect_optional()
        flight = self.state.get("in_flight")
        if flight is not None and "decision_binding" in flight:
            if not self._flight_matches(lease, flight):
                raise SafetyStateError("A different provider decision owns the health reservation")
            self._assert_durable_state(self.state)
            if self._decision_auth_digest() != flight["authorization_sha256"]:
                raise SafetyStateError("Provider authorization changed during a decision")
            if record is None or record.request_sha256 != lease.binding()["request_sha256"]:
                raise SafetyStateError("Provider health reservation has no matching WAL record")
            if record.state == "response_received":
                return self._finish_decision_success(lease, record, flight, identity)
            if record.state in {"failed", "ambiguous"}:
                self._decision_failure_state(lease, record, flight)
                if record.phase == NOT_SENT:
                    raise AsyncProviderLocalError(
                        "The exact decision was durably closed before provider dispatch", identity)
                self._decision_blocked(identity, called=True, delivery_phase=record.phase)
            if record.state == "consumed":
                raise WALResponseConsumed("Provider decision was already consumed")
            if record.state == "may_have_been_sent":
                record = lease.wal.record_error(
                    lease.identity, lease.wal_request, "unknown_outcome", MAY_HAVE_BEEN_SENT,
                )
                self._decision_failure_state(lease, record, flight)
                self._decision_blocked(identity, called=True,
                                       delivery_phase=MAY_HAVE_BEEN_SENT)
            if record.state != "reserved":
                raise WALInvalidTransition("Provider decision is not safe to dispatch")
            reservation = deepcopy(self.state)
        elif flight is not None:
            if record is not None and record.state in {
                    "reserved", "may_have_been_sent", "response_received", "ambiguous"}:
                raise SafetyStateError("Provider WAL obligation conflicts with legacy health flight")
            if self._reconcile_in_flight(now):
                self._decision_blocked(identity, delivery_phase=NOT_SENT)
            self._decision_blocked(identity, delivery_phase=NOT_SENT)
        else:
            outcome = self.state.get("decision_outcome")
            if outcome is not None and outcome["wal_id"] != lease.wal_id:
                raise SafetyStateError("Provider health sidecar is bound to another WAL")
            same_completed_request = (
                outcome is not None
                and outcome["identity"]["request_id"] == lease.identity.request_id
            )
            if same_completed_request and record is None:
                raise SafetyStateError("Provider health outcome has no matching WAL record")
            if not same_completed_request:
                from .provider_decision_wal import canonical_sha256
                if canonical_sha256(self.state) != lease.health_state_sha256:
                    raise SafetyStateError("Provider health changed after lease preparation")
            if record is not None:
                if record.state in {"response_received", "consumed"}:
                    if not self._outcome_matches(lease, record, outcome):
                        raise SafetyStateError("Saved provider answer has no matching health outcome")
                    if record.state == "consumed":
                        raise WALResponseConsumed("Provider decision was already consumed")
                    return lease.recover_result(record)
                if record.state in {"failed", "ambiguous"}:
                    if (record.phase == NOT_SENT and record.error_category == "local_admission"
                            and outcome is None):
                        raise AsyncProviderLocalError(
                            "The exact decision was durably closed before provider dispatch", identity)
                    if not self._outcome_matches(lease, record, outcome):
                        raise SafetyStateError("Provider failure has no matching health outcome")
                    self._decision_blocked(identity, called=record.phase != NOT_SENT,
                                           delivery_phase=record.phase)
                if record.state == "may_have_been_sent":
                    raise SafetyStateError("Ambiguous WAL request has no health reservation")
                if record.state != "reserved":
                    raise WALInvalidTransition("Provider decision is not safe to dispatch")

            pending_auth = self._authorization_preview()
            if pending_auth is not None:
                raise SafetyStateError(
                    "Provider authorization changed after decision lease preparation")
            if self.state["phase"] == "exhausted" or now < self.state["next_probe_at"]:
                self._decision_blocked(identity, delivery_phase=NOT_SENT)

            record = lease.reserve()
            if record.state != "reserved":
                raise WALInvalidTransition("An existing request cannot authorize a new send")
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
            self.state["in_flight"] = {
                "request_id": identity.request_id,
                "started_at": now,
                "healthy_start": healthy_start,
                "decision_binding": lease.binding(),
                "decision_before": before_attempt,
                "authorization_sha256": self._decision_auth_digest(),
            }
            reservation = deepcopy(self.state)
            self._save_decision_state(before_attempt)

        flight = self.state["in_flight"]
        try:
            result = await self.client.evaluate(
                state, questions, identity=identity, deadline=deadline,
                decision_lease=lease,
            )
            if (not isinstance(result, AsyncProviderResult)
                    or result.identity != identity
                    or result.requested_model != lease.model_id
                    or result.request_payload_sha256 != lease.request_payload_sha256):
                raise AsyncProviderPayloadError(identity, "Provider result binding rejected")

            from collections.abc import Mapping

            def mutable(value):
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
            record = lease.inspect()
            if record.state != "response_received":
                raise SafetyStateError("Provider returned without a durable WAL response")
        except BaseException as error:
            record = lease.inspect_optional()
            current_flight = self.state.get("in_flight")
            if (record is not None and record.state == "failed"
                    and record.phase == NOT_SENT and record.error_category == "local_admission"):
                self._restore_decision_before(lease, current_flight)
                raise
            if (record is not None and record.state == "failed"
                    and record.phase == RESPONSE_RECEIVED
                    and record.error_category is not None):
                self._decision_failure_state(lease, record, current_flight)
                raise ProviderBlocked(
                    self.state, called=True, identity=identity,
                    delivery_phase=RESPONSE_RECEIVED,
                ) from None
            kind = (category(error)
                    if isinstance(error, (requests.RequestException, ProviderPayloadError))
                    else None)
            if kind is not None and current_flight is not None:
                if record is not None and record.state == "ambiguous":
                    if kind == "unknown_outcome":
                        raise
                    self._decision_failure_state(lease, record, current_flight)
                    raise ProviderBlocked(
                        self.state, called=True, identity=identity,
                        delivery_phase=getattr(error, "delivery_phase", MAY_HAVE_BEEN_SENT),
                    ) from None
            raise

        record = lease.inspect()
        if record.state != "response_received":
            raise SafetyStateError("Decision WAL response disappeared before health commit")
        return self._finish_decision_success(
            lease, record, self.state["in_flight"], identity,
        )

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
        pending = self._authorization_preview()
        if pending is not None:
            self._apply_authorization(pending)

    def _authorization_preview(self):
        if self.path is None or self.state["phase"] == "healthy":
            return None
        request = read_json(self.path.parent / "provider-authorization.json")
        if request is None or request.get("request_id") == self.state.get("authorization_id"):
            return None
        request_id, evidence = request.get("request_id"), request.get("evidence_sha256")
        if (request.get("incident_id") != self.state["incident_id"]
                or not isinstance(request_id, str) or len(request_id) != 36
                or any(c not in "0123456789abcdef-" for c in request_id)
                or not isinstance(evidence, str) or len(evidence) != 64
                or any(c not in "0123456789abcdef" for c in evidence)):
            raise SafetyStateError("Provider probe authorization does not match this incident")
        history_path = self.path.parent / ("provider-authorized-" + request_id + ".json")
        history = {"request": request, "previous_state": self.state}
        existing = read_json(history_path)
        if existing is not None and existing != history:
            raise SafetyStateError("Provider authorization history conflict")
        return {"request": request, "history_path": history_path,
                "history": history, "existing": existing}

    def _apply_authorization(self, pending, *, archive_terminal_decision=False):
        request = read_json(self.path.parent / "provider-authorization.json")
        if request != pending["request"]:
            raise SafetyStateError("Provider authorization changed during request admission")
        history_path = pending["history_path"]
        history = pending["history"]
        existing = read_json(history_path)
        if existing is not None and existing != history:
            raise SafetyStateError("Provider authorization history conflict")
        # Immutable history first, then an atomic new generation of probe budget.
        if existing is None:
            atomic_json(history_path, history)
        self.state = {**self.state, "phase": "cooldown", "attempts": 0,
                      "next_probe_at": 0,
                      "authorization_id": pending["request"]["request_id"],
                      "budget_category": None, "budget_limit": None, "in_flight": None}
        if (archive_terminal_decision
                and isinstance(self.state.get("decision_outcome"), dict)
                and self.state["decision_outcome"].get("state") == "failed"):
            # The immutable authorization history above retains the complete
            # terminal prior state; it is no longer the active WAL crosslink.
            self.state.pop("decision_outcome")
        self._save()

    @staticmethod
    def _limit(kind):
        return 1 if kind in {"application_schema", "unknown_outcome"} else 3 if kind in {
            "authentication_authorization", "account_quota"} else 8

    @staticmethod
    def _delay(kind, attempt):
        base = 60 if kind in {"authentication_authorization", "account_quota"} else 5
        return min(900, base * 2 ** min(attempt - 1, 8))
