"""Opt-in hierarchical control: commit, execute, observe, verify, and recover.

The inherited run loop retains the existing monotonic deadline and transient
HTTP-failure handling. No live-game or model-performance claims follow from
passing the offline tests.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4
from dataclasses import asdict, replace
from pathlib import Path

from .causal_trace import CausalTrace, traced_step
from .iteration_timing import measured, span, previous_timing
from .operational_safety import MaintenanceAdmissionClosed, StoragePressure
from .provider_health import ProviderCircuit
from .backends.errors import ConnectionPreflightRejected
from .research_log import EventSink, ResearchLogError, validate_output_paths
from .blocked_persistence import DEFAULT_IDLE_OBSERVATIONS, IDLE_DELAY_SECONDS, MAX_IDLE_OBSERVATIONS
from .judgments import DEFAULT_MAX_REQUEST_BYTES, Decision, select_plan
from .loop import AgentLoop
from .memory import CampaignMemory, retain_latest_craft
from .planning.goals import GOALS, completed, goal_order
from .skills import Plan, compile_plans
from .state import GameSnapshot
from .provenance import gameplay_context
from .telemetry import (DISPATCH_STAGES, DispatchCancelledBeforeEntry, error_code, fingerprint,
                        make_attempt, phase, utc_now, validate_phase)
from .wait_record_codec import Encoder as WaitRecordEncoder, encode_line as encode_wait_line


MAX_ASYNC_CONTROLLER_WORKERS = 4
MAX_ASYNC_RESOURCE_SLOTS = 128
_ASYNC_RESOURCE_GUARD = threading.Lock()
_ASYNC_RESOURCE_SLOTS: dict[tuple[str, str], dict] = {}
_SYNC_RESOURCE_USERS: dict[tuple[str, str], int] = {}
_UNKNOWN_SYNC_RESOURCE_USERS = 0
_ASYNC_WORKER_SLOTS = threading.BoundedSemaphore(MAX_ASYNC_CONTROLLER_WORKERS)
_ASYNC_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_ASYNC_CONTROLLER_WORKERS,
    thread_name_prefix="jev-async-controller",
)
_ASYNC_WORKER_LOCAL = threading.local()


class AsyncControllerBusy(RuntimeError):
    """The bounded worker pool or Factorio session is already owned."""


class _AsyncStepCancelled(Exception):
    """Internal stop signal; public cancellation is raised after worker exit."""


def _claim_async_resources(keys: tuple[tuple[str, str], ...]):
    normalized = tuple(sorted(set(keys)))
    if not normalized:
        raise AsyncControllerBusy("Async backend resource identity is unavailable")
    with _ASYNC_RESOURCE_GUARD:
        if _UNKNOWN_SYNC_RESOURCE_USERS:
            raise AsyncControllerBusy(
                "A synchronous step with unknown transport identity is active")
        busy = [key for key in normalized
                if ((key in _ASYNC_RESOURCE_SLOTS
                     and _ASYNC_RESOURCE_SLOTS[key]["lock"].locked())
                    or _SYNC_RESOURCE_USERS.get(key, 0) > 0)]
        if busy:
            raise AsyncControllerBusy(
                "This Factorio session or backend transport already has a controller step")
        registered = set(_ASYNC_RESOURCE_SLOTS) | set(_SYNC_RESOURCE_USERS)
        if len(registered | set(normalized)) > MAX_ASYNC_RESOURCE_SLOTS:
            raise AsyncControllerBusy("Async backend resource ownership table is full")
        # Construct every missing lock before publishing any slot. A Lock
        # constructor itself can fail (for example during resource pressure),
        # and must not leave an earlier key registered or acquired.
        created = {key: {"lock": threading.Lock()} for key in normalized
                   if key not in _ASYNC_RESOURCE_SLOTS}
        inserted = []
        claims = []
        try:
            for key, slot in created.items():
                inserted.append((key, slot))
                _ASYNC_RESOURCE_SLOTS[key] = slot
            for key in normalized:
                slot = _ASYNC_RESOURCE_SLOTS[key]
                lock = slot["lock"]
                was_locked = lock.locked()
                if was_locked:
                    raise AsyncControllerBusy(
                        "Async backend resource claim changed during admission")
                try:
                    acquired = lock.acquire(blocking=False)
                except BaseException:
                    # A test double or lock wrapper can acquire and then raise.
                    # Under the registry guard this transition belongs to this
                    # admission; release it directly without allocating another
                    # tracking entry on the exception path.
                    if lock.locked() and not was_locked:
                        lock.release()
                    raise
                if not acquired:
                    raise AsyncControllerBusy(
                        "Async backend resource claim changed during admission")
                try:
                    claims.append((key, slot))
                except BaseException:
                    lock.release()
                    raise
        except BaseException:
            for _, slot in reversed(claims):
                if slot["lock"].locked():
                    slot["lock"].release()
            for key, slot in reversed(inserted):
                if (_ASYNC_RESOURCE_SLOTS.get(key) is slot
                        and not slot["lock"].locked()):
                    del _ASYNC_RESOURCE_SLOTS[key]
            raise
        return tuple(claims)


def _claim_sync_resources(keys: tuple[tuple[str, str], ...] | None):
    """Atomically claim known session/transport resources for one sync step.

    A known sync call excludes another sync or async call sharing either key.
    Unknown-identity sync calls use one conservative global claim and exclude
    every other sync/async owner until they return. Disjoint known resources can
    still run in parallel.
    """
    normalized = tuple(sorted(set(keys or ())))
    global _UNKNOWN_SYNC_RESOURCE_USERS, _SYNC_RESOURCE_USERS
    with _ASYNC_RESOURCE_GUARD:
        active_async = [key for key, slot in _ASYNC_RESOURCE_SLOTS.items()
                        if slot["lock"].locked()]
        if not normalized:
            if active_async or _SYNC_RESOURCE_USERS or _UNKNOWN_SYNC_RESOURCE_USERS:
                raise AsyncControllerBusy(
                    "Cannot run a synchronous step with unknown transport identity "
                    "while another controller owns resources")
            if _UNKNOWN_SYNC_RESOURCE_USERS >= MAX_ASYNC_RESOURCE_SLOTS:
                raise AsyncControllerBusy(
                    "Unknown synchronous controller ownership table is full")
            claim = (None, ())
            _UNKNOWN_SYNC_RESOURCE_USERS += 1
            return claim
        if _UNKNOWN_SYNC_RESOURCE_USERS:
            raise AsyncControllerBusy(
                "A synchronous step with unknown transport identity is active")
        busy = [key for key in normalized
                if ((key in _ASYNC_RESOURCE_SLOTS
                     and _ASYNC_RESOURCE_SLOTS[key]["lock"].locked())
                    or _SYNC_RESOURCE_USERS.get(key, 0) > 0)]
        if busy:
            raise AsyncControllerBusy(
                "This Factorio session or backend transport already has a controller step")
        registered = set(_ASYNC_RESOURCE_SLOTS) | set(_SYNC_RESOURCE_USERS)
        if len(registered | set(normalized)) > MAX_ASYNC_RESOURCE_SLOTS:
            raise AsyncControllerBusy("Synchronous controller resource registry is full")
        # Build the replacement registry off to the side. If a mapping update
        # fails partway through (including allocation failure), no partial claim
        # becomes visible and the current owners remain untouched.
        updated_users = _SYNC_RESOURCE_USERS.copy()
        for key in normalized:
            updated_users[key] = updated_users.get(key, 0) + 1
        claim = ("known", normalized)
        _SYNC_RESOURCE_USERS = updated_users
        return claim


def _release_sync_resources(claim) -> None:
    kind, keys = claim
    global _UNKNOWN_SYNC_RESOURCE_USERS
    with _ASYNC_RESOURCE_GUARD:
        if kind is None:
            if _UNKNOWN_SYNC_RESOURCE_USERS <= 0:
                raise RuntimeError("Unknown synchronous resource claim underflow")
            _UNKNOWN_SYNC_RESOURCE_USERS -= 1
            return
        for key in keys:
            users = _SYNC_RESOURCE_USERS.get(key, 0)
            if users <= 0:
                raise RuntimeError("Synchronous resource claim underflow")
            if users == 1:
                _SYNC_RESOURCE_USERS.pop(key)
            else:
                _SYNC_RESOURCE_USERS[key] = users - 1


def _release_async_resources(claims) -> None:
    with _ASYNC_RESOURCE_GUARD:
        for key, slot in reversed(claims):
            slot["lock"].release()
            _ASYNC_RESOURCE_SLOTS.pop(key, None)


async def _wait_for_async_worker(worker, execution: dict) -> None:
    """Drain the admitted thread worker despite repeated caller cancellation.

    Shielding prevents a caller cancellation from cancelling the executor
    future. Each additional cancellation interrupts this coroutine's current
    shield await, so catch it, clear the task's pending cancellation count,
    and await the same worker again. Awaiting the future yields to the event
    loop; this is not a polling loop and does not claim remote cancellation.
    """
    current = asyncio.current_task()
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            execution["cancelled"].set()
            uncancel = getattr(current, "uncancel", None)
            cancelling = getattr(current, "cancelling", None)
            if callable(uncancel):
                if callable(cancelling):
                    while cancelling():
                        previous = cancelling()
                        if uncancel() >= previous:
                            break
                else:
                    uncancel()
            continue
        except BaseException:
            # A worker exception is terminal too. The caller-visible
            # cancellation path deliberately keeps cancellation as its result.
            if worker.done():
                return
            raise


class _AsyncProviderBridge:
    """Run the synchronous reducer's provider call on its owning event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, cancelled: threading.Event):
        self.loop, self.cancelled = loop, cancelled
        self._task = None
        self._task_lock = threading.Lock()

    async def _run(self, awaitable):
        return await awaitable

    def call(self, awaitable_factory):
        if self.cancelled.is_set():
            raise _AsyncStepCancelled
        complete = threading.Event()
        result = {}

        def start():
            if self.cancelled.is_set():
                result["error"] = _AsyncStepCancelled()
                complete.set()
                return
            try:
                task = self.loop.create_task(self._run(awaitable_factory()))
            except BaseException as error:
                result["error"] = error
                complete.set()
                return
            with self._task_lock:
                self._task = task

            def finished(done):
                try:
                    result["value"] = done.result()
                except BaseException as error:
                    result["error"] = error
                finally:
                    with self._task_lock:
                        if self._task is done:
                            self._task = None
                    complete.set()

            task.add_done_callback(finished)

        self.loop.call_soon_threadsafe(start)
        cancel_sent = False
        while not complete.wait(0.025):
            if self.cancelled.is_set() and not cancel_sent:
                with self._task_lock:
                    task = self._task
                if task is not None:
                    self.loop.call_soon_threadsafe(task.cancel)
                    cancel_sent = True
        if "error" in result:
            if self.cancelled.is_set() and isinstance(result["error"], asyncio.CancelledError):
                raise _AsyncStepCancelled from None
            raise result["error"]
        return result["value"]


class _AsyncDecisionAdapter:
    """Synchronous reducer facade backed by one exact asynchronous request."""

    def __init__(self, controller):
        self.controller = controller
        self.last_model = None
        self.last_usage = None

    @property
    def model(self):
        return getattr(self.controller._async_provider_client, "model", None)

    @property
    def is_mock(self):
        return getattr(self.controller._async_provider_client, "is_mock", False)

    @property
    def answer_quantum(self):
        return getattr(self.controller._async_provider_client, "answer_quantum", 0)

    def evaluate(self, state: dict, questions: dict) -> dict:
        result = self.controller._async_evaluate_request(state, questions)
        self.last_model = result.get("resolved_model")
        self.last_usage = result.get("usage")
        self.controller._async_last_result = result
        return _json_mutable(result["answers"])


def _serialized_async_actor(method):
    """Allow the admitted worker to enter the async-mode synchronous reducer."""
    from functools import wraps

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if getattr(self, "async_decisions", False):
            scope = getattr(_ASYNC_WORKER_LOCAL, "scope", None)
            if scope is not None and scope.get("owner") is self:
                return method(self, *args, **kwargs)
            raise RuntimeError("Async-decision controllers must be stepped with await step_async()")
        try:
            resource_keys = self._async_resource_lock_keys()
        except AsyncControllerBusy:
            resource_keys = ()
        claim = _claim_sync_resources(resource_keys)
        try:
            return method(self, *args, **kwargs)
        finally:
            _release_sync_resources(claim)

    return wrapper


def _json_safe(value):
    """Keep malformed numeric answers auditable without emitting invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_numeric": repr(value)}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _json_mutable(value):
    """Detach immutable provider JSON projections for the existing reducer."""
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {key: _json_mutable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_mutable(item) for item in value]
    return value


class HierarchicalLoop(AgentLoop):
    memory_type = CampaignMemory

    def __init__(self, backend, jev=None, *, target: str = "rocket_launch",
                 policy: str = "jev", checkpoint: str | None = None,
                 resume_controller: bool = False, confidence_floor: float = 0.45,
                 tick_seconds: float = 2.0, log_file: str | None = None,
                 max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES, max_pending_polls: int = 32,
                 max_stalled_decisions: int = 4, factory_scheduling: str = "serial",
                 research_log: EventSink | None = None,
                 reevaluate_blocked_once: bool = False,
                 exact_checkpoint_sha256: str | None = None,
                 blocked_source_revision: str | None = None,
                 persist_recoverable_blocks: bool = False,
                 initialize_persistent_campaign: bool = False,
                 persistent_idle_observations: int = DEFAULT_IDLE_OBSERVATIONS,
                 async_decisions: bool = False,
                 async_decision_timeout: float = 30.0,
                 two_stage_decisions: bool = False):
        if factory_scheduling not in {"serial", "ready-work"}:
            raise ValueError("Unknown factory scheduling policy")
        self.factory_scheduling = factory_scheduling
        self._capital_fault = False
        if policy not in {"jev", "deterministic", "hybrid"}:
            raise ValueError("Unknown campaign policy")
        if policy != "deterministic" and jev is None:
            raise ValueError("Supply an explicit Jev client; mock use must be intentional")
        if type(two_stage_decisions) is not bool:
            raise ValueError("Two-stage decisions require an explicit boolean")
        if two_stage_decisions and (policy != "jev" or not persist_recoverable_blocks
                                    or checkpoint is None or async_decisions):
            raise ValueError("Two-stage decisions require synchronous persistent strict JEV control")
        self.two_stage_decisions = two_stage_decisions
        if type(async_decisions) is not bool:
            raise ValueError("Async decision mode must be explicitly enabled with a boolean")
        if (not isinstance(async_decision_timeout, (int, float))
                or isinstance(async_decision_timeout, bool)
                or not math.isfinite(async_decision_timeout)
                or not 0 < async_decision_timeout <= 300):
            raise ValueError("Async decision deadline must be in (0, 300] seconds")
        if async_decisions:
            import inspect

            if (policy == "deterministic" or checkpoint is None
                    or not callable(getattr(jev, "prepare_decision_payload", None))
                    or not callable(getattr(jev, "decision_provider_id", None))
                    or not inspect.iscoroutinefunction(getattr(jev, "evaluate", None))):
                raise ValueError(
                    "Async decisions require a checkpoint and an explicit async Jev provider")
        if not math.isfinite(confidence_floor) or not 0 <= confidence_floor <= 1:
            raise ValueError("Confidence floor must be in [0, 1]")
        if not math.isfinite(tick_seconds) or tick_seconds < 0:
            raise ValueError("Invalid decision interval")
        if min(max_request_bytes, max_pending_polls, max_stalled_decisions) < 1:
            raise ValueError("Controller budgets must be positive")
        validate_output_paths(research_log, log_file, checkpoint)
        self.provenance = gameplay_context()
        self.order = goal_order(target)
        self.backend, self.jev, self.policy = backend, jev, policy
        self.async_decisions = async_decisions
        self.async_decision_timeout = float(async_decision_timeout)
        self._async_provider_client = None
        self._async_archive = None
        self._async_wal = None
        self._async_runtime_identity = None
        self._async_loop = None
        self._async_execution = None
        self._async_selection_scope = None
        self._async_active_record = None
        self._async_active_lease = None
        self._async_last_result = None
        self._async_last_outcome = None
        self.target, self.confidence_floor = target, confidence_floor
        self.tick_seconds = tick_seconds
        self.log_file = Path(log_file) if log_file else None
        self.checkpoint = Path(checkpoint) if checkpoint else None
        self.resume_controller = resume_controller
        if type(persist_recoverable_blocks) is not bool:
            raise ValueError("Persistent blocked recovery must be a boolean")
        self.persist_recoverable_blocks = persist_recoverable_blocks
        if type(initialize_persistent_campaign) is not bool:
            raise ValueError("New persistent campaign flag must be a boolean")
        if initialize_persistent_campaign and (
                not persist_recoverable_blocks or resume_controller or reevaluate_blocked_once
                or self.checkpoint is None or self.checkpoint.exists() or self.checkpoint.is_symlink()):
            raise ValueError("New persistent campaign requires an unused checkpoint and no resume")
        if (type(persistent_idle_observations) is not int
                or not 0 <= persistent_idle_observations <= MAX_IDLE_OBSERVATIONS):
            raise ValueError("Persistent idle observations must be an integer in [0, 1000]")
        self.persistent_idle_observations = persistent_idle_observations
        # Process-local: consecutive waits already at the maximum delay with an
        # unchanged decision fingerprint. A restarted invocation starts again.
        self._persistent_idle_waits = 0
        self._persistent_idle_exhausted = False
        self._compact_next_record = False
        self._wait_record_encoder = WaitRecordEncoder("gameplay")
        self._persistent_recovery_status = None
        self._persistent_runtime_wait_level = 0
        if resume_controller and (self.checkpoint is None or not self.checkpoint.is_file()):
            raise ValueError("Resuming requires an existing controller checkpoint")
        if self.checkpoint and self.checkpoint.exists() and not resume_controller:
            raise ValueError("Checkpoint exists; explicitly resume or use a new path")
        self._connector_checkpoint_preflight_sha = None
        attachment = getattr(backend, '_native_attachment', None)
        if (resume_controller and isinstance(attachment, dict)
                and attachment.get('modules', {}).get('connector_ownership') is True):
            # Validate the existing controller bytes before enable_factory can
            # expose any native connector capability. Never migrate an old
            # checkpoint by observing a same-force native ledger.
            raw = self.checkpoint.read_bytes()
            preflight = self.memory_type.from_bytes(raw, attachment['session_id'], target)
            if preflight.connector_ownership is None:
                raise ValueError('Old checkpoint cannot adopt connector ledger')
            self._connector_checkpoint_preflight_sha = hashlib.sha256(raw).hexdigest()
        self.max_request_bytes = max_request_bytes
        self.max_pending_polls = max_pending_polls
        self.max_stalled_decisions = max_stalled_decisions
        self._reevaluate_blocked_once = reevaluate_blocked_once
        self._blocked_reevaluation_checkpoint_sha256 = None
        self._blocked_reevaluation_source = None
        if (type(reevaluate_blocked_once) is not bool
                or (reevaluate_blocked_once
                    and (exact_checkpoint_sha256 is None or blocked_source_revision is None))
                or (not reevaluate_blocked_once
                    and (exact_checkpoint_sha256 is not None or blocked_source_revision is not None))):
            raise ValueError("Blocked decision re-evaluation requires its exact checkpoint and source pins")
        if reevaluate_blocked_once:
            if not resume_controller or self.checkpoint is None:
                raise ValueError("Blocked decision re-evaluation requires a resumed controller checkpoint")
            if policy != "jev" or getattr(jev, "is_mock", False):
                raise ValueError("Blocked decision re-evaluation requires the live Jev selection policy")
            from .blocked_reevaluation import validate_checkpoint_capture, validate_source_revision
            source = validate_source_revision(blocked_source_revision)
            revision = self.provenance.get("code_revision")
            if revision is not None and (
                not isinstance(revision, dict) or revision.get("commit") != source["source_head"]
            ):
                raise ValueError("Blocked decision source differs from supervised source provenance")
            raw = self.checkpoint.read_bytes()
            preflight_memory = validate_checkpoint_capture(
                raw, exact_checkpoint_sha256, self.memory_type, target,
                self.max_stalled_decisions, source["decision_contract_sha256"],
                checkpoint_path=self.checkpoint)
            preflight_index = getattr(preflight_memory, "_blocked_recovery_archive_index", None)
            if preflight_index is not None:
                preflight_index.close()
            if self.checkpoint.read_bytes() != raw:
                raise ValueError("Controller checkpoint changed during blocked-decision preflight")
            self._blocked_reevaluation_checkpoint_sha256 = exact_checkpoint_sha256
            self._blocked_reevaluation_source = source
        if persist_recoverable_blocks:
            revision = self.provenance.get("code_revision")
            if ((not resume_controller and not initialize_persistent_campaign)
                    or self.checkpoint is None or policy != "jev"
                    or getattr(jev, "is_mock", False) or not isinstance(revision, dict)):
                raise ValueError("Persistent blocked recovery requires resumed live Jev control and source provenance")
        self.memory: CampaignMemory | None = None
        self._blocked_recovery_archive_index = None
        self._decision: Decision | None = None
        self._process_id = uuid4().hex
        self._attempt_clock: tuple[str, float] | None = None
        self._phases: list[dict] = []
        self._persistence_failed = False
        from .operational_safety import RuntimeSafety
        self._safety = RuntimeSafety(self.checkpoint, outputs=(
            self.log_file.parent if self.log_file else None,
            getattr(research_log, "run_dir", None))) if self.checkpoint else None
        if async_decisions:
            from .async_decision_archive import AsyncDecisionArchive
            from .provider_decision_wal import ProviderDecisionWAL

            self._async_provider_client = jev
            self.jev = ProviderCircuit(jev, self._safety.directory / "provider.json")
            wal_path = self._safety.directory / "provider-decisions.json"
            self._async_archive = AsyncDecisionArchive(
                self._safety.directory / "decision-archives")
            self._async_wal = (
                ProviderDecisionWAL.initialize(wal_path)
                if not resume_controller else ProviderDecisionWAL(wal_path))
        elif jev is not None and getattr(jev, "uses_http_provider", False):
            self.jev = ProviderCircuit(jev, self._safety.directory / "provider.json"
                                      if self._safety else None)
            jev = self.jev
        from .performance import PerformanceCounters
        from .planning.capacity_evidence import CapacityHistory
        self._performance = PerformanceCounters()
        self._capacity_history = CapacityHistory()
        from .planning.fuel_history import FuelHistory
        self._fuel_history = FuelHistory()
        self.catalog = None
        self._trace = CausalTrace(research_log, "hierarchical", jev, provenance=self.provenance)
        self._async_decision_adapter = (
            _AsyncDecisionAdapter(self) if self.async_decisions else None)
        self._trace.metrics = self._performance if self.factory_scheduling == "ready-work" else None
        self._trace.admission_check = (
            lambda: self._safety.before_dispatch(self.memory.session_id)) if self._safety else None
        if target in {"rocket_launch", "iron_smelting", "steam_power", "automation_science"} \
                and hasattr(backend, "enable_factory"):
            self.catalog = backend.enable_factory()
            self.max_pending_polls = max(self.max_pending_polls, 1800)

    async def step_async(self) -> dict:
        """Run one explicitly enabled durable async-provider controller step.

        The synchronous reducer and backend remain in a bounded worker. The
        provider coroutine stays on this event loop; cancellation is signalled
        to that request and this method waits for the worker's terminal state
        before releasing stable actor ownership.
        """
        if not self.async_decisions:
            raise RuntimeError("Enable async_decisions explicitly before calling step_async")
        loop = asyncio.get_running_loop()
        if self._async_loop is None:
            self._async_loop = loop
        elif self._async_loop is not loop:
            raise RuntimeError("An async controller instance is bound to one event loop")
        resource_keys = self._async_resource_lock_keys()
        if not _ASYNC_WORKER_SLOTS.acquire(blocking=False):
            raise AsyncControllerBusy("The bounded async controller worker pool is full")
        try:
            claim = _claim_async_resources(resource_keys)
        except BaseException:
            _ASYNC_WORKER_SLOTS.release()
            raise
        execution = None
        worker = None
        try:
            cancelled = threading.Event()
            bridge = _AsyncProviderBridge(loop, cancelled)
            execution = {"loop": loop, "cancelled": cancelled, "bridge": bridge,
                         "claim": claim, "worker": None,
                         "dispatch_gate": threading.Lock(), "dispatch_entered": False}
            self._async_execution = execution
            worker = loop.run_in_executor(
                _ASYNC_EXECUTOR, self._run_async_step_worker, execution)
            execution["worker"] = worker
        except BaseException:
            if worker is None:
                # Setup or executor submission failed before a Future was
                # returned. No worker can still mutate the backend, so undo
                # this exact admission rather than leaving a phantom owner.
                if self._async_execution is execution:
                    self._async_execution = None
                try:
                    _release_async_resources(claim)
                finally:
                    _ASYNC_WORKER_SLOTS.release()
            else:
                # A Future was returned before a later setup error. Keep the
                # execution claim until that real worker reaches a terminal
                # state; signal cancellation first so an unentered dispatch
                # remains suppressed.
                if execution is not None:
                    execution["worker"] = worker
                    with execution["dispatch_gate"]:
                        execution["cancelled"].set()
                try:
                    await _wait_for_async_worker(worker, execution)
                finally:
                    if self._async_execution is execution:
                        self._async_execution = None
                    try:
                        _release_async_resources(claim)
                    finally:
                        _ASYNC_WORKER_SLOTS.release()
            raise
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            with execution["dispatch_gate"]:
                cancelled.set()
            await _wait_for_async_worker(worker, execution)
            raise
        finally:
            if not worker.done():
                with execution["dispatch_gate"]:
                    cancelled.set()
                await _wait_for_async_worker(worker, execution)
            self._async_execution = None
            _release_async_resources(claim)
            _ASYNC_WORKER_SLOTS.release()

    def _run_async_step_worker(self, execution: dict):
        _ASYNC_WORKER_LOCAL.scope = {"owner": self, "execution": execution}
        try:
            return self.step()
        finally:
            try:
                del _ASYNC_WORKER_LOCAL.scope
            except AttributeError:
                pass

    async def aclose_async_provider(self) -> None:
        """Close only provider resources owned by this explicitly async client."""
        if not self.async_decisions:
            return
        if self._async_execution is not None:
            worker = self._async_execution.get("worker")
            if worker is not None and not worker.done():
                raise AsyncControllerBusy("Cannot close an async provider during a controller step")
        loop = asyncio.get_running_loop()
        if self._async_loop is None:
            self._async_loop = loop
        elif self._async_loop is not loop:
            raise RuntimeError("Async provider resources must close on their owning event loop")
        close = getattr(self.jev, "aclose", None)
        if callable(close):
            await close()

    def _async_execution_context(self) -> dict:
        scope = getattr(_ASYNC_WORKER_LOCAL, "scope", None)
        if (scope is None or scope.get("owner") is not self
                or not isinstance(scope.get("execution"), dict)):
            raise RuntimeError("Async provider execution is outside step_async()")
        return scope["execution"]

    def _async_cancel_requested(self) -> bool:
        return bool(self._async_execution is not None
                    and self._async_execution["cancelled"].is_set())

    def _async_enter_dispatch(self) -> bool:
        """Linearize cancellation against the already-checkpointed mutation boundary."""
        execution = self._async_execution_context()
        with execution["dispatch_gate"]:
            if execution["cancelled"].is_set():
                return False
            # From this point the worker owns the normal dispatch/verification
            # path. step_async waits for it to finish before returning cancel.
            execution["dispatch_entered"] = True
            return True

    def _async_resource_lock_keys(self) -> tuple[tuple[str, str], ...]:
        attachment = getattr(self.backend, "_native_attachment", None)
        if isinstance(attachment, dict):
            session_id, actor_unit = attachment.get("session_id"), attachment.get("actor_unit")
        else:
            from .backends.mock import MockBackend
            if isinstance(self.backend, MockBackend):
                session_id, actor_unit = getattr(self.backend, "session_id", None), 0
            else:
                session_id = getattr(self.backend, "session_id", None)
                actor_unit = getattr(self.backend, "actor_unit", None)
        if (type(session_id) is not str or not session_id or len(session_id) > 128
                or type(actor_unit) is not int or actor_unit < 0):
            raise AsyncControllerBusy(
                "Async decisions require a stable session and actor identity before worker admission")
        return (("session", session_id),
                ("transport", self._async_backend_transport_identity()))

    def _async_backend_transport_identity(self) -> str:
        """Return a local lock token for a supported backend transport.

        FLE's production composition is FleBackend -> SessionRcon -> one
        RCONClient. Mock facades used by offline controls may expose their
        shared MockBackend as ``_shared``. Unknown native backends fail closed
        rather than assume separate controller objects mean separate clients.
        The token is process-local and is never persisted or exposed in traces.
        """
        from .backends.mock import MockBackend

        backend = self.backend
        seen = set()
        for _ in range(8):
            marker = id(backend)
            if marker in seen:
                break
            seen.add(marker)
            if isinstance(backend, MockBackend):
                return f"mock:{marker:x}"
            instance = getattr(backend, "_instance", None)
            rcon = getattr(instance, "rcon_client", None) if instance is not None else None
            if rcon is not None:
                client = getattr(rcon, "client", None)
                if client is None:
                    raise AsyncControllerBusy(
                        "Async FLE backend has no stable underlying RCON client identity")
                return f"rcon:{id(client):x}"
            shared = getattr(backend, "_shared", None)
            if shared is None or shared is backend:
                break
            backend = shared
        raise AsyncControllerBusy(
            "Async decisions cannot establish a supported backend transport identity")

    def _async_observation_identity(self, snapshot: GameSnapshot) -> dict:
        if snapshot.world_kind == "mock":
            if snapshot.session_id != getattr(self.backend, "session_id", None):
                raise ValueError("Mock async observation session identity changed")
            return {"session_id": snapshot.session_id, "actor_unit": 0,
                    "surface_index": 0, "force_index": 0}
        if snapshot.world_kind != "fle":
            raise ValueError("Async decisions require mock or identified FLE observations")
        runtime = snapshot.factory.get("acceptance_runtime")
        fields = ("session_id", "actor_unit", "surface_index", "force_index")
        if (type(runtime) is not dict
                or any(name not in runtime for name in fields)
                or type(runtime.get("session_id")) is not str
                or runtime.get("session_id") != snapshot.session_id
                or any(type(runtime.get(name)) is not int or runtime[name] < 1
                       for name in fields[1:])):
            raise ValueError("Async FLE observation lacks its exact four-field runtime identity")
        attachment = getattr(self.backend, "_native_attachment", None)
        if (type(attachment) is not dict or attachment.get("qualified") is not True
                or attachment.get("session_id") != runtime["session_id"]
                or attachment.get("actor_unit") != runtime["actor_unit"]):
            raise ValueError("Async FLE observation differs from the retained attachment owner")
        return {name: runtime[name] for name in fields}

    def _validate_async_observation(self, snapshot: GameSnapshot) -> None:
        if not self.async_decisions:
            return
        current = self._async_observation_identity(snapshot)
        expected = self._async_runtime_identity
        if expected is None and self.memory is not None and self.memory.async_decision is not None:
            record = self._async_archive.load(self.memory.async_decision)
            expected = record["selector"]["runtime_identity"]
        if expected is not None and current != expected:
            raise ValueError("Async controller runtime actor identity changed")
        self._async_runtime_identity = current

    def _async_lease_from_record(self, record: dict):
        from .async_provider import RequestIdentity
        from .decision_lease import ProviderDecisionLease
        from .provider_decision_wal import ProviderDecisionIdentity, canonical_sha256

        identity_data = record["identity"]
        identity = ProviderDecisionIdentity(**identity_data)
        request_identity = RequestIdentity(
            session_id=identity.session_id, actor_id=identity.actor_id,
            observation_id=identity.observation_id, decision_id=identity.decision_id,
            request_id=identity.request_id,
        )
        provider = record["provider"]
        wal_request_json = json.dumps(
            provider["wal_request"], ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )
        wal_id = hashlib.sha256(
            str(Path(self._async_wal.path).resolve()).encode("utf-8")).hexdigest()
        if wal_id != provider["wal_id"]:
            raise ValueError("Archived async decision belongs to a different WAL path")
        return ProviderDecisionLease(
            self._async_wal, identity, request_identity,
            provider["payload_json"], wal_request_json,
            canonical_sha256(provider["wal_request"]), provider["payload_sha256"],
            provider["wal_request"]["health_state_sha256"], wal_id,
            provider["provider_id"], provider["model_id"],
            provider["wal_request"]["trace_binding"], self.jev.clock,
        )

    def _async_prepare_selection(self, *, snapshot: GameSnapshot, source_state: dict,
                                 state: dict,
                                 questions: dict, plans: list[Plan],
                                 offered: list[Plan], selection_batch: dict | None = None,
                                 persistent_input_sha256: str | None = None,
                                 source_authorized: bool = False,
                                 authorization_reason: str | None = None,
                                 checkpoint_update=None) -> None:
        if not self.async_decisions:
            return
        execution = self._async_execution_context()
        if execution["cancelled"].is_set():
            raise _AsyncStepCancelled
        if self.memory.async_decision is not None:
            record = self._async_archive.load(self.memory.async_decision)
            lease = self._async_lease_from_record(record)
            self._async_active_record, self._async_active_lease = record, lease
            self._async_selection_scope = {
                                           "source_state": record["selector"]["source_state"],
                                           "state": record["selector"]["state"],
                                           "questions": record["selector"]["questions"],
                                           "record": record, "lease": lease}
            return
        if not isinstance(self.provenance.get("code_revision"), dict):
            raise ValueError("Async selection requires a source-pinned controller revision")
        from .async_provider import RequestIdentity

        runtime = self._async_runtime_identity
        if not isinstance(runtime, dict):
            raise ValueError("Async selection requires a validated four-field actor identity")
        if not isinstance(source_state, dict):
            raise ValueError("Async selection requires the original source state")
        if (authorization_reason is not None
                and (type(authorization_reason) is not str or len(authorization_reason) > 4096)):
            raise ValueError("Async authorization reason exceeds its bounded text contract")
        actor_bytes = json.dumps(runtime, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
        actor_id = "actor:" + hashlib.sha256(actor_bytes).hexdigest()
        request_id = str(uuid4())
        identity = RequestIdentity(
            session_id=snapshot.session_id, actor_id=actor_id,
            observation_id=self._trace.observation_id or "observation:" + uuid4().hex,
            decision_id=self._trace.decision_id or "decision:" + uuid4().hex,
            request_id=request_id,
        )
        lease = self.jev.prepare_decision_lease(
            self._async_wal, state, questions, identity=identity)
        provider_payload = self._async_provider_client.prepare_decision_payload(
            state, questions)
        if provider_payload != lease.payload:
            raise ValueError("Prepared async provider payload differs from its lease")
        wire_body = json.dumps(
            provider_payload, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        from .causal_trace import describe_request_order

        plan_rows = [plan.to_dict() for plan in plans]
        offered_rows = [plan.to_dict() for plan in offered]
        provider = {
            "provider_id": lease.provider_id,
            "model_id": lease.model_id,
            "payload": provider_payload,
            "payload_json": lease.request_json,
            "payload_sha256": lease.request_payload_sha256,
            "wire_body_base64": base64.b64encode(wire_body).decode("ascii"),
            "wire_body_sha256": hashlib.sha256(wire_body).hexdigest(),
            "wal_request": lease.wal_request,
            "wal_id": lease.wal_id,
        }
        archive_id = hashlib.sha256(
            (request_id + ":" + lease.wal_id + ":" + actor_id).encode("utf-8")).hexdigest()
        record = {
            "schema": 1, "archive_id": archive_id, "request_id": request_id,
            "identity": lease.identity.to_dict(),
            "selector": {
                "source_state": source_state,
                "state": state, "questions": questions,
                "candidate_plans": plan_rows, "offered_plans": offered_rows,
                "source_revision": self.provenance["code_revision"],
                "session_id": snapshot.session_id, "actor_id": actor_id,
                "runtime_identity": runtime,
                "observation_id": identity.observation_id,
                "decision_id": identity.decision_id, "request_id": request_id,
                "observation_tick": snapshot.tick, "target": self.target,
                "policy": self.policy, "confidence_floor": self.confidence_floor,
                "max_request_bytes": self.max_request_bytes,
                "request_order": describe_request_order(state, questions),
                "persistent_input_sha256": persistent_input_sha256,
                "selection_batch": selection_batch,
                "source_authorized": source_authorized,
                "source_auth_reason_sha256": (
                    hashlib.sha256(authorization_reason.encode("utf-8")).hexdigest()
                    if authorization_reason is not None else None),
                "frontier_sha256": fingerprint(plan_rows),
            },
            "provider": provider,
        }
        record = _json_mutable(record)
        pointer = self._async_archive.store(record)
        # Archive first, pointer second, WAL reservation and MAY_HAVE_BEEN_SENT
        # only later in AsyncProviderClient.post_json.
        if checkpoint_update is not None:
            checkpoint_update()
        self.memory.async_decision = pointer
        self._save()
        self._async_active_record, self._async_active_lease = record, lease
        self._async_selection_scope = {"source_state": source_state,
                                       "state": state, "questions": questions,
                                       "record": record, "lease": lease}

    @staticmethod
    def _async_json_equal(left, right) -> bool:
        try:
            return json.dumps(left, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False) == json.dumps(
                                  right, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _async_semantic_selection_sha256(state: dict, plans: list[dict], *,
                                         session_id: str, source_revision: dict,
                                         target: str, policy: str,
                                         confidence_floor: float,
                                         max_request_bytes: int,
                                         current_tick: int) -> str:
        """Fingerprint current decision semantics while normalizing known clocks.

        The existing blocked-decision fingerprint has an intentional Jev-only
        policy contract. Async selection also supports the hybrid policy, so
        reuse its source-stable value normalization while binding the actual
        policy and request budget here.
        """
        from .blocked_persistence import _stable

        payload = {
            "schema": 1, "session_id": session_id,
            "source_revision": source_revision, "target": target,
            "policy": policy, "confidence_floor": confidence_floor,
            "max_request_bytes": max_request_bytes,
            "state": _stable(deepcopy(state), current_tick=current_tick),
            "plans": _stable(deepcopy(plans), path=("plans",), current_tick=current_tick),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _async_wal_record_sha256(wal_record) -> str | None:
        if wal_record is None:
            return None
        proof = {
            "identity": wal_record.identity.to_dict(),
            "request_sha256": wal_record.request_sha256,
            "phase": wal_record.phase,
            "state": wal_record.state,
            "result_sha256": wal_record.result_sha256,
            "error_category": wal_record.error_category,
            "event_count": wal_record.event_count,
            "http_status": wal_record.http_status,
        }
        encoded = json.dumps(proof, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _async_lineage_digest(value) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _async_plan_lineage_entries(self) -> list[dict]:
        """Load bounded archive-specific plan and verified-attempt witnesses."""
        from .async_decision_archive import MAX_ARCHIVE_RECORDS
        from .telemetry import validate_attempt

        rows = [row for row in self.memory.history
                if row.get("kind") == "async_plan_lineage"]
        if len(rows) > 1:
            raise ValueError("Checkpoint contains duplicate async plan lineage ledgers")
        if not rows:
            return []
        row = rows[0]
        if (set(row) != {"kind", "schema", "entries", "entries_sha256"}
                or type(row.get("schema")) is not int or row["schema"] != 1
                or type(row.get("entries")) is not list
                or len(row["entries"]) > MAX_ARCHIVE_RECORDS
                or type(row.get("entries_sha256")) is not str):
            raise ValueError("Checkpoint async plan lineage ledger is malformed")
        entries = row["entries"]
        seen_archives, seen_attempts = set(), set()
        allowed_terminal_kinds = {
            "plan_failed", "goal_completed", "passive_wait_yielded",
            "effects_already_satisfied",
        }
        for entry in entries:
            fields = {
                "archive_id", "archive_sha256", "request_id", "selected_plan_id",
                "selected_plan_sha256", "source_revision_sha256", "selector_sha256",
                "provider_payload_sha256", "wire_body_sha256", "state",
                "verified_steps", "terminal",
            }
            if (type(entry) is not dict or set(entry) != fields
                    or any(type(entry.get(name)) is not str
                           or re.fullmatch(r"[0-9a-f]{64}", entry[name]) is None
                           for name in ("archive_id", "archive_sha256", "selected_plan_sha256",
                                        "source_revision_sha256", "selector_sha256",
                                        "provider_payload_sha256", "wire_body_sha256"))
                    or type(entry.get("request_id")) is not str
                    or type(entry.get("selected_plan_id")) is not str
                    or not entry["selected_plan_id"]
                    or entry.get("state") not in {"active", "completed", "abandoned"}
                    or type(entry.get("verified_steps")) is not list
                    or len(entry["verified_steps"]) > 32
                    or (entry.get("state") == "active" and entry.get("terminal") is not None)
                    or (entry.get("state") != "active" and type(entry.get("terminal")) is not dict)
                    or entry["archive_id"] in seen_archives):
                raise ValueError("Checkpoint async plan lineage entry is malformed")
            try:
                from uuid import UUID
                if str(UUID(entry["request_id"])) != entry["request_id"]:
                    raise ValueError
            except (ValueError, AttributeError) as error:
                raise ValueError("Checkpoint async plan lineage request identity is malformed") from error
            seen_archives.add(entry["archive_id"])
            indexes = []
            for proof in entry["verified_steps"]:
                if (type(proof) is not dict
                        or set(proof) != {"step_index", "attempt", "outcome_sha256"}
                        or type(proof.get("step_index")) is not int
                        or not 0 <= proof["step_index"] < 32
                        or type(proof.get("outcome_sha256")) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", proof["outcome_sha256"]) is None
                        or type(proof.get("attempt")) is not dict):
                    raise ValueError("Checkpoint async plan step witness is malformed")
                attempt = proof["attempt"]
                validate_attempt(attempt, finished=True)
                if (attempt.get("outcome") != "verified"
                        or attempt.get("step_index") != proof["step_index"]
                        or self._async_lineage_digest(attempt) != proof["outcome_sha256"]
                        or attempt["id"] in seen_attempts):
                    raise ValueError("Checkpoint async plan attempt witness is not unique and verified")
                seen_attempts.add(attempt["id"])
                indexes.append(proof["step_index"])
            if indexes != sorted(set(indexes)):
                raise ValueError("Checkpoint async plan step witnesses are duplicated or unordered")
            terminal = entry["terminal"]
            if entry["state"] == "completed":
                if (set(terminal) != {"kind", "plan_sha256", "attempt_ids"}
                        or terminal.get("kind") != "verified_completion"
                        or terminal.get("plan_sha256") != entry["selected_plan_sha256"]
                        or type(terminal.get("attempt_ids")) is not list
                        or terminal["attempt_ids"] != [proof["attempt"]["id"]
                                                         for proof in entry["verified_steps"]]):
                    raise ValueError("Checkpoint async completed-plan witness is malformed")
            elif entry["state"] == "abandoned":
                kind = terminal.get("kind")
                if kind not in allowed_terminal_kinds:
                    raise ValueError("Checkpoint async abandoned-plan witness is malformed")
                if kind == "plan_failed":
                    if (set(terminal) != {"kind", "plan_id", "plan_sha256", "reason", "tick",
                                          "attempt", "attempt_sha256", "proof_event"}
                            or terminal.get("plan_id") != entry["selected_plan_id"]
                            or terminal.get("plan_sha256") != entry["selected_plan_sha256"]
                            or type(terminal.get("reason")) is not str
                            or type(terminal.get("tick")) is not int
                            or terminal.get("attempt_sha256") != (
                                self._async_lineage_digest(terminal["attempt"])
                                if type(terminal.get("attempt")) is dict else None)
                            or ((terminal.get("attempt") is None)
                                != (terminal.get("proof_event") is None))):
                        raise ValueError("Checkpoint async failed-plan witness is malformed")
                    if terminal.get("attempt") is not None:
                        attempt = terminal["attempt"]
                        validate_attempt(attempt, finished=True)
                        if (attempt.get("plan_id") != entry["selected_plan_id"]
                                or attempt.get("outcome") not in {
                                    "connection_preflight_rejected",
                                    "transfer_preflight_rejected",
                                    "rejected_transfer_reconciled",
                                    "partial_transfer_reconciled",
                                    "zero_effect_transfer_reconciled",
                                    "wait_expired",
                                }
                                or type(terminal.get("proof_event")) is not dict
                                or terminal["proof_event"].get("attempt_id") != attempt["id"]
                                or (attempt["outcome"] == "transfer_preflight_rejected"
                                    and (terminal["proof_event"].get("kind")
                                         != "transfer_preflight_rejected"
                                         or terminal["proof_event"].get("mutation_started") is not False
                                         or terminal["proof_event"].get("proof")
                                         != attempt["dispatch_phases"].get("transfer_rpc", {}).get("proof")))):
                            raise ValueError("Checkpoint async failed-plan terminal proof is invalid")
                        if attempt["id"] in seen_attempts:
                            raise ValueError("Checkpoint async terminal attempt is reused")
                        seen_attempts.add(attempt["id"])
                elif kind == "goal_completed":
                    event = terminal.get("event")
                    if (set(terminal) != {"kind", "plan_id", "plan_sha256", "event"}
                            or terminal.get("plan_id") != entry["selected_plan_id"]
                            or terminal.get("plan_sha256") != entry["selected_plan_sha256"]
                            or type(event) is not dict or event.get("kind") != "goal_completed"
                            or type(event.get("goal")) is not str
                            or type(event.get("tick")) is not int):
                        raise ValueError("Checkpoint async goal-completion witness is malformed")
                elif kind == "passive_wait_yielded":
                    if (set(terminal) != {"kind", "plan_id", "plan_sha256", "event", "attempt",
                                          "attempt_sha256"}
                            or terminal.get("plan_id") != entry["selected_plan_id"]
                            or terminal.get("plan_sha256") != entry["selected_plan_sha256"]
                            or type(terminal.get("event")) is not dict
                            or type(terminal.get("attempt")) is not dict
                            or terminal.get("attempt_sha256")
                            != self._async_lineage_digest(terminal["attempt"])):
                        raise ValueError("Checkpoint async passive-wait witness is malformed")
                    attempt = terminal["attempt"]
                    validate_attempt(attempt, finished=True)
                    if (attempt.get("outcome") != "wait_replanned"
                            or attempt.get("plan_id") != entry["selected_plan_id"]
                            or attempt.get("action") not in {"idle", "factory_wait"}
                            or terminal["event"].get("plan") != entry["selected_plan_id"]
                            or terminal["event"].get("plan_sha256") != entry["selected_plan_sha256"]
                            or terminal["event"].get("attempt_id") != attempt["id"]
                            or attempt["id"] in seen_attempts):
                        raise ValueError("Checkpoint async passive-wait terminal proof is invalid")
                    seen_attempts.add(attempt["id"])
                else:
                    event = terminal.get("event")
                    if (set(terminal) != {"kind", "plan_id", "plan_sha256", "event"}
                            or terminal.get("plan_id") != entry["selected_plan_id"]
                            or terminal.get("plan_sha256") != entry["selected_plan_sha256"]
                            or type(event) is not dict
                            or event.get("kind") != "async_plan_effects_satisfied"
                            or event.get("plan_id") != entry["selected_plan_id"]
                            or event.get("plan_sha256") != entry["selected_plan_sha256"]
                            or type(event.get("tick")) is not int
                            or re.fullmatch(r"[0-9a-f]{64}", str(event.get("snapshot_sha256"))) is None
                            or type(event.get("step_sha256s")) is not list):
                        raise ValueError("Checkpoint async observed-effects witness is malformed")
        if len({entry["archive_id"] for entry in entries if entry["state"] == "active"}) > 1:
            raise ValueError("Checkpoint has multiple active async plan selections")
        encoded = json.dumps(entries, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("Checkpoint async plan lineage exceeds its byte bound")
        if hashlib.sha256(encoded).hexdigest() != row["entries_sha256"]:
            raise ValueError("Checkpoint async plan lineage digest mismatch")
        return entries

    def _async_store_plan_lineage(self, entries: list[dict]) -> None:
        from .async_decision_archive import MAX_ARCHIVE_RECORDS

        if len(entries) > MAX_ARCHIVE_RECORDS:
            raise ValueError("Async plan lineage reached its bounded record capacity")
        encoded = json.dumps(entries, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("Async plan lineage reached its bounded byte capacity")
        self.memory.event("async_plan_lineage", schema=1, entries=entries,
                          entries_sha256=hashlib.sha256(encoded).hexdigest())

    def _async_add_selected_plan_lineage(self, record: dict, entry: dict) -> None:
        if entry["disposition"] != "selected":
            return
        plan = next((item for item in record["selector"]["candidate_plans"]
                     if item.get("id") == entry["selected_plan_id"]), None)
        if (plan is None or fingerprint(plan) != entry["selected_plan_sha256"]
                or self.memory.active_plan is None
                or fingerprint(self.memory.active_plan) != entry["selected_plan_sha256"]):
            raise ValueError("Async selected-plan lineage differs from the committed plan")
        entries = self._async_plan_lineage_entries()
        prior = next((item for item in entries if item["archive_id"] == record["archive_id"]), None)
        if prior is not None:
            if (prior["archive_sha256"] != record["record_sha256"]
                    or prior["selected_plan_sha256"] != entry["selected_plan_sha256"]):
                raise ValueError("Async selected-plan lineage conflicts with its archive")
            return
        if any(item["state"] == "active" for item in entries):
            raise ValueError("A prior async plan selection was not terminally reconciled")
        if len(entries) >= 128:
            raise ValueError("Async plan lineage reached its bounded record capacity")
        selector = record["selector"]
        entries.append({
            "archive_id": record["archive_id"],
            "archive_sha256": record["record_sha256"],
            "request_id": record["request_id"],
            "selected_plan_id": entry["selected_plan_id"],
            "selected_plan_sha256": entry["selected_plan_sha256"],
            "source_revision_sha256": fingerprint(selector["source_revision"]),
            "selector_sha256": fingerprint(selector),
            "provider_payload_sha256": record["provider"]["payload_sha256"],
            "wire_body_sha256": record["provider"]["wire_body_sha256"],
            "state": "active", "verified_steps": [], "terminal": None,
        })
        self._async_store_plan_lineage(entries)

    def _async_bind_verified_attempt(self, outcome: dict) -> None:
        if not self.async_decisions or self.memory.active_plan is None:
            return
        plan = self.memory.active_plan
        plan_sha256 = fingerprint(plan)
        entries = self._async_plan_lineage_entries()
        active_lineages = [entry for entry in entries if entry["state"] == "active"]
        active = [entry for entry in active_lineages
                  if entry["selected_plan_id"] == plan.get("id")
                  and entry["selected_plan_sha256"] == plan_sha256]
        if not active:
            # Existing non-async plans can be resumed in an async-enabled
            # controller; only a selected archive creates this authority.
            if active_lineages:
                raise ValueError("Verified async attempt has no matching active archive lineage")
            return
        if len(active) != 1:
            raise ValueError("Verified async attempt has ambiguous selected-plan lineage")
        entry = active[0]
        index = outcome.get("step_index")
        if (outcome.get("outcome") != "verified"
                or outcome.get("plan_id") != entry["selected_plan_id"]
                or type(index) is not int or not 0 <= index < len(plan["steps"])
                or outcome.get("step_sha256") != fingerprint(plan["steps"][index])
                or outcome.get("finished_tick", -1) > self.memory.last_tick):
            raise ValueError("Verified attempt does not match its active async plan")
        if any(proof["step_index"] == index for proof in entry["verified_steps"]):
            raise ValueError("Async selected plan has duplicate verified step attempts")
        entry["verified_steps"].append({
            "step_index": index, "attempt": deepcopy(outcome),
            "outcome_sha256": self._async_lineage_digest(outcome),
        })
        entry["verified_steps"].sort(key=lambda proof: proof["step_index"])
        self._async_store_plan_lineage(entries)

    def _async_close_current_plan_lineage(self, plan: dict, terminal: dict | None = None) -> None:
        if not self.async_decisions or type(plan) is not dict:
            return
        plan_id, plan_sha256 = plan.get("id"), fingerprint(plan)
        entries = self._async_plan_lineage_entries()
        active = [item for item in entries if item["state"] == "active"]
        matching = [item for item in active
                    if item["selected_plan_id"] == plan_id
                    and item["selected_plan_sha256"] == plan_sha256]
        if not matching:
            if active:
                raise ValueError("Active async plan lineage differs from the plan being cleared")
            return
        if len(matching) != 1:
            raise ValueError("Async plan clear has ambiguous archive lineage")
        entry = matching[0]
        verified = entry["verified_steps"]
        step_count = len(plan.get("steps", []))
        if ([proof["step_index"] for proof in verified] == list(range(step_count))):
            entry["state"] = "completed"
            entry["terminal"] = {
                "kind": "verified_completion", "plan_sha256": plan_sha256,
                "attempt_ids": [proof["attempt"]["id"] for proof in verified],
            }
            self._async_store_plan_lineage(entries)
            return
        if terminal is None:
            raise ValueError(
                "Cannot clear an async selected plan without complete verified or terminal evidence")
        if terminal.get("kind") not in {
                "plan_failed", "goal_completed", "passive_wait_yielded",
                "effects_already_satisfied"}:
            raise ValueError("Async plan clear has an unsupported terminal disposition")
        entry["state"] = "abandoned"
        entry["terminal"] = terminal
        self._async_store_plan_lineage(entries)

    def _async_failure_terminal(self, plan: dict, reason: str) -> dict | None:
        if not self.async_decisions or type(plan) is not dict:
            return None
        plan_sha256 = fingerprint(plan)
        entries = self._async_plan_lineage_entries()
        lineage = next((entry for entry in entries
                        if entry["state"] == "active"
                        and entry["selected_plan_id"] == plan.get("id")
                        and entry["selected_plan_sha256"] == plan_sha256), None)
        if lineage is None:
            return None
        pending = self.memory.pending
        if pending is None and self.memory.attempt is None:
            return {
                "kind": "plan_failed", "plan_id": plan["id"],
                "plan_sha256": plan_sha256, "reason": reason,
                "tick": self.memory.last_tick, "attempt": None,
                "attempt_sha256": None, "proof_event": None,
            }
        if pending is None or self.memory.attempt is not None:
            return None
        candidates = [row for row in self.memory.attempt_outcomes
                      if row.get("plan_id") == plan["id"]
                      and row.get("step_index") == self.memory.step_index
                      and row.get("action") == pending.get("action")
                      and row.get("started_tick") == pending.get("started_tick")]
        if len(candidates) != 1:
            return None
        attempt = candidates[0]
        proof_kind = {
            "connection_preflight_rejected": "connection_preflight_rejected",
            "transfer_preflight_rejected": "transfer_preflight_rejected",
            "rejected_transfer_reconciled": "rejected_transfer_reconciled",
            "partial_transfer_reconciled": "partial_transfer_reconciled",
            "zero_effect_transfer_reconciled": "zero_effect_transfer_reconciled",
            "wait_expired": "async_wait_expired",
        }.get(attempt.get("outcome"))
        if proof_kind is None:
            return None
        proof_rows = [row for row in self.memory.history
                      if row.get("kind") == proof_kind
                      and row.get("attempt_id") == attempt["id"]]
        if len(proof_rows) != 1:
            return None
        proof_event = deepcopy(proof_rows[0])
        event_plan = proof_event.get("plan_id", proof_event.get("plan"))
        if (event_plan != plan["id"]
                or proof_event.get("step_index") != attempt["step_index"]
                or (attempt["outcome"] == "connection_preflight_rejected"
                    and proof_event.get("mutation_started") is not False)
                or (attempt["outcome"] == "transfer_preflight_rejected"
                    and (proof_event.get("mutation_started") is not False
                         or proof_event.get("proof")
                         != attempt["dispatch_phases"].get("transfer_rpc", {}).get("proof")))):
            return None
        return {
            "kind": "plan_failed", "plan_id": plan["id"],
            "plan_sha256": plan_sha256, "reason": reason,
            "tick": self.memory.last_tick, "attempt": deepcopy(attempt),
            "attempt_sha256": self._async_lineage_digest(attempt),
            "proof_event": proof_event,
        }

    def _async_terminal_for_clear(self, plan: dict) -> dict | None:
        if not self.async_decisions or type(plan) is not dict:
            return None
        plan_sha256 = fingerprint(plan)
        for goal, tick in self.memory.completed_goals.items():
            if goal != plan.get("goal"):
                continue
            event = next((row for row in reversed(self.memory.history)
                          if row.get("kind") == "goal_completed"
                          and row.get("goal") == goal and row.get("tick") == tick), None)
            if event is not None and self.memory.pending is None and self.memory.attempt is None:
                return {"kind": "goal_completed", "plan_id": plan["id"],
                        "plan_sha256": plan_sha256, "event": deepcopy(event)}
        for row in reversed(self.memory.history):
            if row.get("kind") != "async_plan_effects_satisfied":
                continue
            if (row.get("plan_id") == plan["id"]
                    and row.get("plan_sha256") == plan_sha256
                    and self.memory.pending is None and self.memory.attempt is None):
                return {"kind": "effects_already_satisfied", "plan_id": plan["id"],
                        "plan_sha256": plan_sha256, "event": deepcopy(row)}
        for row in reversed(self.memory.history):
            if (row.get("kind") not in {"passive_wait_yielded", "maintenance_required"}
                    or row.get("plan") != plan["id"]
                    or row.get("plan_sha256") != plan_sha256):
                continue
            attempt = next((item for item in reversed(self.memory.attempt_outcomes)
                            if item.get("id") == row.get("attempt_id")), None)
            if (attempt is not None and attempt.get("outcome") == "wait_replanned"
                    and attempt.get("action") in {"idle", "factory_wait"}
                    and self.memory.pending is not None and self.memory.attempt is None):
                return {"kind": "passive_wait_yielded", "plan_id": plan["id"],
                        "plan_sha256": plan_sha256, "event": deepcopy(row),
                        "attempt": deepcopy(attempt),
                        "attempt_sha256": self._async_lineage_digest(attempt)}
        return None

    def _async_settlement_entries(self) -> list[dict]:
        from .async_decision_archive import MAX_ARCHIVE_RECORDS

        rows = [row for row in self.memory.history
                if row.get("kind") == "async_decision_settled"]
        if len(rows) > 1:
            raise ValueError("Checkpoint contains duplicate async settlement ledgers")
        if not rows:
            return []
        row = rows[0]
        if (set(row) != {"kind", "schema", "entries", "entries_sha256"}
                or type(row.get("schema")) is not int or row["schema"] != 1
                or type(row.get("entries")) is not list
                or len(row["entries"]) > MAX_ARCHIVE_RECORDS
                or type(row.get("entries_sha256")) is not str):
            raise ValueError("Checkpoint async settlement ledger is malformed")
        from .async_decision_archive import AsyncDecisionArchive

        entries = [AsyncDecisionArchive.validate_settlement_entry(entry)
                   for entry in row["entries"]]
        if len({entry["archive_id"] for entry in entries}) != len(entries):
            raise ValueError("Checkpoint async settlement ledger has duplicate archives")
        encoded = json.dumps(entries, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != row["entries_sha256"]:
            raise ValueError("Checkpoint async settlement ledger digest mismatch")
        return entries

    def _async_record_settlement(self, record: dict, entry: dict) -> None:
        from .async_decision_archive import MAX_ARCHIVE_RECORDS, AsyncDecisionArchive

        entry = AsyncDecisionArchive.validate_settlement_entry(entry)
        entries = self._async_settlement_entries()
        prior = next((item for item in entries
                      if item["archive_id"] == entry["archive_id"]), None)
        if prior is not None:
            if prior != entry:
                raise ValueError("Async decision settlement conflicts with checkpoint lineage")
        else:
            if len(entries) >= MAX_ARCHIVE_RECORDS:
                raise ValueError("Async settlement ledger reached its bounded record capacity")
            entries.append(entry)
        encoded = json.dumps(entries, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.memory.event(
            "async_decision_settled", schema=1, entries=entries,
            entries_sha256=hashlib.sha256(encoded).hexdigest())
        self._async_add_selected_plan_lineage(record, entry)
        self.memory.async_decision = None
        # The chosen-plan disposition and cleared pointer become durable in one
        # checkpoint before the immutable marker is written. A marker without
        # this retained history row cannot authorize an orphaned request.
        self._save()
        self._async_archive.store_settlement(record, entry)

    def _async_reconcile_orphan_archives(self, *, skip_archive_id: str | None = None) -> None:
        """Fail closed on paid archives absent from the current checkpoint pointer."""
        from .async_decision_archive import AsyncDecisionArchive
        from .provider_decision_wal import NOT_SENT

        entries = self._async_settlement_entries()
        by_id = {entry["archive_id"]: entry for entry in entries}
        lineage_entries = self._async_plan_lineage_entries()
        lineage_by_id = {entry["archive_id"]: entry for entry in lineage_entries}
        if len(lineage_by_id) != len(lineage_entries):
            raise ValueError("Checkpoint async plan lineage contains duplicate archives")
        if any(entry["disposition"] == "selected"
               and entry["archive_id"] not in lineage_by_id for entry in entries):
            raise ValueError("Selected async settlement lacks archive-specific plan lineage")
        if any(entry["archive_id"] not in by_id
               or by_id[entry["archive_id"]]["disposition"] != "selected"
               for entry in lineage_entries):
            raise ValueError("Async plan lineage has no selected checkpoint settlement")
        records = self._async_archive.records()
        for record in records:
            archive_id = record["archive_id"]
            if archive_id == skip_archive_id:
                continue
            marker = self._async_archive.load_settlement(record)
            entry = by_id.get(archive_id)
            lease = self._async_lease_from_record(record)
            wal_record = lease.inspect_optional()
            if entry is not None:
                if (entry["archive_sha256"] != record["record_sha256"]
                        or entry["request_id"] != record["request_id"]
                        or entry["wal_record_sha256"] != self._async_wal_record_sha256(wal_record)
                        or entry["wal_state"] != (wal_record.state if wal_record else None)
                        or entry["wal_phase"] != (wal_record.phase if wal_record else None)):
                    raise ValueError("Async settlement no longer matches its archived WAL outcome")
                if entry["disposition"] == "selected":
                    self._async_validate_settled_plan(
                        record, entry, lineage_by_id[archive_id])
                if marker is not None and marker != entry:
                    raise ValueError("Async settlement marker differs from checkpoint lineage")
                if marker is None:
                    # Recover a crash after the checkpoint lineage committed but
                    # before the redundant archive marker reached disk.
                    self._async_archive.store_settlement(record, entry)
                continue
            if marker is not None:
                raise ValueError(
                    "Async settlement marker has no retained checkpoint disposition")
            if wal_record is None:
                # Archive write precedes the checkpoint pointer and WAL. With
                # no WAL row, this orphan was never submitted to the provider.
                continue
            if wal_record.state == "reserved":
                # Reservation alone proves that transport was never authorized.
                # Restore the original health lease before permitting progress.
                self.jev.abandon_unstarted_decision(lease)
                wal_record = lease.inspect_optional()
            if (wal_record is not None and wal_record.state == "failed"
                    and wal_record.phase == NOT_SENT
                    and wal_record.error_category == "local_admission"):
                continue
            raise ValueError(
                "Unpointed async request has an unresolved or paid provider outcome")

    def _async_validate_settled_plan(self, record: dict, entry: dict,
                                     lineage: dict | None = None) -> None:
        """Validate only this archive's exact selected plan and attempt witnesses."""
        plan_id = entry["selected_plan_id"]
        plan_rows = [plan for plan in record["selector"]["candidate_plans"]
                     if plan.get("id") == plan_id]
        offered_ids = {plan.get("id") for plan in record["selector"]["offered_plans"]}
        if len(plan_rows) != 1 or plan_id not in offered_ids:
            raise ValueError("Settled async choice is not an archived offered candidate")
        plan = plan_rows[0]
        if fingerprint(plan) != entry["selected_plan_sha256"]:
            raise ValueError("Settled async choice differs from its archived plan digest")
        if lineage is None:
            raise ValueError("Settled async choice has no archive-specific plan lineage")
        selector = record["selector"]
        if (lineage["archive_id"] != record["archive_id"]
                or lineage["archive_sha256"] != record["record_sha256"]
                or lineage["request_id"] != record["request_id"]
                or lineage["selected_plan_id"] != plan_id
                or lineage["selected_plan_sha256"] != entry["selected_plan_sha256"]
                or lineage["source_revision_sha256"] != fingerprint(selector["source_revision"])
                or lineage["selector_sha256"] != fingerprint(selector)
                or lineage["provider_payload_sha256"] != record["provider"]["payload_sha256"]
                or lineage["wire_body_sha256"] != record["provider"]["wire_body_sha256"]):
            raise ValueError("Async plan lineage differs from its exact archived context")
        steps = plan.get("steps")
        if type(steps) is not list or not steps:
            raise ValueError("Settled async choice has no verifiable plan steps")
        proofs = lineage["verified_steps"]
        if len(proofs) > len(steps):
            raise ValueError("Async plan lineage has more verified steps than the archived plan")
        for expected, proof in enumerate(proofs):
            attempt = proof["attempt"]
            if (proof["step_index"] != expected
                    or attempt.get("step_sha256") != fingerprint(steps[expected])
                    or attempt.get("plan_id") != plan_id
                    or attempt.get("step_index") != expected
                    or attempt.get("finished_tick", -1) > self.memory.last_tick):
                raise ValueError("Async verified attempt does not match its selected plan step")
            current = next((row for row in self.memory.attempt_outcomes
                            if row.get("id") == attempt["id"]), None)
            if current is not None and current != attempt:
                raise ValueError("Async plan attempt differs from retained checkpoint outcome")
        active = self.memory.active_plan
        if (active is not None and fingerprint(active) == entry["selected_plan_sha256"]
                and lineage["state"] == "active"):
            if (active.get("id") != plan_id or lineage["state"] != "active"
                    or self.memory.step_index != len(proofs)):
                raise ValueError("Active plan and its archive-specific lineage disagree")
            return
        if active is not None and active.get("id") == plan_id:
            # A later selection may reuse the same plan ID with a different
            # serialized definition. Validate this older archive against its
            # own completed/terminal lineage instead of comparing it to the
            # newer active definition.
            pass
        if lineage["state"] == "completed":
            if [proof["step_index"] for proof in proofs] != list(range(len(steps))):
                raise ValueError("Completed async plan lineage lacks every verified plan step")
            return
        if lineage["state"] == "abandoned":
            self._async_validate_abandoned_lineage(plan, lineage)
            return
        raise ValueError("Settled async choice is neither active nor terminal in its own lineage")

    def _async_validate_abandoned_lineage(self, plan: dict, lineage: dict) -> None:
        terminal = lineage["terminal"]
        kind = terminal["kind"]
        if kind == "plan_failed":
            if (terminal["tick"] > self.memory.last_tick
                    or self.memory.failures.get(plan["id"], 0) < 1):
                raise ValueError("Abandoned async plan has no retained failure state")
            attempt = terminal["attempt"]
            proof_event = terminal["proof_event"]
            if attempt is None:
                return
            event_kind = {
                "connection_preflight_rejected": "connection_preflight_rejected",
                "transfer_preflight_rejected": "transfer_preflight_rejected",
                "rejected_transfer_reconciled": "rejected_transfer_reconciled",
                "partial_transfer_reconciled": "partial_transfer_reconciled",
                "zero_effect_transfer_reconciled": "zero_effect_transfer_reconciled",
                "wait_expired": "async_wait_expired",
            }[attempt["outcome"]]
            event_plan = proof_event.get("plan_id", proof_event.get("plan"))
            if (proof_event.get("kind") != event_kind
                    or event_plan != plan["id"]
                    or proof_event.get("attempt_id") != attempt["id"]
                    or proof_event.get("step_index") != attempt["step_index"]
                    or proof_event.get("tick", -1) > self.memory.last_tick
                    or (attempt["outcome"] == "connection_preflight_rejected"
                        and proof_event.get("mutation_started") is not False)
                    or (attempt["outcome"] == "transfer_preflight_rejected"
                        and (proof_event.get("mutation_started") is not False
                             or proof_event.get("proof")
                             != attempt["dispatch_phases"].get("transfer_rpc", {}).get("proof")))):
                raise ValueError("Abandoned async plan lacks exact terminal failure evidence")
            return
        if kind == "goal_completed":
            event = terminal["event"]
            if (event["goal"] != plan["goal"]
                    or self.memory.completed_goals.get(plan["goal"]) != event["tick"]
                    or event["tick"] > self.memory.last_tick):
                raise ValueError("Abandoned async plan lacks its exact completed-goal receipt")
            return
        if kind == "passive_wait_yielded":
            event = terminal["event"]
            if (event.get("tick", -1) > self.memory.last_tick
                    or event.get("plan") != plan["id"]
                    or event.get("plan_sha256") != lineage["selected_plan_sha256"]):
                raise ValueError("Abandoned async wait lacks its exact clear event")
            return
        event = terminal["event"]
        steps = plan.get("steps", [])
        if (event.get("tick", -1) > self.memory.last_tick
                or event.get("step_sha256s") != [fingerprint(step) for step in steps]):
            raise ValueError("Abandoned async plan lacks exact observed-effects evidence")

    def _async_complete_disposition(self, record: dict, disposition: str, *,
                                    selected_plan_id: str | None = None,
                                    selected_plan_sha256: str | None = None) -> bool:
        """Checkpoint a terminal decision, then consume/ack its exact WAL result."""
        if self.memory is None or self.memory.async_decision is None:
            raise ValueError("Async decision disposition has no checkpoint pointer")
        current = self._async_archive.load(self.memory.async_decision)
        if (current["archive_id"] != record["archive_id"]
                or current["record_sha256"] != record["record_sha256"]):
            raise ValueError("Async decision archive changed before disposition")
        if disposition == "selected" and (
                self.memory.active_plan is None
                or self.memory.active_plan.get("id") != selected_plan_id
                or fingerprint(self.memory.active_plan) != selected_plan_sha256):
            raise ValueError("Selected async plan is not durably checkpointed")
        pointer = self._async_archive.pointer(
            record, disposition=disposition, selected_plan_id=selected_plan_id,
            selected_plan_sha256=selected_plan_sha256)
        self.memory.async_decision = pointer
        # Selected plan (if any) and its immutable decision pointer reach the
        # checkpoint before the WAL consume transition or health acknowledgment.
        self._save()

        from .provider_decision_wal import NOT_SENT

        lease = self._async_lease_from_record(record)
        wal_record = lease.inspect_optional()
        if wal_record is None:
            if disposition == "selected":
                raise ValueError("Selected async plan has no durable provider response")
        elif wal_record.state == "response_received":
            if disposition == "cancelled_before_send":
                raise ValueError("A sent async response cannot be labeled pre-send cancellation")
            self._async_validate_saved_response(record, wal_record)
            self._async_wal.consume_response(
                lease.identity, lease.wal_request, wal_record.result_sha256)
            self.jev.acknowledge_decision_consumed(lease)
        elif wal_record.state == "consumed":
            if disposition == "cancelled_before_send":
                raise ValueError("A consumed async response cannot be labeled pre-send cancellation")
            self._async_validate_saved_response(record, wal_record)
            self.jev.acknowledge_decision_consumed(lease)
        elif wal_record.state == "reserved":
            if disposition not in {"cancelled", "cancelled_before_send"}:
                return False
            self.jev.abandon_unstarted_decision(lease)
        elif (wal_record.state == "failed" and wal_record.phase == NOT_SENT
              and wal_record.error_category == "local_admission"):
            self.jev.abandon_unstarted_decision(lease)
        elif (wal_record.state == "failed"
              and wal_record.phase == "response_received"
              and wal_record.error_category is not None):
            if disposition == "selected":
                raise ValueError("A provider failure cannot authorize a selected plan")
        else:
            # MAY_HAVE_BEEN_SENT and ambiguous failures remain owned by this
            # checkpoint pointer. No new request or action is authorized.
            return False
        wal_record = lease.inspect_optional()
        entry = self._async_archive.settlement_entry(
            record, disposition=disposition,
            wal_state=wal_record.state if wal_record is not None else None,
            wal_phase=wal_record.phase if wal_record is not None else None,
            wal_record_sha256=self._async_wal_record_sha256(wal_record),
            selected_plan_id=selected_plan_id,
            selected_plan_sha256=selected_plan_sha256)
        self._async_record_settlement(record, entry)
        self._async_active_record = self._async_active_lease = None
        self._async_selection_scope = None
        return True

    def _async_validate_saved_response(self, record: dict, wal_record) -> None:
        """Revalidate a response receipt before cancellation or crash reconciliation consumes it."""
        lease = self._async_lease_from_record(record)
        if wal_record.state == "consumed":
            from dataclasses import replace

            wal_record = replace(wal_record, state="response_received")
        result = lease.recover_result(wal_record)
        if (result.identity != lease.request_identity
                or result.requested_model != record["provider"]["model_id"]
                or result.request_payload_sha256 != record["provider"]["payload_sha256"]):
            raise ValueError("Archived async response identity or payload binding changed")
        from .judgments import InvalidJudgment, validate_answers

        try:
            validate_answers(
                record["selector"]["questions"], _json_mutable(result.answers),
                quantum=getattr(self._async_provider_client, "answer_quantum", 0))
        except InvalidJudgment as error:
            raise ValueError("Archived async response no longer satisfies its exact questions") from error

    def _async_reconcile_terminal_pointer(self) -> None:
        pointer = self.memory.async_decision if self.memory is not None else None
        if pointer is None:
            if self.async_decisions:
                self._async_reconcile_orphan_archives()
            return
        if pointer["disposition"] == "pending":
            if self.memory.active_plan is not None:
                raise ValueError("Pending async request conflicts with a committed active plan")
            self._async_reconcile_orphan_archives(skip_archive_id=pointer["archive_id"])
            return
        record = self._async_archive.load(pointer)
        if pointer["disposition"] == "selected":
            plan = self.memory.active_plan
            if (not isinstance(plan, dict) or plan.get("id") != pointer["selected_plan_id"]
                    or fingerprint(plan) != pointer["selected_plan_sha256"]):
                raise ValueError("Selected async pointer does not match the durable active plan")
        elif self.memory.active_plan is not None:
            raise ValueError("No-action async disposition conflicts with an active plan")
        if not self._async_complete_disposition(
                record, pointer["disposition"],
                selected_plan_id=pointer.get("selected_plan_id"),
                selected_plan_sha256=pointer.get("selected_plan_sha256")):
            raise ValueError("Async provider outcome remains unresolved; refusing controller progress")
        self._async_reconcile_orphan_archives()

    def _async_cancel_request(self) -> bool:
        pointer = self.memory.async_decision if self.memory is not None else None
        if pointer is None:
            return True
        record = self._async_archive.load(pointer)
        lease = self._async_lease_from_record(record)
        wal_record = lease.inspect_optional()
        from .provider_decision_wal import NOT_SENT, RESPONSE_RECEIVED

        if wal_record is None or wal_record.state == "reserved" or (
                wal_record.state == "failed" and wal_record.phase == NOT_SENT
                and wal_record.error_category == "local_admission"):
            disposition = "cancelled_before_send"
        elif (wal_record.state in {"response_received", "consumed"}
              or wal_record.state == "failed" and wal_record.phase == RESPONSE_RECEIVED):
            disposition = "cancelled"
        else:
            # Keep the original request and its WAL owner for ambiguous send
            # reconciliation. Cancellation never proves remote cancellation.
            return False
        completed = self._async_complete_disposition(record, disposition)
        if completed:
            input_sha256 = record["selector"].get("persistent_input_sha256")
            if input_sha256 is not None and self.memory.blocked_recovery is not None:
                from .blocked_persistence import find_attempt, finish_attempt
                row = find_attempt(
                    self.memory, self.provenance["code_revision"], input_sha256,
                    archive_index=self._blocked_recovery_archive_index)
                if row is not None and row.get("outcome") == "pending":
                    finish_attempt(
                        self.memory, self.provenance["code_revision"], input_sha256,
                        "failed", archive_index=self._blocked_recovery_archive_index)
                    self._save()
        return completed

    def _async_resume_pending_selection(self, snapshot: GameSnapshot,
                                        current_state: dict,
                                        current_plans: list[Plan]) -> dict | None:
        pointer = self.memory.async_decision if self.memory is not None else None
        if pointer is None:
            return None
        if pointer["disposition"] != "pending":
            self._async_reconcile_terminal_pointer()
            return None
        record = self._async_archive.load(pointer)
        selector = record["selector"]
        lease = self._async_lease_from_record(record)
        self._async_active_record, self._async_active_lease = record, lease

        def no_action(reason: str, *, stale: bool = False) -> dict:
            from .provider_decision_wal import NOT_SENT

            wal_record = lease.inspect_optional()
            called = (wal_record is not None and wal_record.phase != NOT_SENT)
            completed = self._async_complete_disposition(
                record, "stale" if stale else "no_action")
            if not completed:
                decision = Decision(
                    None, "observe", "Async request remains ambiguous; no action is authorized",
                    state=selector["state"], questions=selector["questions"],
                    model_called=called,
                    diagnostics={"schema": 1, "outcome": "provider_blocked",
                                 "provider": deepcopy(self.jev.state)})
            else:
                decision = Decision(
                    None, "observe", reason, state=selector["state"],
                    questions=selector["questions"], model_called=called,
                    diagnostics={"schema": 1,
                                 "outcome": "stale_async_response" if stale else "async_no_action"})
            return {"decision": decision, "chosen": None,
                    "input_sha256": selector.get("persistent_input_sha256"),
                    "finalized": False, "trace_done": False}

        runtime = self._async_runtime_identity
        provider_id = self._async_provider_client.decision_provider_id()
        model_id = getattr(self._async_provider_client, "model", None)
        identity = record["identity"]
        actor_id = None
        if isinstance(runtime, dict):
            encoded = json.dumps(runtime, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
            actor_id = "actor:" + hashlib.sha256(encoded).hexdigest()
        compatible = (
            selector["source_revision"] == self.provenance.get("code_revision")
            and selector["session_id"] == snapshot.session_id == self.memory.session_id
            and self._async_json_equal(selector["runtime_identity"], runtime)
            and selector["actor_id"] == actor_id
            and identity["session_id"] == snapshot.session_id
            and identity["actor_id"] == actor_id
            and selector["target"] == self.target
            and selector["policy"] == self.policy
            and selector["confidence_floor"] == self.confidence_floor
            and selector["max_request_bytes"] == self.max_request_bytes
            and record["provider"]["provider_id"] == provider_id
            and record["provider"]["model_id"] == model_id
        )
        if not compatible:
            return no_action(
                "Archived async decision no longer matches source, actor, provider, or state",
                stale=True)

        from .judgments import question_batch

        try:
            archived_plans = [Plan.from_dict(row) for row in selector["candidate_plans"]]
            context, questions, offered = question_batch(
                selector["source_state"], archived_plans,
                max_bytes=selector["max_request_bytes"])
            regenerated = (context, questions, [plan.to_dict() for plan in offered])
            saved = (selector["state"], selector["questions"], selector["offered_plans"])
            if (not self._async_json_equal(regenerated, saved)
                    or fingerprint(selector["candidate_plans"]) != selector["frontier_sha256"]):
                return no_action("Archived async selector context failed validation", stale=True)
            from .causal_trace import describe_request_order
            if not self._async_json_equal(
                    describe_request_order(context, questions), selector["request_order"]):
                return no_action("Archived async request order failed validation", stale=True)
            payload = self._async_provider_client.prepare_decision_payload(context, questions)
            if not self._async_json_equal(payload, record["provider"]["payload"]):
                return no_action("Archived async provider payload is stale", stale=True)
        except (KeyError, TypeError, ValueError, OverflowError):
            return no_action("Archived async selector is malformed", stale=True)

        # A fresh observation's clock and known planner receipt timestamps can
        # advance while the provider is working. Compare the source-bound
        # decision semantics through the same clock-normalized fingerprint
        # used by persistent selection, then let the ordinary fresh
        # pre-dispatch checks decide whether the selected action is still safe.
        # The provider result below is always reduced against the archived
        # request and candidate set, never a newly generated request.
        try:
            common = {
                "session_id": snapshot.session_id,
                "source_revision": selector["source_revision"],
                "target": selector["target"],
                "policy": selector["policy"],
                "confidence_floor": selector["confidence_floor"],
                "max_request_bytes": selector["max_request_bytes"],
            }
            archived_semantics = self._async_semantic_selection_sha256(
                selector["source_state"], selector["candidate_plans"],
                current_tick=selector["observation_tick"], **common)
            current_semantics = self._async_semantic_selection_sha256(
                current_state, [plan.to_dict() for plan in current_plans],
                current_tick=snapshot.tick, **common)
        except (KeyError, TypeError, ValueError, OverflowError):
            return no_action("Archived async candidate context failed validation", stale=True)
        if archived_semantics != current_semantics:
            return no_action("Archived async candidate frontier is stale", stale=True)

        self._async_selection_scope = {
            "source_state": selector["source_state"], "state": selector["state"],
            "questions": selector["questions"], "record": record, "lease": lease,
        }
        try:
            with phase("selection", self._diagnostic_trace):
                decision = select_plan(
                    self._trace.client(self._async_decision_adapter),
                    selector["source_state"], archived_plans,
                    selector["confidence_floor"], selector["max_request_bytes"],
                    prepared_batch=(selector["state"], selector["questions"], offered))
        except _AsyncStepCancelled:
            self._async_cancel_request()
            decision = Decision(
                None, "observe", "Async selection was cancelled before plan dispatch",
                state=selector["state"], questions=selector["questions"],
                diagnostics={"schema": 1, "outcome": "async_cancelled"})
            return {"decision": decision, "chosen": None,
                    "input_sha256": selector.get("persistent_input_sha256"),
                    "finalized": False, "trace_done": False}
        if self._async_cancel_requested():
            self._async_cancel_request()
            decision = Decision(
                None, "observe", "Async selection was cancelled before plan dispatch",
                state=decision.state, questions=decision.questions,
                model_called=decision.model_called,
                diagnostics={"schema": 1, "outcome": "async_cancelled"})
            return {"decision": decision, "chosen": None,
                    "input_sha256": selector.get("persistent_input_sha256"),
                    "finalized": False, "trace_done": False}

        chosen_archived = next((plan for plan in offered
                                if plan.id == decision.plan_id), None)
        if decision.diagnostics.get("outcome") == "provider_blocked":
            self._async_complete_disposition(record, "no_action")
            chosen = None
        elif chosen_archived is None:
            self._async_complete_disposition(record, "no_action")
            chosen = None
        else:
            chosen = next((plan for plan in current_plans
                           if plan.id == chosen_archived.id
                           and self._async_json_equal(plan.to_dict(),
                                                      chosen_archived.to_dict())), None)
            if chosen is None:
                if not self._async_complete_disposition(record, "stale"):
                    decision = Decision(
                        None, "observe", "Archived async response is stale and outcome remains ambiguous",
                        state=selector["state"], questions=selector["questions"],
                        model_called=True,
                        diagnostics={"schema": 1, "outcome": "provider_blocked",
                                     "provider": deepcopy(self.jev.state)})
                else:
                    decision = Decision(
                        None, "observe", "Archived async response selected a stale candidate",
                        state=selector["state"], questions=selector["questions"],
                        model_called=True,
                        diagnostics={"schema": 1, "outcome": "stale_async_response"})
        return {"decision": decision, "chosen": chosen,
                "input_sha256": selector.get("persistent_input_sha256"),
                "finalized": False, "trace_done": False}

    def _async_evaluate_request(self, state: dict, questions: dict) -> dict:
        execution = self._async_execution_context()
        scope = self._async_selection_scope
        if not isinstance(scope, dict):
            raise RuntimeError("Async selection request was not durably prepared")
        if (not self._async_json_equal(state, scope["state"])
                or not self._async_json_equal(questions, scope["questions"])):
            raise ValueError("Reducer request differs from the frozen async selection context")
        lease = scope.get("lease")
        if lease is None:
            raise RuntimeError("Async request archive has no reconstructed lease")
        if execution["cancelled"].is_set():
            raise _AsyncStepCancelled
        bridge = execution["bridge"]
        result = bridge.call(lambda: self.jev.evaluate_async(
            state, questions, identity=lease.request_identity,
            deadline=time.monotonic() + self.async_decision_timeout,
            decision_lease=lease,
        ))
        self._async_active_record = scope["record"]
        self._async_active_lease = lease
        projection = {
            "answers": _json_mutable(result.answers),
            "usage": _json_mutable(result.usage),
            "requested_model": result.requested_model,
            "resolved_model": _json_mutable(result.resolved_model),
            "request_id": lease.identity.request_id,
            "request_payload_sha256": lease.request_payload_sha256,
        }
        return projection

    def _async_cancel_before_send(self) -> None:
        pointer = self.memory.async_decision if self.memory is not None else None
        if pointer is None:
            return
        record = self._async_archive.load(pointer)
        lease = self._async_lease_from_record(record)
        wal_record = lease.inspect_optional()
        if wal_record is None:
            pass
        elif wal_record.state == "reserved":
            self.jev.abandon_unstarted_decision(lease)
        else:
            # A transport-entered, response-bearing, or terminal request keeps
            # its exact archive pointer for later reconciliation.
            self._async_active_record, self._async_active_lease = record, lease
            return
        self.memory.async_decision = self._async_archive.pointer(
            record, disposition="cancelled_before_send")
        self._save()
        self.memory.async_decision = None
        self._save()
        self._async_active_record = self._async_active_lease = None
        self._async_selection_scope = None

    @property
    def terminal(self) -> bool:
        if self.memory is None:
            return False
        if self.memory.status == "blocked" and self._persistent_block_active():
            return False
        return self.memory.status in {"completed", "blocked", "uncertain"}

    @staticmethod
    def _background_identity_inconsistent(memory) -> bool:
        """A tracked background craft job is verifiable; half of one is not.

        The job and its attempt record are written together and polled on every
        observation, so waiting through a consistent pair only keeps verifying
        it (an invalid record turns the status ``uncertain``, which stays
        terminal). Exactly one of the two present is an ambiguous native
        request and must not be waited out as a recoverable block.
        """
        return ((getattr(memory, "background_job", None) is None)
                != (getattr(memory, "background_attempt", None) is None))

    def _persistent_block_active(self) -> bool:
        from .blocked_persistence import is_recoverable_reason
        memory = self.memory
        if (not self.persist_recoverable_blocks or memory is None
                or memory.status != "blocked" or not is_recoverable_reason(memory.reason)
                or memory.pending is not None or memory.attempt is not None
                or memory.active_plan is not None or memory.transfer_recovery is not None
                or self._background_identity_inconsistent(memory)
                or self._persistence_failed or self._capital_fault
                or self._persistent_idle_exhausted):
            return False
        if isinstance(self.jev, ProviderCircuit) and self.jev.state.get("phase") != "healthy":
            return False
        if self._safety is not None and self._safety.phase != "healthy":
            return False
        return True

    def persistent_recovery_wait_seconds(self) -> float:
        if (isinstance(self._persistent_recovery_status, dict)
                and self._persistent_recovery_status.get("phase") ==
                "evaluation_outcome_unknown_waiting"):
            return float(self._persistent_recovery_status.get("next_observation_seconds", 0.0))
        if not self._persistent_block_active():
            return 0.0
        from .blocked_persistence import wait_seconds
        if self.memory.blocked_recovery is None:
            return max(self.tick_seconds, float(2 ** max(1, min(self._persistent_runtime_wait_level, 8))))
        return max(self.tick_seconds, wait_seconds(self.memory))

    def _blocked_recovery_attempt_count(self) -> int:
        if self.memory is None or self.memory.blocked_recovery is None:
            return 0
        archive = self.memory.blocked_recovery_archive
        return ((archive["entry_count"] if archive is not None else 0)
                + len(self.memory.blocked_recovery["attempts"]))

    def _persistent_wait(self, snapshot: GameSnapshot, input_sha256: str, *,
                         source_authorized: bool = False,
                         status_phase: str | None = None,
                         status_details: dict | None = None) -> dict:
        """Persist one observation-only wait while retaining the blocked state."""
        from .blocked_persistence import find_attempt, record_wait
        source = self.provenance["code_revision"]
        if source_authorized:
            if self.memory.blocked_recovery is None:
                self._persistent_runtime_wait_level = min(8, self._persistent_runtime_wait_level + 1)
                delay = float(2 ** self._persistent_runtime_wait_level)
            else:
                state_source = self.memory.blocked_recovery.get("source_revision")
                if state_source == source:
                    delay = record_wait(self.memory, source, input_sha256)
                else:
                    self._persistent_runtime_wait_level = min(8, self._persistent_runtime_wait_level + 1)
                    delay = float(2 ** min(self._persistent_runtime_wait_level, 8))
        else:
            delay = record_wait(self.memory, source, input_sha256)
        backoff_delay = delay  # before any tick_seconds floor, so a large tick cannot fake idleness
        delay = max(self.tick_seconds, delay)
        attempt = find_attempt(self.memory, source, input_sha256,
                               archive_index=self._blocked_recovery_archive_index)
        if attempt is None:
            from .compatible_recovery import approved_sources
            historical = [row for revision in approved_sources(self.memory, source)[:-1]
                          if (row := find_attempt(
                              self.memory, revision, input_sha256,
                              archive_index=self._blocked_recovery_archive_index)) is not None]
            if len(historical) > 1:
                raise ValueError("Ambiguous compatible-source wait identity")
            attempt = historical[0] if historical else None
        unresolved = attempt is not None and attempt.get("outcome") == "pending"
        # An unresolved (possibly billed) decision is never abandoned here; only a
        # resolved, unchanged fingerprint already at the longest delay counts as idle.
        if unresolved or backoff_delay < IDLE_DELAY_SECONDS:
            self._persistent_idle_waits = 0
        else:
            self._persistent_idle_waits += 1
        if (not unresolved and self.persistent_idle_observations
                and self._persistent_idle_waits >= self.persistent_idle_observations):
            self._persistent_idle_exhausted = True
            self._persistent_recovery_status = {
                "phase": "idle_wait_exhausted",
                "reason": self.memory.reason,
                "next_observation_seconds": 0.0,
                "model_call": False,
                "decision_input_sha256": input_sha256,
                "recorded_attempts": self._blocked_recovery_attempt_count(),
            }
            self._compact_next_record = True
            try:
                return self._record(
                    snapshot, "observe",
                    "Blocked; idle wait exhausted without changed game evidence "
                    f"after {self._persistent_idle_waits} observations at {delay:g}s")
            finally:
                self._compact_next_record = False
        self._persistent_recovery_status = {
            "phase": ("evaluation_outcome_unknown_waiting" if unresolved
                      else status_phase or "waiting_for_changed_game_evidence"),
            "reason": self.memory.reason,
            "next_observation_seconds": delay,
            "model_call": False,
            "decision_input_sha256": input_sha256,
            "recorded_attempts": self._blocked_recovery_attempt_count(),
        }
        if status_details is not None:
            self._persistent_recovery_status.update(deepcopy(status_details))
        outcome = ("Decision outcome unresolved; observing for changed evidence" if unresolved
                   else "Blocked; waiting for changed game evidence")
        if not unresolved and status_phase == "alternatives_exhausted_waiting":
            outcome = "Blocked; all currently feasible alternatives were already evaluated"
        elif not unresolved and status_phase == "alternative_batch_limit_waiting":
            outcome = "Blocked; bounded alternative evaluation limit reached"
        self._compact_next_record = True
        try:
            return self._record(snapshot, "observe", outcome)
        finally:
            self._compact_next_record = False

    def _record_alternative_frontier_status(self, snapshot: GameSnapshot, *,
                                            input_sha256: str, state_sha256: str,
                                            frontier_sha256: str, phase_name: str,
                                            seen_count: int, unseen_count: int,
                                            reason: str) -> dict:
        """Durably report exhaustion/cap without changing the recoverable reason."""
        from .blocked_persistence import MAX_SELECTION_BATCHES_PER_STATE, is_recoverable_reason
        if not is_recoverable_reason(reason):
            raise ValueError("Alternative frontier can finish only a recoverable rejection")
        expected_kind = ("blocked_recovery_alternatives_exhausted"
                         if phase_name == "alternatives_exhausted_waiting"
                         else "blocked_recovery_alternative_batch_limit")
        already_recorded = any(
                event.get("kind") == expected_kind
                and event.get("state_sha256") == state_sha256
                and event.get("frontier_sha256") == frontier_sha256
                for event in self.memory.history)
        if not already_recorded:
            # The state transition and its diagnostic are one checkpoint save.
            # This closes the crash window between the final rejected WAL row
            # and the ordinary stalled-decision bookkeeping in step().
            if self.memory.status != "blocked":
                self.memory.reason = reason
            self.memory.stalled_decisions += 1
            # The persistent evaluator has reached its declared bound for
            # this unchanged semantic frontier. Mark that state explicitly
            # blocked now; the threshold remains the rule for ordinary
            # non-persistent failures, not for an exhausted one-use frontier.
            self.memory.status = "blocked"
            self.memory.event(
                expected_kind, state_sha256=state_sha256,
                frontier_sha256=frontier_sha256, tick=snapshot.tick,
                evaluated_batches=seen_count, unseen_candidates=unseen_count,
                max_batches=MAX_SELECTION_BATCHES_PER_STATE, reason=reason)
            details = {"selection_state_sha256": state_sha256,
                       "selection_frontier_sha256": frontier_sha256,
                       "evaluated_batches": seen_count,
                       "unseen_candidates": unseen_count,
                       "max_batches": MAX_SELECTION_BATCHES_PER_STATE}
            self._persistent_recovery_status = {
                "phase": phase_name,
                "reason": self.memory.reason,
                "next_observation_seconds": self.persistent_recovery_wait_seconds(),
                "model_call": bool(self._decision and self._decision.model_called),
                "decision_input_sha256": input_sha256,
                "recorded_attempts": self._blocked_recovery_attempt_count(),
                **details,
            }
            outcome = ("Blocked; all currently feasible alternatives were already evaluated"
                       if phase_name == "alternatives_exhausted_waiting"
                       else "Blocked; bounded alternative evaluation limit reached")
            # Keep this first terminal evaluation as a full decision record.
            # Later unchanged observations use the compact persistent-wait path.
            self._compact_next_record = False
            return self._record(snapshot, "observe", outcome)
        return self._persistent_wait(
            snapshot, input_sha256, status_phase=phase_name,
            status_details={"selection_state_sha256": state_sha256,
                            "selection_frontier_sha256": frontier_sha256,
                            "evaluated_batches": seen_count,
                            "unseen_candidates": unseen_count,
                            "max_batches": MAX_SELECTION_BATCHES_PER_STATE})

    def _persistent_selection_with_alternatives(
            self, snapshot: GameSnapshot, state: dict, plans: list[Plan], *,
            source_authorized: bool = False,
            authorization_reason: str | None = None) -> dict:
        """Evaluate at most three durable, non-overlapping prepared batches.

        A batch is committed to the existing persistent attempt ledger before
        the provider call. Only completed recoverable rejections permit trying
        an unseen candidate batch. Pending, provider, validation, and unknown
        outcomes remain one-use and stop this pass.
        """
        if getattr(self, "two_stage_decisions", False):
            from . import two_stage_controller
            if two_stage_controller.pending(self):
                return two_stage_controller.advance(self)
        from . import judgments
        from .blocked_persistence import (
            MAX_SELECTION_BATCHES_PER_STATE, is_recoverable_reason,
            _candidate_semantic_sha256, decision_input_sha256, find_attempt,
            selection_attempts_for_state, selection_batch_metadata,
            selection_frontier_sha256, selection_state_sha256, was_attempted,
        )

        question_batch = getattr(judgments, "question_batch", None)
        if not callable(question_batch):
            raise RuntimeError("Prepared decision batches are unavailable in this source revision")
        source = self.provenance["code_revision"]
        plan_rows = [plan.to_dict() for plan in plans]
        state_sha256 = selection_state_sha256(
            state, plan_rows, session_id=snapshot.session_id, source_revision=source,
            target=self.target, policy=self.policy, confidence_floor=self.confidence_floor,
            current_tick=snapshot.tick)
        frontier_sha256 = selection_frontier_sha256(
            state, plan_rows, session_id=snapshot.session_id, source_revision=source,
            target=self.target, policy=self.policy, confidence_floor=self.confidence_floor,
            current_tick=snapshot.tick)
        from .compatible_recovery import approved_sources
        aliases = [(revision, selection_state_sha256(
            state, plan_rows, session_id=snapshot.session_id, source_revision=revision,
            target=self.target, policy=self.policy, confidence_floor=self.confidence_floor,
            current_tick=snapshot.tick))
            for revision in approved_sources(self.memory, source)]
        rows = selection_attempts_for_state(
            self.memory, source, state_sha256,
            archive_index=self._blocked_recovery_archive_index,
            compatible_state_hashes=aliases)

        # Old implementations keyed one request by the entire unprepared
        # frontier. It cannot tell us which candidates the provider actually
        # saw, so preserve it as an ambiguity instead of guessing and replaying.
        legacy_input = decision_input_sha256(
            state, plan_rows, session_id=snapshot.session_id, source_revision=source,
            target=self.target, policy=self.policy, confidence_floor=self.confidence_floor,
            current_tick=snapshot.tick)
        legacy_row = find_attempt(
            self.memory, source, legacy_input,
            archive_index=self._blocked_recovery_archive_index)
        if legacy_row is not None and "selection_batch" not in legacy_row:
            return {"record": self._persistent_wait(snapshot, legacy_input)}

        from .paid_selection_reconciliation import scoped_representation_budget_rows
        carried, carried_seen = scoped_representation_budget_rows(
            self.memory,aliases,archive_index=self._blocked_recovery_archive_index)
        carried_keys={(row["source_revision"]["commit"],row["source_revision"]["source_sha256"],
                       row["decision_input_sha256"]) for row in carried}
        async_pending_record = None
        async_pending_input = None
        if getattr(self, "async_decisions", False) and self.memory.async_decision is not None:
            async_pending_record = self._async_archive.load(self.memory.async_decision)
            async_pending_input = async_pending_record["selector"].get(
                "persistent_input_sha256")
        seen_candidates = set(carried_seen) | {
            offered["candidate_sha256"] for row in rows
            if (row["source_revision"]["commit"],row["source_revision"]["source_sha256"],
                row["decision_input_sha256"]) not in carried_keys
            for offered in row["selection_batch"]["offered"]}
        for row in rows:
            if (async_pending_record is not None and row.get("outcome") == "pending"
                    and row.get("decision_input_sha256") == async_pending_input):
                seen_candidates.difference_update(
                    item["candidate_sha256"] for item in row["selection_batch"]["offered"])
                continue
            if row.get("outcome") != "rejected" or not is_recoverable_reason(row.get("reason")):
                return {"record": self._persistent_wait(
                    snapshot, row["decision_input_sha256"])}

        evidence = state.get("candidate_evidence", {})
        if not isinstance(evidence, dict):
            raise ValueError("Invalid persistent candidate evidence map")
        candidate_digests = {
            plan.id: _candidate_semantic_sha256(
                plan.to_dict(), evidence.get(plan.id), current_tick=snapshot.tick)
            for plan in plans
        }
        if len(set(candidate_digests.values())) != len(candidate_digests):
            # Identical semantic candidates cannot safely be distinguished by
            # an ID-only prompt; fail closed rather than re-offer one.
            raise ValueError("Persistent candidate frontier contains semantic duplicates")

        def unseen_plans():
            return [plan for plan in plans if candidate_digests[plan.id] not in seen_candidates]

        if async_pending_record is not None:
            pending_row = next((row for row in rows
                                if row.get("outcome") == "pending"
                                and row.get("decision_input_sha256") == async_pending_input), None)
            if pending_row is None:
                return {"record": self._persistent_wait(snapshot, async_pending_input)}
            resumed = self._async_resume_pending_selection(snapshot, state, unseen_plans())
            if resumed is None:
                raise ValueError("Pending async selection disappeared during recovery")
            return resumed

        legacy_reason = self.memory.reason
        if len(rows) >= MAX_SELECTION_BATCHES_PER_STATE:
            unseen_count = len(unseen_plans())
            input_sha = rows[-1]["decision_input_sha256"] if rows else legacy_input
            prior_reason = (rows[-1]["reason"] if rows else self.memory.reason)
            return {"record": self._record_alternative_frontier_status(
                snapshot, input_sha256=input_sha, state_sha256=state_sha256,
                frontier_sha256=frontier_sha256,
                phase_name=("alternatives_exhausted_waiting" if unseen_count == 0
                            else "alternative_batch_limit_waiting"),
                seen_count=len(rows), unseen_count=unseen_count,
                reason=prior_reason)}

        source_auth_pending = source_authorized
        # Keep waiting on the last exact prepared request. Falling back to the
        # legacy whole-frontier fingerprint on an exhausted repeat resets the
        # durable backoff after every poll and prevents the idle bound from
        # ever accumulating.
        last_input_sha256 = (rows[-1]["decision_input_sha256"] if rows else legacy_input)
        for _batch_number in range(len(rows), MAX_SELECTION_BATCHES_PER_STATE):
            remaining = unseen_plans()
            if not remaining:
                return {"record": self._record_alternative_frontier_status(
                    snapshot, input_sha256=last_input_sha256,
                    state_sha256=state_sha256, frontier_sha256=frontier_sha256,
                    phase_name="alternatives_exhausted_waiting",
                    seen_count=len(rows), unseen_count=0,
                    reason=rows[-1]["reason"])}

            # question_batch performs the same bounded lossless compaction used
            # by selection. A preparation failure is local, so it gets no WAL
            # row and cannot be mistaken for a provider evaluation.
            context, questions, offered = question_batch(
                state, remaining, max_bytes=self.max_request_bytes)
            if not offered:
                raise ValueError("Prepared selection batch has no offered candidates")
            metadata = selection_batch_metadata(
                context, questions, offered, state_sha256=state_sha256,
                frontier_sha256=frontier_sha256, current_tick=snapshot.tick)
            input_sha256 = decision_input_sha256(
                context, [plan.to_dict() for plan in offered],
                session_id=snapshot.session_id, source_revision=source,
                target=self.target, policy=self.policy,
                confidence_floor=self.confidence_floor, current_tick=snapshot.tick,
                questions=questions, selection_batch=metadata)
            last_input_sha256 = input_sha256
            if was_attempted(
                    self.memory, source, input_sha256,
                    allow_source_change=source_auth_pending,
                    archive_index=self._blocked_recovery_archive_index):
                return {"record": self._persistent_wait(
                    snapshot, input_sha256, source_authorized=source_auth_pending)}

            batch_source_authorized = source_auth_pending
            self._persistent_runtime_wait_level = 0
            self._persistent_idle_waits = 0
            self._persistent_recovery_status = {
                "phase": "evaluating_changed_game_evidence",
                "reason": legacy_reason,
                "model_call": True,
                "decision_input_sha256": input_sha256,
                "selection_state_sha256": state_sha256,
                "selection_frontier_sha256": frontier_sha256,
                "offered_candidate_count": len(offered),
                "recorded_attempts": self._blocked_recovery_attempt_count(),
            }
            if getattr(self, "two_stage_decisions", False):
                from . import two_stage_controller
                two_stage_controller.prepare(
                    self, snapshot, state, remaining, context, questions, offered,
                    metadata, input_sha256, source_authorized=batch_source_authorized,
                    authorization_reason=authorization_reason)
                result = two_stage_controller.advance(self)
                # The existing caller durably finishes this batch. Subsequent
                # polls may offer only the bounded, previously unseen remainder.
                return result
            async_client = self.jev
            async_request = False
            if self.async_decisions:
                from .judgments import is_lone_passive_background_wait

                if not is_lone_passive_background_wait(remaining):
                    async_request = True
                    self._async_prepare_selection(
                        snapshot=snapshot, source_state=state, state=context,
                        questions=questions, plans=remaining, offered=offered,
                        selection_batch=metadata,
                        persistent_input_sha256=input_sha256,
                        source_authorized=batch_source_authorized,
                        authorization_reason=authorization_reason,
                        checkpoint_update=lambda: self._record_persistent_attempt(
                            snapshot, input_sha256,
                            source_authorized=batch_source_authorized,
                            authorization_reason=authorization_reason,
                            selection_batch=metadata, save=False))
                    async_client = self._async_decision_adapter
                else:
                    self._async_selection_scope = None
            if not async_request:
                self._record_persistent_attempt(
                    snapshot, input_sha256, source_authorized=batch_source_authorized,
                    authorization_reason=authorization_reason,
                    selection_batch=metadata)
            source_auth_pending = False
            try:
                with phase("selection", self._diagnostic_trace):
                    self._decision = select_plan(
                        self._trace.client(async_client), state, remaining,
                        self.confidence_floor, self.max_request_bytes,
                        prepared_batch=(context, questions, offered))
            except _AsyncStepCancelled:
                self._async_cancel_request()
                return {"record": self._record(
                    snapshot, "observe",
                    "Async selection cancelled; provider delivery phase was preserved without action")}
            except ValueError as error:
                self._decision = Decision(
                    None, "observe", str(error), state=context,
                    diagnostics={"schema": 1, "outcome": "request_rejected"})

            if self.async_decisions and self._async_cancel_requested():
                self._async_cancel_request()
                return {"record": self._record(
                    snapshot, "observe", "Async selection was cancelled before plan persistence")}

            pruned = self._decision.diagnostics.get("pruned_candidate_ids")
            if pruned:
                print(f"[t={snapshot.tick}] request budget pruned {len(pruned)} of "
                      f"{self._decision.diagnostics.get('input_candidates')} candidates "
                      f"({self._decision.diagnostics.get('request_bytes')}/"
                      f"{self.max_request_bytes} bytes): {', '.join(pruned)}", flush=True)
            chosen = next((plan for plan in offered
                           if plan.id == self._decision.plan_id), None)
            if chosen is not None:
                return {"decision": self._decision, "chosen": chosen,
                        "input_sha256": input_sha256, "finalized": False,
                        "trace_done": False}

            outcome = self._decision.diagnostics.get("outcome")
            reason = self._decision.reason
            if outcome == "provider_blocked":
                return {"decision": self._decision, "chosen": None,
                        "input_sha256": input_sha256, "finalized": False,
                        "trace_done": False}
            if (not is_recoverable_reason(reason)
                    or outcome not in {"all_candidates_rejected", "low_choice_confidence"}):
                return {"decision": self._decision, "chosen": None,
                        "input_sha256": input_sha256, "finalized": False,
                        "trace_done": False}

            # Persist a completed rejection before opening another provider
            # request. A crash after this save resumes from the next unseen set.
            self._trace_decision()
            from .blocked_persistence import finish_attempt
            finish_attempt(self.memory, source, input_sha256, "rejected", reason,
                           archive_index=self._blocked_recovery_archive_index)
            self._save()
            if self.async_decisions and self.memory.async_decision is not None:
                record = self._async_archive.load(self.memory.async_decision)
                if not self._async_complete_disposition(record, "no_action"):
                    return {"decision": Decision(
                        None, "observe", "Async result remains unresolved; no action is authorized",
                        state=context, questions=questions, model_called=True,
                        diagnostics={"schema": 1, "outcome": "provider_blocked",
                                     "provider": deepcopy(self.jev.state)}),
                        "chosen": None, "input_sha256": input_sha256,
                        "finalized": False, "trace_done": True}
            seen_candidates.update(
                item["candidate_sha256"] for item in metadata["offered"])
            rows.append({"decision_input_sha256": input_sha256,
                         "outcome": "rejected", "reason": reason,
                         "selection_batch": metadata})
            if not unseen_plans():
                return {"decision": self._decision, "chosen": None,
                        "input_sha256": input_sha256, "finalized": True,
                        "trace_done": True,
                        "wait_phase": "alternatives_exhausted_waiting",
                        "state_sha256": state_sha256,
                        "frontier_sha256": frontier_sha256,
                        "reason": reason,
                        "seen_count": len(rows), "unseen_count": 0}
            if len(rows) >= MAX_SELECTION_BATCHES_PER_STATE:
                return {"decision": self._decision, "chosen": None,
                        "input_sha256": input_sha256, "finalized": True,
                        "trace_done": True,
                        "wait_phase": "alternative_batch_limit_waiting",
                        "state_sha256": state_sha256,
                        "frontier_sha256": frontier_sha256,
                        "reason": reason,
                        "seen_count": len(rows),
                        "unseen_count": len(unseen_plans())}

        raise AssertionError("Persistent selection batch loop exited unexpectedly")

    def _record_persistent_attempt(self, snapshot: GameSnapshot, input_sha256: str, *,
                                   source_authorized: bool = False,
                                   authorization_reason: str | None = None,
                                   outcome: str = "pending",
                                   selection_batch: dict | None = None,
                                   save: bool = True) -> None:
        """Write-ahead one decision fingerprint before model selection."""
        if source_authorized:
            # The changed-contract source authorization and its first concrete
            # fingerprint share the same durable checkpoint commit.
            self._consume_blocked_reevaluation(
                snapshot, input_sha256, authorization_reason=authorization_reason,
                persistent_outcome=outcome, selection_batch=selection_batch, save=save)
            return
        from .blocked_persistence import is_recoverable_reason, finish_attempt, record_attempt
        self._archive_full_recovery_tail()
        prior_recovery = deepcopy(self.memory.blocked_recovery)
        prior_history = deepcopy(self.memory.history)
        try:
            reason = self.memory.reason if is_recoverable_reason(self.memory.reason) else None
            record_attempt(self.memory, self.provenance["code_revision"], input_sha256,
                           reason, snapshot.tick,
                           archive_index=self._blocked_recovery_archive_index,
                           selection_batch=selection_batch)
            if outcome != "pending":
                finish_attempt(self.memory, self.provenance["code_revision"], input_sha256,
                               outcome, reason,
                               archive_index=self._blocked_recovery_archive_index)
            self.memory.event(
                "blocked_recovery_attempt", decision_input_sha256=input_sha256,
                tick=snapshot.tick, source_head=self.provenance["code_revision"]["commit"])
            if save:
                self._save()
        except BaseException:
            self.memory.blocked_recovery = prior_recovery
            self.memory.history = prior_history
            raise

    def _archive_full_recovery_tail(self) -> None:
        """Commit a full attempt tail to immutable history before a new WAL row."""
        from .blocked_persistence import MAX_ATTEMPTS
        if (self.memory.blocked_recovery is None
                or len(self.memory.blocked_recovery["attempts"]) < MAX_ATTEMPTS):
            return
        from .blocked_recovery_archive import archive_full_tail
        old_index = self._blocked_recovery_archive_index
        # First establish a durable base for classifying any failure from the
        # pointer commit below. A failed save can occur either before or after
        # replacement; the controller must stop and restore memory from the
        # exact bytes that remain authoritative on disk.
        self._save()
        prior_bytes = self.checkpoint.read_bytes()
        new_index = archive_full_tail(self.checkpoint, self.memory)
        from .checkpoint_io import checkpoint_data
        expected_new = json.dumps(checkpoint_data(self.memory), sort_keys=True,
                                  allow_nan=False).encode("utf-8")
        try:
            self._save()
        except BaseException:
            new_index.close()
            try:
                actual = self.checkpoint.read_bytes()
                if actual == prior_bytes:
                    restored = self.memory_type.from_bytes(
                        actual, self.memory.session_id, self.target)
                    restored._checkpoint_cache = None
                    if old_index is not None:
                        self._blocked_recovery_archive_index = old_index
                        restored._blocked_recovery_archive_index = old_index
                elif actual == expected_new:
                    restored = self.memory_type.from_bytes(
                        actual, self.memory.session_id, self.target)
                    restored._checkpoint_cache = None
                    from .blocked_recovery_archive import build_index
                    restored_index = build_index(self.checkpoint, restored)
                    self._blocked_recovery_archive_index = restored_index
                    restored._blocked_recovery_archive_index = restored_index
                    if old_index is not None:
                        old_index.close()
                else:
                    raise ValueError("Checkpoint bytes match neither side of archive rotation")
                self.memory = restored
            except BaseException as reconciliation_error:
                # _save has already latched _persistence_failed. A state that
                # cannot be reconciled is unusable and must never reach observe.
                self.memory = None
                raise RuntimeError(
                    "Archive rotation failed and checkpoint state could not be reconciled"
                ) from reconciliation_error
            raise
        self._blocked_recovery_archive_index = new_index
        self.memory._blocked_recovery_archive_index = new_index
        if old_index is not None:
            old_index.close()

    def _blocked_frontier_wait(self, snapshot: GameSnapshot, input_sha256: str, *,
                               source_authorized: bool = False,
                               authorization_reason: str | None = None) -> dict:
        """Authorize and record a no-candidate frontier without calling the model."""
        from .blocked_persistence import was_attempted
        if was_attempted(self.memory, self.provenance["code_revision"], input_sha256,
                         allow_source_change=source_authorized,
                         archive_index=self._blocked_recovery_archive_index):
            return self._persistent_wait(snapshot, input_sha256)
        self._record_persistent_attempt(snapshot, input_sha256,
                                        source_authorized=source_authorized,
                                        authorization_reason=authorization_reason,
                                        outcome="frontier")
        wait = self.persistent_recovery_wait_seconds()
        self._persistent_recovery_status = {
            "phase": "waiting_for_changed_game_evidence",
            "reason": self.memory.reason,
            "next_observation_seconds": wait,
            "model_call": False,
            "decision_input_sha256": input_sha256,
            "recorded_attempts": self._blocked_recovery_attempt_count(),
        }
        return self._record(snapshot, "observe", "Blocked; waiting for changed game evidence")

    def _diagnostic_trace(self, event: dict) -> None:
        if self._trace._failed:
            self._persistence_failed = True
            return  # Preserve the original recording error and prepared checkpoint.
        validate_phase(event)
        self._phases.append(deepcopy(event))
        self._phases = self._phases[-64:]
        if self.memory is not None and self.memory.attempt is not None:
            if event["stage"] in DISPATCH_STAGES:
                self.memory.attempt["dispatch_phases"][event["stage"]] = deepcopy(event)
                if event.get("error_code") == "transfer_preflight_rejected":
                    attempt = self.memory.attempt
                    proof = event["proof"]
                    plan = Plan.from_dict(self.memory.active_plan)
                    step = plan.steps[self.memory.step_index]
                    self._validate_transfer_preflight_binding(
                        proof, attempt, plan, step, self.memory.step_index,
                        self.memory.session_id)
                    self.memory.last_tick = max(self.memory.last_tick, proof["tick"])
                from .telemetry import validate_attempt
                validate_attempt(self.memory.attempt)
                self._save()
            elif event["status"] == "failed":
                self.memory.attempt["observation_error"] = deepcopy(event)
                self._save()

    def _finish_attempt(self, snapshot: GameSnapshot, outcome: str = "verified") -> None:
        attempt = self.memory.attempt
        if attempt is None:
            raise ValueError("Cannot finish an unidentified pending action")
        latency = None
        if self._attempt_clock is not None and self._attempt_clock[0] == attempt["id"]:
            latency = time.perf_counter() - self._attempt_clock[1]
        finished = {
            **deepcopy(attempt), "outcome": outcome, "finished_tick": snapshot.tick,
            "finished_at_utc": utc_now(), "latency_seconds": latency,
        }
        self.memory.attempt_outcomes.append(finished)
        if outcome == "verified":
            self._async_bind_verified_attempt(finished)
        self.memory.attempt_outcomes = retain_latest_craft(self.memory.attempt_outcomes)
        self._trace.release_attempt(attempt["id"])
        self.memory.attempt = None
        self._attempt_clock = None

    def _observe(self, stage: str = "observe") -> GameSnapshot:
        if self._persistence_failed:
            raise RuntimeError("Checkpoint persistence failed; reconstruct before continuing")
        with phase(stage, self._diagnostic_trace):
            return self._observe_snapshot()

    def _initial_memory(self, snapshot: GameSnapshot):
        """Restore once at the first fresh observation, before any actor work."""
        if self._connector_checkpoint_preflight_sha is not None:
            if hashlib.sha256(self.checkpoint.read_bytes()).hexdigest() != self._connector_checkpoint_preflight_sha:
                raise ValueError('Connector checkpoint changed after preflight')
        if self.resume_controller and self._blocked_reevaluation_checkpoint_sha256 is not None:
            from .blocked_reevaluation import validate_checkpoint_capture
            raw = self.checkpoint.read_bytes()
            memory = validate_checkpoint_capture(
                raw, self._blocked_reevaluation_checkpoint_sha256, self.memory_type,
                self.target, self.max_stalled_decisions,
                self._blocked_reevaluation_source["decision_contract_sha256"],
                checkpoint_path=self.checkpoint)
            if memory.session_id != snapshot.session_id or self.checkpoint.read_bytes() != raw:
                raise ValueError("Blocked decision checkpoint identity changed during restore")
        else:
            memory = (self.memory_type.load(self.checkpoint, snapshot.session_id, self.target)
                      if self.resume_controller else self.memory_type(snapshot.session_id, self.target))
        return self._complete_initial_memory_restore(memory)

    def _complete_initial_memory_restore(self, memory, *, archive_index=None):
        """Bind already-validated restore state and apply common recovery checks.

        Composed controllers may need to restore an immutable checkpoint capture
        instead of reopening a path. Keep this final binding/validation shared
        so such restorers cannot skip archive lookup or source-lineage checks.
        """
        if archive_index is not None:
            memory._blocked_recovery_archive_index = archive_index
        self._blocked_recovery_archive_index = getattr(
            memory, "_blocked_recovery_archive_index", None)
        from .compatible_recovery import validate_current_owner
        validate_current_owner(memory, self.provenance)
        if self.persist_recoverable_blocks:
            from .blocked_persistence import validate_memory_state
            validate_memory_state(
                memory, self.provenance.get("code_revision"),
                allow_source_change=self._reevaluate_blocked_once)
        if memory.two_stage_decision is not None and not self.two_stage_decisions:
            raise ValueError("Checkpoint requires its two-stage decision protocol")
        return memory

    def _consume_blocked_reevaluation(self, snapshot: GameSnapshot,
                                      persistent_input: str | None = None, *,
                                      authorization_reason: str | None = None,
                                      persistent_outcome: str = "pending",
                                      selection_batch: dict | None = None,
                                      save: bool = True) -> None:
        """Durably consume the one-use authorization before any model request."""
        from .blocked_reevaluation import validate_blocked_memory

        validate_blocked_memory(self.memory, self.max_stalled_decisions)
        if snapshot.world_kind != "fle" or self.policy != "jev" or getattr(self.jev, "is_mock", False):
            raise ValueError("Blocked decision re-evaluation is limited to live Jev-controlled FLE")
        source = self._blocked_reevaluation_source
        contract = source["decision_contract_sha256"]
        if any(entry["decision_contract_sha256"] == contract
               for entry in self.memory.blocked_reevaluations):
            raise ValueError("This decision contract already consumed a blocked re-evaluation")
        if len(self.memory.blocked_reevaluations) >= 1024:
            raise ValueError("Blocked decision re-evaluation ledger is full")
        ledger_reason = authorization_reason or self.memory.reason
        from .memory import _BLOCKED_REEVALUATION_REASONS
        if ledger_reason not in _BLOCKED_REEVALUATION_REASONS:
            raise ValueError("Blocked decision re-evaluation reason is not eligible")
        if persistent_input is not None:
            self._archive_full_recovery_tail()
        prior_history = deepcopy(self.memory.history)
        prior_ledger = deepcopy(self.memory.blocked_reevaluations)
        prior_recovery = deepcopy(self.memory.blocked_recovery)
        try:
            self.memory.blocked_reevaluations.append({
                "schema": 1,
                "authorization_id": uuid4().hex,
                "blocked_source_revision": source["blocked_source_revision"],
                "source_head": source["source_head"],
                "decision_contract_sha256": contract,
                "checkpoint_sha256": self._blocked_reevaluation_checkpoint_sha256,
                "stalled_decisions": self.memory.stalled_decisions,
                "reason": ledger_reason,
                "tick": snapshot.tick,
                "state": "consumed",
            })
            self.memory.event("blocked_decision_reevaluation_consumed",
                              decision_contract_sha256=contract,
                              blocked_source_revision=source["blocked_source_revision"],
                              source_head=source["source_head"], tick=snapshot.tick,
                              stalled_decisions=self.memory.stalled_decisions)
            if persistent_input is not None:
                from .blocked_persistence import finish_attempt, record_attempt, is_recoverable_reason
                attempt_reason = (self.memory.reason
                                  if is_recoverable_reason(self.memory.reason) else None)
                record_attempt(self.memory, self.provenance["code_revision"], persistent_input,
                               attempt_reason, snapshot.tick, allow_source_change=True,
                               archive_index=self._blocked_recovery_archive_index,
                               selection_batch=selection_batch)
                if persistent_outcome != "pending":
                    finish_attempt(self.memory, self.provenance["code_revision"], persistent_input,
                                   persistent_outcome, attempt_reason,
                                   archive_index=self._blocked_recovery_archive_index)
                self.memory.event("blocked_recovery_attempt", decision_input_sha256=persistent_input,
                                  tick=snapshot.tick,
                                  source_head=self.provenance["code_revision"]["commit"])
            if save:
                self._save()
        except BaseException:
            self.memory.history = prior_history
            self.memory.blocked_reevaluations = prior_ledger
            self.memory.blocked_recovery = prior_recovery
            raise
        self._reevaluate_blocked_once = False

    def _observe_snapshot(self) -> GameSnapshot:
        snapshot = self._trace.observe(self.backend, self._trace.observation_phase)
        if 'solid_routes' in snapshot.factory and not getattr(self, '_solid_routes_enabled', False):
            raise ValueError('Existing solid-route runtime requires its explicit controller capability')
        if 'successors' in snapshot.factory and not getattr(self, '_successors_enabled', False):
            raise ValueError('Existing successor runtime requires its explicit controller capability')
        if 'mining_outposts' in snapshot.factory and not getattr(self, '_mining_outposts_enabled', False):
            raise ValueError('Existing mining-outpost runtime requires its explicit controller capability')
        if not snapshot.session_id or snapshot.world_kind not in {"mock", "fle"}:
            raise ValueError("Hierarchical control requires identified backend/session telemetry")
        if self.policy != "deterministic" and getattr(self.jev, "is_mock", False) and snapshot.world_kind != "mock":
            raise ValueError("A mock model cannot control or benchmark a live backend")
        if type(snapshot.tick) is not int or snapshot.tick < 0:
            raise ValueError("Invalid observation tick")
        # Validate serialized facts rather than allowing NaN into conditions.
        json.dumps(snapshot.for_jev(), allow_nan=False)
        if self.memory is None:
            self.memory = self._initial_memory(snapshot)
        if self.memory.session_id != snapshot.session_id or snapshot.tick < self.memory.last_tick:
            raise ValueError("Session changed or observation tick regressed; refusing to act")
        self._validate_async_observation(snapshot)
        self.memory.last_tick = snapshot.tick
        if snapshot.world_kind == 'fle' or self.memory.connector_ownership is not None:
            from .connector_checkpoint import reconcile
            native = getattr(self.backend, '_factory', None)
            reconcile(self.memory, snapshot, native, resume=self.resume_controller)
        from .capital_controller import observe as observe_capital
        observe_capital(self, snapshot)
        evidence = (self._capacity_history.observe(snapshot, self.catalog)
                    if self.catalog is not None and self.factory_scheduling == "ready-work" else {})
        fuel_evidence = {}
        if self.catalog is not None and self.factory_scheduling == "ready-work":
            pending = self.memory.pending or {}
            fuel_evidence = self._fuel_history.observe(
                snapshot, ambiguous=pending.get("dispatch") in {"prepared", "ambiguous"})
        self._trace.emit("observation_validated", {"accepted": True, "capacity_evidence": evidence,
                                                   "fuel_depletion_estimates": fuel_evidence})
        return snapshot

    def _save(self) -> None:
        if self._persistence_failed:
            raise RuntimeError("Checkpoint persistence failed; reconstruct before continuing")
        self.memory._checkpoint_metrics = {}
        try:
            self._trace.call("checkpoint_written", lambda: self.memory.save(self.checkpoint),
                             details={"persisted": self.checkpoint is not None},
                             result=lambda _: {"checkpoint_io": deepcopy(self.memory._checkpoint_metrics)})
        except BaseException:
            self._persistence_failed = True
            self._trace.checkpoint_metrics(self._performance, self.memory._checkpoint_metrics,
                                           preserve_error=True)
            raise
        try:
            self._trace.checkpoint_metrics(self._performance, self.memory._checkpoint_metrics)
        except BaseException:
            self._persistence_failed = True
            raise

    def _clear_plan(self) -> None:
        attempt = self.memory.attempt
        background = getattr(self.memory, "background_attempt", None)
        if self.memory.active_plan is not None:
            self._async_close_current_plan_lineage(
                self.memory.active_plan, self._async_terminal_for_clear(self.memory.active_plan))
        if attempt is not None and (background is None or background["id"] != attempt["id"]):
            self._trace.release_attempt(attempt["id"])
        if self.memory.active_plan:
            self.memory.release(self.memory.active_plan["id"])
        self.memory.active_plan = None
        self.memory.pending = None
        self.memory.attempt = None
        self._attempt_clock = None
        self.memory.step_index = 0
        self._trace.clear_pending()

    def _fail_plan(self, reason: str) -> None:
        failed_plan = Plan.from_dict(self.memory.active_plan)
        key = failed_plan.id
        plan_dict = self.memory.active_plan
        terminal = self._async_failure_terminal(plan_dict, reason)
        self._async_close_current_plan_lineage(plan_dict, terminal)
        self.memory.failures[key] = self.memory.failures.get(key, 0) + 1
        failure_event = {"plan": key, "reason": reason, "tick": self.memory.last_tick}
        if self.async_decisions:
            failure_event["plan_sha256"] = fingerprint(plan_dict)
        self.memory.event("plan_failed", **failure_event)
        self._trace.emit("plan_failed", {"plan_id": key, "reason": reason})
        self.memory.reason = reason
        self._clear_plan()
        from .capital_controller import fail as fail_capital
        fail_capital(self, failed_plan)
        self._save()

    def _refresh_goals(self, snapshot: GameSnapshot) -> None:
        for key in self.order:
            if key not in self.memory.completed_goals and all(
                dep in self.memory.completed_goals for dep in GOALS[key].prerequisites
            ) and self._trace.call("goal_checked", lambda: completed(key, snapshot),
                                   details={"goal": key}, result=lambda value: {"completed": value}):
                self.memory.completed_goals[key] = snapshot.tick
                self.memory.event("goal_completed", goal=key, tick=snapshot.tick,
                                  world_kind=snapshot.world_kind)
                self._trace.emit("goal_completed", {"goal": key, "tick": snapshot.tick,
                                                    "verification_source": "existing_goal_predicate"})
        if self.target in self.memory.completed_goals:
            self.memory.status, self.memory.reason = "completed", "Verified target milestone"
            self._clear_plan()
            return
        goal = next(key for key in self.order if key not in self.memory.completed_goals)
        if self.memory.active_goal != goal:
            self._clear_plan()
            self.memory.active_goal = goal
            self.memory.event("goal_activated", goal=goal, tick=snapshot.tick)
            self._trace.emit("goal_activated", {"goal": goal})

    def _trace_decision(self) -> None:
        if self._trace.enabled:
            decision = self._decision
            self._trace.emit("decision", {"plan_id": decision.plan_id, "source": decision.source,
                                          "reason": decision.reason, "utilities": decision.utilities,
                                          "model_called": decision.model_called, "policy": self.policy,
                                          "confidence_floor": self.confidence_floor,
                                          "diagnostics": decision.diagnostics,
                                          "selection_support": getattr(self, "_selection_support", {})})

    @measured("record")
    def _record(self, before: GameSnapshot, action: str, outcome: str,
                after: GameSnapshot | None = None, verified: bool = False) -> dict:
        # Consumed by exactly this record, whatever happens below.
        compact, self._compact_next_record = self._compact_next_record, False
        self._save()
        if self._safety is not None:
            self._safety.publish(
                self.memory, after or before, process_id=self._process_id,
                provenance=self.provenance, provider=self.jev.state if isinstance(self.jev, ProviderCircuit) else None,
                verified=verified)
        with span("record_construct"):
            decision = self._decision
            async_result = (self._async_last_result
                            if self.async_decisions and decision is not None
                            and decision.model_called else None)
            record = {
                **self.provenance,
                "schema_version": 2, "controller": "hierarchical", "policy": self.policy,
                "tick": before.tick, "session_id": before.session_id,
                "world_kind": before.world_kind, "goal": self.memory.active_goal,
                "target": self.target, "status": self.memory.status, "reason": self.memory.reason,
                "action": action, "outcome": outcome, "verified": verified,
                "state": before.for_jev(), "after_state": (after or before).for_jev(),
                "completed_goals": dict(self.memory.completed_goals),
                "decision": asdict(decision) if decision else None,
                "model_call": decision is not None and decision.model_called,
                "requested_model": (async_result.get("requested_model") if async_result else
                                    getattr(self.jev, "model", None)),
                "resolved_model": (async_result.get("resolved_model") if async_result else
                                   getattr(self.jev, "last_model", None)
                                   if decision and decision.model_called else None),
                "usage": (async_result.get("usage") if async_result else
                          getattr(self.jev, "last_usage", None)
                          if decision and decision.model_called else None),
                "pending": deepcopy(self.memory.pending), "history": deepcopy(self.memory.history[-8:]),
                "process_id": self._process_id, "recorded_at_utc": utc_now(),
                "phases": deepcopy(self._phases), "attempt": deepcopy(self.memory.attempt),
                "performance": self._performance.snapshot(),
                "capacity_evidence": deepcopy(getattr(after or before, "_capacity_evidence", {})),
                "attempt_outcomes": deepcopy(self.memory.attempt_outcomes[-8:]),
                "planning_diagnostics": deepcopy(getattr(self, "_planning_diagnostics", {})),
                "failure_budgets": dict(self.memory.failures),
                "mining_outposts": bool(getattr(self, "_mining_outposts_enabled", False)),
            }
            if getattr(self, "factory_scheduling", "serial") != "serial":
                record["factory_scheduling"] = self.factory_scheduling
            fair = getattr(getattr(self, "backend", None), "_fair", None)
            metrics = getattr(fair, "metrics", None)
            if isinstance(metrics, dict):
                record["fair_action_metrics"] = dict(metrics)
            record.update(self._record_extras())
            if self.persist_recoverable_blocks:
                record["persistent_recovery"] = deepcopy(self._persistent_recovery_status)
            record["acceptance_configuration"] = {
                "factory_scheduling": getattr(self, "factory_scheduling", "serial"),
                **{name: record.get(name) is True for name in (
                    "background_work", "furnace_output_buffers", "furnace_input_belts",
                    "mining_outposts", "ore_side_successors")},
            }
            if getattr(self, "_solid_routes_enabled", False):
                record["acceptance_configuration"]["solid_routes"] = True
                record["acceptance_configuration"]["solid_science_policy"] = self._solid_science_policy
            if hasattr(self, '_coal_targets'):
                record['acceptance_configuration']['coal_supply'] = True
                record['acceptance_configuration']['coal_kit_policy'] = self._coal_kit_policy
                if getattr(self, '_coal_economic_admission', False):
                    record['acceptance_configuration']['coal_economic_admission'] = True
        previous = previous_timing(self)
        if previous is not None:
            record["previous_iteration_timing"] = previous
        if self.log_file:
            with span("legacy_encode"):
                logged = _json_safe(record)
                prepared = self._wait_record_encoder.prepare(
                    logged, wait=compact, anchor_candidate=True)
                if prepared.is_delta:
                    encoded_bytes = encode_wait_line(prepared)
                    encoded = encoded_bytes.decode("utf-8")
                else:
                    encoded = json.dumps(logged, allow_nan=False) + "\n"
                    encoded_bytes = encoded.encode("utf-8")
            with span("legacy_write"):
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with self.log_file.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded)
                self._wait_record_encoder.commit(prepared, len(encoded_bytes))
        with span("record_console"):
            print(f"[t={before.tick}] {self.memory.status}: {action} -> {outcome}", flush=True)
        return record

    def _model_history(self) -> list:
        if not self.persist_recoverable_blocks:
            return [event for event in self.memory.history
                    if not (isinstance(event.get("kind"), str)
                            and event["kind"] in {
                                "async_decision_settled", "async_plan_lineage"})][-8:]
        from .blocked_persistence import _SYSTEM_HISTORY_EVENTS
        from .paid_selection_reconciliation import validate_representation_budget_carry
        import subprocess
        result=[]
        for event in self.memory.history:
            kind = event.get("kind")
            if (isinstance(kind, str)
                    and (kind in _SYSTEM_HISTORY_EVENTS
                         or kind in {"async_decision_settled", "async_plan_lineage"})):
                continue
            if kind == "paid_duplicate_selection_reconciled":
                try:
                    validate_representation_budget_carry(self.memory,event,
                        archive_index=self._blocked_recovery_archive_index)
                except (ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError):
                    pass  # Invalid administrative claims remain ordinary history.
                else:
                    continue
            result.append(event)
        return result[-8:]

    def _model_facts(self, snapshot: GameSnapshot) -> dict:
        facts = snapshot.for_jev()
        # Diagnostic-only additions must not grow/change model prompts.
        facts.get("factory", {}).pop("acceptance_runtime", None)
        facts.get("factory", {}).pop("consumed", None)
        facts.get("factory", {}).pop("observation_snapshot_schema", None)
        facts.get("factory", {}).pop("observation_query_bounds", None)
        facts.get("factory", {}).pop("inventory_insertable_evidence", None)
        return facts

    def _record_extras(self) -> dict:
        if self.memory.capital_investment is not None:
            return {"capital_investment": deepcopy(self.memory.capital_investment)}
        return {}

    def _step_allowed(self, step, snapshot: GameSnapshot) -> bool:
        if (step.action == 'factory_connect' and snapshot.world_kind == 'fle'
                and self.memory.connector_ownership is None):
            return False
        if step.action == 'factory_connect' and self.memory.connector_ownership is not None:
            from .planning.connection_identity import connection_key
            if connection_key(step.parameters or {}) in self.memory.connector_ownership['routes']:
                return False  # One native receipt may never be billed again.
        parameters = step.parameters or {}
        role = parameters.get("role", "")
        if (self.factory_scheduling == "ready-work" and self.catalog is not None
                and step.action == "factory_place" and role.startswith("capacity:")):
            from .planning.capacity_evidence import expansion_ready
            recipe = role.removeprefix("capacity:").removesuffix(":2")
            if not expansion_ready(snapshot, self.catalog, "recipe:" + recipe):
                return False
        return step.allowed(snapshot)

    def _execution_barrier(self, snapshot: GameSnapshot) -> bool:
        if self._capital_fault:
            return True
        if (self.memory.status == 'uncertain'
                and self.memory.reason == 'Connector route needs exact reconciliation'):
            from .connector_checkpoint import shared_connector_handoff
            # All composed controllers consult this barrier before delegating
            # pending verification. _observe has freshly checked every cell;
            # only the normal verifier may finish the retained attempt.
            return not (snapshot.world_kind == 'fle'
                        and shared_connector_handoff(self.memory)
                        and snapshot.factory.get('connector_ownership', {}).get('active') is None
                        and Plan.from_dict(self.memory.active_plan).steps[0].satisfied(snapshot))
        return False

    def _absent_ambiguous_placement(self, plan: Plan, step, snapshot: GameSnapshot) -> bool:
        """Prove that retrying an ambiguous placement cannot duplicate a building."""
        pending = self.memory.pending or {}
        if pending.get("dispatch") != "ambiguous" or step.action != "factory_place":
            return False
        parameters = step.parameters or {}
        role, name = parameters.get("role"), parameters.get("name")
        entities = snapshot.factory.get("entities", {})
        counts = snapshot.factory.get("force_entity_counts")
        costs = step.costs or {}
        reserved = self.memory.reservations.get(plan.id)
        return bool(
            role and name and role not in entities
            and isinstance(counts, dict) and counts.get(name, 0) == 0
            and costs and reserved == costs
            and all(snapshot.inventory.get(item, 0) >= quantity
                    for item, quantity in costs.items())
        )

    def _absent_ambiguous_connection(self, plan: Plan, step, snapshot: GameSnapshot) -> bool:
        """Prove an ambiguous connection placed no connector before permitting a replan."""
        pending = self.memory.pending or {}
        if pending.get("dispatch") != "ambiguous" or step.action != "factory_connect":
            return False
        # A native route receipt, including a zero-payment active route, is a
        # retained transaction and cannot be dismissed by aggregate counts.
        from .planning.connection_identity import connection_key
        binding = self.memory.connector_ownership
        if binding is not None and connection_key(step.parameters or {}) in binding['routes']:
            return False
        parameters = step.parameters or {}
        source, target, kind = (
            parameters.get("source"), parameters.get("target"), parameters.get("kind")
        )
        entities = snapshot.factory.get("entities", {})
        counts = snapshot.factory.get("force_entity_counts")
        costs = step.costs or {}
        reserved = self.memory.reservations.get(plan.id)
        count = counts.get(kind, 0) if isinstance(counts, dict) else None
        retained = all(snapshot.inventory.get(item, 0) >= quantity
                       for item, quantity in costs.items())
        return bool(
            source in entities and target in entities and kind in {"pipe", "small-electric-pole"}
            and isinstance(counts, dict)
            and type(count) is int and count >= 0
            and set(costs) == {kind} and reserved == costs
            and retained and count == 0
        )

    def _partial_unacknowledged_gather(self, step, snapshot: GameSnapshot) -> bool:
        """Identify a partial native gather without treating it as step success.

        An inventory increase below the committed threshold is not a license to
        replay an unacknowledged harvest.  The resumed controller instead fails
        this plan and replans from the observed inventory.  This is deliberately
        narrower than normal inventory verification: it applies only after an
        prepared or ambiguous factory gather has already been observed at least
        once.
        """
        pending = self.memory.pending or {}
        parameters = step.parameters or {}
        item = parameters.get("resource")
        requested = parameters.get("quantity")
        observed = snapshot.inventory.get(item, 0) if isinstance(item, str) else 0
        return bool(
            pending.get("dispatch") in {"prepared", "ambiguous"}
            and type(pending.get("polls")) is int and pending["polls"] > 0
            and step.action == "factory_gather" and step.effect == "inventory"
            and item == step.item and item
            and type(requested) is int and requested > 0
            and isinstance(observed, (int, float)) and not isinstance(observed, bool)
            and math.isfinite(observed)
            and 0 <= step.threshold - requested < observed < step.threshold
        )

    def _unacknowledged_transfer_receipt(self, plan: Plan, step,
                                         snapshot: GameSnapshot) -> dict | None:
        """Return a durably evidenced incomplete native transfer, if and only if safe.

        A receipt is recorded before the Lua transfer endpoint reports a partial
        insertion as an error.  It proves that a bounded amount was paid, but it
        is deliberately not a successful step: the controller must fail this
        plan and replan from the observed world instead of retrying it.  This
        gate is limited to the original FLE attempt, its live machine identity,
        and its exact receipt. A prepared write-ahead row also qualifies when a
        durable transfer-RPC phase records that dispatch reached the native RPC
        boundary; the phase can remain started, returned, or failed if the
        process stops before the outer dispatcher changes the pending row. A
        zero receipt is admissible only when all source material reserved by
        the pending plan is still observed, proving that the failed transfer
        left no durable effect.
        """
        pending, attempt = self.memory.pending or {}, self.memory.attempt
        parameters = step.parameters or {}
        phases = attempt.get("dispatch_phases") if isinstance(attempt, dict) else None
        rpc_phase = phases.get("transfer_rpc") if isinstance(phases, dict) else None
        rpc_entered = (
            isinstance(rpc_phase, dict)
            and rpc_phase.get("stage") == "transfer_rpc"
            and rpc_phase.get("status") in {"started", "returned", "failed"}
        )
        dispatch = pending.get("dispatch")
        if not (
            snapshot.world_kind == "fle"
            and (dispatch == "ambiguous" or (dispatch == "prepared" and rpc_entered))
            and step.action in {"factory_insert", "factory_extract"}
            and step.effect == "transfer"
            and isinstance(attempt, dict)
            and attempt.get("origin") == "new"
            and attempt.get("action") == step.action
            and attempt.get("plan_id") == plan.id
            and attempt.get("step_index") == self.memory.step_index
            and attempt.get("receipt") == parameters.get("receipt")
            and snapshot.factory.get("player_bound") is True
        ):
            return None
        role, item, requested, receipt_key = (
            parameters.get("role"), parameters.get("item"),
            parameters.get("quantity"), parameters.get("receipt"),
        )
        entities, receipts = snapshot.factory.get("entities", {}), snapshot.factory.get("receipts", {})
        machine = entities.get(role, {}) if isinstance(entities, dict) else {}
        receipt = receipts.get(receipt_key) if isinstance(receipts, dict) else None
        quantity = receipt.get("quantity") if isinstance(receipt, dict) else None
        expected_unit = attempt.get("expected_unit_number")
        receipt_tick = receipt.get("tick") if isinstance(receipt, dict) else None
        if not (
            isinstance(role, str) and isinstance(item, str)
            and type(requested) is int and requested > 0
            and isinstance(receipt_key, str)
            and type(expected_unit) is int and expected_unit > 0
            and machine.get("unit_number") == expected_unit
            and isinstance(receipt, dict)
            and receipt.get("role") == role
            and receipt.get("item") == item
            and receipt.get("extracting") is (step.action == "factory_extract")
            and receipt.get("unit_number") == expected_unit
            and type(quantity) is int and 0 <= quantity < requested
            and type(receipt_tick) is int
            and attempt.get("started_tick", -1) <= receipt_tick <= snapshot.tick
        ):
            return None
        if quantity == 0:
            if step.action == "factory_insert":
                source_retained = (
                    step.costs == {item: requested}
                    and self.memory.reservations.get(plan.id) == step.costs
                    and snapshot.inventory.get(item, 0) >= requested
                )
            else:
                source_retained = (
                    not step.costs
                    and self.memory.reservations.get(plan.id) == {}
                    and machine.get("output", {}).get(item, 0) >= requested
                )
            if not source_retained:
                return None
        return {"quantity": quantity, "receipt_tick": receipt_tick}

    def _prepared_transfer_never_entered_rpc(self, plan: Plan, step,
                                             snapshot: GameSnapshot) -> bool:
        """Authorize one retained transfer only when its native RPC never began.

        A write-ahead ``prepared`` action normally remains potentially mutating:
        a missing acknowledgement is not evidence that a transfer did not take
        effect.  Native transfers are a narrowly different case.  Their durable
        substage record is written before the transfer RPC, and the Lua endpoint
        records its exact receipt before it returns.  If the original actor and
        machine identity remain live, the source still contains the reservation,
        and the transfer-RPC substage never began, re-entering the *same* pending
        action cannot duplicate a prior transfer.

        This intentionally does not cover ambiguous/returned actions, failed
        approaches, legacy attempts, missing receipts, changed machines, or
        ordinary mock commands.  Those states stay fail-closed for manual
        reconciliation.
        """
        pending, attempt = self.memory.pending or {}, self.memory.attempt
        parameters = step.parameters or {}
        if not (
            snapshot.world_kind == "fle"
            and pending.get("dispatch") == "prepared"
            and step.action in {"factory_insert", "factory_extract"}
            and step.effect == "transfer"
            and isinstance(attempt, dict)
            and attempt.get("origin") == "new"
            and attempt.get("action") == step.action
            and attempt.get("receipt") == parameters.get("receipt")
            and attempt.get("observation_error") is None
            and snapshot.factory.get("player_bound") is True
        ):
            return False
        role, item, quantity, receipt = (
            parameters.get("role"), parameters.get("item"),
            parameters.get("quantity"), parameters.get("receipt"),
        )
        machine = snapshot.factory.get("entities", {}).get(role, {})
        stages = attempt.get("dispatch_phases")
        if not (
            isinstance(role, str) and isinstance(item, str)
            and type(quantity) is int and quantity > 0
            and isinstance(receipt, str)
            and isinstance(stages, dict)
            and stages.get("dispatch", {}).get("status") == "started"
            and stages.get("approach", {}).get("status") in {"started", "returned"}
            and "transfer_rpc" not in stages
            and type(attempt.get("expected_unit_number")) is int
            and machine.get("unit_number") == attempt["expected_unit_number"]
            and receipt not in snapshot.factory.get("receipts", {})
            and self._step_allowed(step, snapshot)
        ):
            return False
        if step.action == "factory_insert":
            return (
                step.costs == {item: quantity}
                and self.memory.reservations.get(plan.id) == step.costs
                and snapshot.inventory.get(item, 0) >= quantity
            )
        return (
            not step.costs
            and self.memory.reservations.get(plan.id) == {}
            and machine.get("output", {}).get(item, 0) >= quantity
        )

    def _transfer_preflight_context(self, snapshot: GameSnapshot, plan: Plan,
                                    step, index: int) -> dict | None:
        """Build opt-in identity for the bundled Lua's narrow read-only transfer check."""
        if (snapshot.world_kind != "fle"
                or step.action not in {"factory_insert", "factory_extract"}
                or not isinstance(self.memory.attempt, dict)
                or self.memory.attempt.get("origin") != "new"):
            return None
        parameters = step.parameters or {}
        role, item, quantity, receipt = (parameters.get("role"), parameters.get("item"),
                                         parameters.get("quantity"), parameters.get("receipt"))
        from .bootstrap_output import ROLE as BOOTSTRAP_ROLE
        if role == BOOTSTRAP_ROLE:
            return None
        runtime = snapshot.factory.get("acceptance_runtime")
        machine = snapshot.factory.get("entities", {}).get(role, {})
        attempt = self.memory.attempt
        if (type(runtime) is not dict or type(runtime.get("schema")) is not int
                or runtime.get("schema") != 1
                or runtime.get("session_id") != snapshot.session_id
                or type(runtime.get("actor_unit")) is not int or runtime["actor_unit"] < 1
                or type(runtime.get("player_index")) is not int or runtime["player_index"] < 1
                or type(runtime.get("surface_index")) is not int or runtime["surface_index"] < 1
                or type(runtime.get("force_index")) is not int or runtime["force_index"] < 1
                or snapshot.factory.get("player_bound") is not True
                or not isinstance(machine, dict)
                or type(machine.get("unit_number")) is not int
                or machine["unit_number"] < 1
                or machine["unit_number"] != attempt.get("expected_unit_number")
                or type(machine.get("name")) is not str or not machine["name"]
                or attempt.get("action") != step.action
                or attempt.get("plan_id") != plan.id
                or attempt.get("step_index") != index
                or attempt.get("step_sha256") != fingerprint(asdict(step))
                or attempt.get("receipt") != receipt
                or attempt.get("started_tick") > snapshot.tick
                or snapshot.session_id != self.memory.session_id
                or not isinstance(role, str) or not role
                or not isinstance(item, str) or not item
                or type(quantity) is not int or quantity < 1
                or not isinstance(receipt, str) or not receipt):
            return None
        context = {
            "schema": 1, "attempt_id": attempt["id"], "session_id": snapshot.session_id,
            "plan_id": plan.id, "step_index": index, "step_sha256": attempt["step_sha256"],
            "action": step.action, "started_tick": attempt["started_tick"],
            "observed_tick": snapshot.tick, "receipt": receipt, "item": item,
            "quantity": quantity, "direction": "extract" if step.action == "factory_extract" else "insert",
            "role": role, "machine_unit_number": machine["unit_number"],
            "machine_name": machine["name"], "actor_unit_number": runtime["actor_unit"],
            "actor_player_index": runtime["player_index"],
            "surface_index": runtime["surface_index"], "force_index": runtime["force_index"],
        }
        from .telemetry import validate_transfer_preflight_context
        validate_transfer_preflight_context(context)
        return context

    @staticmethod
    def _validate_transfer_preflight_binding(proof: dict, attempt: dict, plan: Plan,
                                             step, index: int, session_id: str) -> None:
        from .telemetry import validate_transfer_preflight_proof

        validate_transfer_preflight_proof(proof)
        context = proof["request"]
        parameters = step.parameters or {}
        if (context["session_id"] != session_id
                or context["attempt_id"] != attempt.get("id")
                or context["plan_id"] != plan.id
                or context["step_index"] != index
                or context["step_sha256"] != fingerprint(asdict(step))
                or context["action"] != step.action
                or context["started_tick"] != attempt.get("started_tick")
                or context["receipt"] != parameters.get("receipt")
                or context["item"] != parameters.get("item")
                or context["quantity"] != parameters.get("quantity")
                or context["role"] != parameters.get("role")
                or context["machine_unit_number"] != attempt.get("expected_unit_number")
                or proof["tick"] < context["observed_tick"]):
            raise ValueError("Transfer preflight proof does not bind the active operation")

    @staticmethod
    def _transfer_preflight_owner_matches(snapshot: GameSnapshot, proof: dict,
                                          memory: CampaignMemory) -> bool:
        context = proof["request"]
        runtime = snapshot.factory.get("acceptance_runtime")
        machine = snapshot.factory.get("entities", {}).get(context["role"], {})
        if (snapshot.world_kind != "fle" or snapshot.session_id != memory.session_id
                or snapshot.tick < proof["tick"] or snapshot.factory.get("player_bound") is not True
                or type(runtime) is not dict
                or type(runtime.get("schema")) is not int or runtime.get("schema") != 1
                or runtime.get("session_id") != context["session_id"]
                or type(runtime.get("actor_unit")) is not int
                or runtime.get("actor_unit") != context["actor_unit_number"]
                or type(runtime.get("player_index")) is not int
                or runtime.get("player_index") != context["actor_player_index"]
                or type(runtime.get("surface_index")) is not int
                or runtime.get("surface_index") != context["surface_index"]
                or type(runtime.get("force_index")) is not int
                or runtime.get("force_index") != context["force_index"]
                or type(machine) is not dict
                or machine.get("unit_number") != context["machine_unit_number"]
                or machine.get("name") != context["machine_name"]):
            return False
        if context["receipt"] in snapshot.factory.get("receipts", {}):
            return False
        source_quantity = (snapshot.inventory.get(context["item"], 0)
                           if context["direction"] == "insert"
                           else machine.get("output", {}).get(context["item"], 0))
        # A changed observed source is outside the durable preflight's exact
        # identity. Keep the pending operation for reconciliation; never replay.
        expected_source_quantity = proof["source"]["quantity"]
        return (type(source_quantity) is int and source_quantity == expected_source_quantity)

    def _settle_transfer_preflight_rejection(self, snapshot: GameSnapshot, plan: Plan,
                                             proof: dict) -> dict:
        attempt = self.memory.attempt
        if attempt is None:
            raise ValueError("Cannot settle transfer preflight without its attempt")
        reason = (f"Native {proof['request']['item']} transfer to {proof['request']['role']} "
                  f"was rejected before mutation: destination capacity "
                  f"{proof['target']['insertable_count']} is below requested "
                  f"{proof['request']['quantity']}")
        self.memory.last_tick = max(self.memory.last_tick, proof["tick"])
        self.memory.event(
            "transfer_preflight_rejected", plan_id=plan.id, step_index=self.memory.step_index,
            attempt_id=attempt["id"], receipt=proof["request"]["receipt"], proof=deepcopy(proof),
            mutation_started=False, tick=proof["tick"],
        )
        finished_snapshot = deepcopy(snapshot)
        finished_snapshot.tick = proof["tick"]
        self._finish_attempt(finished_snapshot, "transfer_preflight_rejected")
        self.memory.status = "running"
        self._fail_plan(reason)
        return self._record(snapshot, "reconcile", reason)

    def _dispatch_retained_transfer(self, plan: Plan, step, snapshot: GameSnapshot) -> dict:
        """Dispatch the exact preserved native transfer, then verify its receipt."""
        # Preserve the interrupted-phase proof before the normal dispatch tracing
        # records the resumed call under the same bounded phase names.
        self.memory.event(
            "prepared_transfer_recovery_authorized",
            attempt_id=self.memory.attempt["id"], plan=plan.id,
            step_index=self.memory.step_index, receipt=step.parameters["receipt"],
            expected_unit_number=self.memory.attempt["expected_unit_number"],
            original_dispatch_phases=deepcopy(self.memory.attempt["dispatch_phases"]),
            tick=snapshot.tick,
        )
        self._save()
        self._trace.emit("prepared_transfer_recovery_authorized", {
            **self._trace.pending_ref(plan.id, self.memory.step_index, self.memory.pending,
                                      attempt_id=self.memory.attempt["id"]),
            "receipt": step.parameters["receipt"],
            "expected_unit_number": self.memory.attempt["expected_unit_number"],
            "reason": "transfer_rpc_not_entered_with_retained_source",
        })
        try:
            with phase("dispatch", self._diagnostic_trace):
                outcome = self._trace.dispatch(
                    lambda: (
                        self.backend.execute_traced(step.action, step.parameters or {},
                                                    self._diagnostic_trace)
                        if getattr(self.backend, "execute_traced", None)
                        else self.backend.execute(step.action, step.parameters or {})
                    ),
                    step.action, parameters=step.parameters, plan_id=plan.id,
                    step_index=self.memory.step_index, pending=self.memory.pending,
                    checkpointed=self.checkpoint is not None, attempt_id=self.memory.attempt["id"],
                )
        except ResearchLogError:
            raise
        except (MaintenanceAdmissionClosed, StoragePressure):
            return self._record(snapshot, "observe", "Retained transfer admission closed", snapshot)
        except Exception as error:
            if self._persistence_failed:
                raise  # Preserve the primary checkpoint failure and durable pending.
            self.memory.pending["dispatch"] = "ambiguous"
            self.memory.event("recovery_dispatch_error", error_type=error_code(error),
                              tick=snapshot.tick)
            return self._record(snapshot, step.action,
                                "Retained transfer dispatch remained ambiguous", snapshot)
        self.memory.pending["dispatch"] = "returned"
        self._save()
        self._trace.observation_phase = "post_recovery_dispatch"
        after = self._observe("post_dispatch_observe")
        with phase("verification", self._diagnostic_trace):
            verified = self._trace.verify(
                step, after, plan_id=plan.id, index=self.memory.step_index,
                pending=self.memory.pending, phase="post_recovery_dispatch",
                attempt_id=self.memory.attempt["id"],
            )
        if verified:
            self._finish_attempt(after)
            self.memory.status, self.memory.reason = "running", ""
            self.memory.release(plan.id)
            self.memory.pending = None
            self._trace.clear_pending()
            self.memory.step_index += 1
            self.memory.stalled_decisions = 0
            self.memory.event("step_verified", plan=plan.id, action=step.action,
                              tick=after.tick)
            if self.memory.step_index == len(plan.steps):
                self._clear_plan()
            self._refresh_goals(after)
        return self._record(snapshot, step.action,
                            "Re-dispatched retained transfer after proving its RPC never began",
                            after, verified)

    def _verify_pending(self, snapshot: GameSnapshot) -> dict:
        if self._execution_barrier(snapshot):
            return self._record(snapshot, "observe", self.memory.reason)
        plan = Plan.from_dict(self.memory.active_plan)
        step = plan.steps[self.memory.step_index]
        pending = self.memory.pending
        preflight_event = ((self.memory.attempt or {}).get("dispatch_phases") or {}).get("transfer_rpc")
        if isinstance(preflight_event, dict) and preflight_event.get("error_code") == "transfer_preflight_rejected":
            proof = preflight_event.get("proof")
            try:
                self._validate_transfer_preflight_binding(
                    proof, self.memory.attempt, plan, step, self.memory.step_index,
                    self.memory.session_id)
                owner_matches = (
                    self.memory.transfer_recovery is None
                    and preflight_event.get("status") == "failed"
                    and self._transfer_preflight_owner_matches(snapshot, proof, self.memory)
                )
            except (TypeError, ValueError, KeyError, AttributeError):
                owner_matches = False
            if owner_matches:
                return self._settle_transfer_preflight_rejection(snapshot, plan, proof)
            self.memory.status = "uncertain"
            self.memory.reason = (
                "Transfer preflight proof no longer matches the retained actor, source, or receipt; "
                "pending action retained without replay"
            )
            return self._record(snapshot, "observe", self.memory.reason)
        if step.action == 'factory_connect' and snapshot.world_kind == 'fle':
            from .connector_checkpoint import pending_owned
            if not pending_owned(self.memory, step):
                from .planning.connection_identity import connection_key
                binding = self.memory.connector_ownership
                row = (binding or {}).get('routes', {}).get(connection_key(step.parameters or {}))
                if row is not None:
                    self.memory.status, self.memory.reason = (
                        'uncertain', 'Connector route needs exact reconciliation')
                    return self._record(snapshot, 'observe', self.memory.reason)
        if self.memory.transfer_recovery is not None:
            from .transfer_recovery import check_recovery
            evidence = check_recovery(self.memory, snapshot)
            if evidence is None:
                self.memory.status = "uncertain"
                self.memory.reason = "Transfer recovery evidence differs; pending action retained"
                return self._record(snapshot, "observe", self.memory.reason)
            reason = "Proved no durable transfer effect; reject this plan and replan without replay"
            self.memory.event("rejected_transfer_reconciled", **evidence, tick=snapshot.tick)
            self._finish_attempt(snapshot, "rejected_transfer_reconciled")
            self.memory.transfer_recovery = None
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        with phase("verification", self._diagnostic_trace):
            verified = self._trace.verify(step, snapshot, plan_id=plan.id,
                                          index=self.memory.step_index, pending=pending,
                                          phase="pending_poll", attempt_id=self.memory.attempt["id"])
        if step.action == 'factory_connect' and snapshot.world_kind == 'fle':
            from .connector_checkpoint import pending_owned
            verified = verified and pending_owned(self.memory, step)
        if verified:
            self._finish_attempt(snapshot)
            self.memory.status, self.memory.reason = "running", ""
            self.memory.release(plan.id)
            self.memory.pending = None
            self._trace.clear_pending()
            self.memory.step_index += 1
            self.memory.stalled_decisions = 0
            self.memory.event("step_verified", plan=plan.id, action=step.action, tick=snapshot.tick)
            if self.memory.step_index == len(plan.steps):
                self._clear_plan()
            self._refresh_goals(snapshot)
            return self._record(snapshot, "verify", "Observed expected postcondition", verified=True)
        incomplete_transfer = self._unacknowledged_transfer_receipt(plan, step, snapshot)
        if incomplete_transfer is not None:
            quantity = incomplete_transfer["quantity"]
            if quantity == 0:
                reason = (
                    f"Observed zero of requested {step.parameters['quantity']} "
                    f"{step.parameters['item']} in the exact native transfer receipt and "
                    "all reserved source material retained; fail this plan and replan "
                    "without replaying the ambiguous dispatch"
                )
                outcome = event_kind = "zero_effect_transfer_reconciled"
            else:
                reason = (
                    f"Observed {quantity} of requested {step.parameters['quantity']} "
                    f"{step.parameters['item']} in the exact native transfer receipt; "
                    "fail this plan and replan without replaying the ambiguous dispatch"
                )
                outcome = event_kind = "partial_transfer_reconciled"
            self.memory.event(
                event_kind,
                plan=plan.id, step_index=self.memory.step_index,
                receipt=step.parameters["receipt"], requested_quantity=step.parameters["quantity"],
                transferred_quantity=quantity, receipt_tick=incomplete_transfer["receipt_tick"],
                attempt_id=self.memory.attempt["id"], tick=snapshot.tick,
            )
            self._finish_attempt(snapshot, outcome)
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        if (not (self._safety and self._safety.maintenance(self.memory, snapshot))
                and self._prepared_transfer_never_entered_rpc(plan, step, snapshot)):
            return self._dispatch_retained_transfer(plan, step, snapshot)
        if self._absent_ambiguous_placement(plan, step, snapshot):
            name = step.parameters["name"]
            reason = (f"Observed no durable {name} placement and retained all reserved "
                      "materials; replan without replaying the ambiguous dispatch")
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        if self._absent_ambiguous_connection(plan, step, snapshot):
            kind = step.parameters["kind"]
            reason = (f"Observed no durable {kind} construction and retained all reserved "
                      "materials; replan without replaying the ambiguous dispatch")
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        if self._partial_unacknowledged_gather(step, snapshot):
            quantity = snapshot.inventory[step.item]
            self.memory.event(
                "gather_partial_progress", session_id=snapshot.session_id,
                plan=plan.to_dict(), step_index=self.memory.step_index,
                observed_inventory=quantity, tick=snapshot.tick,
                started_tick=pending["started_tick"], attempt_id=self.memory.attempt["id"],
            )
            reason = (
                f"Observed {quantity} {step.item} below committed inventory threshold "
                f"{step.threshold} after an unacknowledged native gather; replan without "
                "replaying the ambiguous dispatch"
            )
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        boiler = snapshot.factory.get("entities", {}).get("utility:boiler", {})
        if step.action == "factory_wait" and boiler and boiler.get("fuel", {}).get("coal", 0) < 5:
            self._finish_attempt(snapshot, "wait_replanned")
            details = {"reason": "boiler fuel", "tick": snapshot.tick}
            if getattr(self, "async_decisions", False):
                details.update(plan=plan.id, plan_sha256=fingerprint(self.memory.active_plan),
                               attempt_id=self.memory.attempt_outcomes[-1]["id"])
            self.memory.event("maintenance_required", **details)
            self._clear_plan()
            return self._record(snapshot, "observe", "Replan a nonmutating wait to replenish boiler fuel")
        if (self.factory_scheduling == "ready-work" and self.catalog is not None
                and self.memory.status == "running" and pending.get("dispatch") == "returned"
                and step.action == "factory_wait" and step.effect == "machine_output"):
            candidates, _ = self._work_candidates(snapshot)
            ready = [candidate for candidate in candidates
                     if self._plan_failure_count(candidate) < 2
                     and candidate.steps[0].action != "factory_wait"
                     and candidate.steps[0].allowed(snapshot)
                     and not candidate.steps[0].satisfied(snapshot)]
            if ready:
                self._finish_attempt(snapshot, "wait_replanned")
                details = {"plan": plan.id,
                           "candidates": [candidate.id for candidate in ready],
                           "tick": snapshot.tick}
                if getattr(self, "async_decisions", False):
                    details.update(plan_sha256=fingerprint(self.memory.active_plan),
                                   attempt_id=self.memory.attempt_outcomes[-1]["id"])
                self.memory.event("passive_wait_yielded", **details)
                self._clear_plan()
                return self._record(snapshot, "observe", "Yield passive machine wait to ready work")
        pending["polls"] += 1
        expired = (snapshot.tick - pending["started_tick"] >= step.timeout_ticks
                   or pending["polls"] >= self.max_pending_polls)
        if expired:
            if self._trace.enabled:
                self._trace.emit("pending_expired", {
                    **self._trace.pending_ref(plan.id, self.memory.step_index, pending,
                                              attempt_id=self.memory.attempt["id"]),
                    "polls": pending["polls"], "timeout_ticks": step.timeout_ticks})
            if step.action in {"idle", "factory_wait"}:
                if self.async_decisions:
                    self.memory.event(
                        "async_wait_expired", plan_id=plan.id,
                        plan_sha256=fingerprint(self.memory.active_plan),
                        step_index=self.memory.step_index,
                        attempt_id=self.memory.attempt["id"], tick=snapshot.tick,
                        started_tick=pending["started_tick"], polls=pending["polls"],
                        timeout_ticks=step.timeout_ticks,
                    )
                self._finish_attempt(snapshot, "wait_expired")
                self._fail_plan("Production made no verified progress within the observation budget")
                return self._record(snapshot, "observe", self.memory.reason)
            # Execution may have partially mutated the game. Do not automatically
            # replay a non-idempotent command after lost acknowledgement.
            self.memory.status = "uncertain"
            self.memory.reason = "Unverified action outcome; inspect/reconcile before another mutation"
            return self._record(snapshot, "observe", self.memory.reason)
        # In a real backend observation allows game time to elapse naturally.
        # The explicitly synthetic backend advances only when given idle.
        if snapshot.world_kind == "mock":
            self._trace.dispatch(lambda: self.backend.act("idle"), "idle",
                                 plan_id=plan.id, step_index=self.memory.step_index,
                                 role="mock_clock_advance")
        return self._record(snapshot, "observe", "Waiting for the in-flight postcondition")

    def _gather_remainder_plan(self, plan: Plan, snapshot: GameSnapshot) -> Plan:
        if len(plan.steps) != 1 or plan.steps[0].action != "factory_gather":
            return plan
        step = plan.steps[0]
        requested = (step.parameters or {}).get("quantity")
        observed = snapshot.inventory.get(step.item)
        if (type(requested) is not int or requested <= 0
                or type(observed) is not int or observed < 0
                or requested != step.threshold - observed):
            return plan
        for receipt in reversed(self.memory.history):
            if receipt.get("kind") != "gather_partial_progress":
                continue
            try:
                original = Plan.from_dict(receipt["plan"])
                previous = original.steps[0]
                quantity = (previous.parameters or {}).get("quantity")
                valid = (
                    receipt.get("session_id") == snapshot.session_id == self.memory.session_id
                    and len(original.steps) == 1 and type(receipt.get("step_index")) is int
                    and receipt["step_index"] == 0
                    and type(receipt.get("tick")) is int
                    and type(receipt.get("started_tick")) is int
                    and 0 <= receipt["started_tick"] <= receipt["tick"] <= snapshot.tick
                    and isinstance(receipt.get("attempt_id"), str) and bool(receipt["attempt_id"])
                    and type(receipt.get("observed_inventory")) is int
                    and receipt["observed_inventory"] == observed
                    and original.goal == plan.goal
                    and previous.action == step.action and previous.effect == step.effect == "inventory"
                    and previous.item == step.item
                    and previous.threshold == step.threshold
                    and type(quantity) is int and requested < quantity
                    and 0 <= previous.threshold - quantity < observed < previous.threshold
                    and (previous.parameters or {}).get("resource") == step.item
                    and original.id in {plan.id, f"{plan.id}:remainder-quantity:{quantity}"}
                )
            except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                continue
            if valid:
                return replace(plan, id=f"{plan.id}:remainder-quantity:{requested}")
        return plan

    def _plan_failure_count(self, plan: Plan) -> int:
        from .planning.connection_identity import connection_failures
        from .planning.fuel_failure_budget import acquisition_failures
        return max(connection_failures(plan.id, self.memory.failures,
                                       self.memory.connection_failure_attribution),
                   acquisition_failures(plan, self.memory.failures))

    def _compile_candidates(self, snapshot: GameSnapshot) -> tuple[list[Plan], str]:
        # Decision-local context only; never a second persistent ownership ledger.
        snapshot._planner_failure_budgets = dict(self.memory.failures)
        if self.catalog is not None and self.memory.active_goal in {
            "rocket_launch", "iron_smelting", "steam_power", "automation_science", "bootstrap_mining"
        }:
            if self.factory_scheduling == "ready-work":
                from .planning.ready_work import ReadyWorkPlanner, compile_ready_factory

                kind = getattr(self, "planner_type", ReadyWorkPlanner)
                if kind is ReadyWorkPlanner:
                    plans, blocker = compile_ready_factory(self.memory.active_goal, snapshot, self.catalog)
                else:
                    plans, blocker = compile_ready_factory(
                        self.memory.active_goal, snapshot, self.catalog, planner_type=kind)
            else:
                from .planning.factory import compile_factory

                plans, blocker = compile_factory(self.memory.active_goal, snapshot, self.catalog)
        else:
            plans, blocker = compile_plans(self.memory.active_goal, snapshot)
        return [self._gather_remainder_plan(plan, snapshot) for plan in plans], blocker

    def _work_candidates(self, snapshot: GameSnapshot) -> tuple[list[Plan], str]:
        from .capital_controller import frontier
        return frontier(self, snapshot)

    def _investment_step_allowed(self, plan, step, snapshot) -> bool:
        from .planning.capital import costs_allowed
        return costs_allowed(replace(plan, steps=(step,)), snapshot,
                             self.memory.capital_investment, self.catalog)

    def _fallback_plan(self, plans: list[Plan]) -> Plan:
        if self.factory_scheduling == "ready-work" and self.catalog is not None:
            evidence = getattr(self, "_selection_support", {})
            ranking = evidence.get("deterministic_ranking", [])
            by_id = {plan.id: plan for plan in plans}
            return next((by_id[key] for key in ranking if key in by_id), plans[0])
        return min(plans, key=lambda plan: (len(plan.steps), plan.id))

    @_serialized_async_actor
    @traced_step
    def step(self) -> dict:
        self._persistent_recovery_status = None
        self._decision = None
        self._async_last_result = None
        self._async_last_outcome = None
        self._selection_support = {}
        self._planning_diagnostics = {}
        self._trace.observation_phase = "before_decision"
        self._phases = []
        from .performance import PerformanceCounters
        self._performance = PerformanceCounters()
        self._trace.metrics = self._performance if self.factory_scheduling == "ready-work" else None
        snapshot = self._observe()
        if self.async_decisions:
            self._async_reconcile_terminal_pointer()
        blocked_reevaluation = self._reevaluate_blocked_once
        blocked_reevaluation_reason = self.memory.reason if blocked_reevaluation else None
        persistent_blocked = self._persistent_block_active()
        admission_checked = False
        if blocked_reevaluation:
            from .blocked_reevaluation import validate_blocked_memory
            validate_blocked_memory(self.memory, self.max_stalled_decisions)
            if snapshot.world_kind != "fle" or self.policy != "jev" or getattr(self.jev, "is_mock", False):
                raise ValueError("Blocked decision re-evaluation is limited to live Jev-controlled FLE")
            if self._safety:
                held = self._safety.admission(self.memory, snapshot)
                if held:
                    return self._record(snapshot, "observe", held)
                admission_checked = True
            if isinstance(self.jev, ProviderCircuit) and self.jev.state["phase"] != "healthy":
                if persistent_blocked:
                    self._persistent_recovery_status = {
                        "phase": "provider_blocked", "reason": self.memory.reason,
                        "model_call": False,
                    }
                return self._record(snapshot, "observe", "Provider circuit is not healthy; re-evaluation not consumed")
            if not self.persist_recoverable_blocks:
                self._consume_blocked_reevaluation(snapshot)
        if self.memory.status == "uncertain" and self.memory.pending:
            return self._verify_pending(snapshot)
        if self.terminal and not blocked_reevaluation:
            return self._record(snapshot, "observe", self.memory.reason)
        # Resolve in-flight work before processing model requests or goal changes.
        if self.memory.pending:
            return self._verify_pending(snapshot)
        if self._safety and not admission_checked:
            held = self._safety.admission(self.memory, snapshot)
            if held:
                return self._record(snapshot, "observe", held)
        self._refresh_goals(snapshot)
        if self.terminal and not (
            blocked_reevaluation and self.memory.status == "blocked"
            and self.memory.reason == blocked_reevaluation_reason
        ):
            return self._record(snapshot, "observe", self.memory.reason, verified=True)
        if self.memory.active_plan is None:
            # The checkpoint intentionally remains blocked until useful work
            # verifies. Its admitted source reevaluation must nevertheless use
            # the same capital intent, deadline and cost filters as normal work.
            self._source_reevaluation_planning = blocked_reevaluation
            try:
                with phase("planning", self._diagnostic_trace):
                    plans, blocker = self._trace.call(
                        "candidate_set_created", lambda: self._work_candidates(snapshot),
                        result=lambda value: {"plans": [plan.to_dict() for plan in value[0]],
                                              "blocker": value[1]})
            finally:
                self._source_reevaluation_planning = False
            generated = list(plans)
            budget_counts = {p.id: self._plan_failure_count(p) for p in generated}
            rejected = [{"plan_id": p.id, "reason": "plan_failure_budget",
                         "failures": budget_counts[p.id]}
                        for p in generated if budget_counts[p.id] >= 2]
            plans = [p for p in generated if budget_counts[p.id] < 2]
            # This boundary is after capability/capital compilation, not a claim
            # that every Lua survey or earlier eligibility rejection was retained.
            self._planning_diagnostics = {
                "schema": 1, "observed_tick": snapshot.tick,
                "boundary": "post_capability_frontier",
                "generated_plan_ids": [p.id for p in generated],
                "eligible_plan_ids": [p.id for p in plans],
                "failure_budget_rejections": rejected,
                "duplicate_plan_ids": [], "ranked_plan_ids": [],
                "deferred_plan_ids": [], "defer_reason": None,
            }
            if self._trace.enabled:
                self._trace.emit("candidate_set_filtered", {
                    **deepcopy(self._planning_diagnostics), "filter": "existing_plan_failure_budget"})
            if not plans:
                frontier_reason = blocker or "Plan failure budget exhausted"
                if blocked_reevaluation and self.persist_recoverable_blocks:
                    from .blocked_persistence import planner_input_sha256
                    input_sha256 = planner_input_sha256(
                        snapshot, [plan.to_dict() for plan in plans], frontier_reason,
                        source_revision=self.provenance["code_revision"], target=self.target)
                    self._record_persistent_attempt(
                        snapshot, input_sha256, source_authorized=True,
                        authorization_reason=blocked_reevaluation_reason, outcome="frontier")
                    blocked_reevaluation = False
                self.memory.status, self.memory.reason = "blocked", blocker or "Plan failure budget exhausted"
                if self._persistent_block_active():
                    from .blocked_persistence import planner_input_sha256, was_attempted
                    input_sha256 = planner_input_sha256(
                        snapshot, [plan.to_dict() for plan in plans], blocker or self.memory.reason,
                        source_revision=self.provenance["code_revision"], target=self.target)
                    if was_attempted(self.memory, self.provenance["code_revision"], input_sha256,
                                     archive_index=self._blocked_recovery_archive_index):
                        return self._persistent_wait(snapshot, input_sha256)
                    return self._blocked_frontier_wait(
                        snapshot, input_sha256, source_authorized=blocked_reevaluation,
                        authorization_reason=blocked_reevaluation_reason)
                return self._record(snapshot, "observe", self.memory.reason)
            if self.factory_scheduling == "ready-work" and self.catalog is not None:
                from .planning.decision_support import distinct_candidates, scheduling_context

                retained = distinct_candidates(plans)
                self._planning_diagnostics["duplicate_plan_ids"] = [
                    p.id for p in plans if all(p is not kept for kept in retained)]
                plans = retained
                self._selection_support = scheduling_context(
                    snapshot, self.catalog, plans, self.memory.active_goal)
                provider_ready = (not isinstance(self.jev, ProviderCircuit)
                                  or self.jev.state["phase"] == "healthy")
                if self.policy == 'jev' and provider_ready:
                    from .planning.decision_support import defer_gather_until_bill_craft
                    deferred, reason = defer_gather_until_bill_craft(
                        plans, self._selection_support, snapshot, self.memory)
                    if reason is not None:
                        self._planning_diagnostics['deferred_plan_ids'] = [
                            p.id for p in plans if p not in deferred]
                        self._planning_diagnostics['defer_reason'] = reason
                        plans = deferred
                        self._selection_support = scheduling_context(
                            snapshot, self.catalog, plans, self.memory.active_goal)
                        if self._trace.enabled:
                            self._trace.emit('candidate_set_filtered', {
                                **deepcopy(self._planning_diagnostics),
                                'filter': 'bill_craft_before_independent_raw_gather'})
                by_id = {plan.id: plan for plan in plans}
                plans = [by_id[key] for key in self._selection_support["deterministic_ranking"]]
                self._planning_diagnostics["ranked_plan_ids"] = [p.id for p in plans]
                self._planning_diagnostics["candidate_evidence"] = deepcopy(
                    self._selection_support["candidate_evidence"])
            if getattr(self, "_solid_science_policy", False):
                # Capture the exact executable frontier independently of the
                # later selected-plan event. This diagnostic is never a prompt
                # field or an authorization gate and adds no observation/save.
                self._planning_diagnostics["candidate_frontier"] = [
                    {"id": plan.id, "sha256": fingerprint(plan.to_dict())} for plan in plans]
            provider_ready = not isinstance(self.jev, ProviderCircuit) or self.jev.state["phase"] == "healthy"
            singleton = bool(self._selection_support and len(plans) == 1
                             and self.policy == "hybrid" and provider_ready)
            persistent_input_sha256 = None
            persistent_attempt_finalized = False
            persistent_trace_done = False
            alternative_wait = None
            alternative_status = None
            if self.policy == "deterministic" or singleton:
                with phase("selection", self._diagnostic_trace):
                    chosen = self._fallback_plan(plans)
                self._decision = Decision(
                    chosen.id, "deterministic-singleton" if singleton else "deterministic",
                    "Only one distinct feasible continuation" if singleton else "",
                    state=self._selection_support,
                    diagnostics={"schema": 1, "outcome": "singleton" if singleton else "deterministic",
                                 "model_skipped": True})
                self._trace_decision()
            else:
                facts = self._model_facts(snapshot)
                if self.catalog is not None and any(
                        isinstance((plan.materials or {}).get('bootstrap_output_pickup'), dict)
                        or isinstance((self._selection_support.get('candidate_evidence') or {})
                            .get(plan.id, {}).get('craft_recipe_demand'), dict)
                        or any(isinstance(((self._selection_support.get('candidate_evidence') or {})
                            .get(plan.id, {}).get(key) or {}).get('recipe_dependency_chain'), dict)
                            for key in ('output_pickup_start_evidence',
                                        'recipe_input_transfer_start_evidence'))
                        for plan in plans):
                    from .planning.bootstrap_chain import catalog_projection
                    facts['factory']['recipe_dependency_catalog'] = catalog_projection(snapshot, self.catalog, plans)
                if facts["factory"]:
                    receipts = facts["factory"].pop("receipts", {})
                    facts["factory"].pop("connectors", None)
                    facts["factory"]["native_transfer_receipt_count"] = len(receipts)
                state = {"facts": facts, "active_goal": asdict(GOALS[self.memory.active_goal]),
                         "history": self._model_history(), **self._selection_support}
                if self.factory_scheduling == "ready-work":
                    state["production_scheduling"] = {
                        "objective": "Advance the next production batch identified in plan descriptions",
                        "guidance": ("Prefer useful work while machines run; batch pickups to evidenced "
                                     "current demand and ready stock. A small current prerequisite "
                                     "does not justify waiting for speculative output."),
                        "ultimate_goal": self.memory.active_goal,
                    }
                if self.two_stage_decisions:
                    from .two_stage_decision import PROTOCOL
                    state["decision_protocol"] = PROTOCOL
                # Every resumable Jev selection is write-ahead persisted. Do
                # not wait for the legacy stalled-decision threshold: a lost
                # response on the first running request is already ambiguous.
                persistent_selection_mode = bool(
                    self.persist_recoverable_blocks and self.policy == "jev")
                if persistent_selection_mode:
                    result = self._persistent_selection_with_alternatives(
                        snapshot, state, plans, source_authorized=blocked_reevaluation,
                        authorization_reason=blocked_reevaluation_reason)
                    if "record" in result:
                        return result["record"]
                    self._decision = result["decision"]
                    chosen = result["chosen"]
                    persistent_input_sha256 = result["input_sha256"]
                    persistent_attempt_finalized = result["finalized"]
                    persistent_trace_done = result["trace_done"]
                    alternative_wait = result.get("wait_phase")
                    if alternative_wait is not None:
                        alternative_status = result
                elif self.async_decisions and self.memory.async_decision is not None:
                    result = self._async_resume_pending_selection(snapshot, state, plans)
                    if result is None:
                        raise ValueError("Pending async request vanished before recovery")
                    self._decision = result["decision"]
                    chosen = result["chosen"]
                    persistent_input_sha256 = result["input_sha256"]
                    persistent_attempt_finalized = result["finalized"]
                    persistent_trace_done = result["trace_done"]
                else:
                    try:
                        prepared_batch = None
                        client = self.jev
                        if self.async_decisions:
                            from .judgments import question_batch, is_lone_passive_background_wait

                            context, questions, offered = question_batch(
                                state, plans, max_bytes=self.max_request_bytes)
                            prepared_batch = (context, questions, offered)
                            if not is_lone_passive_background_wait(plans):
                                self._async_prepare_selection(
                                    snapshot=snapshot, source_state=state,
                                    state=context, questions=questions,
                                    plans=plans, offered=offered)
                                client = self._async_decision_adapter
                            else:
                                self._async_selection_scope = None
                        with phase("selection", self._diagnostic_trace):
                            traced_client = self._trace.client(client)
                            if self.async_decisions:
                                self._decision = select_plan(
                                    traced_client, state, plans,
                                    self.confidence_floor, self.max_request_bytes,
                                    prepared_batch=prepared_batch)
                            else:
                                self._decision = select_plan(
                                    traced_client, state, plans,
                                    self.confidence_floor, self.max_request_bytes)
                    except _AsyncStepCancelled:
                        self._async_cancel_request()
                        return self._record(
                            snapshot, "observe",
                            "Async selection cancelled; provider delivery phase was preserved without action")
                    except ValueError as error:
                        self._decision = Decision(
                            None, "observe", str(error), state=state,
                            diagnostics={"schema": 1, "outcome": "request_rejected"})
                    pruned = self._decision.diagnostics.get("pruned_candidate_ids")
                    if pruned:
                        print(f"[t={snapshot.tick}] request budget pruned {len(pruned)} of "
                              f"{self._decision.diagnostics.get('input_candidates')} candidates "
                              f"({self._decision.diagnostics.get('request_bytes')}/"
                              f"{self.max_request_bytes} bytes): {', '.join(pruned)}", flush=True)
                if self.async_decisions and self._async_cancel_requested():
                    self._async_cancel_request()
                    return self._record(
                        snapshot, "observe",
                        "Async step cancelled before selected-plan persistence")
                if self._decision.diagnostics.get("outcome") == "provider_blocked":
                    # Operational denial is neither model abstention nor planning
                    # failure. No hybrid fallback and no consumed gameplay budget.
                    if persistent_input_sha256 is not None and not persistent_attempt_finalized:
                        from .blocked_persistence import finish_attempt
                        finish_attempt(self.memory, self.provenance["code_revision"],
                                       persistent_input_sha256, "provider_blocked",
                                       archive_index=self._blocked_recovery_archive_index)
                        if self.persist_recoverable_blocks:
                            provider = self._decision.diagnostics.get("provider", {})
                            if not isinstance(provider, dict):
                                provider = {}
                            category = provider.get("category")
                            if category not in {
                                    "application_schema", "service_network",
                                    "authentication_authorization", "account_quota",
                                    "rate_limit", "unknown_outcome"}:
                                category = "unknown"
                            provider_phase = provider.get("phase")
                            if provider_phase not in {"cooldown", "exhausted"}:
                                provider_phase = "blocked"
                            attempts = provider.get("attempts")
                            if type(attempts) is not int or attempts < 0:
                                attempts = None
                            budget_limit = provider.get("budget_limit")
                            if type(budget_limit) is not int or budget_limit < 1:
                                budget_limit = None
                            attempts_text = "unknown" if attempts is None else str(attempts)
                            budget_text = ("unknown" if budget_limit is None
                                           else str(budget_limit))
                            provider_reason = (
                                "Provider circuit requires operator recovery: "
                                f"{category} ({provider_phase}, probes "
                                f"{attempts_text}/{budget_text})")
                            model_called = bool(self._decision.model_called)
                            self.memory.status = "blocked"
                            self.memory.reason = provider_reason
                            self.memory.event(
                                "provider_circuit_operator_recovery_required",
                                decision_input_sha256=persistent_input_sha256,
                                tick=snapshot.tick, reason=provider_reason,
                                category=category,
                                provider_phase=provider_phase, attempts=attempts,
                                budget_limit=budget_limit, model_called=model_called)
                            self._persistent_recovery_status = {
                                "phase": "provider_blocked",
                                "reason": provider_reason,
                                "model_call": model_called,
                                "decision_input_sha256": persistent_input_sha256,
                                "recorded_attempts": self._blocked_recovery_attempt_count(),
                                "provider_category": category,
                                "provider_phase": provider_phase,
                                "provider_attempts": attempts,
                                "provider_budget_limit": budget_limit,
                            }
                        else:
                            self._persistent_recovery_status = {
                                "phase": "provider_blocked", "reason": self._decision.reason,
                                "model_call": self._decision.model_called,
                                "decision_input_sha256": persistent_input_sha256,
                            }
                    if not persistent_trace_done:
                        self._trace_decision()
                    if self.async_decisions and self._async_cancel_requested():
                        self._async_cancel_request()
                    if self.async_decisions and self.memory.async_decision is not None:
                        record = self._async_archive.load(self.memory.async_decision)
                        self._async_complete_disposition(record, "no_action")
                    return self._record(snapshot, "observe", self._decision.reason)
                if not persistent_selection_mode:
                    chosen = next((p for p in plans if p.id == self._decision.plan_id), None)
                    if chosen is None and self.policy == "hybrid":
                        chosen = self._fallback_plan(plans)
                        self._decision.plan_id = chosen.id
                        self._decision.source = "deterministic-fallback"
                if not persistent_trace_done:
                    self._trace_decision()
                if self.async_decisions and self._async_cancel_requested():
                    self._async_cancel_request()
                    return self._record(
                        snapshot, "observe", "Async step cancelled before plan persistence")
                if chosen is None:
                    if alternative_wait is not None:
                        return self._record_alternative_frontier_status(
                            snapshot, input_sha256=persistent_input_sha256,
                            state_sha256=alternative_status["state_sha256"],
                            frontier_sha256=alternative_status["frontier_sha256"],
                            phase_name=alternative_wait,
                            seen_count=alternative_status["seen_count"],
                            unseen_count=alternative_status["unseen_count"],
                            reason=alternative_status["reason"])
                    self.memory.stalled_decisions += 1
                    if not persistent_blocked:
                        self.memory.reason = self._decision.reason
                    if self.memory.stalled_decisions >= self.max_stalled_decisions:
                        self.memory.status = "blocked"
                    if persistent_input_sha256 is not None and not persistent_attempt_finalized:
                        from .blocked_persistence import is_recoverable_reason, finish_attempt
                        if is_recoverable_reason(self._decision.reason):
                            finish_attempt(self.memory, self.provenance["code_revision"],
                                           persistent_input_sha256, "rejected",
                                           self._decision.reason,
                                           archive_index=self._blocked_recovery_archive_index)
                        else:
                            finish_attempt(self.memory, self.provenance["code_revision"],
                                           persistent_input_sha256, "failed",
                                           archive_index=self._blocked_recovery_archive_index)
                    if (persistent_input_sha256 is not None
                            and self._persistent_block_active()):
                        wait = self.persistent_recovery_wait_seconds()
                        self.memory.event(
                            "blocked_recovery_wait", decision_input_sha256=persistent_input_sha256,
                            tick=snapshot.tick, reason=self.memory.reason,
                            next_observation_seconds=wait)
                        self._persistent_recovery_status = {
                            "phase": "waiting_for_changed_game_evidence",
                            "reason": self.memory.reason,
                            "next_observation_seconds": wait,
                            "model_call": self._decision.model_called,
                            "decision_input_sha256": persistent_input_sha256,
                            "recorded_attempts": self._blocked_recovery_attempt_count(),
                        }
                    if self.async_decisions and self.memory.async_decision is not None:
                        record = self._async_archive.load(self.memory.async_decision)
                        self._async_complete_disposition(record, "no_action")
                    return self._record(snapshot, "observe", self.memory.reason)
            if self.async_decisions and self.memory.async_decision is not None:
                pointer = self.memory.async_decision
                record = self._async_archive.load(pointer)
                selector = record["selector"]
                if (self._decision.source == "deterministic-fallback"
                        or not any(self._async_json_equal(chosen.to_dict(), offered)
                                   for offered in selector["offered_plans"])):
                    # A deterministic fallback is not authorized by the model
                    # response. Consume its no-action result before committing
                    # the separate fallback plan.
                    if not self._async_complete_disposition(record, "no_action"):
                        return self._record(
                            snapshot, "observe",
                            "Async provider result remains unresolved; no fallback is authorized")
            from .capital_controller import commit as commit_capital
            commit_capital(self, chosen, snapshot)
            if getattr(self, "_commit_solid", None):
                self._commit_solid(chosen, snapshot)
            if getattr(self, "_commit_successor", None):
                self._commit_successor(chosen, snapshot)
            if blocked_reevaluation or persistent_blocked:
                # A blocked checkpoint becomes runnable only after ordinary
                # selection has produced a real committed plan.
                self.memory.status, self.memory.reason = "running", ""
                if persistent_input_sha256 is not None:
                    self._persistent_recovery_status = {
                        "phase": "selected_plan_entered_normal_execution",
                        "reason": "Selection committed; action still requires ordinary native verification",
                        "model_call": self._decision.model_called,
                        "decision_input_sha256": persistent_input_sha256,
                        "recorded_attempts": self._blocked_recovery_attempt_count(),
                    }
            self.memory.active_plan = chosen.to_dict()
            self.memory.step_index = 0
            self.memory.event("plan_committed", plan=chosen.id, source=self._decision.source,
                              tick=snapshot.tick, **({'definition': chosen.to_dict()}
                                  if getattr(self, '_solid_science_policy', False)
                                  and not (chosen.id.startswith('solid-project:')
                                           and chosen.id.endswith(':kit')) else {}))
            if persistent_input_sha256 is not None:
                from .blocked_persistence import finish_attempt
                finish_attempt(self.memory, self.provenance["code_revision"],
                               persistent_input_sha256, "selected",
                               archive_index=self._blocked_recovery_archive_index)
            if self.async_decisions and self.memory.async_decision is not None:
                record = self._async_archive.load(self.memory.async_decision)
                selected = next((offered for offered in record["selector"]["offered_plans"]
                                 if offered.get("id") == chosen.id), None)
                if (selected is None or not self._async_json_equal(selected, chosen.to_dict())
                        or self._decision.source == "deterministic-fallback"):
                    raise ValueError("Chosen plan is not the exact archived async candidate")
                completed = self._async_complete_disposition(
                    record, "selected", selected_plan_id=chosen.id,
                    selected_plan_sha256=fingerprint(self.memory.active_plan))
                if not completed:
                    raise ValueError("Async selected plan lacks a consumable exact provider response")
            else:
                self._save()
            if self._trace.enabled:
                self._trace.emit("plan_committed", {"plan_id": chosen.id, "plan": chosen.to_dict(),
                                                    "source": self._decision.source})

        # The world can change while a remote model evaluates the old snapshot.
        self._trace.observation_phase = "before_dispatch"
        fresh = self._observe("pre_dispatch_observe")
        if self.async_decisions and self._async_cancel_requested():
            return self._record(
                snapshot, "observe", "Async step cancelled before fresh action admission", fresh)
        if self._execution_barrier(fresh):
            return self._record(snapshot, "observe", self.memory.reason, fresh)
        if self._safety:
            held = self._safety.admission(self.memory, fresh)
            if held:
                return self._record(snapshot, "observe", held, fresh)
        plan = Plan.from_dict(self.memory.active_plan)
        index = self._trace.call(
            "plan_progress", lambda: plan.next_step(fresh, self.memory.step_index),
            details={"plan_id": plan.id, "from_index": self.memory.step_index},
            result=lambda value: {"next_step": value})
        if index == len(plan.steps):
            self._trace.emit("verification", {"phase": "existing_plan_effects", "scope": "plan",
                                              "plan_id": plan.id, "verified": True,
                                              "action_id": None})
            if self.async_decisions:
                plan_dict = self.memory.active_plan
                self.memory.event(
                    "async_plan_effects_satisfied", plan_id=plan.id,
                    plan_sha256=fingerprint(plan_dict), tick=fresh.tick,
                    snapshot_sha256=self._async_lineage_digest(asdict(fresh)),
                    step_sha256s=[fingerprint(step) for step in plan_dict["steps"]],
                )
            self._clear_plan()
            self._refresh_goals(fresh)
            return self._record(snapshot, "verify", "Plan effects already observed", fresh, True)
        self.memory.step_index = index
        step = plan.steps[index]
        if not self._trace.call("precondition_checked",
                                lambda: self._step_allowed(step, fresh)
                                and self._investment_step_allowed(plan, step, fresh),
                                details={"plan_id": plan.id, "step_index": index},
                                result=lambda value: {"allowed": value}):
            self._fail_plan("Plan precondition changed; replan from current observations")
            return self._record(snapshot, "observe", self.memory.reason, fresh)
        try:
            self.memory.reserve(plan.id, step.costs or {}, fresh.inventory)
        except ValueError as error:
            self._fail_plan(str(error))
            return self._record(snapshot, "observe", str(error), fresh)
        self.memory.pending = {"started_tick": fresh.tick, "polls": 0,
                               "action": step.action, "dispatch": "prepared"}
        unit = fresh.factory.get("entities", {}).get((step.parameters or {}).get("role"), {}).get("unit_number")
        self.memory.attempt = make_attempt(
            fresh.session_id, self.target, self.memory.active_plan, index, self.memory.pending,
            process_id=self._process_id,
            unit_number=unit if type(unit) is int and unit > 0 else None,
        )
        self._attempt_clock = (self.memory.attempt["id"], time.perf_counter())
        # Write-ahead checkpoint: after a crash even a prepared command is
        # treated as potentially dispatched, never blindly replayed.
        self._save()
        transfer_preflight = self._transfer_preflight_context(fresh, plan, step, index)
        try:
            def dispatch():
                if self.async_decisions:
                    execution = self._async_execution_context()
                    with execution["dispatch_gate"]:
                        if execution["cancelled"].is_set():
                            raise DispatchCancelledBeforeEntry
                        # Cancellation after this point waits for the ordinary
                        # dispatch receipt and verification before returning.
                        execution["dispatch_entered"] = True
                if step.action.startswith("factory_"):
                    if transfer_preflight is not None:
                        preflight = getattr(self.backend, "execute_transfer_preflight_traced", None)
                        if not callable(preflight):
                            raise RuntimeError("FLE backend lacks typed transfer preflight support")
                        return preflight(step.action, step.parameters or {},
                                         self._diagnostic_trace, transfer_preflight)
                    traced = getattr(self.backend, "execute_traced", None)
                    return (traced(step.action, step.parameters or {}, self._diagnostic_trace) if traced else
                            self.backend.execute(step.action, step.parameters or {}))
                return self.backend.act(step.action)

            with phase("dispatch", self._diagnostic_trace):
                outcome = self._trace.dispatch(
                    dispatch, step.action, parameters=step.parameters,
                    plan_id=plan.id, step_index=index, pending=self.memory.pending,
                    checkpointed=self.checkpoint is not None, attempt_id=self.memory.attempt["id"])
        except ResearchLogError:
            # A failed recorder is not an ambiguous backend return and must not
            # be swallowed by the normal dispatch-error handling.
            raise
        except (MaintenanceAdmissionClosed, StoragePressure) as error:
            # Only these exact local guard contracts prove operation() was never
            # entered. Retain the plan/reservations, record the rejected attempt,
            # and never treat this as a gameplay failure or clear an unknown effect.
            reason = ("maintenance_preflight_rejected" if isinstance(error, MaintenanceAdmissionClosed)
                      else "storage_preflight_rejected")
            self.memory.event(reason, attempt_id=self.memory.attempt["id"], tick=fresh.tick)
            self._finish_attempt(fresh, reason)
            self.memory.pending = None
            self._trace.clear_pending()
            return self._record(snapshot, "observe", reason, fresh)
        except DispatchCancelledBeforeEntry:
            execution = self._async_execution_context()
            attempt = self.memory.attempt
            phase_row = ((attempt or {}).get("dispatch_phases") or {}).get("dispatch")
            exact_witness = (
                execution["cancelled"].is_set()
                and not execution["dispatch_entered"]
                and isinstance(phase_row, dict)
                and phase_row.get("status") == "failed"
                and phase_row.get("error_code") == "cancelled_before_entry"
            )
            if not exact_witness:
                # Without the complete cancelled-gate + failed-phase witness,
                # keep the exact attempt for ordinary ambiguity recovery.
                self.memory.pending["dispatch"] = "ambiguous"
                self.memory.event("dispatch_error", error_type="cancelled_unattributed",
                                  tick=fresh.tick)
                return self._record(
                    snapshot, step.action,
                    "Cancellation did not prove dispatch was suppressed; verification required",
                    fresh)
            attempt_id = self.memory.attempt["id"]
            self.memory.event("dispatch_cancelled_before_entry", attempt_id=attempt_id,
                              plan_id=plan.id, step_index=index, tick=fresh.tick)
            self._finish_attempt(fresh, "cancelled_before_dispatch")
            self.memory.pending = None
            self._trace.clear_pending()
            self._save()
            return self._record(
                snapshot, "observe", "Async step cancelled before action dispatch", fresh)
        except _AsyncStepCancelled:
            # Only DispatchCancelledBeforeEntry carries the exact durable
            # non-entry witness. A generic internal cancellation at this point
            # cannot certify that no operation was entered, so retain the
            # attempt for ordinary ambiguity reconciliation.
            self.memory.pending["dispatch"] = "ambiguous"
            self.memory.event("dispatch_error", error_type="cancelled_unattributed",
                              tick=fresh.tick)
            return self._record(
                snapshot, step.action,
                "Cancellation did not prove dispatch was suppressed; verification required",
                fresh)
        except Exception as error:
            if self._persistence_failed:
                raise  # A phase checkpoint failure is not a backend acknowledgement.
            from .backends.native_factory import TransferPreflightRejected
            if type(error) is TransferPreflightRejected:
                attempt = self.memory.attempt
                phase_row = ((attempt or {}).get("dispatch_phases") or {}).get("transfer_rpc")
                try:
                    proof = error.proof
                    if (transfer_preflight is None
                            or not isinstance(phase_row, dict)
                            or phase_row.get("status") != "failed"
                            or phase_row.get("error_code") != "transfer_preflight_rejected"
                            or phase_row.get("proof") != proof
                            or proof.get("request") != transfer_preflight
                            or attempt is None
                            or self.memory.pending is None
                            or self.memory.pending.get("action") != step.action):
                        raise ValueError("Typed transfer proof lacks its exact durable phase witness")
                    self._validate_transfer_preflight_binding(
                        proof, attempt, plan, step, index, fresh.session_id)
                    return self._settle_transfer_preflight_rejection(fresh, plan, proof)
                except (TypeError, ValueError, KeyError, AttributeError):
                    # A malformed or mismatched proof is not authority to clear
                    # a paid action. Continue through the ordinary ambiguous path.
                    pass
            if step.action == "factory_connect" and type(error) is ConnectionPreflightRejected:
                # Only this explicit backend contract proves the connection
                # mutator was never entered. Generic errors, lost replies and
                # failures after placement remain ambiguous below.
                reason = "Connection preflight rejected: " + error.code
                details = {"code": error.code,
                           "attempt_id": self.memory.attempt["id"], "tick": fresh.tick}
                if getattr(self, "async_decisions", False):
                    details.update(plan_id=plan.id, step_index=self.memory.step_index,
                                   mutation_started=False)
                self.memory.event("connection_preflight_rejected", **details)
                self._trace.emit("connection_preflight_rejected", {
                    **self._trace.attempt_ref(self.memory.attempt["id"]),
                    "action": step.action, "plan_id": plan.id, "step_index": index,
                    "code": error.code, "mutation_started": False,
                })
                self._finish_attempt(fresh, "connection_preflight_rejected")
                self._fail_plan(reason)
                return self._record(snapshot, step.action, reason, fresh)
            self.memory.pending["dispatch"] = "ambiguous"
            self.memory.event("dispatch_error", error_type=error_code(error), tick=fresh.tick)
            return self._record(snapshot, step.action, "Ambiguous dispatch; verification required", fresh)
        self.memory.pending["dispatch"] = "returned"
        self._save()
        self._trace.observation_phase = "after_dispatch"
        after = self._observe("post_dispatch_observe")
        if self._execution_barrier(after):
            return self._record(snapshot, step.action,
                                str(outcome) + "; pending retained for reconciliation", after)
        if step.action == 'factory_connect' and after.world_kind == 'fle':
            from .connector_checkpoint import pending_owned
            if not pending_owned(self.memory, step):
                self.memory.status, self.memory.reason = (
                    'uncertain', 'Connector route needs exact reconciliation')
                return self._record(snapshot, step.action, self.memory.reason, after)
        with phase("verification", self._diagnostic_trace):
            verified = self._trace.verify(step, after, plan_id=plan.id, index=index,
                                          pending=self.memory.pending, phase="post_dispatch",
                                          attempt_id=self.memory.attempt["id"])
        if verified:
            self._finish_attempt(after)
            self.memory.release(plan.id)
            self.memory.pending = None
            self._trace.clear_pending()
            self.memory.step_index += 1
            self.memory.stalled_decisions = 0
            self.memory.event("step_verified", plan=plan.id, action=step.action, tick=after.tick)
            if self.memory.step_index == len(plan.steps):
                self._clear_plan()
            self._refresh_goals(after)
        return self._record(snapshot, step.action, str(outcome), after, verified)
