"""Record-only manual declarations when the live checkpoint is unavailable."""
import json
from copy import deepcopy

import pytest

from jev_factorio import supervisor as module
from jev_factorio.supervisor import atomic_json
from test_supervisor import background_checkpoint, memory_checkpoint, supervisor

A = {"commit": "a" * 40, "source_sha256": "1" * 64}
B = {"commit": "b" * 40, "source_sha256": "2" * 64}


def events(instance):
    return [json.loads(line) for line in
            (instance.config.state_dir / "events.jsonl").read_text().splitlines()]


def revision(instance, monkeypatch):
    monkeypatch.setattr(instance, "snapshot_revision", lambda **kwargs: A)
    instance.record_revision(A, "manual390 fixture")


def _report(reason="checkpoint_reconciliation", evidence="reviewed checkpoint state"):
    return {"actor": "operator-390", "reason": reason, "evidence": [evidence]}


def _damage_checkpoint(instance, case):
    path = instance.config.checkpoint
    if case == "missing":
        path.unlink()
        return None, "missing"
    if case == "partial_json":
        raw = b'{"session_id":'
        status = "malformed"
    elif case == "non_object":
        raw = b"[]"
        status = "non_object"
    elif case == "wrong_session":
        data = json.loads(path.read_text())
        data["session_id"] = "another-supervised-session"
        raw = json.dumps(data).encode()
        status = "wrong_identity"
    elif case == "invalid_schema":
        data = json.loads(path.read_text())
        data["attempt_outcomes"] = {"not": "a list"}
        raw = json.dumps(data).encode()
        status = "invalid"
    else:
        raise AssertionError(case)
    path.write_bytes(raw)
    return raw, status


@pytest.mark.parametrize("case", ["missing", "partial_json", "non_object", "wrong_session", "invalid_schema"])
def test_record_only_run_persists_declaration_for_unreadable_checkpoint(
        supervisor, monkeypatch, case):
    revision(supervisor, monkeypatch)
    monkeypatch.setattr(supervisor, "popen", lambda *args, **kwargs: pytest.fail("record-only launched a child"))
    before_segment = supervisor.state["segment_id"]
    before_revision = deepcopy(supervisor.state["code_revision"])
    before_manual = len([row for row in events(supervisor) if row["event"] == "manual_intervention"])
    raw, expected_state = _damage_checkpoint(supervisor, case)
    report = _report(evidence="secret evidence that must not be copied into the event")

    assert supervisor.run(manual_intervention=report) == 0

    rows = [row for row in events(supervisor) if row["event"] == "manual_intervention"]
    assert len(rows) == before_manual + 1
    row = rows[-1]
    assert row["actor"] == "operator-390"
    assert row["actor_type"] == "human" and row["declaration_only"] is True
    assert row["reason"] == "checkpoint_reconciliation"
    assert row["report_sha256"] and row["evidence_count"] == 1
    assert "secret evidence" not in json.dumps(events(supervisor))
    assert row["checkpoint_state"] == expected_state
    expected_sources = (["captured_checkpoint", "last_valid_checkpoint"]
                        if expected_state == "invalid" else ["last_valid_checkpoint"])
    assert row["checkpoint_obligations"] == {
        "state": "unknown", "known_unresolved": False,
        "saved_sources": expected_sources,
    }
    assert row["source_adopted"] is False
    assert row["source_revision_state"] == "matches_saved"
    assert supervisor.state["segment_id"] == before_segment
    assert supervisor.state["code_revision"] == before_revision
    if raw is None:
        assert not supervisor.config.checkpoint.exists()
        assert row["checkpoint_sha256"] is None
    else:
        assert supervisor.config.checkpoint.read_bytes() == raw
        assert row["checkpoint_sha256"]


@pytest.mark.parametrize("saved_kind", ["pending_action", "background_work"])
def test_invalid_checkpoint_declaration_preserves_saved_incident_and_obligations(
        supervisor, monkeypatch, saved_kind):
    revision(supervisor, monkeypatch)
    saved = (background_checkpoint(supervisor) if saved_kind == "background_work"
             else memory_checkpoint(supervisor))
    if saved_kind == "background_work":
        assert saved["background_job"] is not None
        assert saved["background_attempt"] is not None
        assert saved["background_step"] is not None
    saved["history"] = [{"kind": "retained_failure", "reason": "test"}]
    atomic_json(supervisor.config.checkpoint, saved)
    incident = {
        "incident_id": "manual390-incident",
        "reason": "checkpoint_invalid",
        "checkpoint": deepcopy(saved),
        "source": ["head", "diff"],
        "code_revision": A,
    }
    supervisor.save(last_valid_checkpoint=deepcopy(saved), incident=incident,
                    repair_required=True, phase="blocked", attempt=2,
                    failures={"craft": 3})
    atomic_json(supervisor.config.checkpoint, {"session_id": "fresh", "partial": True})
    damaged = supervisor.config.checkpoint.read_bytes()
    before_cutoff = supervisor.state["cutoff"]
    before_segment = supervisor.state["segment_id"]
    monkeypatch.setattr(supervisor, "popen", lambda *args, **kwargs: pytest.fail("record-only launched a child"))

    assert supervisor.run(manual_intervention=_report()) == 0

    row = [row for row in events(supervisor) if row["event"] == "manual_intervention"][-1]
    assert row["checkpoint_obligations"] == {
        "state": "unknown",
        "known_unresolved": True,
        "saved_sources": ["incident_checkpoint", "last_valid_checkpoint"],
    }
    assert row["incident_id"] == incident["incident_id"]
    assert row["source_adopted"] is False
    assert supervisor.state["incident"] == incident
    assert supervisor.state["last_valid_checkpoint"] == saved
    assert supervisor.state["repair_required"] is True
    assert supervisor.state["phase"] == "blocked"
    assert supervisor.state["attempt"] == 2
    assert supervisor.state["failures"] == {"craft": 3}
    assert supervisor.state["cutoff"] == before_cutoff
    assert supervisor.state["segment_id"] == before_segment
    assert supervisor.config.checkpoint.read_bytes() == damaged


@pytest.mark.parametrize("observed,state", [(B, "changed"), (None, "unavailable")])
def test_unreadable_checkpoint_declaration_does_not_adopt_changed_or_unknown_source(
        supervisor, monkeypatch, observed, state):
    revision(supervisor, monkeypatch)
    saved = memory_checkpoint(supervisor)
    atomic_json(supervisor.config.checkpoint, saved)
    supervisor.save(last_valid_checkpoint=deepcopy(saved))
    _damage_checkpoint(supervisor, "partial_json")
    before_segment = supervisor.state["segment_id"]
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **kwargs: observed)
    monkeypatch.setattr(supervisor, "popen", lambda *args, **kwargs: pytest.fail("record-only launched a child"))

    assert supervisor.run(manual_intervention=_report("code_change")) == 0

    row = [row for row in events(supervisor) if row["event"] == "manual_intervention"][-1]
    assert row["checkpoint_obligations"]["state"] == "unknown"
    assert row["checkpoint_obligations"]["known_unresolved"] is True
    assert row["source_revision_state"] == state
    assert row["source_observed"] == observed
    assert row["source_adopted"] is False
    assert row["source_transition_pending"] is True
    assert supervisor.state["code_revision"] == A
    assert supervisor.state["segment_id"] == before_segment
    assert supervisor.state["last_valid_checkpoint"] == saved


def test_valid_pending_checkpoint_keeps_existing_manual_revision_gate(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    pending = memory_checkpoint(supervisor)
    atomic_json(supervisor.config.checkpoint, pending)
    state_before = json.loads(json.dumps(supervisor.state))
    audit_before = (supervisor.config.state_dir / "events.jsonl").read_bytes()
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **kwargs: B)

    with pytest.raises(ValueError, match="Code provenance changed while a pending action"):
        supervisor.run(manual_intervention=_report("code_change"))

    assert supervisor.state == state_before
    assert (supervisor.config.state_dir / "events.jsonl").read_bytes() == audit_before
    assert supervisor.checkpoint() == pending


@pytest.mark.parametrize("composition,failure_state", [
    ("invalid_schema", "invalid"), ("launch_incompatible", "launch_incompatible"),
])
@pytest.mark.parametrize("observed,source_state", [(B, "changed"), (None, "unavailable")])
def test_invalid_checkpoint_records_declaration_before_source_drift_rejection(
        supervisor, monkeypatch, composition, failure_state, observed, source_state):
    revision(supervisor, monkeypatch)
    if composition == "launch_incompatible":
        pending = background_checkpoint(supervisor)
        assert pending["background_job"] and pending["background_attempt"]
    else:
        pending = memory_checkpoint(supervisor)
        pending["attempt_outcomes"] = {"not": "a list"}
    atomic_json(supervisor.config.checkpoint, pending)
    checkpoint_before = supervisor.config.checkpoint.read_bytes()
    state_before = json.loads(json.dumps(supervisor.state))
    audit_before = (supervisor.config.state_dir / "events.jsonl").read_bytes()
    before_manual = len([row for row in events(supervisor)
                         if row["event"] == "manual_intervention"])
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **kwargs: observed)
    monkeypatch.setattr(supervisor, "popen", lambda *args, **kwargs: pytest.fail("record-only launched a child"))

    with pytest.raises(ValueError, match="Code provenance changed while a pending action"):
        supervisor.run(manual_intervention=_report("code_change"))

    rows = [row for row in events(supervisor) if row["event"] == "manual_intervention"]
    assert len(rows) == before_manual + 1
    row = rows[-1]
    assert row["declaration_only"] is True
    assert row["source_adopted"] is False
    assert row["source_before"] == A
    assert row["source_observed"] == observed
    assert row["source_revision_state"] == source_state
    assert row["source_transition_pending"] is True
    assert row["checkpoint_state"] == failure_state
    assert row["checkpoint_obligations"]["state"] == "unknown"
    assert row["checkpoint_obligations"]["known_unresolved"] is True
    assert "captured_checkpoint" in row["checkpoint_obligations"]["saved_sources"]
    assert row["report_sha256"] and row["evidence_count"] == 1
    assert supervisor.state["manual_interventions"] == state_before.get("manual_interventions", 0) + 1
    assert supervisor.state["segment_id"] == state_before["segment_id"]
    assert supervisor.state["code_revision"] == A
    assert supervisor.state["cutoff"] == state_before["cutoff"]
    assert supervisor.state.get("incident") == state_before.get("incident")
    assert supervisor.state.get("repair_required") == state_before.get("repair_required")
    assert supervisor.state.get("attempt") == state_before.get("attempt")
    assert supervisor.state.get("last_valid_checkpoint") == state_before.get("last_valid_checkpoint")
    assert (supervisor.config.state_dir / "events.jsonl").read_bytes() != audit_before
    assert supervisor.config.checkpoint.read_bytes() == checkpoint_before
    assert not any(row["event"] == "code_revision_changed" for row in events(supervisor))


def test_repeated_unreadable_checkpoint_declarations_are_distinct_durable_events(
        supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    _damage_checkpoint(supervisor, "partial_json")
    checkpoint_bytes = supervisor.config.checkpoint.read_bytes()
    segment = supervisor.state["segment_id"]
    monkeypatch.setattr(supervisor, "popen", lambda *args, **kwargs: pytest.fail("record-only launched a child"))

    for evidence in ("first report", "second report"):
        assert supervisor.run(manual_intervention=_report(evidence=evidence)) == 0

    rows = [row for row in events(supervisor) if row["event"] == "manual_intervention"]
    assert len(rows) == 2
    assert rows[0]["event_id"] != rows[1]["event_id"]
    assert rows[0]["sequence"] + 1 == rows[1]["sequence"]
    assert rows[0]["report_sha256"] != rows[1]["report_sha256"]
    assert supervisor.state["manual_interventions"] == 2
    assert supervisor.state["segment_id"] == segment
    assert supervisor.config.checkpoint.read_bytes() == checkpoint_bytes


def test_manual_audit_outbox_recovers_after_durability_failure(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    _damage_checkpoint(supervisor, "partial_json")
    checkpoint_bytes = supervisor.config.checkpoint.read_bytes()
    original_append = module.append_audit
    monkeypatch.setattr(module, "append_audit",
                        lambda *args: (_ for _ in ()).throw(OSError("simulated disk failure")))
    monkeypatch.setattr(supervisor, "popen", lambda *args, **kwargs: pytest.fail("record-only launched a child"))

    assert supervisor.run(manual_intervention=_report()) == 1
    pending = deepcopy(supervisor.state["audit_pending"])
    assert pending["event"] == "manual_intervention"
    assert supervisor.audit_failed and supervisor.stop_requested
    assert supervisor.config.checkpoint.read_bytes() == checkpoint_bytes

    monkeypatch.setattr(module, "append_audit", original_append)
    supervisor.stop_requested = False
    supervisor.initialize(record_only=True)
    rows = [row for row in events(supervisor) if row["event"] == "manual_intervention"]
    assert len(rows) == 1 and rows[0]["event_id"] == pending["event_id"]
    assert supervisor.state["audit_pending"] is None
    assert supervisor.state["manual_interventions"] == 1
    assert supervisor.config.checkpoint.read_bytes() == checkpoint_bytes
