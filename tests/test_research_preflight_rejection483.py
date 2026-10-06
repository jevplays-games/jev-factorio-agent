"""Core research treatment of explicit, non-mutating connection rejection."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from jev_factorio.backends.errors import ConnectionPreflightRejected
from jev_factorio.replay import replay_log
from jev_factorio.research_evaluation import evaluate_run
from jev_factorio.research_log import ResearchLog, RunConfiguration
from jev_factorio.research_events import EvidenceError, read_run
from jev_factorio.research_reports import TABLE_SCHEMAS, write_report
from test_connection_preflight_controller import controller as make_controller


def run_controller(tmp_path, error):
    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "hierarchical", "deterministic"),
                     environ={}) as sink:
        loop, backend, plan = make_controller(tmp_path, error, sink)
        record = loop.step()
    return path, loop, backend, plan, record


def rewrite_core_events(destination, events):
    """Reseal semantic mutations so tests exercise the consumer, not just hashes."""
    with ResearchLog(destination, RunConfiguration("mock", "hierarchical", "deterministic"),
                     environ={}) as sink:
        for event in events:
            if event["event_type"] in {"run_started", "run_finished"}:
                continue
            payload = deepcopy(event["payload"])
            sink.emit(event["event_type"], payload,
                      factorio_tick=payload.get("factorio_tick"),
                      session_id=payload.get("session_id"))
    return destination


def mutated_public_run(tmp_path, mutation):
    source, _, _, _, _ = run_controller(
        tmp_path / "source", ConnectionPreflightRejected("missing_fluid_port"))
    events = deepcopy(list(read_run(source).events))
    prepared = next(event for event in events if event["event_type"] == "action_prepared")
    returned = next(event for event in events if event["event_type"] == "action_returned")
    rejected = next(event for event in events
                    if event["event_type"] == "connection_preflight_rejected")
    payload = rejected["payload"]

    if mutation == "missing":
        events.remove(rejected)
    elif mutation == "missing_return":
        events.remove(returned)
    elif mutation == "duplicate":
        events.insert(events.index(rejected) + 1, deepcopy(rejected))
    elif mutation == "before_return":
        events.remove(rejected)
        events.insert(events.index(returned), rejected)
    elif mutation == "before_prepare":
        events.remove(rejected)
        events.insert(events.index(prepared), rejected)
    elif mutation == "after_rejection_verification":
        session = payload["session_id"]
        observation_id = "observation:post-rejection"
        observation = {
            "event_type": "observation", "time": {"factorio_tick": payload["factorio_tick"]},
            "payload": {
                "trace_id": payload["trace_id"], "decision_id": payload["decision_id"],
                "observation_id": observation_id, "status": "ok", "session_id": session,
                "world_kind": payload["world_kind"],
                "snapshot": {"session_id": session, "world_kind": payload["world_kind"]},
            },
        }
        verification = {
            "event_type": "verification", "time": {"factorio_tick": payload["factorio_tick"]},
            "payload": {
                "trace_id": payload["trace_id"], "decision_id": payload["decision_id"],
                "observation_id": observation_id, "action_id": payload["action_id"],
                "session_id": session, "world_kind": payload["world_kind"],
                "action_origin": "current_trace", "attempt_id": payload["attempt_id"],
                "plan_id": payload["plan_id"], "step_index": payload["step_index"],
                "started_tick": prepared["payload"]["pending"]["started_tick"],
                "phase": "pending_poll", "predicate": {"action": "factory_connect"},
                "status": "ok", "verified": False,
            },
        }
        events[events.index(rejected) + 1:events.index(rejected) + 1] = [observation, verification]
    else:
        field, value = mutation
        if field == "return_status":
            returned["payload"]["status"] = value
        elif field == "return_role":
            returned["payload"]["role"] = value
        elif field == "return_error":
            returned["payload"]["error"] = value
        elif field == "return_parameters":
            returned["payload"]["parameters"]["source"] = value
        elif field == "factorio_tick":
            payload["factorio_tick"] = value
        elif value is None:
            payload.pop(field, None)
        else:
            payload[field] = value

    destination = tmp_path / "rewritten"
    return rewrite_core_events(destination, events)


@pytest.mark.parametrize("code", [
    "missing_fluid_port", "no_connection_route", "insufficient_connection_materials",
])
def test_public_explicit_rejection_is_separate_from_unverified_and_useful_actions(tmp_path, code):
    path, loop, backend, _, record = run_controller(
        tmp_path, ConnectionPreflightRejected(code))

    result = evaluate_run(path)
    assert record["verified"] is False
    assert backend.calls == 1
    assert loop.memory.pending is None
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "connection_preflight_rejected"
    assert result.integrity["complete"] is True
    assert result.summary["prepared_actions"] == 1
    assert result.summary["returned_actions"] == 1
    assert result.summary["verified_actions"] == 0
    assert result.summary["unverified_actions"] == 0
    assert result.summary["preflight_rejected_actions"] == 1
    assert "connection_preflight_rejected" not in result.summary["uninterpreted_event_types"]
    action = result.tables["actions"][0]
    assert action["action"] == "factory_connect"
    assert action["acknowledged"] is None
    assert action["verified"] is False
    assert action["preflight_rejected"] is True
    assert action["preflight_rejection_code"] == code

    replay = replay_log(path).to_dict()
    assert replay["integrity"]["status"] == "verified_source"
    assert not any(item["code"] == "unverified_action" for item in replay["findings"])

    output = tmp_path / "report"
    write_report([result], output)
    run_row = json.loads((output / "tables" / "runs.jsonl").read_text().splitlines()[0])
    action_row = json.loads((output / "tables" / "actions.jsonl").read_text().splitlines()[0])
    table_schema = json.loads((output / "table_schema.json").read_text())
    assert run_row["prepared_actions"] == 1
    assert run_row["returned_actions"] == 1
    assert run_row["verified_actions"] == 0
    assert run_row["unverified_actions"] == 0
    assert run_row["preflight_rejected_actions"] == 1
    assert action_row["acknowledged"] is None
    assert action_row["verified"] is False
    assert action_row["preflight_rejected"] is True
    assert action_row["preflight_rejection_code"] == code
    report_run = json.loads((output / "summary.json").read_text())["runs"][0]
    assert report_run["verified_actions"] == 0
    assert report_run["unverified_actions"] == 0
    assert report_run["preflight_rejected_actions"] == 1
    assert "preflight_rejected" in TABLE_SCHEMAS["actions"]
    assert "preflight_rejected_actions" in TABLE_SCHEMAS["runs"]
    assert table_schema["schema"] == "jev-factorio.tables.v2"


@pytest.mark.parametrize("error", [ValueError("no route"), TimeoutError("lost reply")])
def test_generic_and_lost_reply_remain_unverified(tmp_path, error):
    path, loop, backend, _, _ = run_controller(tmp_path, error)
    result = evaluate_run(path)
    replay = replay_log(path).to_dict()

    assert backend.calls == 1
    assert loop.memory.pending is not None
    assert not loop.memory.attempt_outcomes
    assert result.summary["unverified_actions"] == 1
    assert result.summary["preflight_rejected_actions"] == 0
    action = result.tables["actions"][0]
    assert action["preflight_rejected"] is False
    assert action["preflight_rejection_code"] is None
    assert any(item["code"] == "unverified_action" for item in replay["findings"])


def test_rejection_subclass_remains_unverified_in_core_report(tmp_path):
    class UntrustedRejection(ConnectionPreflightRejected):
        pass

    path, loop, backend, _, _ = run_controller(
        tmp_path, UntrustedRejection("missing_fluid_port"))
    result = evaluate_run(path)
    replay = replay_log(path).to_dict()

    assert backend.calls == 1
    assert loop.memory.pending is not None
    assert not loop.memory.attempt_outcomes
    assert result.summary["unverified_actions"] == 1
    assert result.summary["preflight_rejected_actions"] == 0
    assert result.tables["actions"][0]["preflight_rejected"] is False
    assert any(item["code"] == "unverified_action" for item in replay["findings"])


def test_preflight_rejection_does_not_enter_paired_work_metrics(tmp_path):
    path, _, _, _, _ = run_controller(
        tmp_path, ConnectionPreflightRejected("missing_fluid_port"))
    result = evaluate_run(path)

    assert result.summary["benchmark_eligible"] is False
    assert result.summary["verified_actions"] == 0
    assert result.summary["preflight_rejected_actions"] == 1


@pytest.mark.parametrize("mutation", [
    "missing_return", "duplicate", "before_return", "before_prepare",
    "after_rejection_verification",
    ("action", "factory_insert"), ("action_id", "action:other"),
    ("trace_id", "trace:other"), ("decision_id", "decision:other"),
    ("plan_id", "plan:other"), ("step_index", 1), ("step_index", True),
    ("attempt_id", "attempt:other"),
    ("observation_id", "observation:other"), ("session_id", "session:other"),
    ("return_role", "mock_clock_advance"),
    ("world_kind", "native"), ("controller", "flat"),
    ("code", "unknown_code"), ("code", ["missing_fluid_port"]), ("code", False),
    ("mutation_started", True), ("mutation_started", 0), ("mutation_started", None),
    ("action_origin", "checkpoint_or_external"), ("action_origin", None),
    ("return_status", "ok"),
    ("return_error", {"category": "other", "http_status": None}),
    ("return_parameters", "other-source"),
    ("factorio_tick", 11), ("code", None), ("attempt_id", None),
])
def test_invalid_or_conflicting_rejection_evidence_never_resolves_action(tmp_path, mutation):
    path = mutated_public_run(tmp_path, mutation)
    # The alternate writer produces a fresh valid hash chain; a semantic failure
    # therefore proves this reducer did not trust the altered rejection.
    assert read_run(path).integrity["complete"] is True
    with pytest.raises(EvidenceError):
        evaluate_run(path)


def test_missing_rejection_remains_unverified_after_resealing(tmp_path):
    path = mutated_public_run(tmp_path, "missing")
    result = evaluate_run(path)

    assert result.summary["unverified_actions"] == 1
    assert result.summary["preflight_rejected_actions"] == 0
    assert result.tables["actions"][0]["preflight_rejected"] is False


def test_rejection_byte_tampering_fails_source_integrity(tmp_path):
    path, _, _, _, _ = run_controller(
        tmp_path, ConnectionPreflightRejected("missing_fluid_port"))
    events_path = path / "events.jsonl"
    before = events_path.read_bytes()
    altered = before.replace(b'missing_fluid_port', b'no_connection_route')
    assert altered != before
    events_path.write_bytes(altered)

    with pytest.raises(EvidenceError):
        evaluate_run(path)


def test_research_report_parquet_exports_rejection_status(tmp_path):
    parquet = pytest.importorskip("pyarrow.parquet", reason="optional Parquet dependency")
    path, _, _, _, _ = run_controller(
        tmp_path / "run", ConnectionPreflightRejected("no_connection_route"))
    result = evaluate_run(path)
    output = tmp_path / "parquet-report"
    write_report([result], output, parquet=True)

    runs = parquet.read_table(output / "tables" / "runs.parquet")
    actions = parquet.read_table(output / "tables" / "actions.parquet")
    assert runs.to_pylist()[0]["prepared_actions"] == 1
    assert runs.to_pylist()[0]["unverified_actions"] == 0
    assert runs.to_pylist()[0]["preflight_rejected_actions"] == 1
    assert actions.to_pylist()[0]["acknowledged"] is None
    assert actions.to_pylist()[0]["verified"] is False
    assert actions.to_pylist()[0]["preflight_rejected"] is True
    assert actions.to_pylist()[0]["preflight_rejection_code"] == "no_connection_route"


def test_research_report_duckdb_exports_rejection_status(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb", reason="optional DuckDB dependency")
    path, _, _, _, _ = run_controller(
        tmp_path / "run", ConnectionPreflightRejected("insufficient_connection_materials"))
    result = evaluate_run(path)
    output = tmp_path / "duckdb-report"
    write_report([result], output)
    monkeypatch.chdir(output)
    with duckdb.connect(":memory:") as connection:
        connection.execute((output / "views.sql").read_text())
        assert connection.execute(
            "SELECT prepared_actions, returned_actions, verified_actions, unverified_actions, "
            "preflight_rejected_actions FROM runs"
        ).fetchall() == [(1, 1, 0, 0, 1)]
        assert connection.execute(
            "SELECT acknowledged, verified, preflight_rejected, preflight_rejection_code "
            "FROM actions"
        ).fetchall() == [(None, False, True, "insufficient_connection_materials")]
