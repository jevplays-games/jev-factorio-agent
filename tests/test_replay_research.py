"""Integration with the canonical writer and controller trace, never a live game."""
import json

import pytest

from jev_factorio.replay import replay_log


def capture(tmp_path):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    from jev_factorio.causal_trace import CausalTrace
    from jev_factorio.backends.mock import MockBackend

    path = tmp_path / "research"
    backend = MockBackend()
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        trace = CausalTrace(sink, "flat")
        trace.begin_step()
        trace.observe(backend, "before_decision")
        trace.emit("candidate_set_created", {"candidates": {"idle": "wait"}, "status": "ok"})
        trace.emit("decision", {"action": "idle", "model_called": False, "reason": "矿石"})
        trace.dispatch(lambda: "waited", "idle", role="flat")
        trace.observe(backend, "after_action")
        trace.emit("verification", {"verified": None, "phase": "flat"})
        trace.emit("step_finished", {"action": "idle"})
    return path


def test_canonical_writer_trace_preserves_source_identity(tmp_path, monkeypatch):
    from jev_factorio.research_log import verify_run
    import subprocess
    import socket

    path = capture(tmp_path)
    expected = verify_run(path)
    before = {entry.name: entry.read_bytes() for entry in path.iterdir()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay attempted external execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    report = replay_log(path, format="research-v1", expected_head=expected["final_event_hash"])
    assert report.status == "incomplete", report.to_dict()["findings"]
    assert report.integrity["status"] == "verified_source"
    assert report.integrity["final_event_hash"] == expected["final_event_hash"]
    rows = [json.loads(line) for line in before["events.jsonl"].splitlines()]
    assert [{key: value for key, value in event.items() if key != "line"}
            for event in report.events] == rows
    assert report.decisions[0]["actions"][0]["verified"] is None
    assert before == {entry.name: entry.read_bytes() for entry in path.iterdir()}


@pytest.mark.parametrize("target", ["events.jsonl", "manifest.json", "integrity.json"])
def test_modified_producer_evidence_fails_before_causal_projection(tmp_path, target):
    path = capture(tmp_path)
    file = path / target
    rows = file.read_text().splitlines()
    row = json.loads(rows[0])
    row["run_id"] = "00000000-0000-0000-0000-000000000000"
    rows[0] = json.dumps(row)
    if target == "manifest.json":
        from jev_factorio.research_log import canonical_bytes
        file.write_bytes(canonical_bytes(row) + b"\n")
    else:
        file.write_text("\n".join(rows) + "\n")
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert report.decisions == []


def test_resealed_invalid_causal_reference_is_rejected(tmp_path):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        sink.emit("action_returned", {"trace_id": "trace", "decision_id": "decision:1",
                                     "action_id": "action:missing", "status": "ok"})
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert any(finding.code == "invalid_action_reference" for finding in report.findings)


def test_missing_seal_preserves_unknown_completeness(tmp_path):
    path = capture(tmp_path)
    (path / "integrity.json").unlink()
    report = replay_log(path, format="research-v1")
    assert report.status == "incomplete"
    assert report.integrity["status"] == "incomplete_source"
    assert any(finding.code == "missing_integrity_seal" for finding in report.findings)


def test_noncanonical_bytes_are_not_accepted_as_original_evidence(tmp_path):
    path = capture(tmp_path)
    file = path / "events.jsonl"
    rows = file.read_text().splitlines()
    rows[0] = json.dumps(json.loads(rows[0]), indent=None)
    file.write_text("\n".join(rows) + "\n")
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert report.decisions == []


@pytest.mark.parametrize("case", ["cross_decision_model", "repeated_plan", "invalid_model_flag"])
def test_resealed_invalid_causal_claims_fail(tmp_path, case):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        context = {"trace_id": "trace", "decision_id": "decision:1"}
        if case == "cross_decision_model":
            sink.emit("model_request", {**context, "model_call_id": "model:1"})
            sink.emit("model_response", {**context, "model_call_id": "model:1", "status": "ok"})
            sink.emit("decision", {**context, "decision_id": "decision:2",
                                   "model_call_id": "model:1", "model_called": True})
        elif case == "repeated_plan":
            for action in ("idle", "mine_coal"):
                sink.emit("plan_committed", {**context, "plan_id": "plan",
                                             "plan": {"id": "plan", "steps": [{"action": action}]}})
        else:
            sink.emit("decision", {**context, "model_called": []})
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"


def test_noncanonical_manifest_never_claims_verified_integrity(tmp_path):
    from jev_factorio.replay import ReplayInputError
    path = capture(tmp_path)
    file = path / "manifest.json"
    file.write_text(json.dumps(json.loads(file.read_text())) + "\n")
    with pytest.raises(ReplayInputError):
        replay_log(path, format="research-v1")


@pytest.mark.parametrize("controller", ["flat", "hierarchical"])
def test_final_mock_controller_producer_replays_without_execution(tmp_path, monkeypatch, controller):
    from jev_factorio.research_log import ResearchLog, RunConfiguration, verify_run
    from jev_factorio.backends.mock import MockBackend
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.loop import AgentLoop
    from jev_factorio.jev_client import MockJevClient

    path = tmp_path / "research"
    backend, client = MockBackend(), MockJevClient()
    with ResearchLog(path, RunConfiguration("mock", controller, "jev"), environ={}) as sink:
        options = {"jev": client, "tick_seconds": 0, "research_log": sink}
        loop = (HierarchicalLoop(backend, target="bootstrap_mining", **options)
                if controller == "hierarchical" else AgentLoop(backend, **options))
        loop.run(steps=40 if controller == "hierarchical" else 8)
    expected = verify_run(path)
    before = {entry.name: entry.read_bytes() for entry in path.iterdir()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay executed live controller behavior")

    monkeypatch.setattr(backend, "observe", forbidden)
    monkeypatch.setattr(backend, "act", forbidden)
    monkeypatch.setattr(client, "evaluate", forbidden)
    report = replay_log(path, format="research-v1", expected_head=expected["final_event_hash"])
    assert report.status == "incomplete", report.to_dict()["findings"]
    assert report.integrity["status"] == "verified_source"
    assert report.decisions
    assert before == {entry.name: entry.read_bytes() for entry in path.iterdir()}


@pytest.mark.parametrize("session", ["another-world", None])
def test_verification_cannot_cross_or_invent_session_continuity(tmp_path, session):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        context = {"trace_id": "trace", "decision_id": "decision:1"}
        sink.emit("step_started", context)
        action = {**context, "action_id": "action:1", "action": "idle",
                  "parameters": {}, "session_id": "original-world"}
        sink.emit("action_prepared", action)
        sink.emit("action_returned", {**action, "status": "ok"})
        observation = {**context, "observation_id": "observation:1", "session_id": session}
        sink.emit("observation", {**observation, "status": "ok"})
        sink.emit("verification", {**observation, "action_id": "action:1", "verified": True})
        sink.emit("step_finished", context)
    report = replay_log(path, format="research-v1")
    assert report.status == ("invalid" if session else "incomplete")
    assert report.decisions[0]["actions"][0]["verified"] is None


@pytest.mark.parametrize("kinds", [
    ["step_finished"], ["step_started", "step_started", "step_finished"],
    ["step_started", "step_finished", "step_failed"],
    ["step_started", "step_finished", "observation_validated"],
])
def test_incomplete_or_conflicting_step_lifecycle_is_not_complete(tmp_path, kinds):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        for kind in kinds:
            sink.emit(kind, {"trace_id": "trace", "decision_id": "decision:1"})
    report = replay_log(path, format="research-v1")
    assert report.status == ("incomplete" if len(kinds) == 1 else "invalid")


@pytest.mark.parametrize("reference", [{"plan_id": "wrong"}, {"step_index": 9}])
def test_delayed_verification_rejects_conflicting_plan_step(tmp_path, reference):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "hierarchical", "jev"), environ={}) as sink:
        context = {"trace_id": "trace", "decision_id": "decision:1", "session_id": "world"}
        sink.emit("step_started", context)
        action = {**context, "action_id": "action:1", "action": "idle",
                  "parameters": {}, "plan_id": "plan", "step_index": 0}
        sink.emit("action_prepared", action)
        sink.emit("action_returned", {**action, "status": "ok"})
        sink.emit("observation", {**context, "observation_id": "observation:1", "status": "ok"})
        sink.emit("verification", {**action, "observation_id": "observation:1",
                                   "verified": True, **reference})
        sink.emit("step_finished", context)
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert report.decisions[0]["actions"][0]["verified"] is None
    assert any(finding.code == "verification_plan_conflict" for finding in report.findings)
