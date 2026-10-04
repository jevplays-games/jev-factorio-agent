"""Abstention only extends signed compatible recovery, never once-only authority."""
from copy import deepcopy
import hashlib
import pytest
from jev_factorio import blocked_reevaluation as blocked
from jev_factorio import compatible_recovery as compatible
from jev_factorio.memory import CampaignMemory
from test_compatible_source_recovery import setup, migrate


def test_default_blocked_validator_keeps_once_only_reason_scope():
    memory = CampaignMemory("session", "rocket_launch", status="blocked",
                            reason="model abstention", stalled_decisions=4)
    with pytest.raises(ValueError, match="quiescent eligible"):
        blocked.validate_blocked_memory(memory, 4)
    blocked.validate_blocked_memory(memory, 4, allow_model_abstention=True)
    for value in (1, "true", None):
        with pytest.raises(ValueError, match="eligibility flag"):
            blocked.validate_blocked_memory(memory, 4, allow_model_abstention=value)
    for field, value in (("pending", {}), ("attempt", {}), ("active_plan", {}),
                         ("reservations", {"actor": 1}), ("stalled_decisions", 3)):
        changed = deepcopy(memory)
        setattr(changed, field, value)
        with pytest.raises(ValueError, match="quiescent eligible"):
            blocked.validate_blocked_memory(changed, 4, allow_model_abstention=True)


def test_signed_compatible_abstention_handoff_preserves_paid_history(tmp_path, monkeypatch):
    _, original, path, authority = setup(tmp_path, monkeypatch)
    memory = original.memory
    memory.reason = "model abstention"
    memory.save(path)
    authority["checkpoint_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    authority["scope"] = compatible.scope(memory)
    rows = deepcopy(memory.blocked_recovery["attempts"])
    history = deepcopy(memory.history)
    migrated = migrate(path, authority)
    assert migrated.reason == "model abstention"
    assert migrated.blocked_recovery["attempts"] == rows
    assert migrated.history == history
    assert migrated.stalled_decisions == memory.stalled_decisions


@pytest.mark.parametrize("guard", ["decision", "budget"])
def test_abstention_does_not_bypass_source_contract_gates(tmp_path, monkeypatch, guard):
    _, original, path, authority = setup(tmp_path, monkeypatch)
    original.memory.reason = "model abstention"
    original.memory.save(path)
    authority["checkpoint_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    authority["scope"] = compatible.scope(original.memory)
    before = path.read_bytes()
    def refuse(*args, **kwargs):
        raise ValueError("contract differs")
    if guard == "decision":
        monkeypatch.setattr(blocked, "validate_source_revision", refuse)
    else:
        monkeypatch.setattr(compatible, "validate_budget_contract", refuse)
    with pytest.raises(ValueError, match="contract differs"):
        migrate(path, authority)
    assert path.read_bytes() == before


def test_original_fingerprint_protocol_and_legacy_reason_constant_are_unchanged():
    from pathlib import Path
    from jev_factorio import blocked_persistence as persistence
    assert persistence.RECOVERABLE_REASONS == frozenset({
        "Candidate evidence insufficient", "low choice confidence"})
    source = Path(persistence.__file__).read_bytes().replace(b"\r\n", b"\n")
    prefix = source.split(b"def _validate_state(")[0]
    assert hashlib.sha256(prefix).hexdigest() == (
        "f0f485cadbc27afe7784ef35f656edf2562a8458a9ce6cd3a0695ccfd1327dfa")
    # Compute on the running Python AST version, rather than a cross-version dump.
    old_contract = prefix + b"def _validate_state(value, session_id):\n    pass\n"
    assert compatible.budget_contract(old_contract) == compatible.budget_contract(source)
    assert persistence.is_recoverable_reason("model abstention")
    for reason in (None, True, 1, "model_abstention", "operator stop", "completed"):
        assert not persistence.is_recoverable_reason(reason)


def test_actual108_failed_paid_row_waits_unchanged_without_a_request(monkeypatch):
    """Real billed WAL row; supplied state key is explicit lookup transport.

    This does not claim a recompiled native snapshot or archive authentication.
    It proves that the existing failed/reasonNone row remains a one-use stop.
    """
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from jev_factorio import blocked_persistence as persistence, judgments
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.state import GameSnapshot
    fixture = json.loads((Path(__file__).parent / "fixtures/native108_failed_selection_row.json").read_text())
    row = fixture["row"]
    assert row["outcome"] == "failed" and row["reason"] is None
    memory = SimpleNamespace(session_id=fixture["provenance"]["session_id"],
        target="rocket_launch", history=[], compatible_source_recoveries=[],
        blocked_recovery_archive=None, blocked_recovery={"schema": 1,
        "session_id": fixture["provenance"]["session_id"],
        "source_revision": deepcopy(row["source_revision"]), "attempts": [deepcopy(row)],
        "last_input_sha256": row["decision_input_sha256"], "wait_level": 0})
    before = deepcopy(memory.blocked_recovery)
    loop = HierarchicalLoop.__new__(HierarchicalLoop)
    loop.memory = memory
    current = {"commit": "f" * 40, "source_sha256": "e" * 64}
    # Complete structurally validated authorization transport, not a native signature claim.
    scope = {"state_sha256": "a" * 64, "blocked_recovery_archive": None,
        "background_job": None, "background_attempt": None, "stalled_decisions": 4,
        "failures_sha256": "b" * 64, "history_sha256": "c" * 64,
        "attempt_outcomes_sha256": "d" * 64, "active_recovery_attempts": 1}
    authority = {"schema": 1, "authorization_id": "actual-row-equal-source-test",
        "checkpoint_sha256": fixture["provenance"]["source_checkpoint_sha256"],
        "session_id": memory.session_id, "target": memory.target,
        "previous_source": deepcopy(row["source_revision"]), "current_source": current,
        "decision_contract_sha256": "a" * 64, "scope": scope,
        "owner_invocation": {"run_id": "same-run", "segment_id": "new-source", "execution_id": "test-owner"},
        "supervisor_history_sha256": "b" * 64, "lock_path": str(Path(__file__).resolve()),
        "provider_state_sha256": None, "provider_identity_sha256": None}
    memory.compatible_source_recoveries = [dict(authority,
        authorization_sha256=compatible.digest_json(authority))]
    memory.blocked_recovery["source_revision"] = current
    before = deepcopy(memory.blocked_recovery)
    assert compatible.approved_sources(memory, current) == [row["source_revision"], current]
    loop.provenance = {"code_revision": current}
    loop.target, loop.policy, loop.confidence_floor = "rocket_launch", "jev", 0.45
    loop._blocked_recovery_archive_index = None
    loop._persistent_wait = lambda snapshot, digest: {"waiting": digest}
    monkeypatch.setattr(persistence, "selection_state_sha256", lambda *args, **kwargs:
        row["selection_batch"]["state_sha256"] if kwargs["source_revision"] == row["source_revision"] else "f" * 64)
    monkeypatch.setattr(judgments, "question_batch", lambda *args, **kwargs: pytest.fail("Already paid row must not request a batch"))
    snapshot = GameSnapshot(tick=row["tick"], player_position=(0, 0), inventory={},
        nearby_resources={}, placed_entities=[], session_id=memory.session_id, world_kind="fle")
    result = loop._persistent_selection_with_alternatives(snapshot, {}, [])
    assert result == {"record": {"waiting": row["decision_input_sha256"]}}
    assert memory.blocked_recovery == before
