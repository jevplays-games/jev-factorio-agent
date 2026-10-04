"""Provenance and crash-boundary tests; no live game, model, or GitHub calls."""
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from jev_factorio import supervisor as module
from jev_factorio.provenance import CONTEXT_ENV, append_audit, gameplay_context
from jev_factorio.supervisor import Supervisor, atomic_json
from test_supervisor import FakeProcess, background_checkpoint, operational_result, supervisor

A = {"commit": "a" * 40, "source_sha256": "1" * 64}
B = {"commit": "b" * 40, "source_sha256": "2" * 64}


def events(instance):
    return [json.loads(line) for line in (instance.config.state_dir / "events.jsonl").read_text().splitlines()]


def revision(instance, monkeypatch, value=A):
    monkeypatch.setattr(instance, "snapshot_revision", lambda **kwargs: value)
    instance.record_revision(value, "test")


def test_run_id_and_sequence_survive_restart(supervisor):
    run_id = supervisor.state["run_id"]
    supervisor.event("first")
    supervisor.initialize()
    supervisor.event("second")
    rows = events(supervisor)
    assert {row["run_id"] for row in rows} == {run_id}
    assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(row["utc"].endswith("+00:00") and row["monotonic_ns"] > 0 for row in rows)
    assert len({row["event_id"] for row in rows}) == len(rows)


def test_explicit_run_id_cannot_change_before_process_recovery(supervisor, monkeypatch):
    supervisor.config.run_id = "another-run"
    monkeypatch.setattr(supervisor, "recover_process", lambda: pytest.fail("identity not checked first"))
    with pytest.raises(ValueError, match="run ID cannot be changed"):
        supervisor.initialize()


def test_manifest_adoption_is_read_only_and_immutable(supervisor, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"run_id": supervisor.state["run_id"], "condition": "jev"}))
    original = path.read_bytes()
    supervisor.config.run_manifest = path
    supervisor.initialize()
    assert path.read_bytes() == original
    assert supervisor.state["run_manifest_sha256"]
    supervisor.config.run_manifest = None
    path.write_text(json.dumps({"run_id": supervisor.state["run_id"], "condition": "other"}))
    with pytest.raises(ValueError, match="manifest cannot be changed"):
        supervisor.initialize()


def test_new_supervisor_adopts_manifest_run_id(supervisor, tmp_path):
    supervisor.config.state_dir = tmp_path / "new-supervisor"
    supervisor.config.state_dir.mkdir()
    path = tmp_path / "manifest.json"
    path.write_text('{"run_id": "research-run-17"}')
    supervisor.config.run_manifest = path
    supervisor.initialize()
    assert supervisor.state["run_id"] == "research-run-17"


def test_manifest_mismatch_does_not_mutate_state(supervisor, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"run_id": "wrong-run"}')
    supervisor.config.run_manifest = path
    before = supervisor.state_path.read_bytes()
    with pytest.raises(ValueError, match="run ID cannot be changed"):
        supervisor.initialize()
    assert supervisor.state_path.read_bytes() == before


def test_legacy_upgrade_keeps_original_evidence_and_deadline(supervisor):
    state = {key: supervisor.state[key] for key in (
        "session_id", "checkpoint", "cwd", "started_at", "cutoff", "attempt", "phase", "process"
    )}
    atomic_json(supervisor.state_path, state)
    log = supervisor.config.state_dir / "events.jsonl"
    legacy = '{"at": 999, "event": "process_started"}\n'
    log.write_text(legacy)
    supervisor.initialize()
    assert log.read_text().startswith(legacy)
    assert events(supervisor)[1]["legacy_history"] is True
    assert supervisor.state["cutoff"] == state["cutoff"]
    assert "run_id" not in events(supervisor)[0]
    assert supervisor.state["gameplay_configuration"]["model_selection"]["needs_review"] is True
    before_launch = log.read_text()
    with pytest.raises(ValueError, match="explicit reviewed --model pin"):
        supervisor.launch(["game"], "gameplay")
    assert log.read_text() == before_launch

    # An explicit model plus live provider credentials is the reviewed
    # migration action. It binds the current provider to this same run.
    supervisor.config.model = "jev-1.13.0"
    supervisor.initialize(record_only=True)
    assert supervisor.state["gameplay_configuration"]["model_selection"] == {
        "provider": "typesafe", "model": "jev-1.13.0", "explicit": True,
    }
    assert log.read_text().startswith(legacy)
    assert events(supervisor)[-1]["event"] == "gameplay_model_binding_reviewed"


def test_gameplay_child_and_audit_share_frozen_context(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    received = []
    def start(command, **kwargs):
        received.append(kwargs["env"])
        return FakeProcess(0)
    supervisor.popen = start
    supervisor.launch(["game"], "gameplay")
    context = json.loads(received[-1][CONTEXT_ENV])
    assert context["run_id"] == supervisor.state["run_id"]
    assert context["segment_id"] == supervisor.state["segment_id"]
    assert context["code_revision"] == A
    assert context["execution_id"] == events(supervisor)[-1]["execution_id"]
    supervisor.stop_process()
    monkeypatch.setenv(CONTEXT_ENV, json.dumps(context))
    assert gameplay_context() == context
    supervisor.launch(["verify"], "verification")
    assert CONTEXT_ENV not in received[-1]
    supervisor.stop_process()


def test_initial_segment_is_not_an_intervention(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    row = events(supervisor)[-1]
    assert row["event"] == "segment_started"
    assert row["intervention_type"] is None


def test_revision_transition_keeps_run_but_changes_segment(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    run_id, segment = supervisor.state["run_id"], supervisor.state["segment_id"]
    assert supervisor.record_revision(B, "repair_attempt", accepted=True)
    assert supervisor.state["run_id"] == run_id
    assert supervisor.state["segment_id"] != segment
    row = events(supervisor)[-1]
    assert row["event"] == "code_revision_changed"
    assert row["from_segment_id"] == segment
    assert row["source_before"] == A and row["source_after"] == B
    count = len(events(supervisor))
    assert supervisor.record_revision(B, "same")
    assert len(events(supervisor)) == count


def test_dirty_change_without_commit_is_segmented(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    changed = {**A, "source_sha256": "3" * 64}
    supervisor.record_revision(changed, "gameplay_start", actor_type="unknown",
                               intervention_type="unattributed_change")
    row = events(supervisor)[-1]
    assert row["event"] == "code_revision_changed"
    assert row["intervention_type"] == "unattributed_change"
    assert row["actor_type"] != "human"


def test_unavailable_revision_is_not_claimed_as_code_change(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    supervisor.record_revision(None, "unreadable")
    row = events(supervisor)[-1]
    assert row["event"] == "source_provenance_changed"
    assert row["change_known"] is False and row["source_after"] is None


def test_incident_id_and_original_baseline_survive_retries(supervisor):
    supervisor.begin_repair("blocked")
    original = json.loads(json.dumps(supervisor.state["incident"]))
    supervisor.begin_repair("another failure")
    supervisor.initialize()
    assert supervisor.state["incident"] == original
    rows = [row for row in events(supervisor) if row["event"] == "incident_started"]
    assert len(rows) == 1 and rows[0]["incident_id"] == original["incident_id"]


def run_repair(instance, monkeypatch, kind, *, accepted=True, after=A, returncode=0):
    revision(instance, monkeypatch)
    instance.begin_repair("blocked")
    before = json.loads(json.dumps(instance.state["incident"]))
    result = instance.config.state_dir / f"repair-{instance.state['attempt'] + 1}.json"
    data = {"status": "repaired", "kind": kind, "session_id": "fresh",
            "checkpoint": str(instance.config.checkpoint.resolve()),
            "run_id": instance.state["run_id"], "incident_id": before["incident_id"],
            "attempt": instance.state["attempt"] + 1, "commit": B["commit"],
            "evidence": ["receipt data"], "operational_verified": True}
    atomic_json(result, data)
    def launch(command, phase, prompt=None):
        instance.process = FakeProcess(returncode)
    monkeypatch.setattr(instance, "launch", launch)
    monkeypatch.setattr(instance, "validate_repair", lambda *args: accepted)
    monkeypatch.setattr(instance, "snapshot_revision", lambda: after)
    answer = instance.repair("blocked")
    return answer, before, events(instance)[-1]


def test_operational_repair_keeps_segment_and_is_explicit(supervisor, monkeypatch):
    answer, before, row = run_repair(supervisor, monkeypatch, "operational")
    assert answer
    assert row["event"] == "repair_finished" and row["accepted"] is True
    assert row["intervention_type"] == "operational_recovery"
    assert row["declared_kind"] == "operational"
    assert row["source_before"] == row["source_after"] == A
    assert row["segment_id"] == "seg-000001"
    assert row["incident_id"] == before["incident_id"]
    assert row["correlation_complete"] is True
    assert row["result_sha256"] and supervisor.state["incident"] is None


def test_code_repair_rotates_segment_after_verified_change(supervisor, monkeypatch):
    answer, before, row = run_repair(supervisor, monkeypatch, "code", after=B)
    assert answer and row["intervention_type"] == "code_repair"
    assert row["segment_id"] == "seg-000002"
    assert any(r["event"] == "code_revision_changed" and r["accepted"] for r in events(supervisor))


def test_rejected_repair_changes_are_still_audited(supervisor, monkeypatch):
    answer, baseline, row = run_repair(supervisor, monkeypatch, "code", accepted=False, after=B)
    assert not answer and not row["accepted"]
    assert row["intervention_type"] == "repair_attempt"
    assert row["source_after"] == B
    assert supervisor.state["repair_required"]
    assert json.loads(json.dumps(supervisor.state["incident"])) == baseline
    assert any(r["event"] == "code_revision_changed" and not r["accepted"] for r in events(supervisor))


def test_operational_claim_cannot_hide_observed_change(supervisor, monkeypatch):
    answer, baseline, row = run_repair(supervisor, monkeypatch, "operational", after=B)
    assert not answer and not row["accepted"]
    assert row["declared_kind"] == "operational"
    assert row["intervention_type"] == "repair_attempt"
    assert json.loads(json.dumps(supervisor.state["incident"])) == baseline


def test_code_claim_needs_observed_verified_commit(supervisor, monkeypatch):
    answer, _, row = run_repair(supervisor, monkeypatch, "code", after=A)
    assert not answer and not row["accepted"]


def test_timed_out_repair_does_not_count_as_accepted(supervisor, monkeypatch):
    answer, _, row = run_repair(supervisor, monkeypatch, "code", after=B, returncode=None)
    assert not answer and row["returncode"] is None
    assert supervisor.state["repair_required"]


@pytest.mark.parametrize("key,value", [("run_id", "wrong"), ("incident_id", "wrong"), ("attempt", 99)])
def test_cross_run_or_stale_repair_result_rejected(supervisor, tmp_path, key, value):
    path = operational_result(supervisor, tmp_path)
    data = json.loads(path.read_text())
    data[key] = value
    atomic_json(path, data)
    assert not supervisor.validate_repair(path, supervisor.checkpoint(), ("head", "diff"))


def test_manual_report_is_record_only_and_does_not_clear_repair_gate(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    supervisor.begin_repair("uncertain")
    baseline = json.loads(json.dumps(supervisor.state["incident"]))
    checkpoint = supervisor.config.checkpoint.read_bytes()
    monkeypatch.setattr(supervisor, "watch_game", lambda: pytest.fail("manual audit launched gameplay"))
    report = {"actor": "operator-1", "reason": "checkpoint_reconciliation",
              "evidence": ["Bearer very-secret-value"]}
    assert supervisor.run(manual_intervention=report) == 0
    row = [r for r in events(supervisor) if r["event"] == "manual_intervention"][-1]
    assert row["actor_type"] == "human" and row["declaration_only"]
    assert row["segment_id"] == "seg-000002"
    assert row["report_sha256"] and row["evidence_count"] == 1
    assert "very-secret-value" not in json.dumps(events(supervisor))
    assert supervisor.state["repair_required"] and supervisor.state["incident"] == baseline
    assert supervisor.config.checkpoint.read_bytes() == checkpoint


def test_manual_report_records_code_transition(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda **kwargs: B)
    supervisor.record_manual_intervention({"actor": "operator", "reason": "code_change", "evidence": ["review"]})
    row = events(supervisor)[-1]
    assert row["event"] == "code_revision_changed" and row["actor_type"] == "human"
    assert row["source_before"] == A and row["source_after"] == B


@pytest.mark.parametrize("report", [{}, {"actor": "operator", "reason": "other", "evidence": []}, []])
def test_invalid_manual_report_is_rejected(supervisor, report):
    with pytest.raises(ValueError):
        supervisor.record_manual_intervention(report)


def test_manual_mode_respects_existing_lock(supervisor):
    import fcntl
    with supervisor.lock_path().open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another supervisor"):
            supervisor.run(manual_intervention={"actor": "operator", "reason": "other", "evidence": ["test"]})


@pytest.mark.parametrize("after_append", [False, True])
def test_outbox_recovery_does_not_lose_or_duplicate_segment(supervisor, monkeypatch, after_append):
    revision(supervisor, monkeypatch)
    original_append = module.append_audit
    def crash(path, record):
        if after_append:
            original_append(path, record)
        raise OSError("simulated crash boundary")
    monkeypatch.setattr(module, "append_audit", crash)
    assert not supervisor.record_revision(B, "repair_attempt")
    pending = json.loads(supervisor.state_path.read_text())["audit_pending"]
    assert pending["event"] == "code_revision_changed"
    assert supervisor.stop_requested
    monkeypatch.setattr(module, "append_audit", original_append)
    supervisor.stop_requested = False
    supervisor.initialize()
    rows = [row for row in events(supervisor) if row["event_id"] == pending["event_id"]]
    assert len(rows) == 1
    assert supervisor.state["segment_id"] == "seg-000002"
    assert supervisor.state["audit_pending"] is None


def test_audit_failure_prevents_spawn(supervisor, monkeypatch):
    monkeypatch.setattr(module, "append_audit", lambda *args: (_ for _ in ()).throw(OSError("full")))
    monkeypatch.setattr(supervisor, "popen", lambda *args, **kwargs: pytest.fail("unaudited spawn"))
    supervisor.launch(["game"], "gameplay")
    assert supervisor.stop_requested and supervisor.process is None


def test_incomplete_tail_is_not_silently_truncated(supervisor):
    path = supervisor.config.state_dir / "events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    original = path.read_bytes()
    supervisor.event("test")
    assert supervisor.stop_requested and path.read_bytes() == original


def test_exception_text_is_not_exported(supervisor):
    supervisor.event("repair_error", error="token=super-secret-value", reason="provider_error: api_key=secret")
    text = json.dumps(events(supervisor))
    assert "super-secret-value" not in text and "api_key=secret" not in text
    assert events(supervisor)[-1]["error_sha256"]


def test_restart_closes_interrupted_attempt_without_accepting_it(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    supervisor.begin_repair("blocked")
    incident_id = supervisor.state["incident"]["incident_id"]
    supervisor.transition("repair_started", {"attempt": 1, "repair_attempt_open": True,
                                            "attempt_incident_id": incident_id})
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda: B)
    supervisor.initialize()
    row = [r for r in events(supervisor) if r["event"] == "repair_interrupted"][-1]
    assert row["incident_id"] == incident_id and row["attempt"] == 1
    assert not row["accepted"] and row["outcome"] == "unknown"
    assert supervisor.state["repair_required"] and not supervisor.state["repair_attempt_open"]
    assert supervisor.state["incident"]["code_revision"] == A
    assert supervisor.state["code_revision"] == B


@pytest.mark.parametrize("failure", ["prompt", "launch"])
def test_retry_closes_every_failed_attempt(supervisor, monkeypatch, failure):
    monkeypatch.setattr(supervisor, "recovery_class", lambda reason: "source_defect")
    revision(supervisor, monkeypatch)
    supervisor.begin_repair("blocked")
    if failure == "prompt":
        original = Path.write_text
        def write(path, *args, **kwargs):
            if path.suffix == ".txt":
                raise OSError("prompt unavailable")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", write)
    else:
        def launch(*args, **kwargs):
            raise ValueError("launch unavailable")
        monkeypatch.setattr(supervisor, "launch", launch)
    assert supervisor.run() == 2
    started = [row for row in events(supervisor) if row["event"] == "repair_started"]
    closed = [row for row in events(supervisor) if row["event"] == "repair_interrupted"]
    assert len(started) == supervisor.config.max_repair_attempts
    assert [row["attempt"] for row in started] == [row["attempt"] for row in closed]
    assert not supervisor.state["repair_attempt_open"]
    assert supervisor.state["repair_required"]


def test_retry_records_own_source_and_preserves_incident_baseline(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    supervisor.begin_repair("blocked")
    values = iter([A, B, B, B])
    monkeypatch.setattr(supervisor, "snapshot_revision", lambda: next(values))
    monkeypatch.setattr(supervisor, "launch",
                        lambda *args: setattr(supervisor, "process", FakeProcess(1)))
    assert not supervisor.repair("blocked")
    assert not supervisor.repair("blocked")
    rows = [row for row in events(supervisor) if row["event"] == "repair_finished"]
    assert rows[0]["source_before"] == A and rows[0]["source_after"] == B
    assert rows[1]["source_before"] == rows[1]["source_after"] == B
    assert supervisor.state["incident"]["code_revision"] == A
    assert supervisor.state["attempt_source_before"] == B


def test_manual_snapshot_after_cutoff_is_bounded_and_keeps_deadline(supervisor, monkeypatch):
    supervisor.record_revision(A, "test")
    cutoff = supervisor.state["cutoff"]
    supervisor.clock.sleep(100)
    calls = []
    def snapshot(cwd, **kwargs):
        calls.append(kwargs)
        return B
    monkeypatch.setattr(module, "source_revision", snapshot)
    supervisor.record_manual_intervention(
        {"actor": "operator", "reason": "code_change", "evidence": ["review"]})
    assert calls[0]["timeout"] == 10
    assert supervisor.state["code_revision"] == B
    assert supervisor.state["cutoff"] == cutoff
    assert events(supervisor)[-1]["source_after"] == B


@pytest.mark.parametrize("extension", ["background_job", "background_step",
                                       "output_commitments", "input_commitments"])
@pytest.mark.parametrize("changed", ["extension", "history", "reservations"])
def test_repair_preserves_extension_locks_without_foreground_pending(
        supervisor, tmp_path, extension, changed):
    if extension in {"background_job", "background_step"}:
        previous = background_checkpoint(supervisor)
    elif extension == "output_commitments":
        from jev_factorio.buffer_controller import buffered_loop_type
        from jev_factorio.controller import HierarchicalLoop

        memory_type = buffered_loop_type(HierarchicalLoop).memory_type
        previous = asdict(memory_type(
            "fresh", "rocket_launch", status="running", last_tick=2,
            output_commitments={"recipe:iron-plate": {
                "source_unit": 17, "layout": "output:17",
                "parts": {"chest": {"role": "paid:chest", "unit_number": 18,
                                       "receipt": "paid-receipt", "paid": 1}},
            }},
        ))
    else:
        from jev_factorio.buffer_controller import buffered_loop_type
        from jev_factorio.controller import HierarchicalLoop
        from jev_factorio.input_controller import input_loop_type

        memory_type = input_loop_type(buffered_loop_type(HierarchicalLoop)).memory_type
        previous = asdict(memory_type(
            "fresh", "rocket_launch", status="running", last_tick=2,
            input_commitments={"recipe:iron-plate": {
                "layout": "input:17", "source_unit": 17, "parts": {},
            }},
        ))
    previous.update(history=[{"kind": "paid", "receipt": "paid"}],
                    reservations={"owner": {"iron-plate": 2}})
    from jev_factorio.memory import checkpoint_memory_type
    from jev_factorio.memory import load_checkpoint_data
    memory_type = checkpoint_memory_type(previous)
    load_checkpoint_data(previous, "fresh", "rocket_launch", memory_type=memory_type)
    atomic_json(supervisor.config.checkpoint, previous)
    result = operational_result(supervisor, tmp_path)
    assert supervisor.validate_repair(result, previous, ("head", "diff"))
    current = deepcopy(previous)
    if changed == "extension":
        if extension == "background_job":
            current["background_schema"] = 2
            current["background_job"] = None
            current["background_attempt"] = None
            current["background_step"] = None
        elif extension == "background_step":
            from jev_factorio.telemetry import fingerprint
            current["background_step"]["threshold"] += 1
            current["background_attempt"]["step_sha256"] = fingerprint(current["background_step"])
        else:
            current[extension] = {}
    elif changed == "history":
        current["history"] = [{"kind": "paid", "receipt": "rewritten"}]
    else:
        current["reservations"] = {}
    if changed == "extension":
        assert current[extension] != previous[extension]
    else:
        assert current[extension] == previous[extension]
    load_checkpoint_data(current, "fresh", "rocket_launch", memory_type=memory_type)
    atomic_json(supervisor.config.checkpoint, current)
    assert not supervisor.validate_repair(result, previous, ("head", "diff"))


def test_repair_rejects_orphan_background_step_and_preserves_saved_candidate(supervisor, tmp_path):
    from jev_factorio.memory import checkpoint_memory_type, load_checkpoint_data

    previous = background_checkpoint(supervisor)
    union_type = checkpoint_memory_type(previous)
    load_checkpoint_data(previous, "fresh", "rocket_launch", memory_type=union_type)
    atomic_json(supervisor.config.checkpoint, previous)
    result = operational_result(supervisor, tmp_path)

    orphan = deepcopy(previous)
    orphan["background_schema"] = 2
    orphan["background_job"] = None
    orphan["background_attempt"] = None
    load_error = None
    try:
        load_checkpoint_data(orphan, "fresh", "rocket_launch", memory_type=union_type)
    except ValueError as error:
        load_error = error
    assert load_error is not None and "unexpected step" in str(load_error)

    atomic_json(supervisor.config.checkpoint, orphan)
    assert not supervisor.validate_repair(result, previous, ("head", "diff"))
    assert supervisor.checkpoint() == orphan


@pytest.mark.parametrize("after_append", [False, True])
def test_interrupted_attempt_outbox_replays_once(supervisor, monkeypatch, after_append):
    revision(supervisor, monkeypatch)
    supervisor.begin_repair("blocked")
    supervisor.transition("repair_started", {
        "attempt": 1, "repair_attempt_open": True, "attempt_source_before": A,
        "attempt_incident_id": supervisor.state["incident"]["incident_id"],
    })
    append = module.append_audit
    def fail(path, record):
        if record["event"] == "repair_interrupted":
            if after_append:
                append(path, record)
            raise OSError("interrupted audit append")
        append(path, record)
    monkeypatch.setattr(module, "append_audit", fail)
    supervisor.close_interrupted_attempt()
    assert supervisor.stop_requested
    assert supervisor.state["audit_pending"]["event"] == "repair_interrupted"
    monkeypatch.setattr(module, "append_audit", append)
    supervisor.stop_requested = False
    supervisor.initialize()
    supervisor.initialize()
    rows = [row for row in events(supervisor) if row["event"] == "repair_interrupted"]
    assert len(rows) == 1
    assert rows[0]["source_before"] == A
    assert not supervisor.state["repair_attempt_open"]
    assert supervisor.state["repair_required"]


def test_manual_report_rejects_saved_process_without_recovery(supervisor, monkeypatch):
    supervisor.begin_repair("blocked")
    supervisor.save(process={"pid": 4242, "identity": "live-identity"},
                    repair_attempt_open=True, attempt=1,
                    attempt_incident_id=supervisor.state["incident"]["incident_id"])
    original_state = supervisor.state_path.read_bytes()
    original_checkpoint = supervisor.config.checkpoint.read_bytes()
    original_audit = (supervisor.config.state_dir / "events.jsonl").read_bytes()
    monkeypatch.setattr(supervisor, "process_identity", lambda pid: "live-identity")
    monkeypatch.setattr(supervisor, "kill_group", lambda *args: pytest.fail("manual report killed a process"))
    monkeypatch.setattr(supervisor, "launch", lambda *args: pytest.fail("manual report launched a process"))
    with pytest.raises(ValueError, match="no saved process"):
        supervisor.run(manual_intervention={
            "actor": "operator", "reason": "other", "evidence": ["captured review"]})
    assert supervisor.state_path.read_bytes() == original_state
    assert supervisor.config.checkpoint.read_bytes() == original_checkpoint
    assert (supervisor.config.state_dir / "events.jsonl").read_bytes() == original_audit


def test_manual_report_does_not_close_open_repair_attempt(supervisor, monkeypatch):
    revision(supervisor, monkeypatch)
    supervisor.begin_repair("blocked")
    supervisor.save(repair_attempt_open=True, attempt=1,
                    attempt_incident_id=supervisor.state["incident"]["incident_id"],
                    attempt_source_before=A)
    original_incident = json.loads(json.dumps(supervisor.state["incident"]))
    original_checkpoint = supervisor.config.checkpoint.read_bytes()
    monkeypatch.setattr(supervisor, "recover_process", lambda: pytest.fail("manual process recovery"))
    monkeypatch.setattr(supervisor, "close_interrupted_attempt", lambda: pytest.fail("manual attempt closure"))
    assert supervisor.run(manual_intervention={
        "actor": "operator", "reason": "other", "evidence": ["captured review"]}) == 0
    assert supervisor.state["repair_attempt_open"]
    assert supervisor.state["incident"] == original_incident
    assert supervisor.config.checkpoint.read_bytes() == original_checkpoint
    assert events(supervisor)[-1]["event"] == "manual_intervention"
