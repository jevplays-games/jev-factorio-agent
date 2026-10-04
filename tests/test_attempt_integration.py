import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from jev_factorio.background import BackgroundMemory, BackgroundWorkLoop
from jev_factorio.diagnostics import reconciliation_report
from jev_factorio.memory import load_checkpoint
from jev_factorio.skills import Plan, Step
from jev_factorio.supervisor import Supervisor, SupervisorConfig, atomic_json
from jev_factorio.telemetry import make_attempt, phase
from test_background_work import ReceiptBackend, controller


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, TimeoutError])
def test_primary_failure_survives_failed_diagnostic_write(error_type):
    original = error_type("original")

    def trace(event):
        if event["status"] == "failed":
            raise OSError("checkpoint failed")

    with pytest.raises(error_type) as raised:
        with phase("dispatch", trace):
            raise original
    assert raised.value is original


@pytest.mark.parametrize("change", ["delete", "replace", "history"])
def test_supervisor_preserves_attempt_evidence_during_repair(tmp_path, change):
    backend = ReceiptBackend()
    backend.state.nearby_resources["iron-ore"] = 5
    config = SupervisorConfig(
        state_dir=tmp_path / "supervisor", checkpoint=tmp_path / "checkpoint.json",
        session_id=backend.state.session_id, started_at=1000,
        repair_command=["repair"], cwd=tmp_path,
    )
    supervisor = Supervisor(config)
    supervisor.source_identity = lambda: ("head", "diff")

    class RepairEvidenceLoop(BackgroundWorkLoop):
        def _compile_candidates(self, snapshot):
            return [Plan("repair-attempt-evidence", "stockpile_fuel", "Build valid attempt history", (
                Step("factory_gather", "inventory", "iron-ore", 5,
                     parameters={"resource": "iron-ore", "quantity": 5}),
                Step("factory_wait", "inventory", "iron-plate", 1000,
                     timeout_ticks=1800),
            ))], ""

    loop = RepairEvidenceLoop(
        backend, policy="deterministic", target="rocket_launch",
        factory_scheduling="ready-work", checkpoint=str(config.checkpoint), tick_seconds=0,
    )
    loop.memory = BackgroundMemory(
        backend.state.session_id, "rocket_launch", active_goal="stockpile_fuel",
        last_tick=backend.state.tick,
    )
    assert loop.step()["verified"] is True
    assert loop.memory.attempt is None and len(loop.memory.attempt_outcomes) == 1
    assert loop.step()["verified"] is False
    assert loop.memory.pending is not None and loop.memory.attempt is not None
    previous_bytes = config.checkpoint.read_bytes()
    previous = json.loads(previous_bytes)
    # The unchanged positive is an actual production-loop checkpoint, not a
    # hand-shaped owner fragment. Both the live write-ahead and finished ledger
    # are accepted by the same composed loader used by repair verification.
    loaded = load_checkpoint(config.checkpoint, backend.state.session_id, "rocket_launch")
    assert loaded.pending == previous["pending"]
    assert loaded.attempt == previous["attempt"]
    assert loaded.attempt_outcomes == previous["attempt_outcomes"]
    assert len(loaded.attempt_outcomes) == 1

    current = deepcopy(previous)
    if change == "delete":
        del current["attempt"]
    elif change == "replace":
        current["attempt"] = make_attempt(
            backend.state.session_id, "rocket_launch", current["active_plan"],
            current["step_index"], current["pending"], process_id="c" * 32,
        )
    else:
        current["attempt_outcomes"] = []
    atomic_json(config.checkpoint, current)
    changed_bytes = config.checkpoint.read_bytes()
    if change == "delete":
        with pytest.raises(ValueError):
            load_checkpoint(config.checkpoint, backend.state.session_id, "rocket_launch")
    else:
        # These altered checkpoints remain structurally and composition-valid,
        # so rejection must come from the repair ownership comparison itself.
        changed = load_checkpoint(config.checkpoint, backend.state.session_id, "rocket_launch")
        assert changed.pending == previous["pending"]
        assert changed.attempt_outcomes == current["attempt_outcomes"]
        if change == "replace":
            assert changed.attempt != loaded.attempt
        else:
            assert changed.attempt == loaded.attempt

    result = tmp_path / "result.json"
    atomic_json(result, {
        "status": "repaired", "kind": "operational",
        "session_id": backend.state.session_id,
        "checkpoint": str(config.checkpoint.resolve()), "operational_verified": True,
        "evidence": ["captured observation"],
    })
    ledger_before = deepcopy(supervisor.state)
    assert not supervisor.validate_repair(result, previous, ("head", "diff"))
    assert config.checkpoint.read_bytes() == changed_bytes
    assert supervisor.state == ledger_before
    config.checkpoint.write_bytes(previous_bytes)
    assert supervisor.validate_repair(result, previous, ("head", "diff"))
    assert config.checkpoint.read_bytes() == previous_bytes
    assert supervisor.state == ledger_before


def test_background_completion_preserves_concurrent_foreground_attempt(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    background = deepcopy(loop.memory.background_attempt)
    assert background and not loop.memory.attempt_outcomes
    assert background["receipt"] == loop.memory.background_job["parameters"]["receipt"]
    plan = Plan("pending-wait", loop.memory.active_goal, "Observe crafting",
                (Step("factory_wait", "crafting_idle", timeout_ticks=1800),))
    loop.memory.active_plan = plan.to_dict()
    loop.memory.pending = {
        "started_tick": backend.state.tick, "polls": 0,
        "action": "factory_wait", "dispatch": "returned",
    }
    loop.memory.attempt = make_attempt(
        loop.memory.session_id, loop.target, loop.memory.active_plan, 0, loop.memory.pending,
        process_id="a" * 32,
    )
    foreground = deepcopy(loop.memory.attempt)
    loop._save()
    resumed = controller(backend, tmp_path, resume=True)
    backend.complete()
    resumed._observe()
    assert resumed.memory.attempt == foreground
    assert resumed.memory.background_job is resumed.memory.background_attempt is None
    outcome = resumed.memory.attempt_outcomes[-1]
    assert outcome["id"] == background["id"] and outcome["outcome"] == "verified"
    assert outcome["latency_seconds"] is None
    assert len(backend.calls) == 1
    assert load_checkpoint(tmp_path / "state.json", backend.state.session_id, loop.target) == resumed.memory


def test_legacy_background_migration_preserves_unknown_identity(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    data = asdict(loop.memory)
    data["version"] = 1
    data["background_schema"] = 1
    for field in ("attempt", "attempt_outcomes", "background_attempt", "background_step"):
        del data[field]
    original_history = deepcopy(data["history"])
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    memory = load_checkpoint(path, backend.state.session_id, loop.target)
    assert path.read_bytes() == original
    assert memory.background_job == data["background_job"]
    assert memory.background_schema == 1 and memory.background_attempt is None
    assert memory.background_step is None
    assert memory.attempt is None and memory.attempt_outcomes == []
    assert memory.history == original_history
    report = reconciliation_report(memory)
    assert report["assessment"] == "background_observation_required"
    assert report["background_attempt"] is None
    memory.save(path)
    restored = load_checkpoint(path, backend.state.session_id, loop.target)
    assert restored == memory
    assert restored.background_job == data["background_job"]
    assert restored.background_attempt is None and restored.history == original_history


def test_schema1_marker_rejects_unexpected_background_step_without_checkpoint_write(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    data = asdict(loop.memory)
    data["background_schema"] = 1
    data["background_attempt"] = None
    path = tmp_path / "schema1-with-step.json"
    path.write_text(json.dumps(data))
    original = path.read_bytes()

    with pytest.raises(ValueError, match="Legacy background checkpoint has unexpected attempt"):
        BackgroundMemory.load(path, backend.state.session_id, loop.target)

    assert path.read_bytes() == original


@pytest.mark.parametrize("change", ["missing", "receipt", "duplicate"])
def test_invalid_background_attempt_rejected_without_checkpoint_write(tmp_path, change):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    data = asdict(loop.memory)
    if change == "missing":
        data["background_attempt"] = None
    elif change == "receipt":
        data["background_attempt"]["receipt"] = "wrong"
    else:
        data["attempt_outcomes"] = [{
            **data["background_attempt"], "outcome": "verified",
            "finished_tick": data["last_tick"],
            "finished_at_utc": data["background_attempt"]["started_at_utc"],
            "latency_seconds": None,
        }]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    with pytest.raises(ValueError):
        BackgroundMemory.load(path, backend.state.session_id, loop.target)
    assert path.read_bytes() == original


def test_offline_reader_preserves_combined_named_extensions(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    data = asdict(loop.memory)
    commitments = {"recipe:iron-plate": {"layout": "layout", "source_unit": 7, "parts": {}}}
    data.update(input_routes_schema=1, input_commitments=commitments)
    path = tmp_path / "combined.json"
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    memory = load_checkpoint(path, backend.state.session_id, loop.target)
    report = reconciliation_report(memory)
    assert report["input_commitments"] == commitments
    assert report["background_attempt"] == data["background_attempt"]
    assert path.read_bytes() == original
    data["unknown_extension"] = {}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_checkpoint(path, backend.state.session_id, loop.target)


def test_ready_wait_yield_records_unverified_attempt_outcome():
    from test_ready_work import pending_loop, production_state

    catalog, state = production_state()
    loop = pending_loop(state, catalog)
    identity = loop.memory.attempt["id"]
    record = loop._verify_pending(state)
    assert not record["verified"]
    assert record["attempt_outcomes"][-1]["id"] == identity
    assert record["attempt_outcomes"][-1]["outcome"] == "wait_replanned"


def test_background_wait_yield_records_unverified_attempt_outcome(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    plan = Plan("wait", loop.target, "Observe craft",
                (Step("factory_wait", "crafting_idle"),))
    loop.memory.active_plan = plan.to_dict()
    loop.memory.pending = {"action": "factory_wait", "dispatch": "returned",
                           "started_tick": backend.state.tick, "polls": 0}
    loop.memory.attempt = make_attempt(
        loop.memory.session_id, loop.target, loop.memory.active_plan, 0, loop.memory.pending,
        process_id="b" * 32,
    )
    background = deepcopy(loop.memory.background_attempt)
    record = loop._verify_pending(backend.state)
    assert not record["verified"]
    assert record["attempt_outcomes"][-1]["outcome"] == "wait_replanned"
    assert loop.memory.background_attempt == background
