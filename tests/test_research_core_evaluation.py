"""Consumer acceptance using production writers and offline controllers."""
import hashlib
import json
from copy import deepcopy

import pytest

from jev_factorio.backends.mock import MockBackend
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.jev_client import MockJevClient
from jev_factorio.loop import AgentLoop
from jev_factorio.research_evaluation import evaluate_run
from jev_factorio.research_events import EvidenceError, read_run
from jev_factorio.research_log import ResearchLog, RunConfiguration
from jev_factorio.research_reports import TABLE_SCHEMAS, write_report
from jev_factorio.skills import Step


def writer(path, controller="flat", policy="jev", target="bootstrap_mining"):
    return ResearchLog(
        path, RunConfiguration(backend="mock", controller=controller,
                               policy=policy, target=target, mock_model=True,
                               requested_model=MockJevClient.model),
        environ={},
    )


def native_observation_run(path, *, world_kind="fle", victory=True,
                           victory_source="native:base-game-rocket-launch",
                           complete_goal=False):
    """Build synthetic evaluator evidence; this is not a native-game claim."""
    backend = "mock" if world_kind == "mock" else "fle"
    config = RunConfiguration(
        backend=backend, controller="hierarchical", policy="deterministic",
        target="rocket_launch", mock_model=(backend == "mock"),
        requested_model=MockJevClient.model if backend == "mock" else None,
    )
    session_id, trace_id, decision_id, observation_id = (
        "session-fixture", "trace-fixture", "decision-fixture", "observation-fixture")
    snapshot = {
        "session_id": session_id, "world_kind": world_kind,
        "victory": victory, "victory_source": victory_source,
    }
    with ResearchLog(path, config, environ={}) as log:
        log.emit("step_started", {"trace_id": trace_id, "decision_id": decision_id})
        log.emit("observation", {
            "trace_id": trace_id, "decision_id": decision_id,
            "observation_id": observation_id, "status": "ok",
            "session_id": session_id, "world_kind": world_kind, "snapshot": snapshot,
        }, session_id=session_id, factorio_tick=60)
        if complete_goal:
            log.emit("goal_checked", {
                "trace_id": trace_id, "decision_id": decision_id,
                "observation_id": observation_id, "goal": "rocket_launch",
                "status": "ok", "completed": True,
            }, session_id=session_id, factorio_tick=60)
            log.emit("goal_completed", {
                "trace_id": trace_id, "decision_id": decision_id,
                "observation_id": observation_id, "goal": "rocket_launch",
                "verification_source": "existing_goal_predicate",
            }, session_id=session_id, factorio_tick=60)
        log.emit("step_finished", {"trace_id": trace_id, "decision_id": decision_id})
    return evaluate_run(path)


def captured_files(path):
    return {entry.name: entry.read_bytes() for entry in path.iterdir() if entry.is_file()}


def test_core_lifecycle_preserves_original_evidence_and_unknown_identity(tmp_path):
    path = tmp_path / "run"
    with writer(path):
        pass
    before = captured_files(path)
    verified = read_run(path)
    result = evaluate_run(path)
    manifest = json.loads(before["manifest.json"])
    events = [json.loads(line) for line in before["events.jsonl"].splitlines()]
    seal = json.loads(before["integrity.json"])
    assert verified.manifest == manifest
    assert list(verified.events) == events
    assert verified.integrity["head_hash"] == seal["final_event_hash"]
    assert verified.integrity["manifest_sha256"] == seal["manifest_hash"]
    for name, field in (("manifest.json", "manifest_file_sha256"),
                        ("events.jsonl", "events_file_sha256"),
                        ("integrity.json", "seal_file_sha256")):
        assert verified.integrity[field] == "sha256:" + hashlib.sha256(before[name]).hexdigest()
    assert result.summary["source_format"] == "logging-core-v1"
    assert result.summary["complete"] is True
    assert result.summary["terminal_status"] == "returned"
    assert result.summary["target_achieved"] is None
    assert result.summary["benchmark_eligible"] is False
    assert result.integrity["authenticated"] is False
    assert result.summary["input_tokens"] is None
    assert result.summary["output_tokens"] is None
    assert result.summary["token_usage_complete"] is False
    assert result.summary["input_tokens_recorded"] == 0
    assert result.summary["output_tokens_recorded"] == 0
    for field in ("session_id", "world_kind", "world_seed", "initial_save_sha256",
                  "world_settings_sha256", "condition", "experiment_id", "replicate"):
        assert result.summary[field] is None
    assert result.summary["evidence_class"] == "unknown"
    assert [row["event_hash"] for row in result.tables["events"]] == [
        event["event_hash"] for event in events
    ]
    assert captured_files(path) == before


def test_unsealed_real_producer_is_incomplete(tmp_path):
    path = tmp_path / "run"
    log = writer(path)
    log.close()
    result = evaluate_run(path)
    assert result.summary["complete"] is False
    assert result.summary["terminal_status"] == "incomplete"
    assert result.summary["target_achieved"] is None
    assert not (path / "integrity.json").exists()


@pytest.mark.parametrize("outcome", ["error", "interrupted"])
def test_sealed_failure_lifecycle_is_not_target_failure_evidence(tmp_path, outcome):
    path = tmp_path / "run"
    with writer(path) as log:
        log.finish(outcome, error_type="RuntimeError" if outcome == "error" else "KeyboardInterrupt")
    result = evaluate_run(path)
    assert result.summary["complete"] is True
    assert result.summary["terminal_status"] == outcome
    assert result.summary["target_achieved"] is None
    assert result.summary["benchmark_eligible"] is False


def test_core_unicode_uses_original_ascii_canonical_hashes(tmp_path):
    path = tmp_path / "run"
    with writer(path) as log:
        log.emit("checkpoint_written", {"label": "café雪"})
    verified = read_run(path)
    event = verified.events[1]
    unhashed = {key: value for key, value in event.items() if key != "event_hash"}
    utf8 = json.dumps(unhashed, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    ascii_bytes = json.dumps(unhashed, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True).encode("ascii")
    assert event["event_hash"] == "sha256:" + hashlib.sha256(ascii_bytes).hexdigest()
    assert event["event_hash"] != "sha256:" + hashlib.sha256(utf8).hexdigest()
    assert evaluate_run(path).tables["events"][1]["event_hash"] == event["event_hash"]


def test_core_requires_original_manifest_directory(tmp_path):
    path = tmp_path / "run"
    with writer(path):
        pass
    relocated = tmp_path / "manifest.json"
    relocated.write_bytes((path / "manifest.json").read_bytes())
    with pytest.raises(EvidenceError, match="original run-directory layout"):
        evaluate_run(path / "events.jsonl", manifest_path=relocated)


@pytest.mark.parametrize("world_kind,victory,victory_source,expected", [
    ("fle", True, "native:base-game-rocket-launch", True),
    ("mock", True, "native:base-game-rocket-launch", False),
    ("fle", True, "modded:rocket-launch", False),
    ("fle", False, "native:base-game-rocket-launch", False),
])
def test_observed_native_victory_is_separate_from_goal_completion(
        tmp_path, world_kind, victory, victory_source, expected):
    result = native_observation_run(
        tmp_path / "run", world_kind=world_kind, victory=victory,
        victory_source=victory_source,
    )
    assert result.summary["native_victory_event_observed"] is expected
    assert result.summary["milestones"] == []
    assert result.summary["target_achieved"] is None
    assert result.summary["benchmark_eligible"] is False


def test_qualified_native_victory_milestone_completes_rocket_target(tmp_path):
    result = native_observation_run(tmp_path / "run", complete_goal=True)
    assert result.summary["native_victory_event_observed"] is True
    assert result.summary["milestones"] == ["rocket_launch"]
    assert result.summary["target_achieved"] is True
    assert result.summary["benchmark_eligible"] is False


def test_nonqualifying_observation_does_not_complete_rocket_milestone(tmp_path):
    result = native_observation_run(
        tmp_path / "run", victory_source="modded:rocket-launch", complete_goal=True,
    )
    assert result.summary["native_victory_event_observed"] is False
    assert result.summary["milestones"] == ["rocket_launch"]
    assert result.summary["target_achieved"] is None
    assert result.summary["benchmark_eligible"] is False


@pytest.mark.parametrize("mutation", ["payload", "reorder", "truncate", "manifest", "seal"])
def test_real_producer_mutations_fail_closed(tmp_path, mutation):
    path = tmp_path / "run"
    with writer(path) as log:
        log.emit("checkpoint_written", {"label": "original"})
    if mutation in {"manifest", "seal"}:
        filename = "manifest.json" if mutation == "manifest" else "integrity.json"
        document = json.loads((path / filename).read_bytes())
        if mutation == "manifest":
            document["configuration"]["target"] = "rocket_launch"
        else:
            document["final_event_hash"] = "sha256:" + "0" * 64
        (path / filename).write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    else:
        lines = (path / "events.jsonl").read_bytes().splitlines(keepends=True)
        if mutation == "payload":
            lines[1] = lines[1].replace(b"original", b"modified")
        elif mutation == "reorder":
            lines[0], lines[1] = lines[1], lines[0]
        else:
            lines.pop()
        (path / "events.jsonl").write_bytes(b"".join(lines))
    with pytest.raises(EvidenceError):
        evaluate_run(path)


def test_verifier_checks_captured_bytes_despite_source_aba(tmp_path, monkeypatch):
    from jev_factorio import research_log

    path = tmp_path / "run"
    with writer(path) as log:
        log.emit("checkpoint_written", {"label": "original"})
    events_path = path / "events.jsonl"
    valid_bytes = events_path.read_bytes()
    invalid_bytes = valid_bytes.replace(b"original", b"modified")
    assert invalid_bytes != valid_bytes
    events_path.write_bytes(invalid_bytes)
    actual_verify = research_log.verify_run
    verifier_paths = []

    def verify_during_valid_source_window(provided_path, **kwargs):
        verifier_paths.append(provided_path)
        events_path.write_bytes(valid_bytes)
        try:
            return actual_verify(provided_path, **kwargs)
        finally:
            events_path.write_bytes(invalid_bytes)

    monkeypatch.setattr(research_log, "verify_run", verify_during_valid_source_window)
    with pytest.raises(EvidenceError, match="Core evidence verification failed"):
        evaluate_run(path)
    assert len(verifier_paths) == 1
    assert events_path.read_bytes() == invalid_bytes


def test_sealed_step_completion_requires_step_start(tmp_path):
    path = tmp_path / "run"
    with writer(path) as log:
        log.emit("step_finished", {"trace_id": "trace-without-start",
                                   "decision_id": "decision:1", "controller": "flat"})
    assert read_run(path).integrity["valid"] is True
    with pytest.raises(EvidenceError, match="completion lacks its unique start"):
        evaluate_run(path)


@pytest.mark.parametrize("ambiguous_return", [False, True])
def test_actual_pending_poll_can_verify_prior_step_dispatch(tmp_path, ambiguous_return):
    from jev_factorio.causal_trace import CausalTrace

    path = tmp_path / "run"
    backend = MockBackend()
    pending = {"started_tick": 0, "action": "walk_to_coal"}

    def dispatch_operation():
        outcome = backend.act("walk_to_coal")
        if ambiguous_return:
            raise RuntimeError("dispatch acknowledgment unavailable")
        return outcome

    with writer(path) as log:
        trace = CausalTrace(log, "flat")
        trace.begin_step()
        trace.observe(backend, "before_decision")
        trace.emit("decision", {"source": "deterministic", "action": "walk_to_coal"})
        if ambiguous_return:
            with pytest.raises(RuntimeError, match="acknowledgment unavailable"):
                trace.dispatch(dispatch_operation, "walk_to_coal", plan_id="plan",
                               step_index=0, pending=pending)
        else:
            trace.dispatch(dispatch_operation, "walk_to_coal", plan_id="plan",
                           step_index=0, pending=pending)
        trace.emit("step_finished", {})
        trace.begin_step()
        snapshot = trace.observe(backend, "pending_poll")
        assert trace.verify(Step("walk_to_coal", "near", item="coal"), snapshot,
                            plan_id="plan", index=0, pending=pending,
                            phase="pending_poll") is True
        trace.emit("step_finished", {})
    result = evaluate_run(path)
    assert result.summary["verified_actions"] == 1
    assert result.summary["returned_actions"] == 1
    assert result.tables["actions"][0]["acknowledged"] is None
    assert ("verification_after_ambiguous_return" in result.summary["warnings"]) == ambiguous_return


@pytest.mark.parametrize("post_observation", [False, True])
def test_actual_verifier_requires_post_dispatch_observation(tmp_path, post_observation):
    from jev_factorio.causal_trace import CausalTrace

    path = tmp_path / "run"
    backend = MockBackend()
    pending = {"started_tick": 0, "action": "walk_to_coal"}
    with writer(path) as log:
        trace = CausalTrace(log, "flat")
        trace.begin_step()
        before = trace.observe(backend, "before_decision")
        trace.emit("decision", {"source": "deterministic", "action": "walk_to_coal"})
        trace.dispatch(lambda: backend.act("walk_to_coal"), "walk_to_coal",
                       plan_id="plan", step_index=0, pending=pending)
        snapshot = trace.observe(backend, "after_action") if post_observation else before
        step = Step("walk_to_coal", "near", item="coal")
        for _ in range(2):
            trace.verify(step, snapshot, plan_id="plan", index=0,
                         pending=pending, phase="after_action")
    assert read_run(path).integrity["valid"] is True
    if post_observation:
        result = evaluate_run(path)
        assert result.summary["verified_actions"] == 1
        assert result.tables["actions"][0]["verification_count"] == 2
    else:
        with pytest.raises(EvidenceError, match="post-dispatch observation"):
            evaluate_run(path)


@pytest.mark.parametrize("mutation", [
    "verification_before_return", "wrong_return_decision",
    "wrong_verification_decision", "ambiguous_error_return",
])
def test_sealed_causal_reference_mutations_are_semantic_errors(tmp_path, mutation):
    from jev_factorio.causal_trace import CausalTrace

    original = tmp_path / "original"
    backend = MockBackend()
    pending = {"started_tick": 0, "action": "walk_to_coal"}
    with writer(original) as log:
        trace = CausalTrace(log, "flat")
        trace.begin_step()
        trace.observe(backend, "before_decision")
        trace.emit("decision", {"source": "deterministic", "action": "walk_to_coal"})
        trace.dispatch(lambda: backend.act("walk_to_coal"), "walk_to_coal",
                       plan_id="plan", step_index=0, pending=pending)
        snapshot = trace.observe(backend, "after_action")
        trace.verify(Step("walk_to_coal", "near", item="coal"), snapshot,
                     plan_id="plan", index=0, pending=pending, phase="after_action")
    assert evaluate_run(original).summary["verified_actions"] == 1
    events = deepcopy(list(read_run(original).events)[1:-1])
    returned = next(event for event in events if event["event_type"] == "action_returned")
    verification = next(event for event in events if event["event_type"] == "verification")
    if mutation == "verification_before_return":
        events.remove(returned)
        events.append(returned)
    elif mutation == "wrong_return_decision":
        returned["payload"]["decision_id"] = "decision:unrelated"
    elif mutation == "wrong_verification_decision":
        verification["payload"]["decision_id"] = "decision:unrelated"
    else:
        returned["payload"]["status"] = "error"
        returned["payload"]["error"] = {"category": "timeout", "http_status": None}
        returned["payload"].pop("outcome", None)
    rewritten = tmp_path / "rewritten"
    with writer(rewritten) as log:
        for event in events:
            log.emit(event["event_type"], event["payload"])
    assert read_run(rewritten).integrity["valid"] is True
    with pytest.raises(EvidenceError):
        evaluate_run(rewritten)


def test_actual_causal_trace_model_dispatch_observation_and_goal(tmp_path):
    from jev_factorio.causal_trace import CausalTrace

    path = tmp_path / "run"
    backend = MockBackend()
    with writer(path) as log:
        trace = CausalTrace(log, "flat")
        trace.begin_step()
        trace.observe(backend, "before_decision")
        trace.client(MockJevClient()).evaluate({}, {"next_action": {
            "type": "choice", "criteria": {"walk_to_coal": "Walk to coal"},
        }})
        trace.emit("decision", {"source": "jev", "action": "walk_to_coal"})
        trace.dispatch(lambda: backend.act("walk_to_coal"), "walk_to_coal")
        trace.observe(backend, "after_action")
        trace.call("goal_checked", lambda: True, details={"goal": "bootstrap_mining"},
                   result=lambda completed: {"completed": completed})
        trace.emit("goal_completed", {"goal": "bootstrap_mining",
                                      "verification_source": "existing_goal_predicate"})
    result = evaluate_run(path)
    assert result.summary["observations"] == 2
    assert result.summary["model_calls"] == 1
    assert result.summary["prepared_actions"] == 1
    assert result.summary["returned_actions"] == 1
    assert result.summary["verified_actions"] == 0
    assert result.summary["target_achieved"] is True
    assert result.summary["evidence_class"] == "synthetic"
    assert result.summary["native_victory_event_observed"] is False
    assert result.summary["input_tokens"] is None
    assert result.summary["output_tokens"] is None
    assert result.tables["model_calls"][0]["resolved_model"] is None
    assert result.tables["actions"][0]["acknowledged"] is None


def test_causal_goal_without_predicate_check_is_rejected(tmp_path):
    from jev_factorio.causal_trace import CausalTrace

    path = tmp_path / "run"
    with writer(path) as log:
        trace = CausalTrace(log, "flat")
        trace.begin_step()
        trace.observe(MockBackend(), "before_decision")
        trace.emit("goal_completed", {"goal": "bootstrap_mining",
                                      "verification_source": "existing_goal_predicate"})
    assert read_run(path).integrity["valid"] is True
    with pytest.raises(EvidenceError):
        evaluate_run(path)


@pytest.mark.parametrize("controller", ["flat", "hierarchical"])
def test_actual_controller_runs_reduce_without_fabricated_success(tmp_path, controller):
    path = tmp_path / "run"
    backend, client = MockBackend(), MockJevClient()
    with writer(path, controller=controller) as log:
        options = {"research_log": log, "tick_seconds": 0,
                   "log_file": str(tmp_path / "legacy.jsonl")}
        loop = (AgentLoop(backend, client, **options) if controller == "flat" else
                HierarchicalLoop(backend, client, policy="jev", target="bootstrap_mining",
                                 checkpoint=str(tmp_path / "checkpoint.json"), **options))
        for _ in range(40):
            if getattr(loop, "terminal", False):
                break
            loop.step()
    result = evaluate_run(path)
    assert result.summary["complete"] is True
    assert result.summary["session_id"] == backend.session_id
    assert result.summary["world_kind"] == "mock"
    assert result.summary["evidence_class"] == "synthetic"
    assert result.summary["benchmark_eligible"] is False
    assert result.summary["observations"] > 0
    assert result.summary["prepared_actions"] > 0
    assert result.summary["native_victory_event_observed"] is False
    assert result.summary["terminal_status"] == "returned"
    if controller == "hierarchical":
        assert result.summary["target_achieved"] is True
        assert result.summary["verified_actions"] > 0
        assert "bootstrap_mining" in result.summary["milestones"]
    else:
        assert result.summary["target_achieved"] is None


def test_real_producer_export_round_trip(tmp_path):
    parquet = pytest.importorskip("pyarrow.parquet", reason="optional Parquet dependency")
    path = tmp_path / "run"
    with writer(path):
        pass
    before = captured_files(path)
    result = evaluate_run(path)
    output = tmp_path / "report"
    write_report([result], output, parquet=True)
    assert json.loads((output / "summary.json").read_bytes())["runs"][0] == result.summary
    for name, columns in TABLE_SCHEMAS.items():
        table = parquet.read_table(output / "tables" / f"{name}.parquet")
        expected = [json.loads(line) for line in
                    (output / "tables" / f"{name}.jsonl").read_text().splitlines()]
        assert table.column_names == list(columns)
        assert table.to_pylist() == expected
    assert captured_files(path) == before


def test_real_producer_duckdb_views(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb", reason="optional DuckDB dependency")
    path = tmp_path / "run"
    with writer(path):
        pass
    result = evaluate_run(path)
    output = tmp_path / "report"
    write_report([result], output)
    monkeypatch.chdir(output)
    with duckdb.connect(":memory:") as connection:
        connection.execute((output / "views.sql").read_text())
        assert connection.execute(
            "SELECT session_id, world_kind, target_achieved FROM runs"
        ).fetchall() == [(None, None, None)]
        assert connection.execute("SELECT event_hash FROM events ORDER BY sequence").fetchall() == [
            (event["event_hash"],) for event in read_run(path).events
        ]
