"""Synthetic, offline contract tests; these do not claim native Factorio validation."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from jev_factorio.evaluation import cli, summarize
from jev_factorio.research_events import EvidenceError, MixedTreatmentError, digest, read_run
from jev_factorio.research_evaluation import evaluate_run
from jev_factorio.research_reports import METRICS, TABLE_SCHEMAS, experiment_summary, write_report


def fixture_hash(value):
    # Independent producer fixture: do not use the evaluator's digest/canonical helpers.
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def rewrite(path, manifest, events):
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    previous = None
    for i, event in enumerate(events, 1):
        event["sequence"] = i
        event["time"]["utc"] = (datetime(2026, 9, 21, tzinfo=timezone.utc) + timedelta(seconds=i)).isoformat()
        event["prev_hash"] = previous
        event.pop("event_hash", None)
        if i == 1:
            event["payload"]["manifest_sha256"] = fixture_hash(manifest)
        event["event_hash"] = fixture_hash(event)
        previous = event["event_hash"]
    (path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def make_run(root, run_id="r1", condition="jev", replicate=0, tokens=10):
    path = root / run_id
    path.mkdir()
    manifest = {
        "schema_version": 1, "run_id": run_id, "experiment_id": "exp1", "trial_id": run_id,
        "condition": condition, "replicate": replicate,
        "session_id": "session-" + run_id, "controller": "hierarchical", "policy": "jev",
        "target": "bootstrap_mining", "backend": "mock", "requested_model": "mock-jev",
        "git": {"commit": "a" * 40, "dirty": False, "patch_sha256": None},
        "runtime": {"python": "3.12", "packages_sha256": "sha256:" + "b" * 64},
        "world": {"seed": 7 + replicate, "settings_sha256": "sha256:" + "c" * 64,
                  "initial_save_sha256": "sha256:" + "d" * 64},
    }
    events = []

    def event(kind, payload=None, **correlation):
        events.append({
            "schema": "jev-factorio.event.v1", "run_id": run_id, "segment_id": "seg1",
            "event_type": kind, "time": {"utc": "", "monotonic_ns": len(events) * 1000,
                                           "factorio_tick": len(events) * 60},
            "correlation": correlation, "payload": payload or {},
        })

    event("run_started")
    event("observation", {"state": {"session_id": manifest["session_id"], "world_kind": "mock"}}, observation_id="o0")
    event("model_request", {"requested_model": "mock-jev", "questions": []}, model_call_id="c1", decision_id="d1")
    event("model_response", {"resolved_model": "mock-jev-1", "usage": {"input_tokens": tokens, "output_tokens": 3},
                              "duration_ns": 2_500_000}, model_call_id="c1", decision_id="d1")
    event("decision", {"source": "jev", "action": "mine"}, decision_id="d1", model_call_id="c1")
    event("action_prepared", {"action": "mine"}, action_id="a1", decision_id="d1")
    event("action_returned", {"ok": True, "duration_ns": 1_000_000}, action_id="a1")
    event("observation", {"state": {"inventory": {"iron-ore": 1}}}, observation_id="o1")
    event("verification", {"verified": True}, action_id="a1", observation_id="o1")
    event("goal_completed", {"goal": "bootstrap_mining", "verified": True}, observation_id="o1")
    event("run_finished", {"status": "completed"})
    rewrite(path, manifest, events)
    return path, manifest, events


def test_valid_run_and_independent_hash(tmp_path):
    path, manifest, events = make_run(tmp_path)
    verified = read_run(path)
    assert verified.integrity["head_hash"] == events[-1]["event_hash"]
    assert verified.integrity["manifest_sha256"] == fixture_hash(manifest)
    assert verified.integrity["authenticated"] is False
    result = evaluate_run(path)
    assert result.summary["verified_actions"] == 1
    assert result.summary["input_tokens"] == 10
    assert result.summary["output_tokens"] == 3
    assert result.summary["target_achieved"] is True
    assert result.summary["evidence_class"] == "synthetic"
    assert result.summary["native_victory_event_observed"] is False
    assert result.summary["benchmark_eligible"] is True
    assert result.tables["model_calls"][0]["duration_ms"] == 2.5
    assert summarize(path / "events.jsonl") == result.summary
    assert digest({"b": 2, "a": 1}) == "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"


def test_action_prepared_must_match_decision_action(tmp_path):
    path, manifest, events = make_run(tmp_path)
    prepared = next(event for event in events if event["event_type"] == "action_prepared")
    prepared["payload"]["action"] = "idle"
    rewrite(path, manifest, events)
    with pytest.raises(EvidenceError, match="Prepared action differs from its decision"):
        evaluate_run(path)


@pytest.mark.parametrize("completion_kind", ["model_response", "provider_error"])
def test_model_completion_must_match_request_decision_when_both_are_recorded(
        tmp_path, completion_kind):
    path, manifest, events = make_run(tmp_path)
    completion = next(event for event in events if event["event_type"] == "model_response")
    completion["event_type"] = completion_kind
    completion["correlation"]["decision_id"] = "other-decision"
    if completion_kind == "provider_error":
        completion["payload"] = {"error_type": "TimeoutError"}
    rewrite(path, manifest, events)
    with pytest.raises(EvidenceError, match="Model completion decision differs from its request"):
        evaluate_run(path)


@pytest.mark.parametrize("request_decision,response_decision", [
    (None, None), (None, "d1"), ("d1", None),
])
def test_optional_legacy_model_decision_correlations_remain_supported(
        tmp_path, request_decision, response_decision):
    path, manifest, events = make_run(tmp_path)
    request = next(event for event in events if event["event_type"] == "model_request")
    response = next(event for event in events if event["event_type"] == "model_response")
    for event, decision in ((request, request_decision), (response, response_decision)):
        if decision is None:
            event["correlation"].pop("decision_id", None)
        else:
            event["correlation"]["decision_id"] = decision
    rewrite(path, manifest, events)
    result = evaluate_run(path)
    assert result.summary["model_calls"] == 1
    assert result.summary["decisions"] == 1


@pytest.mark.parametrize("mutation", ["payload", "link", "gap", "sequence_bool", "hash", "schema", "run", "delete", "reorder"])
def test_corrupted_chains_fail(tmp_path, mutation):
    path, manifest, events = make_run(tmp_path)
    if mutation == "payload":
        events[3]["payload"]["usage"]["input_tokens"] = 999
    elif mutation == "link":
        events[3]["prev_hash"] = "sha256:" + "f" * 64
    elif mutation == "gap":
        events[3]["sequence"] = 99
    elif mutation == "sequence_bool":
        events[0]["sequence"] = True
    elif mutation == "hash":
        events[3]["event_hash"] = "sha256:" + "f" * 64
    elif mutation == "schema":
        events[3]["schema"] = "jev-factorio.event.v99"
    elif mutation == "run":
        events[3]["run_id"] = "other"
    elif mutation == "delete":
        del events[3]
    else:
        events[2], events[3] = events[3], events[2]
    (path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(EvidenceError):
        evaluate_run(path, allow_mixed_treatments=True)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999", '"\\ud800"'])
def test_invalid_json_numbers_and_unicode(tmp_path, value):
    path, _, _ = make_run(tmp_path)
    (path / "events.jsonl").write_text('{"bad":' + value + '}\n')
    with pytest.raises(EvidenceError):
        read_run(path)


@pytest.mark.parametrize("tail", [b"", b"\n", b'{"partial":'])
def test_torn_or_blank_lines_rejected(tmp_path, tail):
    path, _, _ = make_run(tmp_path)
    source = path / "events.jsonl"
    data = source.read_bytes()
    source.write_bytes(data[:-1] if tail == b"" else data + tail)
    with pytest.raises(EvidenceError):
        read_run(path)


def test_duplicate_keys_and_utf16_rejected(tmp_path):
    path, _, _ = make_run(tmp_path)
    source = path / "events.jsonl"
    source.write_text('{"sequence":1,"sequence":1}\n')
    with pytest.raises(EvidenceError):
        read_run(path)
    source.write_bytes('{"test":1}\n'.encode("utf-16"))
    with pytest.raises(EvidenceError):
        read_run(path)


def test_modified_manifest_is_rejected(tmp_path):
    path, manifest, _ = make_run(tmp_path)
    manifest["condition"] = "other"
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="manifest"):
        read_run(path)


@pytest.mark.parametrize("field,value", [("schema_version", True), ("replicate", True), ("replicate", -1),
                                         ("pair_id", 99), ("condition", []), ("runtime", [])])
def test_manifest_types_fail_closed(tmp_path, field, value):
    path, manifest, events = make_run(tmp_path)
    manifest[field] = value
    rewrite(path, manifest, events)
    with pytest.raises(EvidenceError):
        evaluate_run(path)


def test_valid_prefix_is_not_a_completed_trial(tmp_path):
    path, _, _ = make_run(tmp_path)
    source = path / "events.jsonl"
    source.write_bytes(b"".join(source.read_bytes().splitlines(keepends=True)[:-1]))
    result = evaluate_run(path)
    assert result.integrity["valid"] and not result.integrity["complete"]
    assert result.summary["terminal_status"] == "incomplete"
    assert not result.summary["benchmark_eligible"]
    report = experiment_summary([result])
    assert report["replicated"][0]["runs_eligible"] == 0
    assert report["exclusions"][0]["reasons"] == ["incomplete_run"]


@pytest.mark.parametrize("kind", ["code_revision_changed", "manual_intervention"])
def test_interventions_rejected_or_excluded(tmp_path, kind):
    path, manifest, events = make_run(tmp_path)
    event = copy.deepcopy(events[-1])
    event.update(event_type=kind, payload={})
    events.insert(-1, event)
    rewrite(path, manifest, events)
    with pytest.raises(MixedTreatmentError):
        evaluate_run(path)
    result = evaluate_run(path, allow_mixed_treatments=True)
    assert result.summary["mixed_treatments"] and not result.summary["benchmark_eligible"]
    assert result.summary["interventions"][kind] == 1


def test_operational_recovery_is_not_a_code_change(tmp_path):
    path, manifest, events = make_run(tmp_path)
    event = copy.deepcopy(events[-1])
    event.update(event_type="operational_repair", payload={})
    events.insert(-1, event)
    rewrite(path, manifest, events)
    result = evaluate_run(path)
    assert not result.summary["mixed_treatments"]
    assert result.summary["interventions"] == {"operational_repair": 1}


@pytest.mark.parametrize("field,value", [("policy", "deterministic"), ("condition", "new"),
                                         ("git", {"commit": "f" * 40, "dirty": False})])
def test_changed_event_provenance_rejected(tmp_path, field, value):
    path, manifest, events = make_run(tmp_path)
    events[-1]["payload"]["provenance"] = {field: value}
    rewrite(path, manifest, events)
    with pytest.raises(MixedTreatmentError):
        evaluate_run(path)


def test_session_mixing_not_overridable(tmp_path):
    path, manifest, events = make_run(tmp_path)
    events[1]["payload"]["state"]["session_id"] = "wrong"
    rewrite(path, manifest, events)
    with pytest.raises(EvidenceError):
        evaluate_run(path, allow_mixed_treatments=True)


def test_duplicate_verification_and_delayed_ack(tmp_path):
    path, manifest, events = make_run(tmp_path)
    events.insert(9, copy.deepcopy(events[8]))
    del events[6]  # Lost acknowledgement; existing observational verification still establishes success.
    rewrite(path, manifest, events)
    result = evaluate_run(path)
    assert result.summary["verified_actions"] == 1
    assert result.summary["returned_actions"] == 0
    assert result.tables["actions"][0]["verification_count"] == 2


@pytest.mark.parametrize("mutation", ["orphan_response", "orphan_result", "old_observation", "orphan_decision",
                                      "duplicate_request", "duplicate_result", "bool_tokens", "negative_duration",
                                      "false_milestone", "duplicate_start", "post_terminal", "unknown_event"])
def test_semantic_errors_even_with_valid_hashes(tmp_path, mutation):
    path, manifest, events = make_run(tmp_path)
    if mutation == "orphan_response":
        events[3]["correlation"]["model_call_id"] = "unknown"
    elif mutation == "orphan_result":
        events[6]["correlation"]["action_id"] = "unknown"
    elif mutation == "old_observation":
        events[8]["correlation"]["observation_id"] = "o0"
    elif mutation == "orphan_decision":
        events[5]["correlation"]["decision_id"] = "unknown"
    elif mutation == "duplicate_request":
        events.insert(3, copy.deepcopy(events[2]))
    elif mutation == "duplicate_result":
        events.insert(7, copy.deepcopy(events[6]))
    elif mutation == "bool_tokens":
        events[3]["payload"]["usage"]["input_tokens"] = True
    elif mutation == "negative_duration":
        events[3]["payload"]["duration_ns"] = -1
    elif mutation == "false_milestone":
        events[9]["payload"]["verified"] = False
    elif mutation == "duplicate_start":
        events.insert(1, copy.deepcopy(events[0]))
    elif mutation == "post_terminal":
        events.append(copy.deepcopy(events[1]))
    else:
        events[9]["event_type"] = "unsupported_future_event"
    rewrite(path, manifest, events)
    with pytest.raises(EvidenceError):
        evaluate_run(path, allow_mixed_treatments=True)


@pytest.mark.parametrize("mode", ["missing", "empty", "provider_error", "output_missing"])
def test_missing_usage_is_not_zero(tmp_path, mode):
    path, manifest, events = make_run(tmp_path)
    if mode == "output_missing":
        events[3]["payload"]["usage"].pop("output_tokens")
    else:
        events[3]["payload"]["usage"] = {} if mode == "empty" else None
    if mode == "provider_error":
        events[3]["event_type"] = "provider_error"
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert not result["token_usage_complete"]
    assert result["output_tokens"] is None
    if mode != "output_missing":
        assert result["input_tokens"] is None and result["input_tokens_recorded"] == 0


def test_acknowledged_is_not_verified(tmp_path):
    path, manifest, events = make_run(tmp_path)
    events = [e for e in events if e["event_type"] not in {"verification", "goal_completed"}]
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert result["verified_actions"] == 0
    assert result["target_achieved"] is None
    assert "completion_without_target_evidence" in result["warnings"]


@pytest.mark.parametrize("backend,world_kind,expected_observed,expected_target", [
    ("mock", "mock", False, True),
    ("mock", "native", False, True),
    ("fle", "native", True, True),
    ("custom", "unknown", False, None),
])
def test_native_victory_never_inferred_for_mock(
        tmp_path, backend, world_kind, expected_observed, expected_target):
    path, manifest, events = make_run(tmp_path)
    manifest.update(backend=backend, world_kind=world_kind, target="rocket_launch")
    events[1]["payload"]["state"].pop("world_kind")
    events[7]["payload"]["state"].update(victory=True, victory_source="native:base-game-rocket-launch")
    events[9]["payload"]["goal"] = "rocket_launch"
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert result["native_victory_event_observed"] is expected_observed
    assert result["milestones"] == ["rocket_launch"]
    assert result["target_achieved"] is expected_target
    if backend == "mock":
        assert result["evidence_class"] == "synthetic"
        assert result["native_victory_event_observed"] is False


def test_nonqualifying_observation_does_not_complete_live_rocket_target(tmp_path):
    path, manifest, events = make_run(tmp_path)
    manifest.update(backend="fle", world_kind="fle", target="rocket_launch")
    for event in events:
        if event["event_type"] == "observation":
            event["payload"]["state"].update(
                session_id=manifest["session_id"], world_kind="fle",
            )
    observation = next(event for event in events if event["event_type"] == "observation"
                       and event["correlation"].get("observation_id") == "o1")
    observation["payload"]["state"].update(
        victory=True, victory_source="modded:rocket-launch",
    )
    next(event for event in events if event["event_type"] == "goal_completed")["payload"]["goal"] = "rocket_launch"
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert result["native_victory_event_observed"] is False
    assert result["milestones"] == ["rocket_launch"]
    assert result["target_achieved"] is None
    assert result["benchmark_eligible"] is False


@pytest.mark.parametrize("backend,world_kind,victory,victory_source,expected", [
    ("fle", "fle", True, "native:base-game-rocket-launch", True),
    ("mock", "mock", True, "native:base-game-rocket-launch", False),
    ("fle", "fle", True, "modded:rocket-launch", False),
    ("fle", "fle", False, "native:base-game-rocket-launch", False),
])
def test_native_victory_observation_does_not_invent_a_milestone(
        tmp_path, backend, world_kind, victory, victory_source, expected):
    path, manifest, events = make_run(tmp_path)
    manifest.update(backend=backend, world_kind=world_kind, target="rocket_launch")
    for event in events:
        if event["event_type"] == "observation":
            event["payload"]["state"].update(
                session_id=manifest["session_id"], world_kind=world_kind,
            )
    observation = next(event for event in events if event["event_type"] == "observation"
                       and event["correlation"].get("observation_id") == "o1")
    observation["payload"]["state"].update(
        world_kind=world_kind, victory=victory, victory_source=victory_source,
    )
    events = [event for event in events if event["event_type"] != "goal_completed"]
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert result["native_victory_event_observed"] is expected
    assert result["milestones"] == []
    assert result["target_achieved"] is None
    assert result["benchmark_eligible"] is False


def test_paired_replicates_use_runs_not_events(tmp_path):
    items = []
    for replicate in (0, 1):
        for condition, tokens in (("A", 10), ("B", 16 + 2 * replicate)):
            path, _, _ = make_run(tmp_path, f"{condition}{replicate}", condition, replicate, tokens)
            items.append(evaluate_run(path))
    report = experiment_summary(items, pair=("A", "B"))
    assert report["paired"]["matched_pairs"] == 2
    assert report["paired"]["metrics"]["input_tokens"]["mean"] == 7
    assert report["paired"]["metrics"]["input_tokens"]["n_total"] == 2
    assert report["paired"]["unmatched"] == []
    assert all(group["runs_total"] == 2 for group in report["replicated"])
    assert report == experiment_summary(list(reversed(items)), pair=("A", "B"))


def test_missing_pairs_and_world_mismatch_remain_visible(tmp_path):
    p1, _, _ = make_run(tmp_path, "a", "A")
    p2, m2, e2 = make_run(tmp_path, "b", "B")
    m2["world"]["initial_save_sha256"] = "sha256:" + "f" * 64
    rewrite(p2, m2, e2)
    p3, _, _ = make_run(tmp_path, "a2", "A", replicate=1)
    report = experiment_summary([evaluate_run(p) for p in (p1, p2, p3)], pair=("A", "B"))
    assert report["paired"]["matched_pairs"] == 0
    assert len(report["paired"]["unmatched"]) == 2
    assert report["paired"]["metrics"]["input_tokens"]["mean"] is None


def test_duplicate_runs_and_experimental_cells_rejected(tmp_path):
    p1, _, _ = make_run(tmp_path, "one")
    p2, _, _ = make_run(tmp_path, "two")
    one, two = evaluate_run(p1), evaluate_run(p2)
    with pytest.raises(EvidenceError, match="Duplicate run_id"):
        experiment_summary([one, one])
    with pytest.raises(EvidenceError, match="Duplicate condition"):
        experiment_summary([one, two])


def test_condition_drift_never_pooled(tmp_path):
    p1, _, _ = make_run(tmp_path, "one")
    p2, m2, e2 = make_run(tmp_path, "two", replicate=1)
    m2["git"]["commit"] = "e" * 40
    rewrite(p2, m2, e2)
    items = [evaluate_run(p1), evaluate_run(p2)]
    with pytest.raises(MixedTreatmentError):
        experiment_summary(items)
    report = experiment_summary(items, allow_mixed_treatments=True)
    assert len(report["replicated"]) == 2
    assert all(group["runs_eligible"] == 0 for group in report["replicated"])


def test_provider_failure_without_model_id_is_not_treatment_drift(tmp_path):
    p1, _, _ = make_run(tmp_path, "one")
    p2, m2, e2 = make_run(tmp_path, "two", replicate=1)
    e2[3].update(event_type="provider_error", payload={})
    rewrite(p2, m2, e2)
    report = experiment_summary([evaluate_run(p1), evaluate_run(p2)])
    assert len(report["replicated"]) == 1
    assert report["replicated"][0]["metrics"]["input_tokens"]["n_missing"] == 1


def test_absent_experiment_metadata_is_not_invented(tmp_path):
    path, manifest, events = make_run(tmp_path)
    del manifest["experiment_id"]
    rewrite(path, manifest, events)
    report = experiment_summary([evaluate_run(path)])
    assert report["replicated"] == []
    assert "missing:experiment_id" in report["exclusions"][0]["reasons"]


def test_reports_are_read_only_deterministic_and_typed(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-sentinel-do-not-read")
    path, _, _ = make_run(tmp_path)
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    result = evaluate_run(path)
    output = tmp_path / "report"
    write_report([result], output)
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before
    for name, schema in TABLE_SCHEMAS.items():
        rows = [json.loads(line) for line in (output / "tables" / f"{name}.jsonl").read_text().splitlines()]
        assert all(set(row) == set(schema) for row in rows)
    assert json.loads((output / "summary.json").read_text())["runs"][0] == result.summary
    assert 'CAST(NULL AS' in (output / "views.sql").read_text()
    assert not any("secret-sentinel" in p.read_text() for p in output.rglob("*") if p.is_file())
    with pytest.raises(EvidenceError, match="already exists"):
        write_report([result], output)
    assert before == {p.name: p.read_bytes() for p in path.iterdir()}
    second = tmp_path / "report2"
    write_report([result], second)
    assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()} == {
        p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()}


def test_atomic_report_failure_does_not_publish(tmp_path, monkeypatch):
    import jev_factorio.research_reports as reports
    path, _, _ = make_run(tmp_path)
    output = tmp_path / "report"
    def fail(*args, **kwargs):
        raise OSError("injected disk failure")
    monkeypatch.setattr(reports, "_write_json", fail)
    with pytest.raises(OSError):
        write_report([evaluate_run(path)], output)
    assert not output.exists()
    assert not list(tmp_path.glob(".research-report-*"))


def test_missing_parquet_dependency_is_explicit(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.startswith("pyarrow"):
            raise ImportError("not installed")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    path, _, _ = make_run(tmp_path)
    with pytest.raises(EvidenceError, match="Parquet export requires"):
        write_report([evaluate_run(path)], tmp_path / "report", parquet=True)
    assert not (tmp_path / "report").exists()


def test_parquet_round_trip(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet", reason="optional Parquet dependency unavailable")
    path, _, _ = make_run(tmp_path)
    output = tmp_path / "report"
    write_report([evaluate_run(path)], output, parquet=True)
    for name, columns in TABLE_SCHEMAS.items():
        table = pq.read_table(output / "tables" / f"{name}.parquet")
        expected = [json.loads(line) for line in (output / "tables" / f"{name}.jsonl").read_text().splitlines()]
        assert table.column_names == list(columns)
        assert table.to_pylist() == expected


def test_duckdb_views_round_trip(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb", reason="optional SQL integration dependency unavailable")
    path, _, _ = make_run(tmp_path)
    output = tmp_path / "report"
    write_report([evaluate_run(path)], output)
    monkeypatch.chdir(output)
    with duckdb.connect() as db:
        db.execute((output / "views.sql").read_text())
        assert db.sql('SELECT verified_actions FROM runs').fetchone() == (1,)
        assert db.sql('SELECT count(*) FROM interventions').fetchone() == (0,)
        assert db.sql('SELECT input_tokens FROM model_calls').fetchone() == (10,)


def test_cli_report_and_nonzero_failure(tmp_path, capsys):
    path, _, _ = make_run(tmp_path)
    output = tmp_path / "report"
    cli([str(path), "--output-dir", str(output)])
    assert json.loads(capsys.readouterr().out)["runs_total"] == 1
    with pytest.raises(SystemExit) as exc:
        cli([str(path), "--output-dir", str(output)])
    assert exc.value.code == 2


def test_legacy_summary_and_cli_compatibility(tmp_path, capsys):
    record = {"session_id": "s", "world_kind": "mock", "target": "mining", "policy": "jev",
              "requested_model": "mock", "controller": "hierarchical", "after_state": {},
              "status": "running", "verified": True, "action": "mine", "model_call": True,
              "usage": {"input_tokens": 5}, "resolved_model": "mock"}
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps(record) + "\n")
    expected = {"session_id": "s", "world_kind": "mock", "target": "mining", "policy": "jev",
                "evidence_class": "synthetic", "terminal_status": "running", "terminal_reason": None,
                "records": 1, "model_calls": 1, "verified_actions": None, "milestones": {},
                "identified_verified_actions": 0, "verified_waits": 0, "identified_attempts": 0,
                "legacy_records_present": True, "legacy_verified_action_records": 1,
                "attempt_count_scope": "Unique IDs present in log, including carried checkpoint outcomes; not a full-campaign total",
                "verification_latency_seconds": [], "unknown_verification_latencies": 0,
                "observed_production_delta": None,
                "native_victory_event_observed": False, "input_tokens": 5,
                "token_usage_complete": True, "models": ["mock"]}
    assert summarize(path) == expected
    cli([str(path)])
    assert json.loads(capsys.readouterr().out) == [expected]
    with pytest.raises(SystemExit):
        cli([str(path), "--output-dir", str(tmp_path / "report")])


@pytest.mark.parametrize("format_kind", ["proposed", "legacy"])
def test_offline_evaluator_never_imports_game_or_provider(tmp_path, format_kind):
    if format_kind == "proposed":
        path, _, _ = make_run(tmp_path)
    else:
        path = tmp_path / "legacy.jsonl"
        path.write_text(json.dumps({
            "session_id": "s", "world_kind": "mock", "target": "mining", "policy": "jev",
            "requested_model": "mock", "controller": "hierarchical", "after_state": {},
            "status": "running", "verified": True, "action": "mine",
        }) + "\n")
    code = '''
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if any(x in name for x in ("backends", "jev_client", "dotenv", "controller", "requests")):
        raise AssertionError("Forbidden runtime import: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from jev_factorio.evaluation import summarize
result = summarize(sys.argv[1])
assert result["verified_actions"] == (1 if sys.argv[2] == "proposed" else None)
'''
    subprocess.run([sys.executable, "-c", code, str(path), format_kind], check=True, timeout=15)


def test_segment_restart_requires_provenance_not_clock_continuity(tmp_path):
    path, manifest, events = make_run(tmp_path)
    segment = copy.deepcopy(events[-1])
    segment.update(event_type="segment_started", segment_id="seg2",
                   payload={"provenance": {key: manifest.get(key) for key in
                                           ("git", "controller", "policy", "target", "requested_model")}})
    segment["time"]["monotonic_ns"] = 0  # A different process/boot clock is not comparable.
    events.insert(-1, segment)
    events[-1]["segment_id"] = "seg2"
    rewrite(path, manifest, events)
    assert evaluate_run(path).summary["segments"] == 2
    events[-2]["payload"] = {}
    rewrite(path, manifest, events)
    with pytest.raises(EvidenceError, match="Segment transition"):
        evaluate_run(path)


def test_repeated_session_not_an_independent_replicate(tmp_path):
    p1, m1, _ = make_run(tmp_path, "one")
    p2, m2, e2 = make_run(tmp_path, "two", replicate=1)
    m2["session_id"] = m1["session_id"]
    e2[1]["payload"]["state"]["session_id"] = m1["session_id"]
    rewrite(p2, m2, e2)
    with pytest.raises(EvidenceError, match="Repeated world session"):
        experiment_summary([evaluate_run(p1), evaluate_run(p2)])


def test_mixed_resolved_models_rejected_within_and_between_runs(tmp_path):
    p1, m1, e1 = make_run(tmp_path, "one")
    second_request = copy.deepcopy(e1[2])
    second_response = copy.deepcopy(e1[3])
    for event in (second_request, second_response):
        event["correlation"] = {"model_call_id": "c2"}
    second_response["payload"]["resolved_model"] = "different-revision"
    e1[-1:-1] = [second_request, second_response]
    rewrite(p1, m1, e1)
    with pytest.raises(MixedTreatmentError, match="resolved_model"):
        evaluate_run(p1)
    e1[-3:-1] = []
    rewrite(p1, m1, e1)
    p2, m2, e2 = make_run(tmp_path, "two", replicate=1)
    e2[3]["payload"]["resolved_model"] = "different-revision"
    rewrite(p2, m2, e2)
    with pytest.raises(MixedTreatmentError, match="condition label"):
        experiment_summary([evaluate_run(p1), evaluate_run(p2)])


def test_waits_are_not_work_actions(tmp_path):
    path, manifest, events = make_run(tmp_path)
    events[5]["payload"]["action"] = "wait_for_research"
    events[4]["payload"]["action"] = "wait_for_research"
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert result["verified_actions"] == 0 and result["verified_waits"] == 1


def test_pending_request_has_unknown_usage_and_ineligible_run(tmp_path):
    path, manifest, events = make_run(tmp_path)
    request = copy.deepcopy(events[2])
    request["correlation"] = {"model_call_id": "pending"}
    events.insert(-1, request)
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert result["model_calls"] == 2
    assert result["input_tokens"] is None and result["input_tokens_recorded"] == 10
    assert "unresolved_model_requests" in result["warnings"]


def test_regressing_utc_does_not_create_negative_latency(tmp_path):
    path, _, events = make_run(tmp_path)
    events[-1]["time"]["utc"] = "2025-01-01T00:00:00+00:00"
    events[-1].pop("event_hash")
    events[-1]["event_hash"] = fixture_hash(events[-1])
    (path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    result = evaluate_run(path).summary
    assert result["wall_elapsed_seconds"] is None
    assert "wall_clock_regressed" in result["warnings"]


def test_no_model_requests_have_known_zero_usage(tmp_path):
    path, manifest, events = make_run(tmp_path)
    events = [e for e in events if e["event_type"] not in {"model_request", "model_response"}]
    decision = next(e for e in events if e["event_type"] == "decision")
    decision["correlation"].pop("model_call_id")
    decision["payload"]["source"] = "deterministic"
    rewrite(path, manifest, events)
    result = evaluate_run(path).summary
    assert result["model_calls"] == 0 and result["token_usage_complete"]
    assert result["input_tokens"] == 0 and result["output_tokens"] == 0


def test_missing_pair_usage_has_null_delta(tmp_path):
    p1, _, _ = make_run(tmp_path, "a", "A")
    p2, m2, e2 = make_run(tmp_path, "b", "B")
    e2[3]["payload"]["usage"] = None
    rewrite(p2, m2, e2)
    report = experiment_summary([evaluate_run(p1), evaluate_run(p2)], pair=("A", "B"))
    assert report["paired"]["matched_pairs"] == 1
    assert report["paired"]["metrics"]["input_tokens"]["n_missing"] == 1
    assert report["paired"]["metrics"]["input_tokens"]["mean"] is None


def test_cli_rejects_mixed_legacy_and_events(tmp_path):
    path, _, _ = make_run(tmp_path)
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text('{"controller":"hierarchical"}\n')
    with pytest.raises(SystemExit) as error:
        cli([str(path), str(legacy)])
    assert error.value.code == 2


def test_export_cannot_replace_evidence_directory(tmp_path):
    path, _, _ = make_run(tmp_path)
    before = (path / "events.jsonl").read_bytes()
    with pytest.raises(EvidenceError, match="already exists"):
        write_report([evaluate_run(path)], path)
    assert (path / "events.jsonl").read_bytes() == before
