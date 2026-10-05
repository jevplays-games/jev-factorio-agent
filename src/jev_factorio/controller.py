"""Opt-in hierarchical control: commit, execute, observe, verify, and recover.

The inherited run loop retains the existing monotonic deadline and transient
HTTP-failure handling. No live-game or model-performance claims follow from
passing the offline tests.
"""
from __future__ import annotations

import json
import hashlib
import math
import time
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
from .memory import CampaignMemory
from .planning.goals import GOALS, completed, goal_order
from .skills import Plan, compile_plans
from .state import GameSnapshot
from .provenance import gameplay_context
from .telemetry import DISPATCH_STAGES, error_code, fingerprint, make_attempt, phase, utc_now, validate_phase
from .wait_record_codec import Encoder as WaitRecordEncoder, encode_line as encode_wait_line


def _json_safe(value):
    """Keep malformed numeric answers auditable without emitting invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_numeric": repr(value)}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
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
                 persistent_idle_observations: int = DEFAULT_IDLE_OBSERVATIONS):
        if factory_scheduling not in {"serial", "ready-work"}:
            raise ValueError("Unknown factory scheduling policy")
        self.factory_scheduling = factory_scheduling
        self._capital_fault = False
        if policy not in {"jev", "deterministic", "hybrid"}:
            raise ValueError("Unknown campaign policy")
        if policy != "deterministic" and jev is None:
            raise ValueError("Supply an explicit Jev client; mock use must be intentional")
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
        if jev is not None and getattr(jev, "uses_http_provider", False):
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
        self._trace.metrics = self._performance if self.factory_scheduling == "ready-work" else None
        self._trace.admission_check = (
            lambda: self._safety.before_dispatch(self.memory.session_id)) if self._safety else None
        if target in {"rocket_launch", "iron_smelting", "steam_power", "automation_science"} \
                and hasattr(backend, "enable_factory"):
            self.catalog = backend.enable_factory()
            self.max_pending_polls = max(self.max_pending_polls, 1800)

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
        seen_candidates = set(carried_seen) | {
            offered["candidate_sha256"] for row in rows
            if (row["source_revision"]["commit"],row["source_revision"]["source_sha256"],
                row["decision_input_sha256"]) not in carried_keys
            for offered in row["selection_batch"]["offered"]}
        for row in rows:
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

            self._record_persistent_attempt(
                snapshot, input_sha256, source_authorized=source_auth_pending,
                authorization_reason=authorization_reason,
                selection_batch=metadata)
            source_auth_pending = False
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
            try:
                with phase("selection", self._diagnostic_trace):
                    self._decision = select_plan(
                        self._trace.client(self.jev), state, remaining,
                        self.confidence_floor, self.max_request_bytes,
                        prepared_batch=(context, questions, offered))
            except ValueError as error:
                self._decision = Decision(
                    None, "observe", str(error), state=context,
                    diagnostics={"schema": 1, "outcome": "request_rejected"})

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
                                   selection_batch: dict | None = None) -> None:
        """Write-ahead one decision fingerprint before model selection."""
        if source_authorized:
            # The changed-contract source authorization and its first concrete
            # fingerprint share the same durable checkpoint commit.
            self._consume_blocked_reevaluation(
                snapshot, input_sha256, authorization_reason=authorization_reason,
                persistent_outcome=outcome, selection_batch=selection_batch)
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
        self.memory.attempt_outcomes.append({
            **deepcopy(attempt), "outcome": outcome, "finished_tick": snapshot.tick,
            "finished_at_utc": utc_now(), "latency_seconds": latency,
        })
        self.memory.attempt_outcomes = self.memory.attempt_outcomes[-64:]
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
        return memory

    def _consume_blocked_reevaluation(self, snapshot: GameSnapshot,
                                      persistent_input: str | None = None, *,
                                      authorization_reason: str | None = None,
                                      persistent_outcome: str = "pending",
                                      selection_batch: dict | None = None) -> None:
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
        if ledger_reason not in {"Candidate evidence insufficient", "low choice confidence"}:
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
                from .blocked_persistence import finish_attempt, record_attempt
                record_attempt(self.memory, self.provenance["code_revision"], persistent_input,
                               self.memory.reason, snapshot.tick, allow_source_change=True,
                               archive_index=self._blocked_recovery_archive_index,
                               selection_batch=selection_batch)
                if persistent_outcome != "pending":
                    finish_attempt(self.memory, self.provenance["code_revision"], persistent_input,
                                   persistent_outcome, self.memory.reason,
                                   archive_index=self._blocked_recovery_archive_index)
                self.memory.event("blocked_recovery_attempt", decision_input_sha256=persistent_input,
                                  tick=snapshot.tick,
                                  source_head=self.provenance["code_revision"]["commit"])
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
        self.memory.failures[key] = self.memory.failures.get(key, 0) + 1
        self.memory.event("plan_failed", plan=key, reason=reason, tick=self.memory.last_tick)
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
                "requested_model": getattr(self.jev, "model", None),
                "resolved_model": getattr(self.jev, "last_model", None) if decision and decision.model_called else None,
                "usage": getattr(self.jev, "last_usage", None) if decision and decision.model_called else None,
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
            return self.memory.history[-8:]
        from .blocked_persistence import _SYSTEM_HISTORY_EVENTS
        from .paid_selection_reconciliation import validate_representation_budget_carry
        import subprocess
        result=[]
        for event in self.memory.history:
            if event.get("kind") in _SYSTEM_HISTORY_EVENTS:continue
            if event.get("kind") == "paid_duplicate_selection_reconciled":
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
        return (self._capital_fault or
                self.memory.status == 'uncertain' and
                self.memory.reason == 'Connector route needs exact reconciliation')

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
            self.memory.event("maintenance_required", reason="boiler fuel", tick=snapshot.tick)
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
                self.memory.event("passive_wait_yielded", plan=plan.id,
                                  candidates=[candidate.id for candidate in ready], tick=snapshot.tick)
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

    @traced_step
    def step(self) -> dict:
        self._persistent_recovery_status = None
        self._decision = None
        self._selection_support = {}
        self._planning_diagnostics = {}
        self._trace.observation_phase = "before_decision"
        self._phases = []
        from .performance import PerformanceCounters
        self._performance = PerformanceCounters()
        self._trace.metrics = self._performance if self.factory_scheduling == "ready-work" else None
        snapshot = self._observe()
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
            with phase("planning", self._diagnostic_trace):
                plans, blocker = self._trace.call(
                    "candidate_set_created", lambda: self._work_candidates(snapshot),
                    result=lambda value: {"plans": [plan.to_dict() for plan in value[0]],
                                          "blocker": value[1]})
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
                        isinstance((plan.materials or {}).get('bootstrap_output_pickup'), dict) for plan in plans):
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
                else:
                    try:
                        with phase("selection", self._diagnostic_trace):
                            self._decision = select_plan(
                                self._trace.client(self.jev), state, plans,
                                self.confidence_floor, self.max_request_bytes)
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
                    return self._record(snapshot, "observe", self._decision.reason)
                if not persistent_selection_mode:
                    chosen = next((p for p in plans if p.id == self._decision.plan_id), None)
                    if chosen is None and self.policy == "hybrid":
                        chosen = self._fallback_plan(plans)
                        self._decision.plan_id = chosen.id
                        self._decision.source = "deterministic-fallback"
                if not persistent_trace_done:
                    self._trace_decision()
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
                    return self._record(snapshot, "observe", self.memory.reason)
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
            self._save()
            if self._trace.enabled:
                self._trace.emit("plan_committed", {"plan_id": chosen.id, "plan": chosen.to_dict(),
                                                    "source": self._decision.source})

        # The world can change while a remote model evaluates the old snapshot.
        self._trace.observation_phase = "before_dispatch"
        fresh = self._observe("pre_dispatch_observe")
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
        try:
            def dispatch():
                if step.action.startswith("factory_"):
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
        except Exception as error:
            if self._persistence_failed:
                raise  # A phase checkpoint failure is not a backend acknowledgement.
            if step.action == "factory_connect" and type(error) is ConnectionPreflightRejected:
                # Only this explicit backend contract proves the connection
                # mutator was never entered. Generic errors, lost replies and
                # failures after placement remain ambiguous below.
                reason = "Connection preflight rejected: " + error.code
                self.memory.event("connection_preflight_rejected", code=error.code,
                                  attempt_id=self.memory.attempt["id"], tick=fresh.tick)
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
