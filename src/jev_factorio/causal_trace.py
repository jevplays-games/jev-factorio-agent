"""Passive causal instrumentation at existing controller call boundaries."""
from __future__ import annotations

import time
import errno
from dataclasses import asdict, fields
from copy import deepcopy
from functools import wraps
from typing import Callable, TypeVar
from uuid import uuid4

import requests

from .research_log import EventSink, ResearchLogError, _safe_observation_payload, safe_payload
from .iteration_timing import profiled_iteration, measured, span
from .timing_attribution import elapsed_clocks, sample_clocks

class TraceStorageError(ResearchLogError):
    failure_class = "storage_pressure"
    native_effect_possible = False


T = TypeVar("T")


def _snapshot_payload(snapshot: object) -> dict:
    """Expose snapshot fields without copying their nested values first.

    ``emit`` immediately normalizes, redacts, and detaches the complete event
    before it reaches a sink. ``dataclasses.asdict`` would deep-copy every
    nested field here before the observation sanitizer built the sink-owned tree.
    """
    return {field.name: getattr(snapshot, field.name) for field in fields(snapshot)}


def error_facts(error: BaseException) -> dict:
    """Fixed vocabulary only: no exception text, request URL, headers, or repr."""
    status = None
    if isinstance(error, requests.Timeout):
        kind = "timeout"
    elif isinstance(error, requests.ConnectionError):
        kind = "connection"
    elif isinstance(error, requests.HTTPError):
        kind = "http"
        if error.response is not None:
            value = error.response.status_code
            status = value if type(value) is int and 100 <= value <= 599 else None
    elif isinstance(error, (KeyboardInterrupt, SystemExit)):
        kind = "interrupted"
    elif isinstance(error, (ValueError, TypeError)):
        kind = "invalid_data"
    else:
        kind = "other"
    return {"category": kind, "http_status": status}


def traced_step(method):
    """Keep uncaught failures visible without altering return values/retries."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        trace = getattr(self, "_trace", None)
        if trace is None or not trace.enabled:
            return method(self, *args, **kwargs)
        trace.begin_step()
        try:
            result = method(self, *args, **kwargs)
        except BaseException as error:
            trace.error("step_failed", error)
            raise
        trace.emit("step_finished", {key: result.get(key) for key in
                                    ("action", "source", "status", "verified")})
        return result
    return profiled_iteration(wrapper)


class CausalTrace:
    def __init__(self, sink: EventSink | None, controller: str, client=None, *, provenance=None,
                 timing_clock=None):
        self.metrics = None
        self.admission_check = None
        self.sink, self.controller = sink, controller
        self.enabled = sink is not None
        self.trace_id = uuid4().hex if self.enabled else None
        self.observation_phase = "before_decision"
        self._counts: dict[str, int] = {}
        self._failed = False
        self._async_provider_call = None
        self.decision_id = self.observation_id = self.model_call_id = self.action_id = None
        self._session_id = self._world_kind = self._tick = None
        self._pending_key = self._pending_action_id = None
        self._attempt_actions: dict[str, str] = {}
        self.profile_latency = False
        self._model_gap_start = None
        self._iteration_timing = None
        self._timing_clock = sample_clocks if timing_clock is None else timing_clock
        self.provenance = deepcopy(provenance or {})
        self._secrets = tuple(value for name in ("api_key", "api_token")
                              if isinstance(value := getattr(client, name, None), str) and value) \
            if self.enabled else ()

    def set_iteration_timing(self, value) -> None:
        """Keep only a detached prior-step diagnostic for the next request event."""
        if not self.profile_latency:
            return
        try:
            from .iteration_timing import validate_timing
            copied = deepcopy(value)
            self._iteration_timing = validate_timing(copied) if copied is not None else None
        except Exception:
            self._iteration_timing = None

    def _finish_inter_request_timing(self) -> dict | None:
        start = self._model_gap_start
        self._model_gap_start = None
        if not self.profile_latency or not isinstance(start, dict):
            return None
        return elapsed_clocks(
            start, self._timing_clock(),
            'clock_sample_after_model_response_event_emission_to_clock_sample_after_next_model_request_emission_before_client_evaluate')

    def _start_inter_request_timing(self) -> None:
        if self.profile_latency:
            self._model_gap_start = self._timing_clock()

    def identity(self, kind: str) -> str | None:
        if not self.enabled:
            return None
        self._counts[kind] = self._counts.get(kind, 0) + 1
        return f"{kind}:{self._counts[kind]}"

    def _reserve_async_provider_call(self):
        """Reserve this trace against synchronous model-call entry while awaiting."""
        if self._failed:
            raise ResearchLogError("Causal trace has failed")
        if self._async_provider_call is not None:
            raise ResearchLogError("Causal trace already has an async provider call")
        token = object()
        self._async_provider_call = token
        return token

    def _release_async_provider_call(self, token) -> None:
        if self._async_provider_call is token:
            self._async_provider_call = None

    def _require_sync_provider_admission(self) -> None:
        if self._async_provider_call is not None:
            raise ResearchLogError(
                "Synchronous provider call cannot enter during an async provider call")

    def begin_step(self) -> None:
        if self._failed:
            raise ResearchLogError("Causal trace has failed")
        self.decision_id = self.identity("decision")
        self.observation_id = self.model_call_id = self.action_id = None
        self._tick = None
        self.emit("step_started", {})

    def _record_metrics(self, event_type: str, began: int, cpu_began: int, *,
                        elapsed: int | None = None, failed: bool = False,
                        preserve_error: bool = False) -> None:
        """A failed diagnostic cannot authorize reuse or replace a primary error.

        The counter may be partially updated when interrupted. Do not retry it;
        freeze the trace exactly as for an uncertain sink/capture boundary.
        """
        if self.metrics is None:
            return
        self._guard_metrics(
            lambda: self.metrics.call(
                event_type, time.perf_counter_ns() - began if elapsed is None else elapsed,
                failed=failed, cpu_ns=time.process_time_ns() - cpu_began),
            preserve_error=preserve_error,
        )

    def checkpoint_metrics(self, counters, metrics: dict, *, preserve_error: bool = False) -> None:
        """Account for a checkpoint without reopening an uncertain audit state."""
        self._guard_metrics(lambda: counters.checkpoint(metrics), preserve_error=preserve_error)

    def _guard_metrics(self, operation: Callable[[], None], *, preserve_error: bool) -> None:
        try:
            operation()
        except BaseException as error:
            self._failed = True
            if preserve_error:
                return
            if isinstance(error, Exception):
                raise ResearchLogError("Causal metrics recording failed") from None
            raise

    @measured("trace_emit")
    def emit(self, event_type: str, payload: dict) -> None:
        if not self.enabled:
            return
        if self._failed:
            raise ResearchLogError("Causal trace has failed")
        began, cpu_began = time.perf_counter_ns(), time.process_time_ns()
        failed = False
        try:
            envelope = {"trace_id": self.trace_id, "controller": self.controller,
                        "decision_id": self.decision_id,
                        "observation_id": self.observation_id,
                        "model_call_id": self.model_call_id, "action_id": self.action_id,
                        "session_id": self._session_id, "world_kind": self._world_kind,
                        "factorio_tick": self._tick, "supervisor_provenance": self.provenance,
                        **payload}
            # Even a custom sink must not retain or mutate live controller data.
            clean_envelope = (_safe_observation_payload(envelope, self._secrets)
                              if event_type == "observation"
                              else safe_payload(envelope, self._secrets))
            self.sink.emit(event_type, clean_envelope)
        except Exception as error:
            failed = True
            self._failed = True
            if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise TraceStorageError("Causal storage exhausted; retain pending ownership") from None
            raise ResearchLogError("Causal event persistence failed") from None
        except BaseException:
            # The sink may already have written some or all of this event.
            # Preserve the interruption, but never reuse uncertain audit state.
            failed = True
            self._failed = True
            raise
        finally:
            self._record_metrics('trace_emit', began, cpu_began,
                                 failed=failed, preserve_error=failed)

    def error(self, event_type: str, error: BaseException, **details) -> None:
        # Preserve the primary exception if the attempt to log it also fails.
        if not self.enabled or self._failed:
            return
        try:
            self.emit(event_type, {"status": "error", "error": error_facts(error), **details})
        except BaseException:
            # Only this secondary diagnostic is suppressed. Capture can fail
            # before emit() is entered; both paths permanently poison the trace.
            # The caller still raises the original operation failure unchanged.
            self._failed = True

    def call(self, event_type: str, operation: Callable[[], T], *,
             details: dict | None = None,
             result: Callable[[T], dict] | None = None) -> T:
        if self._failed:
            raise ResearchLogError("Causal trace has failed")
        if not self.enabled and self.metrics is None:
            with span(event_type):
                return operation()
        start, cpu_start = time.perf_counter_ns(), time.process_time_ns()
        try:
            with span(event_type):
                value = operation()
        except BaseException as error:
            try:
                elapsed = time.perf_counter_ns() - start
                self._record_metrics(event_type, start, cpu_start,
                                     elapsed=elapsed, failed=True)
                self.error(event_type, error, duration_ns=elapsed, **(details or {}))
            except BaseException:
                # Metric updates and argument assembly are secondary recording
                # work too. Preserve the actual operation failure unchanged.
                self._failed = True
            if self.profile_latency and event_type == 'model_response':
                self._start_inter_request_timing()
            raise
        try:
            elapsed = time.perf_counter_ns() - start
            self._record_metrics(event_type, start, cpu_start, elapsed=elapsed)
        except BaseException:
            self._failed = True
            raise
        if not self.enabled:
            return value
        capture_start, capture_cpu = time.perf_counter_ns(), time.process_time_ns()
        capture_failed = False
        try:
            with span("trace_capture"):
                captured = result(value) if result else {}
                # Assembly is part of capture, not the native operation. A bad
                # callback result must not become a recoverable backend error.
                event = {**(details or {}), "status": "ok", "duration_ns": elapsed, **captured}
        except Exception:
            capture_failed = True
            self._failed = True
            raise ResearchLogError("Cannot capture a causal event") from None
        except BaseException:
            capture_failed = True
            self._failed = True
            raise
        finally:
            self._record_metrics('trace_capture', capture_start, capture_cpu,
                                 failed=capture_failed, preserve_error=capture_failed)
        self.emit(event_type, event)
        return value

    def observe(self, backend, phase: str):
        if not self.enabled:
            return self.call("observation", backend.observe)
        observation_id = self.identity("observation")

        def captured(snapshot):
            self.observation_id = observation_id
            self._session_id, self._world_kind = snapshot.session_id, snapshot.world_kind
            self._tick = snapshot.tick
            return {"observation_id": observation_id, "snapshot": _snapshot_payload(snapshot),
                    "validation": "not_yet_validated"}

        return self.call("observation", backend.observe,
                         details={"phase": phase, "observation_id": observation_id}, result=captured)

    def client(self, client):
        return TracedClient(client, self) if (self.enabled or self.metrics is not None) and client is not None else client

    def pending_ref(self, plan_id: str, index: int, pending: dict, *, attempt_id=None) -> dict:
        key = (plan_id, index, pending["started_tick"], pending["action"])
        known = key == self._pending_key
        return {"action_id": self._pending_action_id if known else None,
                "action_origin": "current_trace" if known else "checkpoint_or_external",
                "attempt_id": attempt_id,
                "plan_id": plan_id, "step_index": index,
                "started_tick": pending["started_tick"]}

    def clear_pending(self) -> None:
        self._pending_key = self._pending_action_id = None

    def dispatch(self, operation: Callable[[], T], action: str, *, parameters=None,
                 plan_id=None, step_index=None, pending=None, checkpointed=False,
                 role="plan", attempt_id=None) -> T:
        if self.admission_check is not None and role != "mock_clock_advance":
            self.admission_check()
        if not self.enabled:
            return self.call("action_returned", operation)
        action_id = self.identity("action")
        self.action_id = action_id
        related = self._pending_action_id if role == "mock_clock_advance" else None
        facts = {"action_id": action_id, "action": action, "parameters": parameters or {},
                 "plan_id": plan_id, "step_index": step_index, "role": role,
                 "attempt_id": attempt_id,
                 "related_action_id": related}
        self.emit("action_prepared", {**facts, "checkpointed": checkpointed,
                                      "pending": pending, "dispatch": "prepared"})
        if pending is not None and role == "plan":
            self._pending_key = (plan_id, step_index, pending["started_tick"], action)
            self._pending_action_id = action_id
            if attempt_id is not None:
                self._attempt_actions[attempt_id] = action_id
        try:
            return self.call("action_returned", operation, details=facts,
                             result=lambda outcome: {"outcome": outcome})
        except TraceStorageError as error:
            error.native_effect_possible = True
            raise

    def verify(self, step, snapshot, *, plan_id: str, index: int,
               pending: dict, phase: str, attempt_id=None) -> bool:
        if not self.enabled:
            return self.call("verification", lambda: step.satisfied(snapshot))
        return self.call("verification", lambda: step.satisfied(snapshot),
                         details={**self.pending_ref(plan_id, index, pending, attempt_id=attempt_id),
                                  "phase": phase, "predicate": asdict(step)},
                         result=lambda satisfied: {"verified": satisfied})

    def attempt_ref(self, attempt_id) -> dict:
        action_id = self._attempt_actions.get(attempt_id)
        return {"attempt_id": attempt_id, "action_id": action_id,
                "action_origin": "current_trace" if action_id else "checkpoint_or_external"}

    def release_attempt(self, attempt_id) -> None:
        self._attempt_actions.pop(attempt_id, None)


class TracedClient:
    """Delegate metadata unchanged; record the exact evaluate boundary once."""
    def __init__(self, client, trace: CausalTrace):
        self._client, self._trace = client, trace

    def __getattr__(self, name):
        return getattr(self._client, name)

    def evaluate(self, state: dict, questions: dict) -> dict:
        trace, client = self._trace, self._client
        trace._require_sync_provider_admission()
        trace.model_call_id = trace.identity("model")
        payload = {"state": state, "questions": questions,
                   "requested_model": getattr(client, "model", None),
                   "is_mock": getattr(client, "is_mock", False),
                   "dispatch": "prepared"}
        if trace.profile_latency and trace._iteration_timing is not None:
            payload['previous_iteration_timing'] = deepcopy(trace._iteration_timing)
        trace.emit("model_request", payload)
        # Stop the between-call timer only after the request event is durable,
        # immediately before entering the client operation. Thus the measured
        # interval includes request record construction and its append/fsync.
        details = {"requested_model": getattr(client, "model", None),
                   "resolved_model": None, "usage": None}
        inter_request = (trace._finish_inter_request_timing()
                         if trace.profile_latency else None)
        if inter_request is not None:
            details['inter_request_timing'] = inter_request

        def invoke_client():
            if not trace.profile_latency:
                return client.evaluate(state, questions)
            call_start = sample_clocks()
            try:
                return client.evaluate(state, questions)
            finally:
                details['client_evaluate_timing'] = elapsed_clocks(
                    call_start, sample_clocks(),
                    'clock_samples_around_client_evaluate_call')

        answers = trace.call("model_response", invoke_client,
                             details=details,
                             result=lambda value: {"answers": value,
                                                   "resolved_model": getattr(client, "last_model", None),
                                                   "usage": getattr(client, "last_usage", None)})
        if trace.profile_latency:
            trace._start_inter_request_timing()
        return answers
