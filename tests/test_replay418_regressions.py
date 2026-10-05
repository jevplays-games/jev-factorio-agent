"""Bounded PR8 replay regressions using offline, hash-sealed evidence only."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_replay import Capture, codes
from jev_factorio.replay import ReplayInputError, ReplayReport, cli, replay_log
from jev_factorio.replay_causal import audit_producer


_DEFAULT_PLAN_STEPS = object()


def _producer_run(tmp_path, name, *, action="mine", parameters=None,
                  result_decision="d1", result_trace="trace",
                  result_session="world-1", result_action=None,
                  result_parameters=None, verification_decision="d1",
                  verification_trace="trace", verification_session="world-1",
                  observation_trace=None,
                  verification_observation="o2", verification_attempt="attempt-1",
                  verification_origin=None, verification_plan=None,
                  verification_step=None, plan=False, plan_action=None,
                  plan_parameters=None, plan_steps=_DEFAULT_PLAN_STEPS,
                  prepared_step=0, include_plan_commit=True, expiry=None):
    from jev_factorio.research_log import ResearchLog, RunConfiguration

    parameters = {} if parameters is None else parameters
    path = tmp_path / name
    with ResearchLog(path, RunConfiguration("mock", "hierarchical", "jev"), environ={}) as sink:
        before = {"trace_id": "trace", "decision_id": "d1", "session_id": "world-1",
                  "factorio_tick": 10}
        sink.emit("step_started", before)
        sink.emit("observation", {**before, "observation_id": "o1", "status": "ok",
                                   "state": {"tick": 10}})
        selection = {**before, "observation_id": "o1", "model_called": False}
        if plan:
            selection["plan_id"] = "p1"
            sink.emit("decision", {**selection, "action": None})
            steps = (plan_steps if plan_steps is not _DEFAULT_PLAN_STEPS else
                     [{"action": action if plan_action is None else plan_action,
                       "parameters": parameters if plan_parameters is None else plan_parameters}])
            if include_plan_commit:
                sink.emit("plan_committed", {**before, "plan_id": "p1",
                    "plan": {"id": "p1", "steps": steps}})
        else:
            selection["action"] = action
            sink.emit("decision", selection)
        prepared = {**before, "action_id": "a1", "action": action,
                    "parameters": parameters, "attempt_id": "attempt-1",
                    "plan_id": "p1" if plan else None,
                    "step_index": prepared_step if plan else None}
        sink.emit("action_prepared", prepared)
        result = {**prepared, "trace_id": result_trace,
                  "decision_id": result_decision, "session_id": result_session,
                  "action": action if result_action is None else result_action,
                  "parameters": parameters if result_parameters is None else result_parameters,
                  "status": "ok", "outcome": {"accepted": True}}
        sink.emit("action_returned", result)
        if verification_decision != "d1":
            next_context = {"trace_id": verification_trace,
                            "decision_id": verification_decision,
                            "session_id": verification_session,
                            "factorio_tick": 11}
            sink.emit("step_finished", {**before})
            sink.emit("step_started", next_context)
            sink.emit("observation", {**next_context, "observation_id": "o2",
                                       "status": "ok", "state": {"tick": 11}})
            sink.emit("decision", {**next_context, "observation_id": "o2",
                                    "action": None, "model_called": False})
        else:
            next_context = {"trace_id": verification_trace,
                            "decision_id": verification_decision,
                            "session_id": verification_session,
                            "factorio_tick": 11}
            if verification_observation == "o2":
                sink.emit("observation", {**next_context,
                                           "trace_id": observation_trace or next_context["trace_id"],
                                           "observation_id": "o2",
                                           "status": "ok", "state": {"tick": 11}})
        if expiry is not None:
            sink.emit("pending_expired", {**next_context, "action_id": "a1",
                "action_origin": expiry.get("action_origin", "current_trace"),
                "attempt_id": expiry.get("attempt_id", "attempt-1"),
                "plan_id": "p1" if plan else None,
                "step_index": prepared_step if plan else None,
                "polls": 2, "timeout_ticks": 1, **expiry.get("payload", {})})
        verify = {**next_context, "action_id": "a1",
                  "observation_id": verification_observation, "verified": True}
        if verification_attempt is not None:
            verify["attempt_id"] = verification_attempt
        if verification_origin is not None:
            verify["action_origin"] = verification_origin
        if plan:
            verify.update(plan_id="p1" if verification_plan is None else verification_plan,
                          step_index=prepared_step if verification_step is None else verification_step)
        sink.emit("verification", verify)
        sink.emit("step_finished", next_context)
    return path


def _research_manifest_run(tmp_path, name="manifest-run"):
    from jev_factorio.research_log import ResearchLog, RunConfiguration

    path = tmp_path / name
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        context = {"trace_id": "trace", "decision_id": "d1", "session_id": "world-1"}
        sink.emit("step_started", context)
        sink.emit("observation", {**context, "observation_id": "o1", "status": "ok"})
        sink.emit("step_finished", context)
    return path


def _reseal_manifest(path, manifest):
    from jev_factorio.research_log import INTEGRITY_SCHEMA, canonical_bytes, digest

    (path / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    rows = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    previous = digest(manifest)
    for event in rows:
        if event["event_type"] == "run_started":
            assert set(event["payload"]) == {"manifest_hash"}
            event["payload"]["manifest_hash"] = previous
        event["prev_hash"] = previous
        event["event_hash"] = digest({key: value for key, value in event.items()
                                       if key != "event_hash"})
        previous = event["event_hash"]
    (path / "events.jsonl").write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in rows))
    seal = {"schema": INTEGRITY_SCHEMA, "schema_version": 1, "run_id": rows[0]["run_id"],
            "manifest_hash": digest(manifest), "event_count": len(rows),
            "final_event_hash": previous}
    (path / "integrity.json").write_bytes(canonical_bytes(seal) + b"\n")


@pytest.mark.parametrize("status", ["aborted", "abstained", "verified_without_dispatch", "dispatched"])
def test_decision_finished_rejects_later_causal_evidence(tmp_path, status):
    capture = Capture()
    capture.decision(model=False)
    start = next(i for i, event in enumerate(capture.events)
                 if event["event_type"] == "plan_committed")
    tail = deepcopy(capture.events[start:])
    capture.events = capture.events[:start]
    capture.add("decision_finished", {"status": status, "reason": "Observed terminal boundary"}, "d1")
    capture.add("model_request", {"observation_id": "d1:before", "candidate_set_id": "d1:candidates",
                                  "state": {}, "questions": {}}, "d1", model_call_id="late-model")
    capture.add("model_response", {"answers": {}}, "d1", model_call_id="late-model")
    capture.events.extend(tail)
    capture.finish()

    report = replay_log(capture.write(tmp_path / status))
    assert report.status == "invalid"
    assert "post_terminal_evidence" in codes(report)
    assert report.decisions[0]["actions"] == []


@pytest.mark.parametrize("status", ["aborted", "abstained", "verified_without_dispatch", "dispatched"])
def test_decision_finished_positive_boundaries_are_preserved(tmp_path, status):
    capture = Capture()
    capture.decision(model=False)
    if status in {"aborted", "abstained"}:
        capture.events = [event for event in capture.events if event["event_type"] not in {
            "plan_committed", "action_prepared", "action_dispatched", "action_returned", "verification"}]
        if status == "abstained":
            selected = capture.of_type("decision")["payload"]
            selected["plan_id"] = None
            selected["action"] = None
    elif status == "verified_without_dispatch":
        capture.events = [event for event in capture.events if event["event_type"] not in {
            "action_prepared", "action_dispatched", "action_returned", "verification"}]
        capture.add("observation", {"observation_id": "done", "state": {}}, "d1")
        capture.add("verification", {"scope": "plan", "plan_id": "gather",
                                      "observation_id": "done", "verified": True}, "d1")
    capture.add("decision_finished", {"status": status, "reason": "Captured terminal"}, "d1")
    capture.finish()
    report = replay_log(capture.write(tmp_path / f"valid-{status}"))
    assert report.status == "complete", report.to_dict()["findings"]
    assert report.decisions[0]["termination"]["payload"]["status"] == status


def test_terminal_does_not_close_a_later_decision(tmp_path):
    capture = Capture()
    capture.decision("d1", model=False)
    start = next(i for i, event in enumerate(capture.events)
                 if event["event_type"] == "plan_committed")
    capture.events = capture.events[:start]
    capture.add("decision_finished", {"status": "aborted", "reason": "Precondition changed"}, "d1")
    capture.decision("d2", model=False)
    capture.finish()
    report = replay_log(capture.write(tmp_path / "next-decision"))
    assert report.status == "complete", report.to_dict()["findings"]
    assert len(report.decisions) == 2


def test_decision_finished_cannot_be_repeated(tmp_path):
    capture = Capture()
    capture.decision(model=False)
    start = next(i for i, event in enumerate(capture.events)
                 if event["event_type"] == "plan_committed")
    capture.events = capture.events[:start]
    capture.add("decision_finished", {"status": "aborted", "reason": "Terminal"}, "d1")
    capture.add("decision_finished", {"status": "aborted", "reason": "Duplicate"}, "d1")
    capture.finish()
    report = replay_log(capture.write(tmp_path / "duplicate-terminal"))
    assert report.status == "invalid"
    assert "post_terminal_evidence" in codes(report)


def test_payload_only_decision_identity_cannot_evade_terminal_filter(tmp_path):
    capture = Capture()
    capture.decision(model=False)
    start = next(i for i, event in enumerate(capture.events)
                 if event["event_type"] == "plan_committed")
    capture.events = capture.events[:start]
    capture.add("decision_finished", {"status": "aborted", "reason": "Terminal"}, "d1")
    capture.add("action_prepared", {"decision_id": "d1", "action": "mine",
                                    "parameters": {}, "action_id": "late"},
                action_id="late")
    capture.finish()
    report = replay_log(capture.write(tmp_path / "payload-only-decision"))
    assert report.status == "invalid"
    assert "post_terminal_evidence" in codes(report)
    assert report.decisions[0]["actions"] == []


def test_terminal_decision_identity_is_scoped_to_its_segment(tmp_path):
    capture = Capture()
    capture.decision("d1", model=False)
    start = next(i for i, event in enumerate(capture.events)
                 if event["event_type"] == "plan_committed")
    capture.events = capture.events[:start]
    capture.add("decision_finished", {"status": "aborted", "reason": "Segment ended"}, "d1")
    capture.segment = "segment-2"
    capture.add("segment_started", {"previous_segment_id": "segment-1"})
    capture.decision("d1", model=False)
    capture.finish()
    report = replay_log(capture.write(tmp_path / "new-segment"))
    assert report.status == "complete", report.to_dict()["findings"]
    assert report.segments == ["segment-1", "segment-2"]
    assert [frame["decision_id"] for frame in report.decisions] == ["d1", "d1"]


def test_producer_same_decision_action_verification_is_a_positive_control(tmp_path):
    path = _producer_run(tmp_path, "same-decision")
    report = replay_log(path, format="research-v1")
    assert report.integrity["status"] == "verified_source"
    assert report.decisions[0]["actions"][0]["verified"] is True
    assert "unknown_candidate_reference" in codes(report)
    assert report.status == "incomplete"


@pytest.mark.parametrize("kwargs,expected_finding", [
    ({"result_decision": "d2"}, "action_decision_conflict"),
    ({"result_trace": "other-trace"}, "invalid_action_reference"),
    ({"result_session": "other-world"}, "action_session_conflict"),
    ({"result_action": "different"}, "action_result_conflict"),
    ({"result_parameters": {"count": 9}}, "action_result_conflict"),
])
def test_conflicting_action_return_is_not_attributed(tmp_path, kwargs, expected_finding):
    path = _producer_run(tmp_path, f"action-return-{expected_finding}", **kwargs)
    report = replay_log(path, format="research-v1")
    action = report.decisions[0]["actions"][0]
    assert expected_finding in codes(report)
    assert action["acknowledgment"] == "unknown"
    assert action["verified"] is None


@pytest.mark.parametrize("kwargs,expected_finding", [
    ({"verification_decision": "d2"}, "verification_decision_conflict"),
    ({"verification_trace": "other-trace"}, "invalid_action_reference"),
    ({"verification_session": "other-world"}, "verification_session_conflict"),
    ({"verification_observation": "missing-observation"}, "invalid_observation_reference"),
    ({"observation_trace": "other-trace"}, "invalid_observation_reference"),
    ({"verification_observation": "o1"}, "stale_verification"),
    ({"verification_attempt": "other-attempt"}, "verification_attempt_conflict"),
])
def test_conflicting_verification_remains_unknown(tmp_path, kwargs, expected_finding):
    path = _producer_run(tmp_path, f"verification-{expected_finding}", **kwargs)
    report = replay_log(path, format="research-v1")
    assert expected_finding in codes(report)
    assert report.decisions[0]["actions"][0]["verified"] is None


def test_correlated_prior_action_verification_across_steps_is_preserved(tmp_path):
    path = _producer_run(tmp_path, "prior-action", verification_decision="d2",
                         verification_origin="current_trace", verification_attempt="attempt-1",
                         plan=True)
    report = replay_log(path, format="research-v1")
    assert report.decisions[0]["actions"][0]["verified"] is True
    assert report.decisions[0]["actions"][0]["acknowledgment"] == "returned"
    assert "unknown_candidate_reference" in codes(report)


@pytest.mark.parametrize("kwargs,finding", [
    ({"prepared_step": 1}, "invalid_plan_step"),
    ({"plan_steps": [None]}, "invalid_plan_step"),
    ({"plan_action": "smelt"}, "plan_step_mismatch"),
    ({"plan_parameters": {"count": 9}}, "plan_step_mismatch"),
])
def test_invalid_committed_plan_step_cannot_be_verified(tmp_path, kwargs, finding):
    path = _producer_run(tmp_path, f"invalid-plan-{finding}-{len(kwargs)}",
                         plan=True, **kwargs)
    report = replay_log(path, format="research-v1")
    action = report.decisions[0]["actions"][0]
    assert finding in codes(report)
    assert action["acknowledgment"] == "unknown"
    assert action["verified"] is None
    assert report.status == "invalid"


@pytest.mark.parametrize("kwargs,finding", [
    ({"plan_steps": "not-a-step-list"}, "unknown_plan_step"),
    ({"include_plan_commit": False}, "unknown_plan_origin"),
])
def test_unavailable_plan_provenance_does_not_overclaim_verification(tmp_path, kwargs, finding):
    path = _producer_run(tmp_path, f"unknown-plan-{finding}", plan=True, **kwargs)
    report = replay_log(path, format="research-v1")
    action = report.decisions[0]["actions"][0]
    assert finding in codes(report)
    assert action["acknowledgment"] == "returned"
    assert action["verified"] is None
    assert report.status == "incomplete"


def test_current_trace_pending_expiry_is_bound_to_the_prepared_attempt(tmp_path):
    path = _producer_run(tmp_path, "pending-expiry", expiry={}, plan=True)
    report = replay_log(path, format="research-v1")
    assert report.decisions[0]["actions"][0]["expiries"]
    assert report.decisions[0]["actions"][0]["verified"] is True


@pytest.mark.parametrize("expiry,expected", [
    ({"attempt_id": "another-attempt"}, "expiry_attempt_conflict"),
    ({"action_origin": "checkpoint_or_external"}, "expiry_origin_conflict"),
    ({"payload": {"session_id": "another-world"}}, "expiry_session_conflict"),
])
def test_pending_expiry_conflict_cannot_be_attributed(tmp_path, expiry, expected):
    path = _producer_run(tmp_path, f"bad-expiry-{expected}", expiry=expiry, plan=True)
    report = replay_log(path, format="research-v1")
    action = report.decisions[0]["actions"][0]
    assert expected in codes(report)
    assert action["expiries"] == []
    assert action["verified"] is None


@pytest.mark.parametrize("action,parameters", [([], {}), ("mine", "bad")])
@pytest.mark.parametrize("plan", [False, True])
def test_malformed_prepared_action_shape_cannot_be_verified(tmp_path, action, parameters, plan):
    path = _producer_run(tmp_path, f"malformed-{plan}-{type(action).__name__}-{type(parameters).__name__}",
                         action=action, parameters=parameters, plan=plan)
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert report.decisions[0]["actions"][0]["verified"] is None
    assert "invalid_prepared_action" in codes(report) or "invalid_prepared_parameters" in codes(report)


def test_direct_producer_audit_rejects_payload_correlation_disagreement():
    def event(kind, payload, sequence, correlation=None):
        return {"event_type": kind, "payload": payload, "sequence": sequence,
                "session_id": payload.get("session_id"),
                "correlation": correlation or {},
                "time": {"factorio_tick": payload.get("factorio_tick")}}

    context = {"trace_id": "trace", "decision_id": "d1", "session_id": "world-1"}
    prepared = {**context, "action_id": "a1", "action": "mine", "parameters": {},
                "attempt_id": "attempt-1"}
    rows = [event("step_started", context, 1),
            event("action_prepared", prepared, 2),
            event("action_returned", {**prepared, "status": "ok"}, 3,
                  {"action_id": "forged-action"}),
            event("verification", {**prepared, "observation_id": "o1", "verified": True}, 4)]
    report = ReplayReport(format="research-v1")
    audit_producer(rows, report)
    assert "correlation_conflict" in codes(report)
    assert report.decisions[0]["actions"][0]["verified"] is None


def test_direct_producer_audit_rejects_out_of_order_action_return():
    def event(kind, payload, sequence):
        return {"event_type": kind, "payload": payload, "sequence": sequence,
                "session_id": payload.get("session_id"), "correlation": {},
                "time": {"factorio_tick": payload.get("factorio_tick")}}

    context = {"trace_id": "trace", "decision_id": "d1", "session_id": "world-1"}
    prepared = {**context, "action_id": "a1", "action": "mine", "parameters": {},
                "attempt_id": "attempt-1"}
    rows = [event("step_started", context, 1),
            event("action_prepared", prepared, 3),
            event("action_returned", {**prepared, "status": "ok"}, 2)]
    report = ReplayReport(format="research-v1")
    audit_producer(rows, report)
    assert "invalid_causal_chronology" in codes(report)
    action = report.decisions[0]["actions"][0]
    assert action["acknowledgment"] == "unknown"
    assert action["result"] is None


@pytest.mark.parametrize("controller", ["flat", "hierarchical"])
def test_actual_offline_cli_lifecycle_events_are_recognized(tmp_path, controller):
    run_dir = tmp_path / f"cli-{controller}"
    source = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source)
    env.pop("TYPESAFE_API_KEY", None)
    env.pop("CLOUDFLARE_API_TOKEN", None)
    policy = "jev"
    mock_flags = ["--mock-model"] if controller == "hierarchical" else []
    completed = subprocess.run([
        sys.executable, "-m", "jev_factorio.main", "--backend", "mock",
        "--controller", controller, "--policy", policy, "--steps", "1",
        "--tick-seconds", "0", *mock_flags, "--run-dir", str(run_dir),
    ], cwd=source.parent, env=env, text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    report = replay_log(run_dir, format="research-v1")
    assert report.integrity["status"] == "verified_source"
    assert "unsupported_causal_event" not in codes(report)
    assert "unknown_causal_scope" not in codes(report)
    assert [event["event_type"] for event in report.to_dict().get("run_evidence", [])] == [
        "controller_initialized", "controller_stopped"]
    assert report.to_dict()["events"]


@pytest.mark.parametrize("kind,payload", [
    ("controller_initialized", {"requested_model": "mock", "model_is_mock": []}),
    ("controller_stopped", {"terminal": "yes", "controller_status": "running"}),
    ("controller_initialized", {"requested_model": "mock", "model_is_mock": True,
                                 "decision_id": "smuggled"}),
])
def test_malformed_or_scoped_lifecycle_events_are_invalid(tmp_path, kind, payload):
    from jev_factorio.research_log import ResearchLog, RunConfiguration

    path = tmp_path / f"bad-{kind}-{len(payload)}"
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        if not (kind == "controller_initialized" and payload.get("decision_id") == "smuggled"):
            sink.emit("controller_initialized", {"requested_model": "mock", "model_is_mock": True})
        sink.emit(kind, payload)
        if kind == "controller_initialized":
            sink.emit("controller_stopped", {"terminal": False, "controller_status": "running"})
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert any(code in codes(report) for code in {
        "invalid_lifecycle_payload", "invalid_lifecycle_scope", "duplicate_controller_initialized"})


@pytest.mark.parametrize("case", ["json", "schema", "field", "value", "identity"])
def test_invalid_research_manifest_is_a_public_input_error_and_cli_code_two(tmp_path, capsys, case):
    path = _research_manifest_run(tmp_path, f"manifest-{case}")
    manifest_path = path / "manifest.json"
    if case == "json":
        manifest_path.write_bytes(b"{\"schema\":")
    else:
        manifest = json.loads(manifest_path.read_text())
        if case == "schema":
            manifest["schema_version"] = 2
        elif case == "field":
            del manifest["provenance"]
        elif case == "value":
            manifest["durability"] = "unsupported"
        elif case == "identity":
            manifest["run_id"] = "00000000-0000-4000-8000-000000000001"
            _reseal_manifest(path, manifest)
        if case != "identity":
            manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True) + "\n")
    before = {item.name: item.read_bytes() for item in path.iterdir()}

    with pytest.raises(ReplayInputError):
        replay_log(path, format="research-v1")
    output = tmp_path / f"must-not-exist-{case}.json"
    assert cli([str(path), "--format", "research-v1", "--output", str(output)]) == 2
    capsys.readouterr()
    assert not output.exists()
    assert before == {item.name: item.read_bytes() for item in path.iterdir()}


def test_matching_research_manifest_identity_is_accepted(tmp_path, capsys):
    path = _research_manifest_run(tmp_path, "manifest-matching-identity")
    before = {item.name: item.read_bytes() for item in path.iterdir()}

    report = replay_log(path, format="research-v1")
    assert report.integrity["status"] == "verified_source"
    assert report.manifest["run_id"] == report.run_id
    assert cli([str(path), "--format", "research-v1"]) in {0, 3}
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["integrity"]["status"] == "verified_source"
    assert rendered["manifest"]["run_id"] == rendered["run_id"]
    assert before == {item.name: item.read_bytes() for item in path.iterdir()}


@pytest.mark.parametrize("damage", ["chain", "seal"])
def test_chain_or_seal_tampering_stays_invalid_code_one_and_inputs_are_unchanged(
        tmp_path, capsys, damage):
    path = _research_manifest_run(tmp_path, f"tampered-{damage}")
    if damage == "chain":
        event_file = path / "events.jsonl"
        rows = event_file.read_text().splitlines()
        event = json.loads(rows[1])
        event["payload"]["observation_id"] = "tampered"
        rows[1] = json.dumps(event, sort_keys=True, separators=(",", ":"))
        event_file.write_text("\n".join(rows) + "\n")
    else:
        seal_path = path / "integrity.json"
        seal = json.loads(seal_path.read_text())
        seal["final_event_hash"] = "sha256:" + "0" * 64
        seal_path.write_text(json.dumps(seal, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True) + "\n")
    before = {item.name: item.read_bytes() for item in path.iterdir()}
    assert cli([str(path), "--format", "research-v1"]) == 1
    capsys.readouterr()
    assert before == {item.name: item.read_bytes() for item in path.iterdir()}
