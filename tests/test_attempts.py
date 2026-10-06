import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import requests

from jev_factorio.backends.fle import FleBackend
from jev_factorio.backends.native_factory import NativeFactory
from jev_factorio.memory import CampaignMemory
from jev_factorio.skills import Plan, Step
from jev_factorio.telemetry import error_code, make_attempt, phase, utc_now, validate_attempt

from attempt_helpers import ReceiptBackend, controller, install_plan


def load(path):
    return CampaignMemory.load(path, "receipt-session", "bootstrap_mining")


def prepared_native_transfer_checkpoint(tmp_path, backend):
    """Persist a valid FLE transfer interrupted between approach and its RPC."""
    loop = controller(tmp_path, backend)
    snapshot = loop._observe()
    step = Step(
        "factory_insert", "transfer",
        costs={"automation-science-pack": 20},
        parameters={
            "role": "utility:lab", "item": "automation-science-pack", "quantity": 20,
            "receipt": "transfer:preserved",
        },
    )
    plan = Plan("same-plan-id", "bootstrap_mining", "Preserved native transfer", (step,))
    loop.memory.active_goal = "bootstrap_mining"
    loop.memory.active_plan = plan.to_dict()
    loop.memory.step_index = 0
    loop.memory.reserve(plan.id, step.costs, snapshot.inventory)
    loop.memory.pending = {
        "started_tick": snapshot.tick, "polls": 0, "action": step.action, "dispatch": "prepared",
    }
    loop.memory.attempt = make_attempt(
        snapshot.session_id, loop.target, loop.memory.active_plan, 0, loop.memory.pending,
        process_id=loop._process_id, unit_number=7,
    )
    loop.memory.attempt["dispatch_phases"] = {
        "dispatch": {
            "stage": "dispatch", "status": "started", "at_utc": utc_now(),
            "seconds": None, "error_code": None,
        },
        "approach": {
            "stage": "approach", "status": "started", "at_utc": utc_now(),
            "seconds": None, "error_code": None,
        },
    }
    loop._save()
    return load(tmp_path / "checkpoint.json")


def test_attempt_is_saved_before_dispatch_and_bound_to_exact_step(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    original = backend.execute

    def execute(action, parameters):
        saved = load(tmp_path / "checkpoint.json")
        assert saved.pending["dispatch"] == "prepared"
        assert saved.attempt["action"] == action
        assert saved.attempt["receipt"] == parameters["receipt"]
        assert saved.attempt["expected_unit_number"] == 7
        assert saved.attempt["dispatch_phases"]["dispatch"]["status"] == "started"
        return original(action, parameters)

    monkeypatch.setattr(backend, "execute", execute)
    loop = controller(tmp_path, backend)
    record = loop.step()
    outcome = record["attempt_outcomes"][0]
    assert record["verified"]
    assert outcome["action"] == "factory_insert" and outcome["outcome"] == "verified"
    assert outcome["latency_seconds"] >= 0
    assert loop.memory.attempt is loop.memory.pending is None
    assert not any("attempt" in event for event in loop.memory.history)
    assert load(tmp_path / "checkpoint.json").attempt_outcomes == [outcome]


def test_cancelled_before_dispatch_requires_exact_non_entry_witness():
    from copy import deepcopy

    base = make_attempt(
        "receipt-session", "bootstrap_mining",
        {"id": "cancel-plan", "steps": [{"action": "walk_to_coal"}]},
        0, {"started_tick": 0}, process_id="a" * 32)
    phase = {
        "stage": "dispatch", "status": "failed", "at_utc": utc_now(),
        "seconds": 0.0, "error_code": "cancelled_before_entry",
    }
    valid = {
        **base, "dispatch_phases": {"dispatch": phase},
        "outcome": "cancelled_before_dispatch", "finished_tick": 0,
        "finished_at_utc": utc_now(), "latency_seconds": 0.0,
    }
    validate_attempt(valid, finished=True)

    for status, code, extra_stage in [
        ("started", None, None),
        ("returned", None, None),
        ("failed", "cancelled_before_entry", "approach"),
        ("failed", "execution", None),
    ]:
        forged = deepcopy(valid)
        forged_phase = {**phase, "status": status,
                        "error_code": code,
                        "seconds": None if status == "started" else 0.0}
        forged["dispatch_phases"] = {"dispatch": forged_phase}
        if extra_stage:
            forged["dispatch_phases"][extra_stage] = {
                "stage": extra_stage, "status": "started", "at_utc": utc_now(),
                "seconds": None, "error_code": None,
            }
        with pytest.raises(ValueError, match="Invalid attempt outcome"):
            validate_attempt(forged, finished=True)



@pytest.mark.parametrize("mode", ["delayed", "lost_ack", "lost_observation", "interrupt"])
def test_late_verification_survives_resume_without_replay(monkeypatch, tmp_path, mode):
    backend = ReceiptBackend(mode)
    install_plan(monkeypatch, backend)
    first = controller(tmp_path, backend)
    if mode == "lost_observation":
        with pytest.raises(TimeoutError):
            first.step()
        assert load(tmp_path / "checkpoint.json").attempt["observation_error"]["error_code"] == "timeout"
    elif mode == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            first.step()
    else:
        assert not first.step()["verified"]
    saved = load(tmp_path / "checkpoint.json")
    if mode == "delayed":
        backend.publish()
    resumed = controller(tmp_path, backend, resume=True)
    result = resumed.step()
    assert result["verified"] and result["action"] == "verify"
    assert len(backend.calls) == 1
    outcome = result["attempt_outcomes"][0]
    assert outcome["id"] == saved.attempt["id"]
    assert outcome["process_id"] != result["process_id"]
    assert outcome["latency_seconds"] is None
    assert load(tmp_path / "checkpoint.json").pending is None
    assert "secret" not in (tmp_path / "checkpoint.json").read_text()


@pytest.mark.parametrize("mode", ["no_effect", "partial"])
def test_unresolved_receipt_stays_pending_and_never_replays(monkeypatch, tmp_path, mode):
    backend = ReceiptBackend(mode)
    install_plan(monkeypatch, backend)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first.step()
    identity = first.memory.attempt["id"]
    assert first.step()["status"] == "uncertain"
    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    assert resumed.step()["status"] == "uncertain"
    assert resumed.memory.attempt["id"] == identity
    assert not resumed.memory.attempt_outcomes
    assert len(backend.calls) == 1


def test_ambiguous_native_partial_receipt_reconciles_without_replay(monkeypatch, tmp_path):
    """Only an exact, live FLE receipt may turn a partial transfer into a replan."""
    backend = ReceiptBackend("partial")
    backend.state.world_kind = "fle"
    backend.state.factory["player_bound"] = True
    install_plan(monkeypatch, backend)
    original_execute = backend.execute

    def execute_then_report_partial(action, parameters):
        original_execute(action, parameters)
        raise RuntimeError("native transfer reported a partial result")

    monkeypatch.setattr(backend, "execute", execute_then_report_partial)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first.step()
    backend.state.factory["receipts"]["transfer:0"]["tick"] = backend.state.tick
    saved = load(tmp_path / "checkpoint.json")
    assert saved.pending["dispatch"] == "ambiguous"
    assert saved.reservations == {"same-plan-id": {}}
    assert backend.state.factory["receipts"]["transfer:0"]["quantity"] == 10

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["action"] == "reconcile" and result["status"] == "running"
    assert "without replaying" in result["outcome"]
    assert len(backend.calls) == 1
    assert resumed.memory.pending is resumed.memory.active_plan is resumed.memory.attempt is None
    assert resumed.memory.failures == {"same-plan-id": 1}
    outcome = resumed.memory.attempt_outcomes[-1]
    assert outcome["id"] == saved.attempt["id"]
    assert outcome["outcome"] == "partial_transfer_reconciled"
    event = next(event for event in resumed.memory.history
                 if event["kind"] == "partial_transfer_reconciled")
    assert event["receipt"] == "transfer:0"
    assert event["requested_quantity"] == 20 and event["transferred_quantity"] == 10
    reloaded = load(tmp_path / "checkpoint.json")
    assert reloaded.pending is reloaded.active_plan is reloaded.attempt is None
    assert reloaded.attempt_outcomes[-1] == outcome


def test_ambiguous_native_zero_receipt_reconciles_only_with_retained_source(monkeypatch, tmp_path):
    """Zero transfer receipts need exact FLE evidence and retained source material."""
    backend = ReceiptBackend("zero")
    backend.state.world_kind = "fle"
    backend.state.factory["player_bound"] = True
    install_plan(monkeypatch, backend, reserve_transfer=True)
    original_execute = backend.execute

    def execute_then_report_zero(action, parameters):
        original_execute(action, parameters)
        raise RuntimeError("native transfer reported no destination capacity")

    monkeypatch.setattr(backend, "execute", execute_then_report_zero)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first.step()
    backend.state.factory["receipts"]["transfer:0"]["tick"] = backend.state.tick
    saved = load(tmp_path / "checkpoint.json")
    assert saved.pending["dispatch"] == "ambiguous"
    assert saved.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert backend.state.inventory["automation-science-pack"] == 20
    assert backend.state.factory["receipts"]["transfer:0"]["quantity"] == 0

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["action"] == "reconcile" and result["status"] == "running"
    assert "zero of requested 20" in result["outcome"]
    assert len(backend.calls) == 1
    assert resumed.memory.pending is resumed.memory.active_plan is resumed.memory.attempt is None
    assert resumed.memory.failures == {"same-plan-id": 1}
    outcome = resumed.memory.attempt_outcomes[-1]
    assert outcome["id"] == saved.attempt["id"]
    assert outcome["outcome"] == "zero_effect_transfer_reconciled"
    event = next(event for event in resumed.memory.history
                 if event["kind"] == "zero_effect_transfer_reconciled")
    assert event["requested_quantity"] == 20 and event["transferred_quantity"] == 0
    reloaded = load(tmp_path / "checkpoint.json")
    assert reloaded.pending is reloaded.active_plan is reloaded.attempt is None
    assert reloaded.attempt_outcomes[-1] == outcome


@pytest.mark.parametrize("change", ["source_not_retained", "actor_not_bound"])
def test_ambiguous_native_zero_receipt_stays_pending_without_all_exact_evidence(
    monkeypatch, tmp_path, change
):
    backend = ReceiptBackend("zero")
    backend.state.world_kind = "fle"
    backend.state.factory["player_bound"] = True
    install_plan(monkeypatch, backend, reserve_transfer=True)
    original_execute = backend.execute

    def execute_then_report_zero(action, parameters):
        original_execute(action, parameters)
        raise RuntimeError("native transfer reported no destination capacity")

    monkeypatch.setattr(backend, "execute", execute_then_report_zero)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first.step()
    backend.state.factory["receipts"]["transfer:0"]["tick"] = backend.state.tick
    saved = load(tmp_path / "checkpoint.json")
    if change == "source_not_retained":
        backend.state.inventory["automation-science-pack"] = 19
    else:
        backend.state.factory["player_bound"] = False

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["status"] == "uncertain"
    assert resumed.memory.attempt["id"] == saved.attempt["id"]
    assert resumed.memory.reservations == saved.reservations
    assert len(backend.calls) == 1


@pytest.mark.parametrize("change", [
    "mock_world", "returned_dispatch", "wrong_entity", "wrong_receipt",
])
def test_partial_transfer_reconciliation_fails_closed_without_exact_evidence(
    monkeypatch, tmp_path, change
):
    backend = ReceiptBackend("partial")
    backend.state.world_kind = "fle"
    backend.state.factory["player_bound"] = True
    install_plan(monkeypatch, backend)
    original_execute = backend.execute

    def execute_then_report_partial(action, parameters):
        original_execute(action, parameters)
        raise RuntimeError("native transfer reported a partial result")

    monkeypatch.setattr(backend, "execute", execute_then_report_partial)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first.step()
    backend.state.factory["receipts"]["transfer:0"]["tick"] = backend.state.tick
    saved = load(tmp_path / "checkpoint.json")
    receipt = backend.state.factory["receipts"]["transfer:0"]
    if change == "mock_world":
        backend.state.world_kind = "mock"
    elif change == "returned_dispatch":
        saved.pending["dispatch"] = "returned"
    elif change == "wrong_entity":
        backend.state.factory["entities"]["utility:lab"]["unit_number"] = 8
    else:
        receipt["role"] = "other"
    saved.save(tmp_path / "checkpoint.json")

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["status"] == "uncertain"
    assert resumed.memory.attempt["id"] == saved.attempt["id"]
    assert resumed.memory.reservations == saved.reservations
    assert len(backend.calls) == 1


def test_resumed_prepared_native_transfer_reuses_retained_action_only_before_rpc(
    monkeypatch, tmp_path
):
    """A pre-RPC interruption may retry the same fair native transfer once.

    The synthetic backend is FLE-shaped only to exercise the recovery gate; it
    is not native-game evidence.  The test's explicit phase trace models the
    durable NativeFactory ordering: approach begins before transfer_rpc, and a
    process loss before the latter cannot have removed inventory or invoked the
    Lua transfer endpoint.
    """
    backend = ReceiptBackend()
    backend.state.world_kind = "fle"
    backend.state.factory["player_bound"] = True
    saved = prepared_native_transfer_checkpoint(tmp_path, backend)
    assert saved.pending["dispatch"] == "prepared"
    assert saved.attempt["dispatch_phases"]["dispatch"]["status"] == "started"
    assert saved.attempt["dispatch_phases"]["approach"]["status"] == "started"
    assert "transfer_rpc" not in saved.attempt["dispatch_phases"]
    assert saved.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert backend.calls == []

    native_path = []
    def execute_traced(action, parameters, trace):
        native_path.append((action, deepcopy(parameters)))
        return backend.execute(action, parameters)
    backend.execute_traced = execute_traced
    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["verified"] is True
    assert result["outcome"].startswith("Re-dispatched retained transfer")
    assert len(backend.calls) == 1
    assert native_path == backend.calls
    action, parameters = backend.calls[0]
    assert action == "factory_insert"
    assert parameters["receipt"] == saved.attempt["receipt"]
    assert backend.state.inventory["automation-science-pack"] == 0
    assert backend.state.factory["receipts"][parameters["receipt"]]["unit_number"] == 7
    assert resumed.memory.pending is None
    recovery = next(event for event in resumed.memory.history
                    if event["kind"] == "prepared_transfer_recovery_authorized")
    assert recovery["attempt_id"] == saved.attempt["id"]
    assert recovery["original_dispatch_phases"] == saved.attempt["dispatch_phases"]


@pytest.mark.parametrize("change", [
    "ambiguous_dispatch", "transfer_rpc_started", "actor_not_bound",
    "machine_changed", "source_not_retained",
])
def test_prepared_transfer_recovery_fails_closed_without_all_native_evidence(
    monkeypatch, tmp_path, change
):
    backend = ReceiptBackend("no_effect")
    backend.state.world_kind = "fle"
    backend.state.factory["player_bound"] = True
    saved = prepared_native_transfer_checkpoint(tmp_path, backend)
    if change == "ambiguous_dispatch":
        saved.pending["dispatch"] = "ambiguous"
    elif change == "transfer_rpc_started":
        saved.attempt["dispatch_phases"]["transfer_rpc"] = {
            "stage": "transfer_rpc", "status": "started", "at_utc": utc_now(),
            "seconds": None, "error_code": None,
        }
    elif change == "actor_not_bound":
        backend.state.factory["player_bound"] = False
    elif change == "machine_changed":
        backend.state.factory["entities"]["utility:lab"]["unit_number"] = 8
    else:
        backend.state.inventory["automation-science-pack"] = 19
    saved.save(tmp_path / "checkpoint.json")

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.pending == {**saved.pending, "polls": saved.pending["polls"] + 1}
    assert resumed.memory.reservations == saved.reservations
    assert backend.calls == []


def test_distinct_attempts_can_share_plan_id(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    first = loop.step()
    backend.state.inventory["automation-science-pack"] = 20
    second = loop.step()
    assert len(second["attempt_outcomes"]) == 2
    a, b = second["attempt_outcomes"]
    assert a["plan_id"] == b["plan_id"] and a["id"] != b["id"]
    assert a["receipt"] != b["receipt"]
    assert len(first["attempt_outcomes"]) == 1  # No mutable alias into live memory.


def legacy_checkpoint(monkeypatch, tmp_path):
    backend = ReceiptBackend("delayed")
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    loop.step()
    data = asdict(loop.memory)
    data["version"] = 1
    del data["attempt"], data["attempt_outcomes"]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(data))
    return path, json.loads(path.read_text())


def test_legacy_migration_preserves_pending_and_is_read_only(monkeypatch, tmp_path):
    path, original = legacy_checkpoint(monkeypatch, tmp_path)
    raw = path.read_bytes()
    first, second = load(path), load(path)
    assert path.read_bytes() == raw
    assert first == second and first.version == 2
    assert first.attempt["origin"] == "legacy"
    assert first.attempt["started_at_utc"] is first.attempt["process_id"] is None
    for key, value in original.items():
        if key != "version":
            assert asdict(first)[key] == value
    first.save(tmp_path / "migrated.json")
    assert load(tmp_path / "migrated.json") == first
    original["pending"]["polls"] += 1
    path.write_text(json.dumps(original))
    assert load(path).attempt["id"] == first.attempt["id"]


@pytest.mark.parametrize("key,value", [
    ("id", "bad"), ("step_index", True), ("step_sha256", "0" * 64),
    ("receipt", "other"), ("expected_unit_number", True), ("started_tick", -1),
    ("process_id", "not-a-process"), ("origin", "invented"), ("extra", "secret"),
])
def test_bad_attempt_checkpoint_is_rejected_without_reset(monkeypatch, tmp_path, key, value):
    backend = ReceiptBackend("delayed")
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    loop.step()
    data = asdict(loop.memory)
    data["attempt"][key] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        load(path)
    assert path.read_bytes() == before


def test_missing_attempt_duplicate_outcome_and_unknown_schema_rejected(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    loop.step()
    data = asdict(loop.memory)
    for patch in ({"version": 3}, {"attempt_outcomes": data["attempt_outcomes"] * 2},
                  {"attempt": data["attempt_outcomes"][0]}):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({**data, **patch}))
        with pytest.raises(ValueError):
            load(path)


def test_checkpoint_failure_prevents_dispatch(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    original = CampaignMemory.save

    def fail_prepared(memory, path):
        if memory.pending is not None:
            raise OSError("disk full")
        return original(memory, path)

    monkeypatch.setattr(CampaignMemory, "save", fail_prepared)
    with pytest.raises(OSError):
        controller(tmp_path, backend).step()
    assert backend.calls == []


@pytest.mark.parametrize("failed_stage", [None, "approach", "transfer_rpc"])
def test_native_transfer_tracing_preserves_stages_and_arguments(monkeypatch, failed_stage):
    native = object.__new__(NativeFactory)  # Never initialize the live adapter.
    native.backend = SimpleNamespace(_tools=object())
    operations, events = [], []

    def execute_stage(stage, *arguments):
        operations.append((stage, arguments))
        if stage == failed_stage:
            raise RuntimeError("Bearer secret password and raw Lua body")
        return SimpleNamespace(position="position")

    native.approach_role = lambda role: execute_stage("approach", role)
    native.call = lambda *args: execute_stage("transfer_rpc", *args)
    backend = FleBackend()
    backend._factory = native
    parameters = {"role": "lab", "item": "pack", "quantity": 20, "receipt": "original-receipt"}
    if failed_stage:
        with pytest.raises(RuntimeError):
            backend.execute_traced("factory_insert", parameters, events.append)
        assert events[-1]["stage"] == failed_stage and events[-1]["status"] == "failed"
        assert events[-1]["error_code"] == "execution"
    else:
        backend.execute_traced("factory_insert", parameters, events.append)
        assert operations[-1] == ("transfer_rpc", ("transfer", "lab", "pack", 20, "original-receipt", False))
        assert operations[0] == ("approach", ("lab",))
        assert [e["stage"] for e in events if e["status"] == "returned"] == ["approach", "transfer_rpc"]
    assert "secret" not in json.dumps(events)


@pytest.mark.parametrize("error,code", [
    (TimeoutError("secret"), "timeout"), (requests.Timeout("secret"), "timeout"),
    (requests.ConnectionError("secret"), "connection"), (requests.HTTPError("secret"), "http"),
    (ValueError("secret"), "invalid_data"), (OSError("secret"), "io"),
    (KeyboardInterrupt("secret"), "interrupted"), (RuntimeError("secret"), "execution"),
])
def test_error_codes_never_include_raw_payloads(error, code):
    assert error_code(error) == code
    events = []
    with pytest.raises(type(error)):
        with phase("dispatch", events.append):
            raise error
    assert events[-1]["error_code"] == code
    assert "secret" not in json.dumps(events)


def test_phases_are_diagnostic_and_not_postconditions(monkeypatch, tmp_path):
    backend = ReceiptBackend("no_effect")
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    result = loop.step()
    assert result["attempt"]["dispatch_phases"]["dispatch"]["status"] == "returned"
    assert not result["verified"]
    assert not loop.memory.attempt_outcomes


def test_wait_timeout_is_separate_from_verified_mutations(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend, action="factory_wait")
    backend.execute = lambda *args: "wait"
    loop = controller(tmp_path, backend, max_pending_polls=1)
    loop.step()
    result = loop.step()
    outcome = result["attempt_outcomes"][0]
    assert outcome["action"] == "factory_wait" and outcome["outcome"] == "wait_expired"
    assert loop.memory.pending is loop.memory.attempt is None
    validate_attempt(outcome, finished=True)


def test_checkpoint_completion_survives_gap_before_jsonl_write(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)

    def crash_record(*args, **kwargs):
        loop._save()
        raise OSError("synthetic crash after checkpoint, before JSONL")

    monkeypatch.setattr(loop, "_record", crash_record)
    with pytest.raises(OSError):
        loop.step()
    saved = load(tmp_path / "checkpoint.json")
    assert saved.pending is None and len(saved.attempt_outcomes) == 1
    assert not (tmp_path / "run.jsonl").exists()
    # A terminal snapshot makes the next iteration read-only; the saved outcome
    # is carried into the new log even though the original JSONL write was lost.
    backend.state.drill_output_connected = True
    backend.state.drill_status = "working"
    backend.state.placed_entities = ["burner-mining-drill"]
    backend.state.iron_ore_collected = 5
    resumed = controller(tmp_path, backend, resume=True)
    result = resumed.step()
    assert result["attempt_outcomes"] == saved.attempt_outcomes
    assert len(backend.calls) == 1


def test_native_substage_is_durable_before_its_operation(monkeypatch, tmp_path):
    backend = ReceiptBackend("no_effect")
    install_plan(monkeypatch, backend)
    native = object.__new__(NativeFactory)
    native.backend = SimpleNamespace(_tools=object())
    approaches = []

    def approach(role):
        approaches.append(role)
        saved = load(tmp_path / "checkpoint.json")
        assert saved.attempt["dispatch_phases"]["approach"]["status"] == "started"
        assert saved.pending["dispatch"] == "prepared"
        raise RuntimeError("secret failure")

    native.approach_role = approach
    native.call = lambda *args: pytest.fail("transfer must not be reached")
    backend.execute_traced = lambda action, parameters, trace: native.execute(action, parameters, trace=trace)
    result = controller(tmp_path, backend).step()
    assert approaches == ["utility:lab"]
    saved = load(tmp_path / "checkpoint.json")
    assert saved.attempt["dispatch_phases"]["approach"]["status"] == "failed"
    assert "transfer_rpc" not in saved.attempt["dispatch_phases"]
    assert saved.pending["dispatch"] == "ambiguous" and not result["verified"]


@pytest.mark.parametrize("policy", ["jev", "deterministic", "hybrid"])
def test_mock_bootstrap_retains_progress_and_separates_waits(tmp_path, policy):
    from jev_factorio.backends.mock import MockBackend
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.evaluation import summarize
    from jev_factorio.jev_client import MockJevClient

    loop = HierarchicalLoop(MockBackend(), jev=MockJevClient(), policy=policy,
                            target="bootstrap_mining", tick_seconds=0,
                            checkpoint=str(tmp_path / "mock.json"), log_file=str(tmp_path / "mock.jsonl"))
    loop.run(steps=40)
    summary = summarize(tmp_path / "mock.jsonl")
    assert loop.memory.status == "completed"
    assert summary["model_calls"] == (0 if policy == "deterministic" else 4)
    assert summary["verified_actions"] == 5 and summary["verified_waits"] == 1
    assert summary["observed_production_delta"] is None
    assert not summary["native_victory_event_observed"]
