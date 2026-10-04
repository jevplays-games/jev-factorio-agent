"""Integration of the actual controller with an explicitly synthetic backend."""
from copy import deepcopy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from jev_factorio.background import BackgroundMemory, BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.craft_jobs import CraftJob
from jev_factorio.input_controller import input_loop_type
from jev_factorio.memory import CampaignMemory, checkpoint_memory_type, load_checkpoint_data
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.planning.background_work import independent_candidates, research_demands
from jev_factorio.planning.connection_identity import connection_key
from jev_factorio.skills import Plan, Step
from jev_factorio.telemetry import make_attempt
from test_factory import catalog, machine, recipe, snapshot


def science_catalog():
    result = catalog()
    result.recipes["automation-science-pack"] = recipe("automation-science-pack", {"iron-plate": 1})
    result.recipes["logistic-science-pack"] = recipe("logistic-science-pack", {"copper-ore": 1})
    result.technologies["study"] = {
        "enabled": True, "prerequisites": [], "effects": [], "count": 100, "energy_ticks": 60,
        "ingredients": [{"name": "automation-science-pack", "amount": 1},
                        {"name": "logistic-science-pack", "amount": 1}],
    }
    return result


class ReceiptBackend:
    craft_jobs_supported = True

    def __init__(self):
        self.state = snapshot(inventory={"iron-plate": 10}, nearby_resources={"iron-ore": 0, "copper-ore": 0})
        self.state.factory.update(craft_jobs_protocol=1, craft_job_actor={
            "session_id": self.state.session_id, "player_index": 1, "unit_number": 9,
            "surface_index": 1, "force_index": 1,
        })
        self.calls = []
        self.observations = 0
        self.fail_observation = None
        self.lose_ack = False
        self.before_observation = None

    def enable_factory(self):
        return science_catalog()

    def observe(self):
        self.observations += 1
        if self.before_observation:
            self.before_observation(self)
        if self.observations == self.fail_observation:
            raise OSError("synthetic observation loss")
        return deepcopy(self.state)

    def execute(self, action, parameters):
        self.calls.append((action, deepcopy(parameters)))
        if action == "factory_craft_job":
            self.state.inventory["iron-plate"] -= parameters["batches"]
            self.state.factory.update(crafting_queue=1, craft_job={
                **self.state.factory["craft_job_actor"], "id": parameters["receipt"],
                "recipe": parameters["recipe"], "requested": parameters["batches"],
                "accepted": parameters["batches"], "finished": 0,
                "started_tick": self.state.tick, "last_progress_tick": self.state.tick,
                "inputs": {"iron-plate": parameters["batches"]},
                "outputs": {"automation-science-pack": parameters["batches"]},
                "baseline": {"automation-science-pack": 0}, "status": "running",
                "queue_valid": True, "paid": True,
            })
            if self.lose_ack:
                raise TimeoutError("synthetic lost acknowledgement")
        elif action == "factory_gather":
            item = parameters["resource"]
            self.state.inventory[item] = self.state.inventory.get(item, 0) + parameters["quantity"]
        elif action != "factory_wait":
            raise AssertionError(action)
        return "synthetic return"

    def act(self, action):
        assert action == "idle"
        self.state.tick += 1

    def complete(self):
        self.state.tick += 100
        self.state.inventory["automation-science-pack"] = 10
        self.state.factory["produced"]["automation-science-pack"] = 10
        self.state.factory["crafting_queue"] = 0
        self.state.factory["craft_job"].update(status="completed", finished=10,
            last_progress_tick=self.state.tick, completed_tick=self.state.tick)


class TraceSink:
    def __init__(self):
        self.events = []

    def emit(self, event_type, payload):
        self.events.append({"event_type": event_type, "payload": deepcopy(payload)})


class ScenarioLoop(BackgroundWorkLoop):
    """Deterministic test candidates; the real dispatcher/receipts stay intact."""
    def _compile_candidates(self, current):
        if self.memory.background_job:
            have = current.inventory.get("iron-ore", 0)
            return [Plan("gather-iron", self.memory.active_goal, "Independent ore", (
                Step("factory_gather", "inventory", "iron-ore", have + 5,
                     parameters={"resource": "iron-ore", "quantity": 5}),
            ))], ""
        plan = Plan("craft-science", self.memory.active_goal, "Craft ten science", (
            Step("factory_craft", "inventory", "automation-science-pack", 10,
                 costs={"iron-plate": 10}, timeout_ticks=1800,
                 parameters={"recipe": "automation-science-pack", "batches": 10}),
        ))
        return [self._tracked_plan(plan, current)], ""


def controller(backend, tmp_path, *, resume=False, research_log=None):
    loop = ScenarioLoop(backend, policy="deterministic", factory_scheduling="ready-work",
                        target="automation_science", checkpoint=str(tmp_path / "state.json"),
                        resume_controller=resume, tick_seconds=0, research_log=research_log)
    if not resume:
        loop.memory = BackgroundMemory(backend.state.session_id, loop.target,
            active_goal=loop.target, completed_goals={goal: 0 for goal in loop.order[:-1]}, last_tick=10)
    return loop


class ConnectorPageReader:
    """Read-only local stand-in for the bounded native connector detail page."""
    def __init__(self, backend):
        self.backend = backend
        self.calls = []

    def command(self, script):
        assert "connector_page" in script
        self.calls.append(script)
        route, cells = self.backend.connector_route, self.backend.connector_cells
        return json.dumps({"id": route["id"], "valid": True,
                           "cell_count": len(cells), "offset": 1, "cells": cells})


class PendingConnectorBackend(ReceiptBackend):
    def __init__(self):
        super().__init__()
        self.output_buffers_supported = True
        self.input_routes_supported = True
        self.mining_outposts_supported = True
        self.state.world_kind = "fle"
        self.state.inventory["pipe"] = 2
        self.state.factory["entities"].update({
            "utility:water": machine("offshore-pump", unit_number=21,
                                      fluid_ports=[{"id": 1, "fluid": "water"}]),
            "utility:engine": machine("steam-engine", unit_number=23,
                                      fluid_ports=[{"id": 2, "fluid": "steam"}]),
        })
        self.state.factory["connector_ownership"] = {
            "protocol": 1, "session_id": self.state.session_id,
            "active": None, "routes": {},
        }
        self.connector_route = None
        self.connector_cells = []

    def execute(self, action, parameters):
        if action != "factory_connect":
            return super().execute(action, parameters)
        self.calls.append((action, deepcopy(parameters)))
        receipt = connection_key(parameters)
        self.connector_cells = [
            {"index": 1, "position": {"x": 0.5, "y": 1.5},
             "unit_number": 200, "paid": 1, "external": False},
            {"index": 2, "position": {"x": 1.5, "y": 1.5},
             "unit_number": None, "paid": 0, "external": False},
        ]
        self.connector_route = {
            "id": receipt, **parameters, "source_unit": 21, "target_unit": 23,
            "actor_unit": 9, "surface_index": 1, "force_index": 1,
            "session_id": self.state.session_id, "state": "building",
            "paid": 1, "external": 0, "pending": 2, "cell_count": 2,
        }
        self.state.inventory["pipe"] -= 1  # One paid native cell; one is still pending.
        self.state.factory["connector_ownership"] = {
            "protocol": 1, "session_id": self.state.session_id,
            "active": receipt, "routes": {receipt: deepcopy(self.connector_route)},
        }
        raise TimeoutError("synthetic connector acknowledgement lost after one paid cell")


@pytest.mark.parametrize("kind", [BackgroundWorkLoop,
    outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))])
def test_background_connector_reconciliation_barrier_retains_paid_pending_work(
        tmp_path, kind):
    backend = PendingConnectorBackend()
    backend.state.world_kind = "fle"
    backend.state.factory["entities"].update({
        "utility:boiler": machine("boiler", unit_number=21,
                                   fluid_ports=[{"id": 1, "fluid": "steam"}]),
        "utility:engine": machine("steam-engine", unit_number=23,
                                   fluid_ports=[{"id": 2, "fluid": "steam"}]),
    })
    if kind is not BackgroundWorkLoop:
        for key in ("output_buffers", "input_routes", "mining_outposts"):
            backend.state.factory[key] = {
                "protocol": 1, "session_id": backend.state.session_id,
                "tick": backend.state.tick, "sources": {},
            }
    params = {"source": "utility:boiler", "target": "utility:engine",
              "kind": "pipe", "fluid": "steam"}
    plan = Plan("connector:ambiguous-paid-prefix", "rocket_launch",
                "Build a bounded steam connection", (Step(
                    "factory_connect", "connection", costs={"pipe": 2},
                    parameters=params),))
    path = tmp_path / "background-connector.json"
    loop = kind(backend, policy="deterministic", factory_scheduling="ready-work",
                target="rocket_launch", checkpoint=str(path), tick_seconds=0)
    loop.memory = loop.memory_type(backend.state.session_id, loop.target,
        active_goal=loop.target, completed_goals={goal: 0 for goal in loop.order[:-1]},
        last_tick=backend.state.tick,
        connector_ownership={"protocol": 1, "session_id": backend.state.session_id,
                             "routes": {}})
    backend._factory = ConnectorPageReader(backend)
    loop._work_candidates = lambda _: ([plan], "")

    first = loop.step()
    assert first["action"] == "factory_connect" and not first["verified"]
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert len(backend.calls) == 1
    assert backend.state.inventory["pipe"] == 1
    pending = deepcopy(loop.memory.pending)
    attempt = deepcopy(loop.memory.attempt)
    active_plan = deepcopy(loop.memory.active_plan)
    reservations = deepcopy(loop.memory.reservations)
    outcomes = deepcopy(loop.memory.attempt_outcomes)

    result = loop.step()  # Observe and bind the exact paid cell, then hit the barrier.

    assert loop.memory.status == result["status"] == "uncertain"
    assert loop.memory.reason == "Connector route needs exact reconciliation"
    assert loop._execution_barrier(backend.state)
    assert not result["verified"]
    assert loop.memory.pending == pending
    assert loop.memory.attempt == attempt
    assert loop.memory.active_plan == active_plan
    assert loop.memory.reservations == reservations
    assert loop.memory.attempt_outcomes == outcomes
    receipt = connection_key(params)
    bound = loop.memory.connector_ownership["routes"][receipt]
    assert bound["state"] == "building" and bound["paid"] == 1 and bound["pending"] == 2
    assert bound["cells"][0]["unit_number"] == 200 and bound["cells"][0]["paid"]
    assert not bound["cells"][1]["paid"] and bound["cells"][1]["unit_number"] is None
    assert len(backend.calls) == 1 and backend.state.inventory["pipe"] == 1

    loaded = loop.memory_type.load(path, backend.state.session_id, "rocket_launch")
    assert loaded.pending == pending and loaded.attempt == attempt
    assert loaded.active_plan == json.loads(json.dumps(active_plan))
    assert loaded.reservations == reservations
    assert loaded.connector_ownership == loop.memory.connector_ownership
    result = loop.step()
    assert result["status"] == "uncertain" and not result["verified"]
    assert loop.memory.pending == pending and loop.memory.attempt == attempt
    assert loop.memory.connector_ownership == loaded.connector_ownership
    assert len(backend.calls) == 1 and backend.state.inventory["pipe"] == 1

    # A checkpoint reload rechecks the same receipt/cell and cannot repay it.
    backend._factory = None
    resumed = kind(backend, policy="deterministic", factory_scheduling="ready-work",
                   target="rocket_launch", checkpoint=str(path),
                   resume_controller=True, tick_seconds=0)
    backend._factory = ConnectorPageReader(backend)
    before_calls = len(backend.calls)
    recovered = resumed.reconcile_only()
    assert recovered["status"] == "uncertain"
    assert resumed.memory.pending == pending and resumed.memory.attempt == attempt
    assert resumed.memory.active_plan == json.loads(json.dumps(active_plan))
    assert resumed.memory.reservations == reservations
    assert resumed.memory.connector_ownership == loaded.connector_ownership
    assert len(backend.calls) == before_calls and backend.state.inventory["pipe"] == 1


def test_craft_then_independent_gather_then_verified_completion(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    first = loop.step()
    assert first["verified"] is False and first["background_job"]
    assert loop.memory.pending is None and loop.memory.reservations == {}
    assert loop.memory.active_plan is None
    assert backend.calls[0][0] == "factory_craft_job"
    assert loop.step()["action"] == "factory_gather"
    assert loop.memory.background_job and backend.state.inventory["iron-ore"] == 5
    backend.complete()
    record = loop.step()
    assert loop.memory.background_job is None and record["status"] == "completed"
    completed = load_checkpoint_data(
        json.loads((tmp_path / "state.json").read_text()), backend.state.session_id, loop.target)
    assert completed.background_schema == 2 and completed.background_step is None
    assert len(backend.calls) == 2
    assert any(event["kind"] == "background_job_completed" for event in loop.memory.history)


def test_checkpoint_resume_never_requeues_a_background_craft(tmp_path):
    backend = ReceiptBackend()
    controller(backend, tmp_path).step()
    restored = controller(backend, tmp_path, resume=True)
    restored.step()
    assert [action for action, _ in backend.calls] == ["factory_craft_job", "factory_gather"]
    assert restored.memory.background_job
    with pytest.raises(ValueError):
        CampaignMemory.load(tmp_path / "state.json", backend.state.session_id, restored.target)


def test_background_attempt_step_fingerprint_is_bound_and_legacy_state_is_preserved(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    checkpoint = tmp_path / "state.json"
    saved = json.loads(checkpoint.read_text())
    valid = BackgroundMemory.load(checkpoint, backend.state.session_id, loop.target)
    assert valid.background_schema == 3
    assert valid.background_job == saved["background_job"]
    assert valid.background_attempt == saved["background_attempt"]
    assert valid.background_step == saved["background_step"]
    composed = load_checkpoint_data(saved, backend.state.session_id, loop.target)
    assert type(composed) is type(valid)
    assert composed.background_step == saved["background_step"]

    step_only = deepcopy(saved)
    for key in ("background_schema", "background_job", "background_attempt"):
        step_only.pop(key)
    with pytest.raises(ValueError, match="Incomplete background checkpoint extension"):
        load_checkpoint_data(step_only, backend.state.session_id, loop.target)

    incomplete_schema3 = deepcopy(saved)
    incomplete_schema3.pop("background_step")
    with pytest.raises(ValueError, match="identity must coexist"):
        load_checkpoint_data(incomplete_schema3, backend.state.session_id, loop.target)

    legacy_v2 = deepcopy(saved)
    legacy_v2["background_schema"] = 2
    legacy_v2.pop("background_step")
    legacy_v2_path = tmp_path / "schema-2.json"
    legacy_v2_path.write_text(json.dumps(legacy_v2))
    restored_v2 = BackgroundMemory.load(
        legacy_v2_path, backend.state.session_id, loop.target)
    assert restored_v2.background_schema == 2
    assert restored_v2.background_attempt == legacy_v2["background_attempt"]
    assert restored_v2.background_job == legacy_v2["background_job"]
    assert load_checkpoint_data(
        legacy_v2, backend.state.session_id, loop.target).background_job == legacy_v2["background_job"]

    orphan_schema2 = deepcopy(legacy_v2)
    orphan_schema2["background_step"] = saved["background_step"]
    with pytest.raises(ValueError, match="unexpected step"):
        load_checkpoint_data(orphan_schema2, backend.state.session_id, loop.target)

    tampered_v2 = deepcopy(legacy_v2)
    tampered_v2["background_attempt"]["step_sha256"] = "f" * 64
    tampered_v2_path = tmp_path / "schema-2-tampered.json"
    tampered_v2_path.write_text(json.dumps(tampered_v2))
    with pytest.raises(ValueError, match="step fingerprint"):
        BackgroundMemory.load(tampered_v2_path, backend.state.session_id, loop.target)

    legacy = deepcopy(legacy_v2)
    legacy["background_schema"] = 1
    legacy["background_attempt"] = None
    legacy_path = tmp_path / "schema-1.json"
    legacy_path.write_text(json.dumps(legacy))
    legacy_bytes = legacy_path.read_bytes()
    restored_legacy = BackgroundMemory.load(
        legacy_path, backend.state.session_id, loop.target)
    assert restored_legacy.background_schema == 1
    assert restored_legacy.background_job == legacy["background_job"]
    assert restored_legacy.background_attempt is None
    assert restored_legacy.history == legacy["history"]
    assert load_checkpoint_data(legacy, backend.state.session_id, loop.target).background_attempt is None
    assert legacy_path.read_bytes() == legacy_bytes

    tampered = deepcopy(saved)
    tampered["background_attempt"]["step_sha256"] = "f" * 64
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="step fingerprint"):
        BackgroundMemory.load(tampered_path, backend.state.session_id, loop.target)

    changed_step = deepcopy(saved)
    changed_step["background_step"]["threshold"] += 1
    changed_step_path = tmp_path / "changed-step.json"
    changed_step_path.write_text(json.dumps(changed_step))
    with pytest.raises(ValueError, match="step fingerprint"):
        BackgroundMemory.load(changed_step_path, backend.state.session_id, loop.target)

    changed_job = deepcopy(saved)
    changed_job["background_job"]["deadline_tick"] += 1
    changed_job_path = tmp_path / "changed-job.json"
    changed_job_path.write_text(json.dumps(changed_job))
    with pytest.raises(ValueError, match="step fingerprint"):
        BackgroundMemory.load(changed_job_path, backend.state.session_id, loop.target)


def test_empty_background_checkpoint_is_schema2_and_composes_without_rewriting(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    path = tmp_path / "empty.json"
    loop._save()
    saved = json.loads(Path(loop.checkpoint).read_text())
    assert saved["background_schema"] == 2 and saved["background_step"] is None
    assert checkpoint_memory_type(saved) is BackgroundMemory
    restored = load_checkpoint_data(saved, backend.state.session_id, loop.target)
    assert restored.background_schema == 2 and restored.background_step is None


def test_reconcile_only_verifies_paid_job_after_fresh_resume_once(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    original = controller(backend, tmp_path)
    original.memory.stalled_decisions = 3
    original.memory.failures["previous-plan"] = 2
    original._save()
    original.step()
    assert original.memory.stalled_decisions == 3  # Admission is not verified progress.
    history_before = deepcopy(original.memory.history)
    failures_before = deepcopy(original.memory.failures)
    backend.complete()
    calls_before = len(backend.calls)

    restored = controller(backend, tmp_path, resume=True)
    assert restored.memory is None  # The native observation validates before checkpoint restore.
    class BombModel:
        def evaluate(self, *args, **kwargs):
            pytest.fail("model called during reconciliation")
    restored.jev = BombModel()
    monkeypatch.setattr(restored, "step", lambda: pytest.fail("step called during reconciliation"))
    monkeypatch.setattr(backend, "execute", lambda *args: pytest.fail("execute called during reconciliation"))

    first = restored.reconcile_only()
    assert first == {
        "status": "running", "tick": backend.state.tick,
        "background_state": "verified_completed", "verified_attempt_added": True,
    }
    saved = BackgroundMemory.load(tmp_path / "state.json", backend.state.session_id, restored.target)
    assert saved.background_job is None and saved.background_attempt is None
    assert saved.stalled_decisions == 0
    assert saved.failures == failures_before
    assert saved.history[:-1] == history_before
    assert saved.history[-1]["kind"] == "background_job_completed"
    assert len([row for row in saved.attempt_outcomes if row["outcome"] == "verified"]) == 1
    assert [event["kind"] for event in saved.history].count("background_job_completed") == 1
    assert len(backend.calls) == calls_before

    second = restored.reconcile_only()
    assert second["background_state"] == "none"
    assert second["verified_attempt_added"] is False
    saved_again = BackgroundMemory.load(tmp_path / "state.json", backend.state.session_id, restored.target)
    assert len([row for row in saved_again.attempt_outcomes if row["outcome"] == "verified"]) == 1
    assert [event["kind"] for event in saved_again.history].count("background_job_completed") == 1
    assert len(backend.calls) == calls_before


def test_reconcile_only_retains_unmatched_receipt_as_uncertain(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    original = controller(backend, tmp_path)
    original.memory.stalled_decisions = 3
    original.step()
    assert original.memory.stalled_decisions == 3
    backend.state.factory["craft_job"]["id"] = "unmatched-receipt"
    calls_before = len(backend.calls)

    restored = controller(backend, tmp_path, resume=True)
    class BombModel:
        def evaluate(self, *args, **kwargs):
            pytest.fail("model called during reconciliation")
    restored.jev = BombModel()
    monkeypatch.setattr(restored, "step", lambda: pytest.fail("step called during reconciliation"))
    monkeypatch.setattr(backend, "execute", lambda *args: pytest.fail("execute called during reconciliation"))
    result = restored.reconcile_only()

    assert result["status"] == "uncertain"
    assert result["background_state"] == "uncertain"
    assert result["verified_attempt_added"] is False
    assert restored.memory.background_job is not None
    assert restored.memory.stalled_decisions == 3
    assert restored.memory.background_job["failed"]
    assert restored.memory.attempt_outcomes == []
    assert len(backend.calls) == calls_before
    saved = BackgroundMemory.load(tmp_path / "state.json", backend.state.session_id, restored.target)
    assert saved.status == "uncertain" and saved.stalled_decisions == 3


def test_reconcile_only_preserves_valid_running_job_without_dispatch(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    original = controller(backend, tmp_path)
    original.memory.stalled_decisions = 3
    original.step()
    assert original.memory.stalled_decisions == 3
    calls_before = len(backend.calls)

    restored = controller(backend, tmp_path, resume=True)
    monkeypatch.setattr(backend, "execute", lambda *args: pytest.fail("execute called during reconciliation"))
    result = restored.reconcile_only()

    assert result["status"] == "running" and result["background_state"] == "pending"
    assert result["verified_attempt_added"] is False
    assert restored.memory.stalled_decisions == 3
    assert restored.memory.background_job is not None
    assert restored.memory.background_attempt is not None
    assert restored.memory.attempt_outcomes == []
    assert len(backend.calls) == calls_before
    saved = BackgroundMemory.load(tmp_path / "state.json", backend.state.session_id, restored.target)
    assert saved.background_job == restored.memory.background_job
    assert saved.background_attempt == restored.memory.background_attempt
    assert saved.stalled_decisions == 3


def test_reconcile_only_propagates_observation_error_without_releasing_checkpoint(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    original = controller(backend, tmp_path)
    original.step()
    checkpoint_before = (tmp_path / "state.json").read_bytes()
    calls_before = len(backend.calls)
    backend.fail_observation = backend.observations + 1

    restored = controller(backend, tmp_path, resume=True)
    monkeypatch.setattr(backend, "execute", lambda *args: pytest.fail("execute called during reconciliation"))
    with pytest.raises(OSError, match="observation loss"):
        restored.reconcile_only()

    assert restored.memory is None
    assert (tmp_path / "state.json").read_bytes() == checkpoint_before
    assert len(backend.calls) == calls_before


def delay_native_craft_start(backend, monkeypatch, *, corrupt=None):
    execute = backend.execute

    def delayed(action, parameters):
        # The real game keeps ticking while write-ahead persistence or dispatch
        # preparation runs. This exceeds the craft's 1800-tick execution budget.
        if action == "factory_craft_job":
            backend.state.tick += 3794
        result = execute(action, parameters)
        if action == "factory_craft_job" and corrupt is not None:
            field, value = corrupt
            backend.state.factory["craft_job"][field] = value
        return result

    monkeypatch.setattr(backend, "execute", delayed)


def test_pending_poll_admission_traces_failed_predicate_before_background_transfer(
        tmp_path, monkeypatch):
    backend, sink = ReceiptBackend(), TraceSink()
    delay_native_craft_start(backend, monkeypatch, corrupt=("queue_valid", False))
    loop = controller(backend, tmp_path, research_log=sink)
    first = loop.step()
    assert first["background_job"] is None
    assert loop.memory.pending["dispatch"] == "returned"
    assert loop.memory.background_job is None
    assert [action for action, _ in backend.calls] == ["factory_craft_job"]

    step = Plan.from_dict(loop.memory.active_plan).steps[loop.memory.step_index]
    attempt_id = loop.memory.attempt["id"]
    backend.state.factory["craft_job"]["queue_valid"] = True
    calls_before = list(backend.calls)
    observations_before = backend.observations
    result = loop.step()

    assert result["background_job"] is not None
    assert loop.memory.pending is None and loop.memory.background_job is not None
    assert backend.calls == calls_before
    assert backend.observations == observations_before + 1
    verification = [event["payload"] for event in sink.events
                    if event["event_type"] == "verification"
                    and event["payload"].get("phase") == "pending_poll"]
    admitted = [event["payload"] for event in sink.events
                if event["event_type"] == "background_job_admitted"]
    assert len(verification) == len(admitted) == 1
    assert verification[0]["verified"] is False
    assert verification[0]["phase"] == "pending_poll"
    assert verification[0]["predicate"] == asdict(step)
    assert verification[0]["action_origin"] == "current_trace"
    prepared = [event["payload"] for event in sink.events
                if event["event_type"] == "action_prepared"
                and event["payload"].get("action") == "factory_craft_job"]
    returned = [event["payload"] for event in sink.events
                if event["event_type"] == "action_returned"
                and event["payload"].get("action") == "factory_craft_job"]
    poll_observation = [event["payload"] for event in sink.events
                        if event["event_type"] == "observation"
                        and event["payload"].get("phase") == "before_decision"][-1]
    assert len(prepared) == len(returned) == 1
    assert verification[0]["decision_id"] == poll_observation["decision_id"]
    assert verification[0]["observation_id"] == poll_observation["observation_id"]
    assert verification[0]["plan_id"] == prepared[0]["plan_id"]
    assert verification[0]["step_index"] == prepared[0]["step_index"] == 0
    assert verification[0]["started_tick"] == loop.memory.background_attempt["started_tick"]
    assert verification[0]["attempt_id"] == prepared[0]["attempt_id"] == returned[0]["attempt_id"]
    assert verification[0]["attempt_id"] == admitted[0]["attempt_id"] == attempt_id
    assert verification[0]["action_id"] == prepared[0]["action_id"] == returned[0]["action_id"]
    assert verification[0]["action_id"] == admitted[0]["action_id"]
    event_types = [event["event_type"] for event in sink.events]
    assert event_types.index("verification") < event_types.index("background_job_admitted")
    assert "model_request" not in event_types


def test_delayed_native_start_admits_once_and_survives_resume(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    delay_native_craft_start(backend, monkeypatch)
    loop = controller(backend, tmp_path)
    result = loop.step()
    assert result["background_job"] and loop.memory.pending is None
    job = deepcopy(loop.memory.background_job)
    attempt = deepcopy(loop.memory.background_attempt)
    assert job["started_tick"] == attempt["started_tick"] + 3794
    assert job["deadline_tick"] == job["started_tick"] + 1800

    resumed = controller(backend, tmp_path, resume=True)
    resumed.step()  # Independent work must not requeue the paid craft.
    assert resumed.memory.background_job["deadline_tick"] == job["deadline_tick"]
    assert resumed.memory.background_attempt == attempt
    backend.complete()
    # A late observation still accepts native completion inside the fixed budget.
    backend.state.tick = job["deadline_tick"] + 100
    result = resumed.step()
    assert result["status"] == "completed" and resumed.memory.background_job is None
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1


@pytest.mark.parametrize("complete_late", [False, True])
def test_delayed_start_retains_fixed_native_execution_timeout(tmp_path, monkeypatch, complete_late):
    backend = ReceiptBackend()
    delay_native_craft_start(backend, monkeypatch)
    loop = controller(backend, tmp_path)
    loop.step()
    deadline = loop.memory.background_job["deadline_tick"]
    backend.state.tick = deadline
    if complete_late:
        backend.complete()  # Native completion itself now misses the deadline.
    result = loop.step()
    assert result["status"] == "uncertain"
    assert loop.memory.background_job["deadline_tick"] == deadline
    assert [action for action, _ in backend.calls] == ["factory_craft_job"]


@pytest.mark.parametrize("corrupt", [("paid", False), ("queue_valid", False), ("id", "other-job")])
def test_delayed_start_cannot_admit_untrusted_native_receipt(tmp_path, monkeypatch, corrupt):
    backend = ReceiptBackend()
    delay_native_craft_start(backend, monkeypatch, corrupt=corrupt)
    loop = controller(backend, tmp_path)
    loop.step()
    assert loop.memory.pending and loop.memory.background_job is None
    result = loop.step()
    assert result["status"] == "uncertain"
    assert loop.memory.pending and loop.memory.background_job is None
    assert [action for action, _ in backend.calls] == ["factory_craft_job"]


@pytest.mark.parametrize("dispatch", ["prepared", "ambiguous"])
def test_delayed_unacknowledged_craft_keeps_original_pending_barrier(tmp_path, monkeypatch, dispatch):
    backend = ReceiptBackend()
    delay_native_craft_start(backend, monkeypatch)
    backend.lose_ack = True
    loop = controller(backend, tmp_path)
    loop.step()
    loop.memory.pending["dispatch"] = dispatch
    pending = deepcopy(loop.memory.pending)
    loop._save()
    resumed = controller(backend, tmp_path, resume=True)
    result = resumed.step()
    assert result["status"] == "uncertain" and resumed.memory.background_job is None
    assert resumed.memory.pending["started_tick"] == pending["started_tick"]
    assert resumed.memory.pending["dispatch"] == dispatch
    assert [action for action, _ in backend.calls] == ["factory_craft_job"]


def test_exhausted_independent_work_waits_for_background_completion(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    initial = controller(backend, tmp_path)
    initial.step()
    loop = BackgroundWorkLoop(
        backend, policy="deterministic", factory_scheduling="ready-work",
        target="automation_science", checkpoint=str(tmp_path / "state.json"),
        resume_controller=True, tick_seconds=0,
    )
    candidate = Plan("failed-gather", loop.target, "Independent ore", (
        Step("factory_gather", "inventory", "iron-ore", 5,
             parameters={"resource": "iron-ore", "quantity": 5}),
    ))
    monkeypatch.setattr("jev_factorio.background.independent_candidates",
                        lambda *args: [candidate])
    loop._observe()
    loop.memory.failures[candidate.id] = 2
    record = loop.step()
    assert record["status"] == "running"
    assert loop.memory.background_job
    assert all(action != "factory_gather" for action, _ in backend.calls)
    assert loop.memory.failures[candidate.id] == 2
    backend.complete()
    record = loop.step()
    assert record["status"] == "completed"
    assert loop.memory.background_job is None


def test_lost_post_dispatch_observation_recovers_returned_receipt_without_replay(tmp_path):
    backend = ReceiptBackend()
    backend.fail_observation = 3
    loop = controller(backend, tmp_path)
    with pytest.raises(OSError):
        loop.step()
    assert loop.memory.pending["dispatch"] == "returned"
    restored = controller(backend, tmp_path, resume=True)
    result = restored.step()
    assert result["verified"] is False and restored.memory.background_job
    assert len(backend.calls) == 1


@pytest.mark.parametrize("dispatch", ["prepared", "ambiguous"])
def test_uncertain_dispatch_cannot_free_actor_even_with_running_native_receipt(tmp_path, dispatch):
    backend = ReceiptBackend()
    backend.lose_ack = True
    loop = controller(backend, tmp_path)
    loop.step()
    loop.memory.pending["dispatch"] = dispatch
    loop._save()
    resumed = controller(backend, tmp_path, resume=True)
    resumed.step()
    assert resumed.memory.background_job is None and resumed.memory.pending
    assert len(backend.calls) == 1
    backend.complete()
    resumed.step()
    assert resumed.memory.pending is None and len(backend.calls) == 1


def test_inventory_alone_and_success_text_cannot_verify_tracked_craft(tmp_path):
    backend = ReceiptBackend()
    backend.lose_ack = True
    loop = controller(backend, tmp_path)
    loop.step()
    backend.state.inventory["automation-science-pack"] = 100
    loop.step()
    assert loop.memory.pending and loop.memory.background_job is None
    assert len(backend.calls) == 1


def test_cancel_on_fresh_observation_blocks_selected_independent_dispatch(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    trigger = backend.observations + 2
    def cancel_on_fresh(value):
        if value.observations == trigger:
            value.state.factory["craft_job"]["status"] = "invalid"
    backend.before_observation = cancel_on_fresh
    result = loop.step()
    assert result["status"] == "uncertain" and loop.memory.background_job
    assert len(backend.calls) == 1


def test_uncertain_background_does_not_erase_unrelated_pending_mutation(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    plan = Plan("pending-gather", loop.target, "Possible partial action", (
        Step("factory_gather", "inventory", "iron-ore", 5,
             parameters={"resource": "iron-ore", "quantity": 5}),))
    loop.memory.active_plan = plan.to_dict()
    loop.memory.pending = {"action": "factory_gather", "started_tick": 10, "polls": 1, "dispatch": "ambiguous"}
    loop.memory.attempt = make_attempt(
        loop.memory.session_id, loop.target, loop.memory.active_plan,
        0, loop.memory.pending, process_id=loop._process_id,
    )
    backend.state.inventory["iron-ore"] = 5
    backend.state.factory["craft_job"]["status"] = "invalid"
    pending = deepcopy(loop.memory.pending)
    result = loop.step()
    assert result["status"] == "uncertain" and loop.memory.pending == pending
    assert len(backend.calls) == 1


def test_checkpoint_failure_poison_stops_further_calls(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    def fail(*args):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr(BackgroundMemory, "save", fail)
    with pytest.raises(OSError):
        loop.step()
    count = backend.observations
    with pytest.raises(RuntimeError, match="persistence"):
        loop.step()
    assert backend.observations == count and len(backend.calls) == 1


def test_paid_background_completion_save_failure_keeps_old_checkpoint(tmp_path, monkeypatch):
    backend = ReceiptBackend()
    original = controller(backend, tmp_path)
    original.memory.stalled_decisions = 3
    original.memory.failures["previous-plan"] = 2
    original._save()
    original.step()
    assert original.memory.stalled_decisions == 3
    backend.complete()
    checkpoint = tmp_path / "state.json"
    durable_before = checkpoint.read_bytes()

    resumed = controller(backend, tmp_path, resume=True)

    def fail_save(self, path):
        raise OSError("synthetic completion checkpoint failure")

    monkeypatch.setattr(BackgroundMemory, "save", fail_save)
    with pytest.raises(OSError, match="completion checkpoint"):
        resumed.reconcile_only()

    assert resumed._save_poisoned is True
    assert resumed.memory.stalled_decisions == 0
    assert checkpoint.read_bytes() == durable_before
    durable = BackgroundMemory.load(checkpoint, backend.state.session_id, resumed.target)
    assert durable.stalled_decisions == 3
    assert durable.failures == {"previous-plan": 2}
    assert durable.background_job is not None and durable.background_attempt is not None


def test_legacy_checkpoint_migration_is_read_only(tmp_path):
    path = tmp_path / "legacy.json"
    memory = CampaignMemory("test-factory", "automation_science")
    memory.save(path)
    before = path.read_bytes()
    restored = BackgroundMemory.load(path, "test-factory", "automation_science")
    assert restored.background_job is None and path.read_bytes() == before


def test_output_locks_check_both_costs_and_transferred_items(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    backend.state.factory["entities"]["utility:lab"] = machine("lab")
    backend.state.inventory["automation-science-pack"] = 1
    transfer = Step("factory_insert", "transfer", costs={"automation-science-pack": 1},
                    parameters={"role": "utility:lab", "item": "automation-science-pack",
                                "quantity": 1, "receipt": "deliver"})
    assert transfer.allowed(backend.state)
    assert not loop._step_allowed(transfer, backend.state)


def test_research_prefetch_starts_before_lab_empty_and_caps_remaining_demand():
    current = snapshot()
    current.factory.update(research="study", research_progress=0.1)
    current.factory["entities"]["utility:lab"] = machine("lab", input={"automation-science-pack": 4,
                                                                                   "logistic-science-pack": 20})
    assert research_demands(current, science_catalog()) == [("automation-science-pack", 16)]
    current.factory["research_progress"] = 0.97
    assert research_demands(current, science_catalog()) == []
    current.factory["entities"]["utility:lab"]["input"]["automation-science-pack"] = 1
    demands = research_demands(current, science_catalog())
    # Floating-point progress can conservatively add one unit; never a full buffer.
    assert 1 <= demands[0][1] <= 3


def test_independent_research_ingredient_is_gathered_while_pack_is_locked(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    current = backend.state
    current.factory.update(research="study", research_progress=0.0)
    current.factory["entities"]["utility:lab"] = machine("lab", input={"automation-science-pack": 0,
                                                                                    "logistic-science-pack": 0})
    before = deepcopy(current)
    plans = independent_candidates("rocket_launch", current, science_catalog(), loop._job())
    assert any(plan.steps[0].action == "factory_gather" and plan.steps[0].item == "copper-ore" for plan in plans)
    assert all(loop._job().permits(plan.steps[0]) and plan.steps[0].allowed(current) for plan in plans)
    assert current == before and len(plans) <= 8


def test_atomic_inventory_replaces_stale_fle_inventory_without_extra_call():
    from types import SimpleNamespace
    from jev_factorio.backends.craft_jobs import CraftJobFactory

    current = snapshot(inventory={"automation-science-pack": 0})
    current.factory["craft_job_inventory"] = {"tick": current.tick, "items": {"automation-science-pack": 1}}
    calls = []
    def observe(value):
        calls.append("observe")
        return value
    adapter = object.__new__(CraftJobFactory)
    adapter.native = SimpleNamespace(observe=observe)
    assert adapter.observe(current).inventory == {"automation-science-pack": 1}
    assert calls == ["observe"] and "craft_job_inventory" not in current.factory
    with pytest.raises(ValueError, match="atomic"):
        adapter.observe(current)


def test_background_checkpoint_accepts_independent_wait_with_none_parameters(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    loop.memory.active_plan = Plan("wait", loop.target, "wait", (
        Step("factory_wait", "crafting_idle"),)).to_dict()
    loop._save()
    restored = BackgroundMemory.load(tmp_path / "state.json", backend.state.session_id, loop.target)
    assert restored.background_job and restored.active_plan["steps"][0]["parameters"] is None


@pytest.mark.parametrize("arguments", [
    ["--background-work"],
    ["--background-work", "--controller", "hierarchical", "--factory-scheduling", "ready-work"],
    ["--background-work", "--controller", "hierarchical", "--backend", "fle"],
    ["--background-work", "--controller", "hierarchical", "--backend", "fle",
     "--factory-scheduling", "ready-work", "--target", "bootstrap_mining"],
])
def test_cli_rejects_unsupported_background_mode_before_backend_start(monkeypatch, arguments):
    import sys
    from jev_factorio import main

    monkeypatch.setattr(sys, "argv", ["jev-factorio", *arguments])
    monkeypatch.setattr(main, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: pytest.fail("backend started"))
    with pytest.raises(SystemExit) as error:
        main.cli()
    assert error.value.code == 2


def test_background_failure_after_independent_dispatch_retains_pending(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    original = backend.execute
    def cancel_after_execute(action, parameters):
        result = original(action, parameters)
        backend.state.factory["craft_job"]["status"] = "invalid"
        return result
    backend.execute = cancel_after_execute
    record = loop.step()
    assert record["status"] == "uncertain" and record["verified"] is False
    assert loop.memory.pending["dispatch"] == "returned"
    assert loop.memory.pending["action"] == "factory_gather"
    assert backend.state.inventory["iron-ore"] == 5
    assert len(backend.calls) == 2
    loop.step()
    assert loop.memory.pending and len(backend.calls) == 2


def test_craft_completion_does_not_replace_native_milestone_production_counter(tmp_path):
    from jev_factorio.planning.goals import completed

    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    backend.complete()
    backend.state.factory["produced"] = {}
    assert loop._job().observe(backend.state)
    assert not completed("automation_science", backend.state)
