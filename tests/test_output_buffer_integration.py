"""Actual planner/controller integration over synthetic snapshots, not native saves."""
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.factory_contract import allowed
from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.output_buffers import OutputBufferPlanner
from jev_factorio.skills import Plan, Step
from test_factory import catalog, machine, recipe, snapshot


def setup(ready=False):
    data = catalog()
    data.recipes["wooden-chest"] = recipe("wooden-chest", {"wood": 2})
    data.recipes["burner-inserter"] = recipe("burner-inserter", {"iron-plate": 1})
    state = snapshot(tick=200, inventory={"wooden-chest": 1, "burner-inserter": 1, "coal": 5})
    source = "recipe:iron-plate"
    state.factory["entities"][source] = machine(fuel={"coal": 10}, input={"iron-ore": 19},
                                              output={"iron-plate": 2}, crafting=True,
                                              products_finished=20, recipe="iron-plate")
    row = {"source": source, "source_unit": 17, "layout": "output:17:1:0:4", "item": "iron-plate",
           "state": "proposed", "chest_role": "output-chest:17", "parts": {}, "held": 0,
           "topology": False}
    state.factory["output_buffers"] = {"protocol": 1, "tick": state.tick,
        "session_id": state.session_id, "sources": {source: row}}
    if ready:
        for part, role, name, unit in (("chest", "output-chest:17", "wooden-chest", 18),
                                      ("inserter", "output-arm:17", "burner-inserter", 19)):
            row["parts"][part] = {"role": role, "unit_number": unit, "receipt": "r:"+part, "paid": 1}
            state.factory["entities"][role] = machine(name, unit_number=unit, fuel={"coal": 5})
        row.update(state="ready", topology=True, flow={"layout": row["layout"], "source_unit": 17,
            "first_tick": 30, "last_tick": 180, "positive_samples": 3, "received": 3, "conservation": True})
    return state, data, row


def need(state, data, amount=20):
    return OutputBufferPlanner(data, state, "rocket_launch")._need("iron-plate", amount)


def test_builds_first_component_and_roundtrips_exact_identity():
    state, data, row = setup()
    plan = need(state, data)
    assert plan.steps[0].action == "factory_buffer_build"
    assert plan.steps[0].parameters["part"] == "chest"
    assert plan.steps[0].costs == {"wooden-chest": 1}
    assert plan.steps[0].allowed(state)
    assert not plan.steps[0].satisfied(state)
    assert Plan.from_dict(plan.to_dict()) == plan


def test_missing_kit_uses_ordinary_crafting_without_recursive_buffer_construction():
    state, data, row = setup()
    state.inventory = {"wood": 2, "coal": 5}
    plan = need(state, data)
    assert plan.steps[0].action == "factory_craft"
    assert plan.steps[0].parameters["recipe"] == "wooden-chest"


def test_bootstrap_and_small_critical_requests_do_not_buy_infrastructure():
    state, data, row = setup()
    assert need(state, data, 1).steps[0].action == "factory_extract"
    state.factory["entities"][row["source"]]["products_finished"] = 0
    assert need(state, data).steps[0].action != "factory_buffer_build"


def test_ready_buffer_collects_batch_not_racing_furnace_output():
    state, data, row = setup(True)
    state.factory["entities"][row["chest_role"]]["output"] = {"iron-plate": 2}
    step = need(state, data).steps[0]
    assert step.action == "factory_wait" and step.threshold == 10
    assert step.verification == {"role": row["chest_role"]}
    state.factory["entities"][row["chest_role"]]["output"] = {"iron-plate": 12}
    step = need(state, data).steps[0]
    assert step.action == "factory_extract" and step.parameters["role"] == row["chest_role"]
    assert step.parameters["quantity"] == 12


def test_tail_and_one_plate_requirement_do_not_wait_for_ten():
    state, data, row = setup(True)
    state.factory["entities"][row["chest_role"]]["output"] = {"iron-plate": 1}
    assert need(state, data, 1).steps[0].parameters["quantity"] == 1
    src = state.factory["entities"][row["source"]]
    src.update(output={}, input={}, crafting=False)
    assert need(state, data).steps[0].parameters["quantity"] == 1


def test_placement_is_not_flow_success_and_ordinary_transfer_contract_is_guarded():
    state, data, row = setup(True)
    row["flow"] = {}
    plan = need(state, data)
    assert plan.steps[0].effect == "buffer_flow"
    assert not plan.steps[0].satisfied(state)
    assert not allowed("factory_extract", {"role": row["source"], "item": "iron-plate",
                                         "quantity": 1, "receipt": "t1"}, state)


def test_refuels_inserter_with_existing_transfer_protocol():
    state, data, row = setup(True)
    arm = row["parts"]["inserter"]["role"]
    state.factory["entities"][arm]["fuel"] = {"coal": 1}
    step = need(state, data).steps[0]
    assert step.action == "factory_insert" and step.parameters["role"] == arm
    assert step.costs == {"coal": 4}


class BufferBackend:
    output_buffers_supported = True

    def __init__(self):
        self.state, self.catalog, self.row = setup()
        self.calls = []
        self.mode = "success"

    def enable_factory(self):
        return self.catalog

    def observe(self):
        return deepcopy(self.state)

    def execute(self, action, parameters):
        self.calls.append((action, deepcopy(parameters)))
        assert action == "factory_buffer_build"
        self.state.inventory["wooden-chest"] -= 1
        self.row["state"] = "building"
        self.row["parts"]["chest"] = {"role": self.row["chest_role"], "unit_number": 18,
                                      "receipt": parameters["receipt"], "paid": 1}
        self.state.factory["entities"][self.row["chest_role"]] = machine("wooden-chest", unit_number=18)
        if self.mode == "fault":
            self.row["state"] = "fault"
        if self.mode == "lost_ack":
            raise TimeoutError("synthetic lost response")
        return "synthetic return"


def loop_for(backend, tmp_path):
    kind = buffered_loop_type(HierarchicalLoop)
    loop = kind(backend, policy="deterministic", factory_scheduling="ready-work",
                target="rocket_launch", checkpoint=str(tmp_path / "state.json"), tick_seconds=0)
    loop.memory = loop.memory_type(backend.state.session_id, "rocket_launch", active_goal="rocket_launch",
                                completed_goals={"stockpile_fuel": 1, "bootstrap_mining": 2}, last_tick=200)
    # Isolate buffer dispatch from the unrelated rocket-sized research tree.
    loop._compile_candidates = lambda s: ([need(s, backend.catalog)], "")
    return loop


def test_real_dispatcher_requires_observed_paid_component(tmp_path):
    backend = BufferBackend()
    loop = loop_for(backend, tmp_path)
    result = loop.step()
    assert result["verified"] and loop.memory.pending is None
    assert len(backend.calls) == 1 and backend.state.inventory["wooden-chest"] == 0
    saved = loop.memory_type.load(tmp_path / "state.json", backend.state.session_id, "rocket_launch")
    assert saved.pending is None


def test_lost_build_acknowledgment_reconciles_without_rebuilding(tmp_path):
    backend = BufferBackend(); backend.mode = "lost_ack"
    loop = loop_for(backend, tmp_path)
    assert not loop.step()["verified"]
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert loop.step()["verified"]
    assert len(backend.calls) == 1


def test_fault_after_build_preserves_pending_reservations_and_blocks_replay(tmp_path):
    backend = BufferBackend(); backend.mode = "fault"
    loop = loop_for(backend, tmp_path)
    result = loop.step()
    assert result["status"] == "uncertain" and not result["verified"]
    assert loop.memory.pending and loop.memory.reservations
    loop.step()
    assert len(backend.calls) == 1 and loop.memory.pending


def test_missing_native_extension_never_resets_world_or_dispatches(tmp_path):
    backend = BufferBackend()
    loop = loop_for(backend, tmp_path)
    backend.state.factory.pop("output_buffers")
    assert loop.step()["status"] == "uncertain"
    assert not backend.calls


@pytest.mark.parametrize("arguments", [
    ["--furnace-output-buffers"],
    ["--controller", "hierarchical", "--backend", "mock", "--factory-scheduling", "ready-work",
     "--furnace-output-buffers"],
])
def test_cli_rejects_invalid_opt_in_before_backend(monkeypatch, arguments):
    from jev_factorio import main
    monkeypatch.setattr("sys.argv", ["jev-factorio", *arguments])
    monkeypatch.setattr(main, "make_backend", lambda *a, **k: pytest.fail("backend initialized"))
    with pytest.raises(SystemExit) as failure:
        main.cli()
    assert failure.value.code == 2


@pytest.mark.parametrize("part", ["chest", "inserter"])
def test_paid_component_placement_does_not_require_future_commissioning_coal(part):
    state, data, row = setup()
    state.inventory.pop("coal")
    if part == "inserter":
        row["state"] = "building"
        row["parts"]["chest"] = {"role": row["chest_role"], "unit_number": 18,
                                  "receipt": "paid-chest", "paid": 1}
        state.factory["entities"][row["chest_role"]] = machine("wooden-chest", unit_number=18)
    plan = need(state, data)
    assert plan.steps[0].action == "factory_buffer_build"
    assert plan.steps[0].parameters["part"] == part
    assert plan.steps[0].allowed(state)
    assert plan.steps[0].costs == {"wooden-chest" if part == "chest" else "burner-inserter": 1}


@pytest.mark.parametrize("ready,expected", [(2, "factory_buffer_build"),
                                           (10, "factory_extract"), (20, "factory_extract")])
def test_unbuilt_buffer_preserves_batched_existing_furnace_pickup(ready, expected):
    state, data, row = setup()
    state.inventory.pop("coal")
    state.factory["entities"][row["source"]]["output"] = {"iron-plate": ready}
    before = deepcopy(state)
    plan = need(state, data)
    assert plan.steps[0].action == expected
    assert plan.steps[0].allowed(state)
    if expected == "factory_extract":
        assert plan.steps[0].parameters["role"] == row["source"]
        assert plan.steps[0].parameters["quantity"] == ready
        assert plan.materials["output_pickup"]["planner_item_path"] == ["iron-plate"]
    assert state == before


def test_placed_inserter_still_requires_fuel_acquisition_then_native_flow():
    state, data, row = setup(True)
    state.inventory.pop("coal")
    row["flow"] = {}
    arm = row["parts"]["inserter"]["role"]
    state.factory["entities"][arm]["fuel"] = {}
    state.factory["entities"][row["source"]]["output"] = {"iron-plate": 20}
    plan = need(state, data)
    assert plan.steps[0].action == "factory_gather"
    assert plan.steps[0].item == "coal"
    assert plan.materials["fuel_service"]["consumers"][0]["role"] == arm
    assert not plan.steps[0].satisfied(state)
