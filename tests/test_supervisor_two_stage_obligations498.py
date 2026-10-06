"""Supervisor gates retain real strict two-stage decision obligations."""

from copy import deepcopy
import hashlib
import json
import os

import pytest

from jev_factorio import blocked_persistence
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import CampaignMemory
from jev_factorio.skills import Plan, Step
from jev_factorio.supervisor import Supervisor, SupervisorConfig, atomic_json
from test_blocked_persistence import LiveMockBackend, SOURCE
from test_two_stage_controller import LiveClient


class CapturedCheckpoint(BaseException):
    pass


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProcess:
    pid = 99999999

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self):
        self.returncode = -9
        return self.returncode


def _campaign(tmp_path, monkeypatch, *, client=None):
    import jev_factorio.controller as controller_module

    monkeypatch.setattr(controller_module, "gameplay_context",
                        lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(
        backend.session_id, "rocket_launch", active_goal="rocket_launch", last_tick=0,
        status="blocked", reason="low choice confidence", stalled_decisions=5,
        failures={"old-plan": 2},
        history=[{"kind": "retained", "marker": "original history"}],
    )
    blocked_persistence.record_attempt(memory, SOURCE, "a" * 64, memory.reason, 0)
    # Keep this unrelated fixture seed terminal. Missing outcomes in retained
    # legacy rows are ambiguous and are covered by dedicated fail-closed tests.
    blocked_persistence.finish_attempt(memory, SOURCE, "a" * 64, "rejected")
    memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=client or LiveClient(), policy="jev", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True, two_stage_decisions=True,
    )
    loop._safety.admission = lambda *_args: None
    plans = [
        Plan("walk-iron", "rocket_launch", "Reach observed iron",
             (Step("walk_to_iron", "near", "iron-ore"),)),
        Plan("walk-coal", "rocket_launch", "Reach observed coal",
             (Step("walk_to_coal", "near", "coal"),)),
    ]
    loop._work_candidates = lambda _snapshot: (plans, "")
    return loop, backend, checkpoint


def _checkpoint_at_phase(tmp_path, monkeypatch, phase):
    loop, backend, checkpoint = _campaign(tmp_path, monkeypatch)
    original_save = loop._save

    def save_then_capture():
        original_save()
        record = loop.memory.two_stage_decision
        if record is not None and record["phase"] == phase:
            raise CapturedCheckpoint(phase)

    monkeypatch.setattr(loop, "_save", save_then_capture)
    with pytest.raises(CapturedCheckpoint, match=phase):
        loop.step()
    value = json.loads(checkpoint.read_text())
    loaded = CampaignMemory.from_bytes(checkpoint.read_bytes(), backend.session_id,
                                       "rocket_launch")
    assert loaded.two_stage_decision["phase"] == phase
    assert value["blocked_recovery"]["attempts"][-1]["outcome"] == "pending"
    return value


def _supervisor(tmp_path, monkeypatch, checkpoint):
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test-credential")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    state_dir = tmp_path / "supervisor-state"
    state_dir.mkdir(exist_ok=True)
    config = SupervisorConfig(
        state_dir=state_dir, checkpoint=tmp_path / "checkpoint.json",
        session_id=checkpoint["session_id"], started_at=1000,
        repair_command=["repair"], cwd=tmp_path, duration_hours=1,
        poll_seconds=1, hang_seconds=3, backoff_seconds=1,
    )
    instance = Supervisor(config, clock=FakeClock(), sleep=lambda _seconds: None,
                          popen=lambda *_args, **_kwargs: FakeProcess())
    before = {"commit": "b" * 40, "source_sha256": "d" * 64}
    monkeypatch.setattr(instance, "snapshot_revision", lambda **_kwargs: before)
    monkeypatch.setattr(instance, "source_identity", lambda: ("head", "diff"))
    instance.initialize(record_only=True)
    instance.save(code_revision=before, revision_initialized=True)
    return instance, before


def _archive_outer_attempt(checkpoint, checkpoint_path, *, rows=None, drop_outcome=False):
    """Build one content-addressed legacy archive segment for reader controls."""
    from jev_factorio.blocked_recovery_archive import _canonical, _directory

    rows = deepcopy(rows if rows is not None else checkpoint["blocked_recovery"]["attempts"][:1])
    assert rows
    if drop_outcome:
        for row in rows:
            row.pop("outcome", None)
    segment = {
        "schema": 1, "session_id": checkpoint["session_id"],
        "target": checkpoint["target"], "first_sequence": 0,
        "last_sequence": len(rows) - 1, "previous_sha256": None, "attempts": rows,
    }
    raw = _canonical(segment)
    digest = hashlib.sha256(raw).hexdigest()
    archive_dir = _directory(checkpoint_path)
    archive_dir.mkdir(mode=0o700)
    os.chmod(archive_dir, 0o700)
    segment_path = archive_dir / f"segment-{digest}.json"
    segment_path.write_bytes(raw)
    os.chmod(segment_path, 0o600)

    value = deepcopy(checkpoint)
    value["blocked_recovery"]["attempts"] = []
    value["blocked_recovery_archive"] = {
        "schema": 1, "session_id": checkpoint["session_id"],
        "target": checkpoint["target"], "entry_count": len(rows),
        "segment_count": 1, "head_sha256": digest,
    }
    archived_keys = {(row["source_revision"]["commit"],
                      row["source_revision"]["source_sha256"],
                      row["decision_input_sha256"]) for row in rows}
    value["blocked_recovery"]["attempts"] = [
        row for row in value["blocked_recovery"]["attempts"]
        if (row["source_revision"]["commit"], row["source_revision"]["source_sha256"],
            row["decision_input_sha256"]) not in archived_keys
    ]
    return value


def _rewrite_archived_attempt(value, checkpoint_path, mutate):
    """Reseal a deliberately altered but schema-valid archive row and pointer."""
    from jev_factorio.blocked_recovery_archive import _canonical, _directory

    archive_dir = _directory(checkpoint_path)
    old_pointer = value["blocked_recovery_archive"]
    old_path = archive_dir / f"segment-{old_pointer['head_sha256']}.json"
    segment = json.loads(old_path.read_bytes())
    mutate(segment["attempts"][-1])
    raw = _canonical(segment)
    digest = hashlib.sha256(raw).hexdigest()
    new_path = archive_dir / f"segment-{digest}.json"
    new_path.write_bytes(raw)
    os.chmod(new_path, 0o600)
    old_pointer["head_sha256"] = digest


@pytest.mark.parametrize("phase", [
    "assessment_ready", "assessment_pending", "assessment_received",
    "choice_ready", "choice_pending", "choice_received", "settled",
])
def test_actual_controller_crash_phases_remain_supervisor_obligations(
        tmp_path, monkeypatch, phase):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, phase)

    assert Supervisor.has_unresolved_work(checkpoint)


def test_actual_timeout_checkpoint_is_not_a_supervisor_completion(tmp_path, monkeypatch):
    timeout_client = LiveClient(timeout="assessment")
    loop, backend, checkpoint = _campaign(tmp_path, monkeypatch, client=timeout_client)

    loop.step()

    value = json.loads(checkpoint.read_text())
    CampaignMemory.from_bytes(checkpoint.read_bytes(), backend.session_id, "rocket_launch")
    assert timeout_client.calls
    assert value["two_stage_decision"]["phase"] == "assessment_pending"
    assert value["blocked_recovery"]["attempts"][-1]["outcome"] == "provider_blocked"
    assert Supervisor.has_unresolved_work(value)


def test_completed_status_cannot_hide_two_stage_pending_from_public_watcher(
        tmp_path, monkeypatch):
    value = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    value["status"] = "completed"
    original = json.dumps(value, sort_keys=True)
    atomic_json(tmp_path / "checkpoint.json", value)
    supervisor, before = _supervisor(tmp_path, monkeypatch, value)
    monkeypatch.setattr(supervisor, "snapshot_revision",
                        lambda **_kwargs: before)
    supervisor.popen = lambda *_args, **_kwargs: pytest.fail(
        "completed checkpoint with an unresolved two-stage request launched")

    assert supervisor.watch_game() == "checkpoint_reconciliation"
    assert json.dumps(supervisor.checkpoint(), sort_keys=True) == original


@pytest.mark.parametrize("record_mode", ["missing", "null"])
def test_legacy_outer_pending_row_cannot_be_erased_by_missing_two_stage_record(
        tmp_path, monkeypatch, record_mode):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    if record_mode == "missing":
        checkpoint.pop("two_stage_decision")
    else:
        checkpoint["two_stage_decision"] = None
    checkpoint["status"] = "completed"
    raw = json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode()
    loaded = CampaignMemory.from_bytes(raw, checkpoint["session_id"], "rocket_launch")
    assert loaded.two_stage_decision is None
    assert checkpoint["blocked_recovery"]["attempts"][-1]["outcome"] == "pending"
    assert Supervisor.has_unresolved_work(checkpoint)

    atomic_json(tmp_path / "checkpoint.json", checkpoint)
    supervisor, before = _supervisor(tmp_path, monkeypatch, checkpoint)
    supervisor.popen = lambda *_args, **_kwargs: pytest.fail(
        "legacy outer pending request launched after strict record was erased")
    assert supervisor.watch_game() == "checkpoint_reconciliation"
    assert supervisor.state["code_revision"] == before
    assert supervisor.state["incident"]["checkpoint"] == checkpoint


def test_legacy_outer_pending_row_blocks_manual_source_adoption_without_record(
        tmp_path, monkeypatch):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "choice_pending")
    checkpoint.pop("two_stage_decision")
    atomic_json(tmp_path / "checkpoint.json", checkpoint)
    supervisor, before = _supervisor(tmp_path, monkeypatch, checkpoint)
    monkeypatch.setattr(supervisor, "snapshot_revision",
                        lambda **_kwargs: {"commit": "c" * 40,
                                           "source_sha256": "e" * 64})
    prior_state = deepcopy(supervisor.state)

    with pytest.raises(ValueError, match="pending action requires reconciliation"):
        supervisor.record_manual_intervention({
            "actor": "operator", "reason": "code_change", "evidence": ["reviewed"]})

    assert supervisor.state["code_revision"] == prior_state["code_revision"] == before
    assert supervisor.state["segment_id"] == prior_state["segment_id"]
    assert supervisor.state["cutoff"] == prior_state["cutoff"]
    assert supervisor.checkpoint() == checkpoint


def test_missing_legacy_outcome_remains_pending_after_typed_checkpoint_load(
        tmp_path, monkeypatch):
    value = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    value.pop("two_stage_decision")
    seed = next(row for row in value["blocked_recovery"]["attempts"]
                if row["decision_input_sha256"] == "a" * 64)
    seed.pop("outcome")
    # Make the newer row terminal so only the old no-outcome row proves the hold.
    value["blocked_recovery"]["attempts"][-1]["outcome"] = "rejected"
    loaded = CampaignMemory.from_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        value["session_id"], "rocket_launch")
    assert loaded.two_stage_decision is None
    assert "outcome" not in next(row for row in loaded.blocked_recovery["attempts"]
                                 if row["decision_input_sha256"] == "a" * 64)
    assert Supervisor.has_unresolved_work(value)
    atomic_json(tmp_path / "checkpoint.json", value)
    supervisor, before = _supervisor(tmp_path, monkeypatch, value)
    monkeypatch.setattr(supervisor, "snapshot_revision",
                        lambda **_kwargs: {"commit": "c" * 40,
                                           "source_sha256": "e" * 64})
    prior_state = deepcopy(supervisor.state)
    with pytest.raises(ValueError, match="pending action requires reconciliation"):
        supervisor.record_manual_intervention({
            "actor": "operator", "reason": "code_change", "evidence": ["reviewed"]})
    assert supervisor.state["code_revision"] == prior_state["code_revision"] == before
    assert supervisor.state["segment_id"] == prior_state["segment_id"]
    assert supervisor.checkpoint() == value


def test_checkpoint_bound_archive_keeps_legacy_pending_outer_attempt(tmp_path, monkeypatch):
    from jev_factorio.memory import checkpoint_memory_type, load_checkpoint_data

    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    checkpoint.pop("two_stage_decision")
    checkpoint_path = tmp_path / "checkpoint.json"
    # Older ledger rows predate selection_batch and have no outcome field.
    # Keep the newer shaped row terminal so the archived legacy row is the
    # only unresolved evidence in this control.
    selection_attempt = next(row for row in checkpoint["blocked_recovery"]["attempts"]
                             if row["decision_input_sha256"] == "a" * 64)
    checkpoint["blocked_recovery"]["attempts"][-1]["outcome"] = "rejected"
    archived = _archive_outer_attempt(
        checkpoint, checkpoint_path, rows=[selection_attempt], drop_outcome=True)
    archived["status"] = "completed"

    memory = load_checkpoint_data(
        archived, archived["session_id"], archived["target"],
        checkpoint_path=checkpoint_path,
        memory_type=checkpoint_memory_type(archived))
    index = getattr(memory, "_blocked_recovery_archive_index")
    try:
        rows = list(index.compatibility_rows(memory=memory))
        assert any(row["decision_input_sha256"] == "a" * 64
                   and row.get("outcome", "pending") == "pending" for row in rows)
    finally:
        index.close()

    assert Supervisor.has_unresolved_work(archived, checkpoint_path)
    atomic_json(checkpoint_path, archived)
    supervisor, before = _supervisor(tmp_path, monkeypatch, archived)
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **_kwargs: before)
    supervisor.popen = lambda *_args, **_kwargs: pytest.fail(
        "archived legacy pending request launched")
    assert supervisor.watch_game() == "checkpoint_reconciliation"


def test_matching_terminal_archived_outer_attempt_is_a_valid_completion(
        tmp_path, monkeypatch):
    loop, backend, checkpoint = _campaign(
        tmp_path, monkeypatch, client=LiveClient(confidence=0.37))
    loop.step()
    value = json.loads(checkpoint.read_text())
    assert value["two_stage_decision"]["phase"] == "settled"
    assert value["blocked_recovery"]["attempts"][-1]["outcome"] == "rejected"
    archived = _archive_outer_attempt(
        value, checkpoint, rows=value["blocked_recovery"]["attempts"])
    archived["status"] = "completed"
    from jev_factorio.memory import checkpoint_memory_type, load_checkpoint_data

    loaded = load_checkpoint_data(
        archived, backend.session_id, "rocket_launch", checkpoint_path=checkpoint,
        memory_type=checkpoint_memory_type(archived))
    try:
        assert list(loaded._blocked_recovery_archive_index.compatibility_rows(
            memory=loaded))
    finally:
        loaded._blocked_recovery_archive_index.close()

    assert not Supervisor.has_unresolved_work(archived, checkpoint)
    atomic_json(checkpoint, archived)
    supervisor, before = _supervisor(tmp_path, monkeypatch, archived)
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **_kwargs: before)
    supervisor.popen = lambda *_args, **_kwargs: pytest.fail(
        "settled archived request incorrectly launched a replacement process")
    assert supervisor.watch_game() == "completed"


@pytest.mark.parametrize("mismatch", ["source", "input", "outcome"])
def test_archive_header_cannot_substitute_for_exact_outer_completion_binding(
        tmp_path, monkeypatch, mismatch):
    loop, backend, checkpoint = _campaign(
        tmp_path, monkeypatch, client=LiveClient(confidence=0.37))
    loop.step()
    value = json.loads(checkpoint.read_text())
    record = value["two_stage_decision"]
    assert record["phase"] == "settled"
    archived = _archive_outer_attempt(
        value, checkpoint, rows=value["blocked_recovery"]["attempts"])
    archived["status"] = "completed"

    def corrupt_terminal_binding(row):
        if mismatch == "source":
            row["source_revision"] = {
                "commit": "c" * 40, "source_sha256": "e" * 64,
            }
        elif mismatch == "input":
            row["decision_input_sha256"] = "f" * 64
        else:
            row["outcome"] = "failed"

    _rewrite_archived_attempt(archived, checkpoint, corrupt_terminal_binding)
    from jev_factorio.memory import checkpoint_memory_type, load_checkpoint_data

    loaded = load_checkpoint_data(
        archived, backend.session_id, "rocket_launch", checkpoint_path=checkpoint,
        memory_type=checkpoint_memory_type(archived))
    try:
        assert list(loaded._blocked_recovery_archive_index.compatibility_rows(
            memory=loaded))
    finally:
        loaded._blocked_recovery_archive_index.close()

    # No archive pointer or independently terminal-looking row can replace
    # the exact source/input/outcome binding held by the strict decision.
    assert Supervisor.has_unresolved_work(archived, checkpoint)
    assert Supervisor.has_unresolved_work(archived)


@pytest.mark.parametrize("current_revision", [
    {"commit": "c" * 40, "source_sha256": "e" * 64}, None,
])
def test_changed_or_unknown_source_watch_reconciles_without_launch(
        tmp_path, monkeypatch, current_revision):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "choice_pending")
    original = json.dumps(checkpoint, sort_keys=True)
    atomic_json(tmp_path / "checkpoint.json", checkpoint)
    supervisor, before = _supervisor(tmp_path, monkeypatch, checkpoint)
    monkeypatch.setattr(supervisor, "snapshot_revision",
                        lambda **_kwargs: current_revision)
    supervisor.popen = lambda *_args, **_kwargs: pytest.fail(
        "unresolved source transition launched a child")

    assert supervisor.watch_game() == "checkpoint_reconciliation"
    assert supervisor.state["incident"]["checkpoint"] == checkpoint
    assert supervisor.state["code_revision"] == before
    assert json.dumps(supervisor.checkpoint(), sort_keys=True) == original


def test_same_source_watch_keeps_existing_blocked_campaign_without_repair(
        tmp_path, monkeypatch):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    atomic_json(tmp_path / "checkpoint.json", checkpoint)
    supervisor, before = _supervisor(tmp_path, monkeypatch, checkpoint)
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **_kwargs: before)
    supervisor.popen = lambda *_args, **_kwargs: pytest.fail(
        "blocked two-stage campaign launched without an ordinary decision")

    assert supervisor.watch_game() == "blocked"
    assert not supervisor.state.get("repair_required")
    assert supervisor.state["code_revision"] == before


def test_terminal_outer_disposition_allows_reviewed_manual_source_transition(
        tmp_path, monkeypatch):
    loop, backend, checkpoint = _campaign(
        tmp_path, monkeypatch, client=LiveClient(confidence=0.37))
    loop.step()
    value = json.loads(checkpoint.read_text())
    assert value["two_stage_decision"]["phase"] == "settled"
    assert value["blocked_recovery"]["attempts"][-1]["outcome"] == "rejected"
    original_checkpoint = deepcopy(value)
    atomic_json(checkpoint, value)
    supervisor, before = _supervisor(tmp_path, monkeypatch, value)
    after = {"commit": "c" * 40, "source_sha256": "e" * 64}
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **_kwargs: after)

    supervisor.record_manual_intervention({
        "actor": "operator", "reason": "code_change", "evidence": ["reviewed"]})

    assert supervisor.state["code_revision"] == after
    assert supervisor.state["segment"] == 2
    assert before != after
    assert supervisor.checkpoint() == original_checkpoint


@pytest.mark.parametrize("current_revision", [
    {"commit": "c" * 40, "source_sha256": "e" * 64}, None,
])
def test_changed_or_unknown_source_cannot_be_manually_adopted_with_two_stage_work(
        tmp_path, monkeypatch, current_revision):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    atomic_json(tmp_path / "checkpoint.json", checkpoint)
    supervisor, before = _supervisor(tmp_path, monkeypatch, checkpoint)
    monkeypatch.setattr(supervisor, "snapshot_revision",
                        lambda **_kwargs: current_revision)
    prior_state = deepcopy(supervisor.state)

    with pytest.raises(ValueError, match="pending action requires reconciliation"):
        supervisor.record_manual_intervention({
            "actor": "operator", "reason": "code_change", "evidence": ["reviewed"]})

    assert supervisor.state["code_revision"] == prior_state["code_revision"] == before
    assert supervisor.state["segment_id"] == prior_state["segment_id"]
    assert supervisor.state["cutoff"] == prior_state["cutoff"]
    assert supervisor.checkpoint() == checkpoint


def test_repair_cannot_remove_two_stage_record_or_complete_while_outer_row_pending(
        tmp_path, monkeypatch):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    atomic_json(tmp_path / "checkpoint.json", checkpoint)
    supervisor, _before = _supervisor(tmp_path, monkeypatch, checkpoint)
    result = tmp_path / "repair-result.json"
    atomic_json(result, {
        "status": "repaired", "kind": "operational", "session_id": checkpoint["session_id"],
        "checkpoint": str(supervisor.config.checkpoint.resolve()),
        "operational_verified": True, "evidence": ["reviewed retained state"],
    })

    completed = deepcopy(checkpoint)
    completed["status"] = "completed"
    atomic_json(supervisor.config.checkpoint, completed)
    assert not supervisor.validate_repair(result, checkpoint, ("head", "diff"))

    without_record = deepcopy(checkpoint)
    without_record["status"] = "running"
    without_record["two_stage_decision"] = None
    atomic_json(supervisor.config.checkpoint, without_record)
    assert not supervisor.validate_repair(result, checkpoint, ("head", "diff"))

    without_outer = deepcopy(checkpoint)
    without_outer["status"] = "running"
    without_outer.pop("two_stage_decision")
    without_outer["blocked_recovery"] = None
    atomic_json(supervisor.config.checkpoint, without_outer)
    assert not supervisor.validate_repair(result, checkpoint, ("head", "diff"))


@pytest.mark.parametrize("mutation", ["remove", "introduce"])
def test_repair_preserves_outer_ledger_even_without_strict_decision_record(
        tmp_path, monkeypatch, mutation):
    checkpoint = _checkpoint_at_phase(tmp_path, monkeypatch, "assessment_pending")
    checkpoint.pop("two_stage_decision")
    if mutation == "introduce":
        checkpoint["blocked_recovery"] = None
        checkpoint["status"] = "running"
    atomic_json(tmp_path / "checkpoint.json", checkpoint)
    supervisor, _before = _supervisor(tmp_path, monkeypatch, checkpoint)
    result = tmp_path / f"repair-outer-{mutation}.json"
    atomic_json(result, {
        "status": "repaired", "kind": "operational", "session_id": checkpoint["session_id"],
        "checkpoint": str(supervisor.config.checkpoint.resolve()),
        "operational_verified": True, "evidence": ["reviewed retained state"],
    })

    repaired = deepcopy(checkpoint)
    repaired["status"] = "running"
    if mutation == "remove":
        repaired["blocked_recovery"] = None
    else:
        repaired["blocked_recovery"] = {
            "schema": 1, "session_id": checkpoint["session_id"],
            "source_revision": SOURCE,
            "attempts": [{
                "source_revision": SOURCE, "decision_input_sha256": "f" * 64,
                "reason": "low choice confidence", "tick": 0,
            }],
            "last_input_sha256": "f" * 64, "wait_level": 0,
        }
    atomic_json(supervisor.config.checkpoint, repaired)

    assert not supervisor.validate_repair(result, checkpoint, ("head", "diff"))


def test_settled_before_outer_disposition_remains_unresolved(tmp_path, monkeypatch):
    terminal = _checkpoint_at_phase(tmp_path, monkeypatch, "settled")
    attempt = terminal["blocked_recovery"]["attempts"][-1]
    assert attempt["outcome"] == "pending"
    assert Supervisor.has_unresolved_work(terminal)


def test_fully_settled_real_controller_result_remains_a_completion_control(
        tmp_path, monkeypatch):
    loop, backend, checkpoint = _campaign(
        tmp_path, monkeypatch, client=LiveClient(confidence=0.37))

    result = loop.step()

    value = json.loads(checkpoint.read_text())
    CampaignMemory.from_bytes(checkpoint.read_bytes(), backend.session_id, "rocket_launch")
    assert result["status"] == "blocked"
    assert value["two_stage_decision"]["phase"] == "settled"
    assert value["two_stage_decision"]["outcome"] == "low_choice_confidence"
    assert value["blocked_recovery"]["attempts"][-1]["outcome"] == "rejected"
    assert value.get("active_plan") is None
    assert not Supervisor.has_unresolved_work(value)
    completed = deepcopy(value)
    completed["status"] = "completed"
    CampaignMemory.from_bytes(json.dumps(completed).encode(), backend.session_id,
                              "rocket_launch")
    assert not Supervisor.has_unresolved_work(completed)


def test_terminal_settlement_can_pass_repair_only_with_all_ownership_retained(
        tmp_path, monkeypatch):
    loop, backend, checkpoint = _campaign(
        tmp_path, monkeypatch, client=LiveClient(confidence=0.37))
    loop.step()
    previous = json.loads(checkpoint.read_text())
    assert previous["two_stage_decision"]["phase"] == "settled"
    assert previous["blocked_recovery"]["attempts"][-1]["outcome"] == "rejected"
    atomic_json(checkpoint, {**previous, "status": "completed"})
    supervisor, _before = _supervisor(tmp_path, monkeypatch, previous)
    result = tmp_path / "valid-settled-result.json"
    atomic_json(result, {
        "status": "repaired", "kind": "operational", "session_id": backend.session_id,
        "checkpoint": str(supervisor.config.checkpoint.resolve()),
        "operational_verified": True, "evidence": ["reviewed terminal selection receipt"],
    })

    assert supervisor.validate_repair(result, previous, ("head", "diff"))


@pytest.mark.parametrize("mutation", ["missing_attempt", "invalid_record"])
def test_malformed_or_missing_outer_binding_fails_closed(tmp_path, monkeypatch, mutation):
    loop, _backend, checkpoint = _campaign(
        tmp_path, monkeypatch, client=LiveClient(confidence=0.37))
    loop.step()
    value = json.loads(checkpoint.read_text())
    assert value["two_stage_decision"]["phase"] == "settled"
    if mutation == "missing_attempt":
        value["blocked_recovery"]["attempts"] = [
            row for row in value["blocked_recovery"]["attempts"]
            if row["decision_input_sha256"] !=
            value["two_stage_decision"]["binding"]["input_sha256"]
        ]
    else:
        value["two_stage_decision"]["binding"]["session_id"] = "other-session"

    assert Supervisor.has_unresolved_work(value)


def test_settled_decision_with_incompatible_outer_disposition_stays_unresolved(
        tmp_path, monkeypatch):
    loop, _backend, checkpoint = _campaign(
        tmp_path, monkeypatch, client=LiveClient(confidence=0.37))
    loop.step()
    value = json.loads(checkpoint.read_text())
    row = next(row for row in value["blocked_recovery"]["attempts"]
               if row["decision_input_sha256"] ==
               value["two_stage_decision"]["binding"]["input_sha256"])
    assert row["outcome"] == "rejected"
    row["outcome"] = "failed"

    assert Supervisor.has_unresolved_work(value)


def test_settled_terminal_row_with_mismatched_request_batch_stays_unresolved(
        tmp_path, monkeypatch):
    loop, backend, checkpoint = _campaign(tmp_path, monkeypatch)
    loop.step()
    value = json.loads(checkpoint.read_text())
    row = next(row for row in value["blocked_recovery"]["attempts"]
               if row["decision_input_sha256"] ==
               value["two_stage_decision"]["binding"]["input_sha256"])
    assert row["outcome"] != "pending"
    row["selection_batch"]["request_sha256"] = "f" * 64

    assert Supervisor.has_unresolved_work(value)
