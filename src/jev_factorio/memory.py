"""Atomic, session-bound controller checkpoints (not Factorio save files)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .planning.materials import quantities
from .telemetry import fingerprint, make_attempt, validate_attempt


_BLOCKED_REEVALUATION_REASONS = frozenset({
    "Candidate evidence insufficient", "low choice confidence",
    "Current native boiler identity and coal stock are required",
    "Furnace fuel service requires current owned source identity",
})


def retain_latest_craft(rows: list[dict], *, events: bool = False) -> list[dict]:
    """Keep the latest craft witness inside, never in addition to, the 64 slots."""
    key, value = ("kind", "background_job_completed") if events else ("action", "factory_craft_job")
    latest = next((i for i in range(len(rows)-1, -1, -1) if rows[i].get(key) == value), None)
    if latest is None or latest >= len(rows)-64:
        return rows[-64:]
    return [rows[latest], *rows[-63:]]


def _history_partition(history: object) -> tuple[list[dict], list[dict]]:
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ValueError("Invalid checkpoint history")
    administrative = [row for row in history if row.get("kind") == "paid_duplicate_selection_reconciled"]
    ordinary = [row for row in history if row.get("kind") != "paid_duplicate_selection_reconciled"]
    if len(administrative) > 1 or len(ordinary) > 64:
        raise ValueError("Invalid bounded ordinary or administrative history")
    return ordinary, administrative


def ordinary_history(history: object, *, session_id=None, target=None) -> list[dict]:
    """Keep ordinary64 unchanged; exclude only one complete signed administrative scope."""
    ordinary, administrative = _history_partition(history)
    if administrative:
        from .paid_selection_reconciliation import authenticate_representation_history_receipt
        row = administrative[0]
        carry = row.get("budget_carry")
        if not isinstance(carry, dict):
            raise ValueError("Invalid governed administrative history")
        authenticate_representation_history_receipt(row,
            carry.get("session_id") if session_id is None else session_id,
            carry.get("target") if target is None else target)
    return ordinary


def validate_history_authority(memory, *, archive_index=None) -> None:
    _, administrative = _history_partition(memory.history)
    if administrative:
        from .paid_selection_reconciliation import validate_representation_budget_carry
        validate_representation_budget_carry(memory, administrative[0], archive_index=archive_index)


@dataclass
class CampaignMemory:
    session_id: str
    target: str
    version: int = 2
    active_goal: str | None = None
    completed_goals: dict[str, int] = field(default_factory=dict)
    active_plan: dict | None = None
    step_index: int = 0
    pending: dict | None = None
    reservations: dict[str, dict[str, float]] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    connection_failure_attribution: dict = field(default_factory=dict)
    connector_ownership: dict | None = None
    history: list[dict] = field(default_factory=list)
    last_tick: int = -1
    status: str = "running"
    reason: str = ""
    stalled_decisions: int = 0
    attempt: dict | None = None
    attempt_outcomes: list[dict] = field(default_factory=list)
    transfer_recovery: dict | None = None
    capital_investment: dict | None = None
    blocked_reevaluations: list[dict] = field(default_factory=list)
    blocked_recovery: dict | None = None
    # Keep this optional extension after the legacy fields so positional
    # CampaignMemory construction retains its historical argument order.
    blocked_recovery_archive: dict | None = None
    compatible_source_recoveries: list[dict] = field(default_factory=list)
    # Appended optional records preserve legacy positional construction.
    two_stage_decision: dict | None = None
    planner_fault_recovery: dict | None = None

    def event(self, kind: str, **details) -> None:
        validate_history_authority(self, archive_index=getattr(self, "_blocked_recovery_archive_index", None))
        proposed = [*self.history, {"kind": kind, **details}]
        # Validate new administrative authority before any history mutation.
        administrative = [row for row in proposed if row.get("kind") == "paid_duplicate_selection_reconciled"]
        if len(administrative) > 1:
            raise ValueError("Duplicate governed administrative history")
        if kind == "paid_duplicate_selection_reconciled":
            from .paid_selection_reconciliation import validate_representation_budget_carry
            validate_representation_budget_carry(self, administrative[0],
                archive_index=getattr(self, "_blocked_recovery_archive_index", None))
        ordinary = [row for row in proposed if row.get("kind") != "paid_duplicate_selection_reconciled"]
        self.history = retain_latest_craft(ordinary, events=True) + administrative

    def reserve(self, owner: str, costs: dict[str, float], inventory: dict[str, int]) -> None:
        costs = quantities(costs)
        available = quantities(inventory)
        for key, held in self.reservations.items():
            if key != owner:
                for item, amount in held.items():
                    available[item] = available.get(item, 0) - amount
        if any(amount > available.get(item, 0) for item, amount in costs.items()):
            raise ValueError("Insufficient unreserved construction materials")
        self.reservations[owner] = costs

    def release(self, owner: str) -> None:
        self.reservations.pop(owner, None)

    def save(self, path: Path | None) -> None:
        from .checkpoint_io import save_checkpoint
        save_checkpoint(self, path)

    @classmethod
    def load(cls, path: Path, session_id: str, target: str) -> CampaignMemory:
        memory = cls.from_bytes(path.read_bytes(), session_id, target)
        if memory.blocked_recovery_archive is not None:
            from .blocked_recovery_archive import build_index
            memory._blocked_recovery_archive_index = build_index(path, memory)
        try:
            validate_history_authority(memory, archive_index=getattr(memory, "_blocked_recovery_archive_index", None))
        except BaseException:
            index = getattr(memory, "_blocked_recovery_archive_index", None)
            if index is not None:
                index.close()
            raise
        return memory

    @classmethod
    def from_bytes(cls, raw: bytes, session_id: str, target: str) -> CampaignMemory:
        """Validate one immutable checkpoint capture through the full composed loader."""
        def invalid_constant(value):
            raise ValueError(f"Invalid numeric constant in checkpoint: {value}")

        try:
            data = json.loads(raw.decode('utf-8'), parse_constant=invalid_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid controller checkpoint; refusing to reset it") from error
        return cls._from_data(data, session_id, target)

    @classmethod
    def _from_data(cls, data: dict, session_id: str, target: str) -> CampaignMemory:
        try:
            if not isinstance(data, dict) or type(data.get("version")) is not int:
                raise ValueError("Invalid checkpoint version")
            if data["version"] == 1 and {"attempt", "attempt_outcomes"} & data.keys():
                raise ValueError("Legacy checkpoint has unexpected attempt fields")
            if data["version"] == 2 and not {"attempt", "attempt_outcomes"} <= data.keys():
                raise ValueError("Version 2 checkpoint is missing attempt fields")
            memory = cls(**data)
            if memory.planner_fault_recovery is not None:
                from .planner_fault_recovery import validate_record as validate_planner_fault
                validate_planner_fault(memory, boundary=False)
            if memory.two_stage_decision is not None:
                from .two_stage_decision import validate as validate_two_stage
                validate_two_stage(memory.two_stage_decision, session_id, target)
            if memory.transfer_recovery is not None and not isinstance(memory.transfer_recovery, dict):
                raise ValueError("Invalid transfer recovery reference")
            if memory.transfer_recovery is not None and memory.pending is None:
                raise ValueError("Transfer recovery requires a retained pending action")
            if (memory.version not in {1, 2} or memory.session_id != session_id
                    or memory.target != target or not session_id):
                raise ValueError("Checkpoint version, session, or target mismatch")
            if (type(memory.last_tick) is not int or type(memory.step_index) is not int
                    or memory.step_index < 0 or not isinstance(memory.history, list)
                    or not isinstance(memory.completed_goals, dict)
                    or not isinstance(memory.failures, dict)
                    or memory.status not in {"running", "completed", "blocked", "uncertain"}):
                raise ValueError("Invalid checkpoint state")
            from .planning.goals import goal_order
            from .planning.connection_identity import validate_attribution
            validate_attribution(memory.connection_failure_attribution, memory.failures)
            if memory.connector_ownership is not None:
                from .connector_checkpoint import validate_binding
                validate_binding(memory.connector_ownership, session_id)
            ordinary_history(memory.history, session_id=session_id, target=target)
            order = goal_order(target)
            if (memory.last_tick < -1 or memory.active_goal not in [None, *order]
                    or not set(memory.completed_goals).issubset(order)
                    or any(type(t) is not int or not 0 <= t <= memory.last_tick
                           for t in memory.completed_goals.values())
                    or any(type(n) is not int or n < 0 for n in memory.failures.values())
                    or type(memory.stalled_decisions) is not int or memory.stalled_decisions < 0
                    or not all(isinstance(e, dict) for e in memory.history)):
                raise ValueError("Invalid checkpoint receipts or counters")
            if memory.capital_investment is not None:
                from .planning.capital import MARKER, matches, validate_state
                validate_state(memory.capital_investment, memory.last_tick)
                if memory.target != 'rocket_launch' or memory.active_goal != 'rocket_launch':
                    raise ValueError('Capital investment requires the rocket production goal')
                if memory.active_plan and MARKER in (memory.active_plan.get('materials') or {}):
                    from .skills import Plan
                    if not matches(Plan.from_dict(memory.active_plan), memory.capital_investment):
                        raise ValueError('Active plan and capital investment disagree')
            for costs in memory.reservations.values():
                quantities(costs)
            if memory.active_plan is not None:
                # Import locally to avoid a memory/skill dependency cycle.
                from .skills import Plan
                plan = Plan.from_dict(memory.active_plan)
                if plan.goal != memory.active_goal or memory.step_index >= len(plan.steps):
                    raise ValueError("Checkpoint step outside plan")
            if memory.pending is not None and memory.active_plan is None:
                raise ValueError("Pending action without an active plan")
            if memory.pending is not None:
                pending = memory.pending
                if (not isinstance(pending, dict)
                        or set(pending) != {"started_tick", "polls", "action", "dispatch"}
                        or type(pending["started_tick"]) is not int
                        or not 0 <= pending["started_tick"] <= memory.last_tick
                        or type(pending["polls"]) is not int or pending["polls"] < 0
                        or pending["action"] != plan.steps[memory.step_index].action
                        or pending["dispatch"] not in {"prepared", "ambiguous", "returned"}):
                    raise ValueError("Invalid pending action in checkpoint")
            # Migration changes metadata only, never the pending command or receipts.
            # Loading (including offline diagnostics) does not write the source file.
            if memory.version == 1:
                memory.version = 2
                if memory.pending:
                    memory.attempt = make_attempt(session_id, target, memory.active_plan,
                                                  memory.step_index, memory.pending)
            if (memory.pending is None) != (memory.attempt is None):
                raise ValueError("Pending action and attempt identity must coexist")
            if memory.attempt is not None:
                validate_attempt(memory.attempt)
                attempt = memory.attempt
                step = memory.active_plan["steps"][memory.step_index]
                if (attempt["action"] != memory.pending["action"]
                        or attempt["started_tick"] != memory.pending["started_tick"]
                        or attempt["plan_id"] != plan.id or attempt["step_index"] != memory.step_index
                        or attempt["step_sha256"] != fingerprint(step)
                        or attempt["receipt"] != (step.get("parameters") or {}).get("receipt")):
                    raise ValueError("Attempt does not match the pending operation")
            if not isinstance(memory.attempt_outcomes, list) or len(memory.attempt_outcomes) > 64:
                raise ValueError("Invalid attempt outcome history")
            seen = {memory.attempt["id"]} if memory.attempt else set()
            for outcome in memory.attempt_outcomes:
                validate_attempt(outcome, finished=True)
                if outcome["id"] in seen or outcome["finished_tick"] > memory.last_tick:
                    raise ValueError("Duplicate or future attempt outcome")
                seen.add(outcome["id"])
            if (not isinstance(memory.blocked_reevaluations, list)
                    or len(memory.blocked_reevaluations) > 1024):
                raise ValueError("Invalid blocked-decision re-evaluation ledger")
            reevaluation_contracts = set()
            for entry in memory.blocked_reevaluations:
                required = {
                    "schema", "authorization_id", "blocked_source_revision", "source_head",
                    "decision_contract_sha256", "checkpoint_sha256", "stalled_decisions",
                    "reason", "tick", "state",
                }
                if (not isinstance(entry, dict) or set(entry) != required
                        or type(entry["schema"]) is not int or entry["schema"] != 1
                        or type(entry["authorization_id"]) is not str
                        or re.fullmatch(r"[0-9a-f]{32}", entry["authorization_id"]) is None
                        or type(entry["blocked_source_revision"]) is not str
                        or re.fullmatch(r"[0-9a-f]{40}", entry["blocked_source_revision"]) is None
                        or type(entry["source_head"]) is not str
                        or re.fullmatch(r"[0-9a-f]{40}", entry["source_head"]) is None
                        or type(entry["decision_contract_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", entry["decision_contract_sha256"]) is None
                        or type(entry["checkpoint_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", entry["checkpoint_sha256"]) is None
                        or type(entry["stalled_decisions"]) is not int
                        # Verified background work may reset the streak before
                        # a persistent block consumes its one-use authorization.
                        or entry["stalled_decisions"] < 0
                        or not isinstance(entry["reason"], str)
                        or entry["reason"] not in _BLOCKED_REEVALUATION_REASONS
                        or type(entry["tick"]) is not int or not 0 <= entry["tick"] <= memory.last_tick
                        or entry["state"] != "consumed"):
                    raise ValueError("Invalid blocked-decision re-evaluation ledger entry")
                if entry["decision_contract_sha256"] in reevaluation_contracts:
                    raise ValueError("Decision contract was already re-evaluated")
                reevaluation_contracts.add(entry["decision_contract_sha256"])
            if memory.blocked_recovery is not None:
                from .blocked_persistence import _validate_state
                _validate_state(memory.blocked_recovery, memory.session_id)
            from .compatible_recovery import validate_lineage
            validate_lineage(memory)
            if memory.blocked_recovery_archive is not None:
                archive = memory.blocked_recovery_archive
                required = {"schema", "session_id", "target", "entry_count",
                            "segment_count", "head_sha256"}
                if (not isinstance(archive, dict) or set(archive) != required
                        or type(archive["schema"]) is not int or archive["schema"] != 1
                        or archive["session_id"] != memory.session_id
                        or archive["target"] != memory.target
                        or type(archive["entry_count"]) is not int or archive["entry_count"] < 1
                        or type(archive["segment_count"]) is not int or archive["segment_count"] < 1
                        or archive["entry_count"] < archive["segment_count"]
                        or archive["entry_count"] > archive["segment_count"] * 1024
                        or type(archive["head_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", archive["head_sha256"]) is None
                        or memory.blocked_recovery is None):
                    raise ValueError("Invalid blocked-recovery archive pointer")
            # Validate cross-family paid identities at the shared base loader,
            # so composed checkpoints cannot bypass this check by omitting the
            # output-buffer extension that historically performed it.
            from .output_buffers import _validate_composed_paid_identities
            _validate_composed_paid_identities(memory)
            return memory
        except (TypeError, KeyError, AttributeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid controller checkpoint; refusing to reset it") from error


def checkpoint_memory_type(data: dict):
    """Return the exact composed memory type required by checkpoint fields."""
    from .controller import HierarchicalLoop

    if not isinstance(data, dict):
        raise ValueError("Invalid controller checkpoint")
    loop_type = HierarchicalLoop
    if {"background_schema", "background_job", "background_attempt", "background_step"} & data.keys():
        if not {"background_schema", "background_job"} <= data.keys():
            raise ValueError("Incomplete background checkpoint extension")
        from .background import BackgroundWorkLoop
        loop_type = BackgroundWorkLoop
    if {"output_buffers_schema", "output_commitments"} & data.keys():
        if not {"output_buffers_schema", "output_commitments"} <= data.keys():
            raise ValueError("Incomplete output-buffer checkpoint extension")
        from .buffer_controller import buffered_loop_type
        loop_type = buffered_loop_type(loop_type)
    if {"input_routes_schema", "input_commitments"} & data.keys():
        if not {"input_routes_schema", "input_commitments"} <= data.keys():
            raise ValueError("Incomplete input-route checkpoint extension")
        from .input_controller import input_loop_type
        loop_type = input_loop_type(loop_type)
    if {'outposts_schema', 'outpost_commitments'} & data.keys():
        if not {'outposts_schema', 'outpost_commitments', 'input_routes_schema', 'input_commitments'} <= data.keys():
            raise ValueError('Incomplete mining-outpost checkpoint extension')
        from .outpost_controller import outpost_loop_type
        loop_type = outpost_loop_type(loop_type)
    if {'successor_schema', 'successor_projects', 'successor_receipts'} & data.keys():
        if not {'successor_schema', 'successor_projects', 'successor_receipts'} <= data.keys():
            raise ValueError('Incomplete successor checkpoint extension')
        from .successor_controller import successor_loop_type
        loop_type = successor_loop_type(loop_type)
    # Funding and catalog fields are ownership evidence too. They can appear
    # even when a damaged/edited checkpoint has lost the extension's schema
    # marker; selecting the base loader in that case would silently discard
    # paid-work state.
    if ({"solid_routes_schema", "solid_science_policy", "solid_intents", "solid_epoch",
         "solid_commitments", "solid_funding", "solid_funding_catalogs"} & data.keys()):
        from .solid_controller import CHECKPOINT_FIELDS, solid_loop_type
        if not CHECKPOINT_FIELDS <= data.keys():
            raise ValueError("Incomplete solid-route checkpoint extension")
        loop_type = solid_loop_type(loop_type)
    if ({"coal_supply_schema", "coal_kit_policy", "coal_economic_admission", "coal_targets",
         "coal_epoch", "coal_commitments", "coal_funding"} & data.keys()):
        from .coal_controller import CHECKPOINT_FIELDS, coal_loop_type
        if not CHECKPOINT_FIELDS <= data.keys():
            raise ValueError("Incomplete coal checkpoint extension")
        loop_type = coal_loop_type(loop_type)
    return loop_type.memory_type


def load_checkpoint_bytes(raw: bytes, session_id: str, target: str, *,
                          checkpoint_path: Path | None = None,
                          memory_type=None) -> CampaignMemory:
    """Validate one immutable capture through the complete composed loader.

    ``checkpoint_path`` is required for archived blocked-recovery history; it
    supplies the content-addressed segment directory without changing the
    checkpoint bytes. Supplying ``memory_type`` lets repair compare old and new
    captures through the union of both checkpoints' enabled schemas.
    """
    def invalid_constant(value):
        raise ValueError(f"Invalid numeric constant in checkpoint: {value}")

    try:
        data = json.loads(raw.decode('utf-8'), parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid controller checkpoint; refusing to reset it") from error
    if not isinstance(data, dict):
        raise ValueError("Invalid controller checkpoint")
    memory_type = memory_type or checkpoint_memory_type(data)
    memory = memory_type.from_bytes(raw, session_id, target)
    if memory.blocked_recovery_archive is not None:
        if checkpoint_path is None:
            raise ValueError("Archived checkpoint validation requires its checkpoint path")
        from .blocked_recovery_archive import build_index
        memory._blocked_recovery_archive_index = build_index(checkpoint_path, memory)
    try:
        validate_history_authority(memory, archive_index=getattr(memory, "_blocked_recovery_archive_index", None))
    except BaseException:
        index = getattr(memory, "_blocked_recovery_archive_index", None)
        if index is not None:
            index.close()
        raise
    return memory


def load_checkpoint_data(data: dict, session_id: str, target: str, *,
                         checkpoint_path: Path | None = None,
                         memory_type=None) -> CampaignMemory:
    """Validate a detached checkpoint object with the same composed schema."""
    try:
        raw = json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid detached controller checkpoint") from error
    return load_checkpoint_bytes(raw, session_id, target,
                                 checkpoint_path=checkpoint_path, memory_type=memory_type)


def load_checkpoint(path: Path, session_id: str, target: str) -> CampaignMemory:
    return load_checkpoint_bytes(path.read_bytes(), session_id, target,
                                 checkpoint_path=path)
