from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace

import pytest

from jev_factorio.factory_contract import allowed, connected, satisfied, validate_command
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.factory import FactoryPlanner, compile_factory
from jev_factorio.skills import Plan, Step
from jev_factorio.state import GameSnapshot
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.backends.native_factory import NativeFactory
from jev_factorio.jev_client import MockJevClient
from jev_factorio.memory import CampaignMemory
from jev_factorio.telemetry import make_attempt


def recipe(name, ingredients, category="crafting", enabled=True, product=None):
    return {
        "name": name, "category": category, "enabled": enabled, "hidden": False, "energy": 1,
        "ingredients": [{"name": item, "amount": count, "type": "item"}
                        for item, count in ingredients.items()],
        "products": [{"name": product or name, "amount": 1, "type": "item", "probability": 1}],
    }


def catalog():
    data = {
        "version": "2.0.77", "mods": {"base": "2.0.77"}, "hand_categories": {"crafting": True},
        "recipes": {
            "stone-furnace": recipe("stone-furnace", {"stone": 5}),
            "iron-plate": recipe("iron-plate", {"iron-ore": 1}, "smelting"),
        },
        "technologies": {},
        "machines": {"stone-furnace": {
            "categories": {"smelting": True}, "burner": True, "electric": False, "speed": 1,
        }},
    }
    return Catalog.from_dict(data)


def snapshot(**changes):
    state = GameSnapshot(session_id="test-factory", world_kind="mock", tick=10,
                         researched=[], nearby_resources={"coal": 0, "stone": 10, "iron-ore": 5},
                         factory={"player_connected": True, "player_bound": True, "crafting_queue": 0,
                                  "entities": {}, "receipts": {}, "produced": {}})
    for key, value in changes.items():
        setattr(state, key, value)
    if state.world_kind == "mock":
        state.factory.setdefault("fair_resource_targets", {
            item: {"name": item, "surface_index": 1, "position": {"x": distance, "y": 0}}
            for item, distance in state.nearby_resources.items()
            if item in {"coal", "iron-ore", "copper-ore", "stone"}
        })
    return state


def machine(name="stone-furnace", **changes):
    result = {"name": name, "unit_number": 17, "position": {"x": 0, "y": 0},
              "fuel": {}, "input": {}, "output": {}, "fluids": {}, "energy": 0}
    return {**result, **changes}


def plan(state, target="iron_smelting"):
    plans, blocker = compile_factory(target, state, catalog())
    assert not blocker
    assert len(plans) == 1
    return plans[0].steps[0]


def test_native_catalog_version_and_mod_gates():
    with pytest.raises(ValueError, match="2.0"):
        Catalog.from_dict({"version": "2.1.0"})
    with pytest.raises(ValueError, match="base-game"):
        Catalog.from_dict({"version": "2.0.77", "mods": {"base": "2.0.77", "space-age": "2.0.77"}})


def test_smelting_requires_resources_then_native_crafting_then_placement():
    state = snapshot()
    step = plan(state)
    assert step.action == "factory_gather" and step.item == "stone"
    state.inventory["stone"] = 5
    step = plan(state)
    assert step.action == "factory_craft" and step.costs == {"stone": 5}
    assert step.parameters == {"recipe": "stone-furnace", "batches": 1}
    state.inventory = {"stone-furnace": 1}
    step = plan(state)
    assert step.action == "factory_place" and step.costs == {"stone-furnace": 1}
    assert step.parameters["role"] == "recipe:iron-plate"


def test_smelting_never_inserts_ore_without_fuel():
    state = snapshot(inventory={"iron-ore": 10})
    state.factory["entities"]["recipe:iron-plate"] = machine()
    step = plan(state)
    assert step.action == "factory_gather" and step.item == "coal"
    state.inventory["coal"] = 50
    step = plan(state)
    assert step.action == "factory_insert" and step.parameters["item"] == "coal"
    assert step.costs == {"coal": 5}


def test_raw_gather_commits_one_observed_fair_target_with_a_unique_postcondition():
    state = snapshot(inventory={"wood": 12},
                     nearby_resources={"wood": 0.65, "coal": 1, "stone": 1, "iron-ore": 1})
    state.factory["fair_resource_targets"] = {"wood": {
        "name": "tree-01", "surface_index": 1, "position": {"x": 3, "y": 4},
    }}

    first = FactoryPlanner(catalog(), state, "rocket_launch")._need("wood", 20)

    assert first.id == (
        'factory:factory_gather:wood:target:13:site:'
        '{"name":"tree-01","position":{"x":3.0,"y":4.0},"surface_index":1}'
    )
    assert first.steps[0].parameters == {"resource": "wood", "quantity": 1}
    assert first.steps[0].threshold == 13

    state.inventory["wood"] = 13
    later = FactoryPlanner(catalog(), state, "rocket_launch")._need("wood", 20)

    assert later.id == first.id.replace("target:13:", "target:14:")
    assert later.steps[0].parameters == {"resource": "wood", "quantity": 1}


@pytest.mark.parametrize("changed_site", [
    {"name": "tree-02"},
    {"surface_index": 2},
    {"position": {"x": 3, "y": 5}},
])
def test_wood_failure_budget_is_scoped_to_the_observed_native_site(changed_site):
    state = snapshot(inventory={"wood": 12},
                     nearby_resources={"wood": 0.65, "coal": 1, "stone": 1, "iron-ore": 1})
    site = {"name": "tree-01", "surface_index": 1, "position": {"x": 3, "y": 4}}
    state.factory["fair_resource_targets"] = {"wood": site}

    first = FactoryPlanner(catalog(), state, "rocket_launch")._need("wood", 20)
    repeated = FactoryPlanner(catalog(), state, "rocket_launch")._need("wood", 20)
    assert repeated.id == first.id
    state.factory["fair_resource_targets"] = {"wood": {**site, **changed_site}}
    later = FactoryPlanner(catalog(), state, "rocket_launch")._need("wood", 20)

    assert first.id != later.id
    failures = {first.id: 2}
    assert [plan.id for plan in (first, later) if failures.get(plan.id, 0) < 2] == [later.id]


def test_wood_without_a_fair_native_identity_returns_to_observation():
    state = snapshot(inventory={"wood": 12},
                     nearby_resources={"wood": 0.65, "coal": 1, "stone": 1, "iron-ore": 1})

    plan = FactoryPlanner(catalog(), state, "rocket_launch")._need("wood", 20)

    assert plan.steps[0].action == "factory_explore"
    assert plan.steps[0].parameters == {"radius": 12}


@pytest.mark.parametrize("item", ["coal", "iron-ore", "copper-ore", "stone"])
def test_ore_failure_budget_preserves_site_and_inventory_target(item):
    state = snapshot(inventory={item: 12}, nearby_resources={item: 5})
    scheduler = FactoryPlanner(catalog(), state, "rocket_launch")
    first = scheduler._need(item, 20)
    repeated = FactoryPlanner(catalog(), state, "rocket_launch")._need(item, 20)
    assert repeated.id == first.id
    failures = {f"factory:factory_gather:{item}": 2, first.id: 2}
    saved_failures = dict(failures)

    state.factory["fair_resource_targets"][item]["position"]["x"] = 6
    relocated = FactoryPlanner(catalog(), state, "rocket_launch")._need(item, 20)
    assert relocated.id != first.id
    assert failures.get(relocated.id, 0) == 0

    changed_threshold = FactoryPlanner(catalog(), state, "rocket_launch")._need(item, 21)
    assert changed_threshold.id != relocated.id
    assert changed_threshold.steps[0].threshold == 21
    assert failures == saved_failures
    assert failures[repeated.id] == 2


@pytest.mark.parametrize("item", ["coal", "iron-ore", "copper-ore", "stone"])
def test_ore_cached_coordinate_without_native_site_cannot_authorize_gather(item):
    state = snapshot(nearby_resources={item: 5})
    state.factory.pop("fair_resource_targets")
    candidate = FactoryPlanner(catalog(), state, "rocket_launch")._need(item, 20)
    assert candidate.steps[0].action == "factory_explore"


def test_refueling_respects_the_remaining_native_fuel_stack_capacity():
    state = snapshot(inventory={"coal": 50})
    state.factory["entities"]["recipe:iron-plate"] = machine(fuel={"coal": 4})
    step = plan(state)
    assert step.action == "factory_insert"
    assert step.parameters["quantity"] == 46
    assert step.costs == {"coal": 46}


def test_machine_outputs_are_collected_before_requesting_more_inputs():
    state = snapshot()
    state.factory["entities"]["recipe:iron-plate"] = machine(output={"iron-plate": 7})
    step = plan(state)
    assert step.action == "factory_extract"
    assert step.parameters["quantity"] == 7
    assert step.allowed(state)
    assert not step.satisfied(state)


def test_inflight_machine_batch_is_not_paid_for_twice():
    state = snapshot()
    state.factory["entities"]["recipe:iron-plate"] = machine(
        fuel={"coal": 10}, input={"iron-ore": 9}, crafting=True
    )
    step = plan(state)
    assert step.action == "factory_wait"
    assert step.effect == "machine_output"


def test_machine_batches_fit_one_native_ingredient_stack():
    data = catalog()
    data.recipes["iron-plate"]["ingredients"][0]["amount"] = 20
    data.stack_sizes["iron-ore"] = 50
    state = snapshot(inventory={"iron-ore": 400})
    state.factory["entities"]["recipe:iron-plate"] = machine(fuel={"coal": 10})
    step = FactoryPlanner(data, state, "iron_smelting")._production(
        data.recipes["iron-plate"], "recipe:iron-plate", 20, ()
    ).steps[0]
    assert step.parameters["quantity"] == 40
    state.factory["entities"]["recipe:iron-plate"]["input"] = {"iron-ore": 40}
    assert FactoryPlanner(data, state, "iron_smelting")._production(
        data.recipes["iron-plate"], "recipe:iron-plate", 20, ()
    ) is None


def test_factory_parameters_round_trip_through_checkpoint_plan():
    state = snapshot(inventory={"stone": 5})
    original = FactoryPlanner(catalog(), state, "iron_smelting").plan()
    assert Plan.from_dict(original.to_dict()) == original


def test_inflight_native_crafting_is_observed_not_requeued():
    state = snapshot()
    state.factory["crafting_queue"] = 5
    step = plan(state)
    assert step.action == "factory_wait" and step.effect == "crafting_idle"
    assert not step.satisfied(state)
    state.factory["crafting_queue"] = 0
    assert step.satisfied(state)


def test_disconnected_player_blocks_native_binding_and_crafting():
    state = snapshot(inventory={"stone": 5})
    state.factory["player_connected"] = False
    state.factory["player_bound"] = False
    plans, blocker = compile_factory("iron_smelting", state, catalog())
    assert not plans
    assert "connected game client" in blocker
    assert not allowed("factory_bind", {}, state)
    state.factory["player_bound"] = True
    assert not allowed("factory_craft", {"recipe": "stone-furnace", "batches": 1}, state)


@pytest.mark.parametrize("quantity", [-1, 0, 201, True, float("nan"), 1.5])
def test_invalid_native_transfer_quantities_are_rejected(quantity):
    with pytest.raises(ValueError):
        validate_command("factory_insert", {
            "role": "machine", "item": "coal", "quantity": quantity, "receipt": "unique",
        })


def test_native_transfer_receipt_requires_matching_entity_and_quantity():
    state = snapshot()
    state.factory["entities"]["furnace"] = machine()
    parameters = {"role": "furnace", "item": "coal", "quantity": 5, "receipt": "transfer:1"}
    state.factory["receipts"]["transfer:1"] = {
        "role": "furnace", "item": "coal", "quantity": 4, "unit_number": 17,
        "extracting": False,
    }
    assert not satisfied("transfer", "", 0, parameters, state, "factory_insert")
    state.factory["receipts"]["transfer:1"]["quantity"] = 5
    assert satisfied("transfer", "", 0, parameters, state, "factory_insert")
    assert not satisfied("transfer", "", 0, parameters, state, "factory_extract")
    state.factory["entities"]["furnace"]["unit_number"] = 18
    assert not satisfied("transfer", "", 0, parameters, state, "factory_insert")


def test_connections_require_live_topology_not_a_command_receipt():
    state = snapshot()
    state.factory["connections"] = {"source:target": True}
    state.factory["entities"] = {
        "source": machine(electric_network_id=4,
                          fluid_ports=[{"id": 5, "fluid": "water"}, {"id": 6, "fluid": "steam"}]),
        "target": machine(electric_network_id=7, fluid_ports=[{"id": 6, "fluid": "steam"}]),
    }
    assert not connected(state.factory, "source", "target", "small-electric-pole", "electricity")
    assert not connected(state.factory, "source", "target", "pipe", "water")
    assert connected(state.factory, "source", "target", "pipe", "steam")
    state.factory["entities"]["target"]["electric_network_id"] = 4
    assert connected(state.factory, "source", "target", "small-electric-pole", "electricity")


def test_native_pipe_connection_uses_native_fluid_handler_points(monkeypatch):
    fle = pytest.importorskip("fle.env")
    pump = SimpleNamespace(
        name="offshore-pump",
        connection_points=[fle.Position(x=-36.5, y=-27.5)],
        position=fle.Position(x=-36.5, y=-28.5),
    )
    boiler = SimpleNamespace(
        name="boiler",
        connection_points=[
            fle.Position(x=-28.5, y=-27.5),
            fle.Position(x=-24.5, y=-27.5),
        ],
        steam_output_point=fle.Position(x=-26.5, y=-29.5),
        position=fle.Position(x=-26.5, y=-28),
    )
    connections = []
    preflight_budget = object()
    factory = object.__new__(NativeFactory)
    factory.backend = SimpleNamespace(
        _tools=SimpleNamespace(),
        _fair=SimpleNamespace(
            select_feasible_pipe_pair=lambda sources, targets, fluid: (
                sources[0], targets[0], preflight_budget,
            ),
            connect=lambda *arguments, **keywords:
                connections.append((arguments, keywords)),
        ),
    )
    monkeypatch.setattr(factory, "entity", lambda role: pytest.fail("FLE port geometry used"))
    def native_points(role, fluid, *, output):
        assert fluid == "water"
        assert output == (role == "utility:water")
        return {"utility:water": pump, "utility:boiler": boiler}[role].connection_points
    monkeypatch.setattr(factory, "native_fluid_connection_points", native_points)
    monkeypatch.setattr(factory, "call", lambda *arguments: "{}")
    prototype = SimpleNamespace(value=("pipe", object()))
    monkeypatch.setattr(factory, "prototype", lambda name: prototype)

    outcome = factory.execute("factory_connect", {
        "source": "utility:water", "target": "utility:boiler",
        "kind": "pipe", "fluid": "water",
    })

    assert outcome.startswith("Constructed pipe connection")
    assert connections == [((
        fle.Position(x=-36.5, y=-27.5), fle.Position(x=-28.5, y=-27.5),
        prototype, "water",
    ), {"identity": {
        "source": "utility:water", "target": "utility:boiler",
        "kind": "pipe", "fluid": "water",
    }, "preflight_budget": preflight_budget})]


def test_native_observation_admits_only_a_fair_native_wood_target(monkeypatch):
    fle = pytest.importorskip("fle.env")
    calls = []

    class Tools:
        def nearest(self, resource):
            assert resource is not fle.Resource.Wood
            return fle.Position(x=9, y=7)

    factory = object.__new__(NativeFactory)
    factory.catalog = SimpleNamespace(version="2.0.77")
    factory.backend = SimpleNamespace(
        _drill=None,
        _resources={},
        _tools=Tools(),
        _fair=SimpleNamespace(call=lambda *arguments: calls.append(arguments) or {
            "position": {"x": 3, "y": 4}, "name": "tree-01", "surface_index": 1,
        }),
    )
    monkeypatch.setattr(factory, "command", lambda script: (
        '{"tick": 17, "entities": {}, "researched": [], '
        '"rockets_launched": 0, "rocket_baseline": 0}'
    ))
    state = snapshot(world_kind="fle", player_position=(0, 0))

    observed = factory.observe(state)

    assert calls == [
        ("discover_mine_target", item, {"x": 0, "y": 0}, 256)
        for item in ("wood", "coal", "iron-ore", "copper-ore", "stone")
    ]
    assert factory.backend._resources["wood"] == fle.Position(x=3, y=4)
    assert observed.nearby_resources["wood"] == 5
    assert observed.factory["fair_resource_targets"] == {
        "wood": {"name": "tree-01", "surface_index": 1,
                 "position": {"x": 3.0, "y": 4.0}}
    }


def test_native_observation_does_not_fall_back_to_stale_fle_resources(monkeypatch):
    fle = pytest.importorskip("fle.env")

    class Tools:
        def nearest(self, resource):
            assert resource is not fle.Resource.Wood
            return fle.Position(x=9, y=7)

    factory = object.__new__(NativeFactory)
    factory.catalog = SimpleNamespace(version="2.0.77")
    factory.backend = SimpleNamespace(
        _drill=None,
        _resources={item: fle.Position(x=3, y=4)
                    for item in ("wood", "coal", "iron-ore", "copper-ore", "stone")},
        _tools=Tools(),
        _fair=SimpleNamespace(call=lambda *arguments: {}),
    )
    monkeypatch.setattr(factory, "command", lambda script: (
        '{"tick": 17, "entities": {}, "researched": [], '
        '"rockets_launched": 0, "rocket_baseline": 0}'
    ))

    observed = factory.observe(snapshot(
        world_kind="fle", player_position=(0, 0),
        nearby_resources={item: 5 for item in ("wood", "coal", "iron-ore", "copper-ore", "stone")},
    ))

    for item in ("wood", "coal", "iron-ore", "copper-ore", "stone"):
        assert item not in factory.backend._resources
        assert item not in observed.nearby_resources
    assert "fair_resource_targets" not in observed.factory


def test_native_observation_uses_fair_site_admission_for_all_ores(monkeypatch):
    fle = pytest.importorskip("fle.env")
    requested = []

    class Tools:
        def nearest(self, resource):
            assert resource in {fle.Resource.Water, fle.Resource.CrudeOil}
            return fle.Position(x=9, y=7)

    def native_mine_target(function, resource, center, radius):
        assert function == "discover_mine_target"
        assert center == {"x": 0, "y": 0}
        assert radius == 256
        requested.append(resource)
        if resource == "wood":
            return {}
        return {
            "name": resource, "surface_index": 1,
            "position": {"x": 3, "y": 4},
        }

    factory = object.__new__(NativeFactory)
    factory.catalog = SimpleNamespace(version="2.0.77")
    factory.backend = SimpleNamespace(
        _drill=None,
        _resources={},
        _tools=Tools(),
        _fair=SimpleNamespace(call=native_mine_target),
    )
    monkeypatch.setattr(factory, "command", lambda script: (
        '{"tick": 17, "entities": {}, "researched": [], '
        '"rockets_launched": 0, "rocket_baseline": 0}'
    ))

    observed = factory.observe(snapshot(world_kind="fle", player_position=(0, 0)))

    assert requested == ["wood", "coal", "iron-ore", "copper-ore", "stone"]
    for item in requested[1:]:
        assert observed.nearby_resources[item] == 5
        assert factory.backend._resources[item] == fle.Position(x=3, y=4)
        assert observed.factory["fair_resource_targets"][item] == {
            "name": item, "surface_index": 1, "position": {"x": 3.0, "y": 4.0},
        }


def test_native_observation_discovers_ore_across_the_generated_exploration_area(monkeypatch):
    fle = pytest.importorskip("fle.env")
    calls = []

    class Tools:
        def nearest(self, resource):
            assert resource in {fle.Resource.Water, fle.Resource.CrudeOil}
            return fle.Position(x=9, y=7)

    def discover(function, resource, center, radius):
        calls.append((function, resource, center, radius))
        if resource != "copper-ore":
            return {}
        return {
            "name": "copper-ore", "surface_index": 1,
            "position": {"x": 640, "y": -128},
        }

    factory = object.__new__(NativeFactory)
    factory.catalog = SimpleNamespace(version="2.0.77")
    factory.backend = SimpleNamespace(
        _drill=None, _resources={}, _tools=Tools(), _fair=SimpleNamespace(call=discover),
    )
    monkeypatch.setattr(factory, "command", lambda script: (
        '{"tick": 17, "entities": {}, "researched": [], '
        '"rockets_launched": 0, "rocket_baseline": 0, "exploration_radius": 32}'
    ))

    observed = factory.observe(snapshot(world_kind="fle", player_position=(0, 0)))

    assert ("discover_mine_target", "copper-ore", {"x": 0, "y": 0}, 1024) in calls
    assert observed.nearby_resources["copper-ore"] == pytest.approx(
        (640 ** 2 + 128 ** 2) ** 0.5
    )
    assert observed.factory["fair_resource_targets"]["copper-ore"] == {
        "name": "copper-ore", "surface_index": 1, "position": {"x": 640.0, "y": -128.0},
    }


@pytest.mark.parametrize("item", ["wood", "coal", "iron-ore", "copper-ore", "stone"])
@pytest.mark.parametrize("invalid_site", [
    {"name": None}, {"name": ""}, {"name": " "},
    {"surface_index": None}, {"surface_index": 0}, {"surface_index": -1},
    {"surface_index": True}, {"surface_index": 1.5},
    {"position": {"x": True, "y": 4}},
    {"position": {"x": float("inf"), "y": 4}},
    {"position": {"x": 3, "y": float("nan")}},
    {"position": {}},
])
def test_native_observation_rejects_resource_without_a_native_site(monkeypatch, invalid_site, item):
    fle = pytest.importorskip("fle.env")

    class Tools:
        def nearest(self, resource):
            assert resource is not fle.Resource.Wood
            return fle.Position(x=9, y=7)

    factory = object.__new__(NativeFactory)
    factory.catalog = SimpleNamespace(version="2.0.77")
    factory.backend = SimpleNamespace(
        _drill=None,
        _resources={},
        _tools=Tools(),
        _fair=SimpleNamespace(call=lambda *arguments: {
            "position": {"x": 3, "y": 4},
            "name": "tree-01" if item == "wood" else item, "surface_index": 1,
            **invalid_site,
        }),
    )
    monkeypatch.setattr(factory, "command", lambda script: (
        '{"tick": 17, "entities": {}, "researched": [], '
        '"rockets_launched": 0, "rocket_baseline": 0}'
    ))

    observed = factory.observe(snapshot(world_kind="fle", player_position=(0, 0)))

    assert item not in factory.backend._resources
    assert item not in observed.nearby_resources
    assert "fair_resource_targets" not in observed.factory

    state = snapshot(inventory={item: 12}, nearby_resources={item: 5})
    state.factory["fair_resource_targets"] = {item: {
        "position": {"x": 3, "y": 4},
        "name": "tree-01" if item == "wood" else item, "surface_index": 1,
        **invalid_site,
    }}
    assert FactoryPlanner(catalog(), state, "rocket_launch")._fair_resource_identity(item, 13) is None


def test_native_fluid_points_keep_typed_filters_and_boiler_steam_output():
    water = SimpleNamespace(x=1, y=2, type="water")
    steam = SimpleNamespace(x=3, y=4, type="steam")
    entity = SimpleNamespace(
        input_connection_points=[water, steam],
        output_connection_points=[water, steam],
        connection_points=[SimpleNamespace(x=9, y=9)],
        steam_output_point=steam,
    )

    assert NativeFactory.fluid_connection_points(entity, "water", output=False) == [water]
    assert NativeFactory.fluid_connection_points(entity, "water", output=True) == [water]
    assert NativeFactory.fluid_connection_points(entity, "steam", output=True) == [steam]
    with pytest.raises(ValueError, match="input fluid"):
        NativeFactory.fluid_connection_points(entity, "crude-oil", output=False)


def test_native_generator_boundary_points_are_normalized_to_pipe_cells():
    fle = pytest.importorskip("fle.env")
    engine = SimpleNamespace(
        name="steam-engine",
        position=fle.Position(x=-16.5, y=-27.5),
        connection_points=[
            fle.Position(x=-16.5, y=-30),
            fle.Position(x=-16.5, y=-25),
        ],
    )

    assert NativeFactory.fluid_connection_points(engine, "steam", output=False) == [
        fle.Position(x=-16.5, y=-30.5),
        fle.Position(x=-16.5, y=-24.5),
    ]


def test_native_typed_output_does_not_treat_unknown_fluid_as_compatible():
    unknown = SimpleNamespace(x=1, y=2, type="")
    entity = SimpleNamespace(output_connection_points=[unknown])
    with pytest.raises(ValueError, match="output fluid"):
        NativeFactory.fluid_connection_points(entity, "water", output=True)


def test_recipe_and_technology_cycles_block_before_actuation():
    data = catalog()
    data.recipes["stone-furnace"]["enabled"] = False
    data.technologies["loop"] = {
        "enabled": True, "prerequisites": ["loop"],
        "effects": [{"type": "unlock-recipe", "recipe": "stone-furnace"}],
    }
    plans, blocker = compile_factory("iron_smelting", snapshot(), data)
    assert not plans and "cycle" in blocker


def test_research_trigger_requires_native_production_not_carried_items():
    data = catalog()
    data.technologies["steam-power"] = {
        "enabled": True, "prerequisites": [], "effects": [],
        "trigger": {"type": "craft-item", "item": {"name": "iron-plate"}, "count": 50},
    }
    state = snapshot(inventory={"iron-plate": 50})
    state.factory["produced"]["iron-plate"] = 0
    scheduler = FactoryPlanner(data, state, "rocket_launch")
    assert scheduler._research("steam-power").steps[0].action == "factory_gather"
    state.factory["produced"]["iron-plate"] = 50
    step = FactoryPlanner(data, state, "rocket_launch")._research("steam-power").steps[0]
    assert step.action == "factory_wait" and step.effect == "researched"
    assert not step.satisfied(state)
    state.researched.append("steam-power")
    assert step.satisfied(state)


def test_exploration_is_bounded_and_does_not_create_resources():
    state = snapshot(nearby_resources={})
    step = plan(state)
    assert step.action == "factory_explore" and step.parameters == {"radius": 12}
    state.factory["exploration_radius"] = 32
    plans, blocker = compile_factory("iron_smelting", state, catalog())
    assert not plans and "bounded exploration" in blocker
    state.factory["exploration_radius"] = 8
    step = FactoryPlanner(catalog(), state, "steam_power")._machine(
        "utility:water", "offshore-pump", (), "water"
    ).steps[0]
    assert step.action == "factory_explore"


def test_rocket_completion_cannot_come_from_mock_command_success():
    state = snapshot()
    assert not satisfied("rocket_launched", "", 0, {}, state)
    state.victory = True
    assert not satisfied("rocket_launched", "", 0, {}, state)
    state.victory_source = "native:base-game-rocket-launch"
    assert satisfied("rocket_launched", "", 0, {}, state)


def test_lua_binding_never_assigns_a_character_to_a_disconnected_player():
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("""
        storage = {agent_characters = {{force = {rockets_launched = 0}}}}
        calls = 0
        player = {
            connected = false,
            set_controller = function() calls = calls + 1 end
        }
        game = {get_player = function() return player end}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    with pytest.raises(Exception, match="connected game client"):
        lua.execute("storage.campaign.bind_player()")
    assert lua.eval("calls") == 0


def test_lua_fluid_branch_uses_the_matching_native_segment():
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("""
        storage = {agent_characters = {{force = {rockets_launched = 0}}}}
        rcon = {print = function(value) observed = value end}
        helpers = {table_to_json = function(value) return value end}
        local function pipe(segment, fluid, horizontal)
            return {
                position = {x = horizontal, y = 0},
                fluidbox = {
                    {name = fluid},
                    get_fluid_segment_id = function() return segment end
                }
            }
        end
        pipes = {pipe(11, "water", 9), pipe(12, "steam", 8), pipe(12, "steam", 2)}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    lua.execute("""
        storage.campaign.entities.source = {
            valid = true, force = {}, position = {x = 0, y = 0},
            surface = {find_entities_filtered = function() return pipes end},
            fluidbox = {
                {name = "water"}, {name = "steam"},
                get_filter = function(index) return index == 1 and "water" or "steam" end,
                get_fluid_segment_id = function(index) return index == 2 and 12 or nil end,
                get_pipe_connections = function(index)
                    if index == 2 then return {} end
                    return {{
                        target = {get_fluid_segment_id = function() return 11 end},
                        target_fluidbox_index = 1
                    }}
                end
            }
        }
        storage.campaign.entities.target = {
            valid = true, position = {x = 10, y = 0}
        }
        storage.campaign.pipe_source("source", "target", "steam")
    """)
    assert lua.eval("observed.x") == 8
    lua.execute('storage.campaign.pipe_source("source", "target", "water")')
    assert lua.eval("observed.x") == 9
    lua.execute('storage.campaign.pipe_source("source", "target", "petroleum-gas")')
    assert lua.eval("next(observed)") is None


def test_lua_observation_counts_force_entities_without_mutation():
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("""
        observations = 0
        local force = {
            rockets_launched = 0, technologies = {}, current_research = nil,
            research_progress = 0,
            get_item_production_statistics = function()
                return {get_input_count = function() return 0 end}
            end
        }
        pump = {valid = true, name = "offshore-pump"}
        pipe = {
            valid = true, name = "pipe", unit_number = 42,
            position = {x = 1.5, y = 2.5}, fluidbox = {{name = "water"}}
        }
        local surface = {
            find_entities_filtered = function(filter)
                assert(filter.force == force)
                observations = observations + 1
                return {pump, pipe}
            end
        }
        local agent = {valid = true, force = force, surface = surface}
        player = {connected = true, character = agent, crafting_queue_size = 0}
        storage = {agent_characters = {agent}}
        prototypes = {item = {}}
        game = {tick = 12, get_player = function() return player end}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    lua.execute("""
        local observed = storage.campaign.observe()
        assert(observed.force_entity_counts["offshore-pump"] == 1)
        assert(observed.force_entity_counts.pipe == 1)
        assert(#observed.connectors.pipe == 1)
        assert(observed.connectors.pipe[1].unit_number == 42)
        assert(observed.connectors.pipe[1].position.x == 1.5)
        assert(observed.connectors.pipe[1].fluid == "water")
        assert(#observed.connectors["small-electric-pole"] == 0)
        assert(observations == 1)
        assert(pump.valid and pump.name == "offshore-pump")
        assert(next(storage.campaign.entities) == nil)
        assert(next(storage.campaign.receipts) == nil)
    """)


def test_research_supplies_native_lab_costs_before_waiting():
    data = catalog()
    data.technologies["automation"] = {
        "enabled": True, "prerequisites": [], "effects": [],
        "count": 10, "energy_ticks": 600,
        "ingredients": [{"name": "automation-science-pack", "amount": 1}],
    }
    state = snapshot(inventory={"automation-science-pack": 10})
    state.factory["research"] = "automation"
    state.factory["entities"] = {
        "utility:water": machine("offshore-pump", fluid_ports=[{"id": 1, "fluid": "water"}]),
        "utility:boiler": machine("boiler", fuel={"coal": 10},
                                  fluid_ports=[{"id": 1, "fluid": "water"},
                                               {"id": 2, "fluid": "steam"}]),
        "utility:engine": machine("steam-engine", electric_network_id=1,
                                  fluid_ports=[{"id": 2, "fluid": "steam"}]),
        "utility:lab": machine("lab", electric_network_id=1),
    }
    step = FactoryPlanner(data, state, "rocket_launch")._research("automation").steps[0]
    assert step.action == "factory_insert"
    assert step.parameters["role"] == "utility:lab"
    assert step.parameters["quantity"] == 10
    state.factory["entities"]["utility:lab"]["input"] = {"automation-science-pack": 10}
    step = FactoryPlanner(data, state, "rocket_launch")._research("automation").steps[0]
    assert step.effect == "research_progress"
    assert not step.satisfied(state)
    state.factory["research_progress"] = 0.01
    assert step.satisfied(state)
    data.technologies["logistics"] = deepcopy(data.technologies["automation"])
    state.factory["research"] = "logistics"
    state.factory["entities"]["utility:lab"]["input"] = {}
    step = FactoryPlanner(data, state, "rocket_launch")._research("automation").steps[0]
    assert step.action == "factory_insert"
    assert step.parameters["item"] == "automation-science-pack"
    assert state.factory["research"] == "logistics"


def test_research_wait_failure_budget_is_scoped_to_observed_progress_and_supplies():
    data = catalog()
    data.technologies["automation"] = {
        "enabled": True, "prerequisites": [], "effects": [],
        "count": 100, "energy_ticks": 600,
        "ingredients": [
            {"name": "automation-science-pack", "amount": 1},
            {"name": "logistic-science-pack", "amount": 1},
        ],
    }
    state = snapshot()
    state.factory["research"] = "automation"
    state.factory["entities"] = {
        "utility:water": machine("offshore-pump", fluid_ports=[{"id": 1, "fluid": "water"}]),
        "utility:boiler": machine("boiler", fuel={"coal": 10},
                                  fluid_ports=[{"id": 1, "fluid": "water"},
                                               {"id": 2, "fluid": "steam"}]),
        "utility:engine": machine("steam-engine", electric_network_id=1,
                                  fluid_ports=[{"id": 2, "fluid": "steam"}]),
        "utility:lab": machine(
            "lab", electric_network_id=1,
            input={"automation-science-pack": 1, "logistic-science-pack": 1},
        ),
    }

    first = FactoryPlanner(data, state, "rocket_launch")._research("automation")
    assert first.steps[0].action == "factory_wait"
    assert first.id == (
        "factory:factory_wait:automation:progress:0:supplies:"
        "automation-science-pack=1,logistic-science-pack=1"
    )

    # A failed no-progress wait remains rejected for that same observed epoch.
    failures = {first.id: 2}
    repeated = FactoryPlanner(data, state, "rocket_launch")._research("automation")
    assert repeated.id == first.id
    assert failures[repeated.id] == 2

    # Native progress or a newly supplied lab admits a distinct passive wait;
    # neither case clears nor weakens the old failure record.
    state.factory["research_progress"] = 0.27
    progressed = FactoryPlanner(data, state, "rocket_launch")._research("automation")
    assert progressed.id != first.id
    assert failures.get(progressed.id, 0) == 0
    state.factory["research_progress"] = 0.12345678901231
    precise_first = FactoryPlanner(data, state, "rocket_launch")._research("automation")
    state.factory["research_progress"] = 0.12345678901239
    precise_second = FactoryPlanner(data, state, "rocket_launch")._research("automation")
    assert precise_second.id != precise_first.id
    state.factory["research_progress"] = 0
    state.factory["entities"]["utility:lab"]["input"]["logistic-science-pack"] = 20
    resupplied = FactoryPlanner(data, state, "rocket_launch")._research("automation")
    assert resupplied.id != first.id
    assert failures.get(resupplied.id, 0) == 0


def test_rocket_ready_dispatch_still_requires_native_launch_evidence():
    data = catalog()
    data.recipes["rocket-part"] = recipe("rocket-part", {}, "rocket-building")
    state = snapshot(researched=["rocket-silo"])
    state.factory["entities"]["recipe:rocket-part"] = machine(
        "rocket-silo", rocket_ready=True, rocket_parts=100, parts_required=100
    )
    # Readiness now includes the native pad and cargo contract, not rocket_ready alone.
    from test_launch_readiness import scenario
    _, ready = scenario(cargo={"raw-fish": 1})
    state.factory["entities"]["recipe:rocket-part"]["unit_number"] = 30
    state.factory["launch_readiness"] = ready.factory["launch_readiness"]
    step = FactoryPlanner(data, state, "rocket_launch").plan().steps[0]
    assert step.action == "factory_launch"
    assert step.allowed(state)
    assert not step.satisfied(state)
    state.victory = True
    state.victory_source = "native:base-game-rocket-launch"
    assert step.satisfied(state)


@pytest.mark.parametrize("capacity", [2, 5])
def test_lua_transfers_conserve_items_and_reject_replay(capacity):
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("""
        game = {tick = 10}
        defines = {inventory = {character_main = 1, chest = 2, furnace_source = 3}}
        source_count = 5
        target_count = 0
        local inventory = {
            get_item_count = function() return source_count end,
            remove = function(stack) source_count = source_count-stack.count; return stack.count end,
            insert = function(stack) source_count = source_count+stack.count; return stack.count end
        }
        target_inventory = {
            get_insertable_count = function() return capacity end,
            insert = function(stack)
                local inserted = math.min(capacity, stack.count)
                target_count = target_count + inserted
                return inserted
            end
        }
        storage = {agent_characters = {{
            force = {rockets_launched = 0}, position = {x = 0, y = 0},
            get_inventory = function() return inventory end
        }}}
        storage.fair = {actor = function()
            return {can_reach_entity = function() return true end}
        end}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    lua.execute(f"capacity = {capacity}")
    lua.execute("""
        storage.campaign.entities.furnace = {
            valid = true, type = "furnace", unit_number = 17, position = {x = 0, y = 0},
            get_inventory = function() return target_inventory end
        }
    """)
    command = "storage.campaign.transfer('furnace', 'coal', 5, 'unique', false)"
    if capacity < 5:
        with pytest.raises(Exception, match="destination capacity is short"):
            lua.execute(command)
        assert lua.eval("source_count") == 5
        assert lua.eval("target_count") == 0
        assert lua.eval("storage.campaign.receipts.unique") is None
    else:
        lua.execute(command)
        assert lua.eval("source_count + target_count") == 5
        assert lua.eval("storage.campaign.receipts.unique.quantity") == capacity
        with pytest.raises(Exception, match="already exists"):
            lua.execute(command)
        assert lua.eval("source_count + target_count") == 5


def test_lua_furnace_coal_transfer_targets_fuel_inventory_after_fair_reach_check():
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("""
        game = {tick = 10}
        defines = {inventory = {character_main = 1, chest = 2, furnace_source = 3, fuel = 4}}
        source_count, fuel_count, source_slot_used = 5, 0, false
        local player_inventory = {
            get_item_count = function() return source_count end,
            remove = function(stack) source_count = source_count-stack.count; return stack.count end,
            insert = function(stack) source_count = source_count+stack.count; return stack.count end
        }
        fuel_inventory = {
            get_insertable_count = function() return 5 end,
            insert = function(stack) fuel_count = fuel_count+stack.count; return stack.count end
        }
        furnace_source = {
            get_insertable_count = function() return 5 end,
            insert = function(stack) source_slot_used = true; return stack.count end
        }
        storage = {agent_characters = {{
            force = {rockets_launched = 0}, position = {x = 0, y = 0},
            get_inventory = function() return player_inventory end
        }}}
        storage.fair = {actor = function()
            return {can_reach_entity = function() return true end}
        end}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    lua.execute("""
        storage.campaign.entities.furnace = {
            valid = true, type = "furnace", burner = true, unit_number = 17,
            position = {x = 0, y = 0},
            get_inventory = function(kind)
                if kind == defines.inventory.fuel then return fuel_inventory end
                return furnace_source
            end
        }
        storage.campaign.transfer('furnace', 'coal', 5, 'fuel-transfer', false)
    """)
    assert lua.eval("source_count") == 0
    assert lua.eval("fuel_count") == 5
    assert lua.eval("source_slot_used") is False
    assert lua.eval("storage.campaign.receipts['fuel-transfer'].quantity") == 5


def test_lua_furnace_coal_transfer_rejects_remote_fuel_before_mutation():
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("""
        game = {tick = 10}
        defines = {inventory = {character_main = 1, chest = 2, furnace_source = 3, fuel = 4}}
        source_count, fuel_count = 5, 0
        local player_inventory = {
            get_item_count = function() return source_count end,
            remove = function(stack) source_count = source_count-stack.count; return stack.count end,
            insert = function(stack) source_count = source_count+stack.count; return stack.count end
        }
        fuel_inventory = {
            get_insertable_count = function() return 5 end,
            insert = function(stack) fuel_count = fuel_count+stack.count; return stack.count end
        }
        storage = {agent_characters = {{
            force = {rockets_launched = 0}, position = {x = 0, y = 0},
            get_inventory = function() return player_inventory end
        }}}
        storage.fair = {actor = function()
            return {can_reach_entity = function() return false end}
        end}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    lua.execute("""
        storage.campaign.entities.furnace = {
            valid = true, type = "furnace", burner = true, unit_number = 17,
            position = {x = 100, y = 100},
            get_inventory = function() return fuel_inventory end
        }
    """)
    with pytest.raises(Exception, match="out of reach"):
        lua.execute("storage.campaign.transfer('furnace', 'coal', 5, 'remote-fuel', false)")
    assert lua.eval("source_count") == 5
    assert lua.eval("fuel_count") == 0
    assert lua.eval("storage.campaign.receipts['remote-fuel']") is None


def test_lua_transfer_rejects_remote_interaction_before_inventory_mutation():
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("""
        game = {tick = 10}
        defines = {inventory = {character_main = 1, chest = 2}}
        source_count = 5
        target_count = 0
        local inventory = {
            get_item_count = function() return source_count end,
            remove = function(stack) source_count = source_count-stack.count; return stack.count end,
            insert = function(stack) source_count = source_count+stack.count; return stack.count end
        }
        storage = {agent_characters = {{
            force = {rockets_launched = 0}, position = {x = 0, y = 0},
            get_inventory = function() return inventory end
        }}}
        storage.fair = {actor = function()
            return {can_reach_entity = function() return false end}
        end}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    lua.execute("""
        storage.campaign.entities.furnace = {
            valid = true, unit_number = 17, position = {x = 100, y = 100},
            can_insert = function() return true end,
            insert = function(stack) target_count = target_count + stack.count; return stack.count end
        }
    """)
    with pytest.raises(Exception, match="out of reach"):
        lua.execute("storage.campaign.transfer('furnace', 'coal', 5, 'remote-denied', false)")
    assert lua.eval("source_count") == 5
    assert lua.eval("target_count") == 0
    assert lua.eval("storage.campaign.receipts['remote-denied']") is None


class FactorySimulation:
    def __init__(self, lose_transfer_ack=False):
        self.state = snapshot(inventory={"coal": 5})
        self.state.placed_entities = ["burner-mining-drill", "wooden-chest"]
        self.state.drill_status = "working"
        self.state.drill_output_connected = True
        self.state.iron_ore_collected = 5
        self.actions = []
        self.lose_transfer_ack = lose_transfer_ack

    def enable_factory(self):
        return catalog()

    def observe(self):
        self.state.tick += 1
        for entity in self.state.factory["entities"].values():
            if entity.get("input", {}).get("iron-ore", 0) and entity.get("fuel", {}).get("coal", 0):
                entity["input"]["iron-ore"] -= 1
                entity["output"]["iron-plate"] = entity["output"].get("iron-plate", 0) + 1
                produced = self.state.factory["produced"]
                produced["iron-plate"] = produced.get("iron-plate", 0) + 1
        return deepcopy(self.state)

    def execute(self, action, parameters):
        self.actions.append((action, deepcopy(parameters)))
        inventory = self.state.inventory
        entities = self.state.factory["entities"]
        if action == "factory_gather":
            item = parameters["resource"]
            inventory[item] = inventory.get(item, 0) + parameters["quantity"]
        elif action == "factory_craft":
            data = catalog().recipes[parameters["recipe"]]
            for ingredient in data["ingredients"]:
                inventory[ingredient["name"]] -= ingredient["amount"] * parameters["batches"]
            inventory[data["name"]] = inventory.get(data["name"], 0) + parameters["batches"]
        elif action == "factory_place":
            inventory[parameters["name"]] -= 1
            entities[parameters["role"]] = machine(parameters["name"])
        elif action in {"factory_insert", "factory_extract"}:
            item, quantity = parameters["item"], parameters["quantity"]
            entity = entities[parameters["role"]]
            if action == "factory_insert":
                inventory[item] -= quantity
                compartment = entity["fuel" if item == "coal" else "input"]
                compartment[item] = compartment.get(item, 0) + quantity
            else:
                entity["output"][item] -= quantity
                inventory[item] = inventory.get(item, 0) + quantity
            self.state.factory["receipts"][parameters["receipt"]] = {
                **parameters, "unit_number": entity["unit_number"],
                "extracting": action == "factory_extract",
            }
            if self.lose_transfer_ack:
                self.lose_transfer_ack = False
                raise TimeoutError("Synthetic lost acknowledgment after a conserved transfer")
        elif action != "factory_wait":
            raise AssertionError(action)
        return "Synthetic action; not native gameplay evidence"


@pytest.mark.parametrize("lose_ack", [False, True])
def test_controller_completes_smelting_with_material_conservation_and_receipts(tmp_path, lose_ack):
    backend = FactorySimulation(lose_ack)
    controller = HierarchicalLoop(
        backend, policy="deterministic", target="iron_smelting",
        checkpoint=str(tmp_path / "checkpoint.json"), tick_seconds=0,
    )
    controller.run(steps=100)
    assert controller.memory.status == "completed"
    assert backend.state.inventory["iron-plate"] >= 10
    assert backend.state.factory["produced"]["iron-plate"] >= 10
    assert backend.state.inventory.get("stone", 0) == 0
    assert sum(action == "factory_place" for action, _ in backend.actions) == 1
    receipts = [parameters["receipt"] for action, parameters in backend.actions
                if action in {"factory_insert", "factory_extract"}]
    assert len(receipts) == len(set(receipts))


def test_opt_in_hybrid_labels_fallback_without_weakening_preconditions(tmp_path):
    class AbstainingModel(MockJevClient):
        def evaluate(self, state, questions):
            answers = super().evaluate(state, questions)
            answers["candidate"]["confidence"] = 0
            return answers

    backend = FactorySimulation()
    controller = HierarchicalLoop(
        backend, policy="hybrid", jev=AbstainingModel(), target="iron_smelting",
        checkpoint=str(tmp_path / "checkpoint.json"), tick_seconds=0,
    )
    record = controller.step()
    assert record["decision"]["source"] == "deterministic-fallback"
    assert record["decision"]["answers"]["candidate"]["confidence"] == 0
    assert record["model_call"] is True
    assert record["verified"]


def test_model_context_omits_raw_audit_bodies_without_losing_evidence(tmp_path):
    backend = FactorySimulation()
    backend.state.factory["receipts"] = {"prior": {"quantity": 5}}
    backend.state.factory["connectors"] = {"pipe": [
        {"unit_number": index, "position": {"x": index + 0.5, "y": 0.5}, "fluid": "water"}
        for index in range(512)
    ]}
    controller = HierarchicalLoop(
        backend, policy="hybrid", jev=MockJevClient(), target="iron_smelting",
        checkpoint=str(tmp_path / "checkpoint.json"), tick_seconds=0,
    )
    record = controller.step()
    model_factory = record["decision"]["state"]["facts"]["factory"]
    assert "receipts" not in model_factory
    assert "connectors" not in model_factory
    assert model_factory["native_transfer_receipt_count"] == 1
    assert record["state"]["factory"]["receipts"] == {"prior": {"quantity": 5}}
    assert len(record["state"]["factory"]["connectors"]["pipe"]) == 512
    assert backend.state.factory["receipts"] == {"prior": {"quantity": 5}}


def test_ambiguous_placement_reconciles_from_native_absence_before_retry(tmp_path):
    class LostPlacement(FactorySimulation):
        def __init__(self):
            super().__init__()
            self.lose_placement = True
            self.state.factory["force_entity_counts"] = {"stone-furnace": 0}

        def execute(self, action, parameters):
            if action == "factory_place" and self.lose_placement:
                self.actions.append((action, deepcopy(parameters)))
                self.lose_placement = False
                raise RuntimeError("Synthetic placement failed before native construction")
            result = super().execute(action, parameters)
            if action == "factory_place":
                name = parameters["name"]
                self.state.factory["force_entity_counts"][name] = 1
            return result

    backend = LostPlacement()
    checkpoint = tmp_path / "checkpoint.json"
    first = HierarchicalLoop(
        backend, policy="deterministic", target="iron_smelting",
        checkpoint=str(checkpoint), tick_seconds=0,
    )
    first.step()
    first.step()
    record = first.step()
    assert record["outcome"] == "Ambiguous dispatch; verification required"
    assert first.memory.pending["dispatch"] == "ambiguous"

    resumed = HierarchicalLoop(
        backend, policy="deterministic", target="iron_smelting",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
    )
    actions_before = list(backend.actions)
    record = resumed.step()
    assert record["action"] == "reconcile"
    assert record["status"] == "running"
    assert backend.actions == actions_before
    assert resumed.memory.pending is None
    assert resumed.memory.active_plan is None
    assert resumed.memory.reservations == {}
    assert resumed.memory.failures == {"factory:factory_place:recipe:iron-plate": 1}

    record = resumed.step()
    assert record["verified"] is True
    assert sum(action == "factory_place" for action, _ in backend.actions) == 2
    assert backend.state.inventory["stone-furnace"] == 0
    assert backend.state.factory["force_entity_counts"]["stone-furnace"] == 1


@pytest.mark.parametrize("observation,retained_material", [
    ({}, True),
    ({"force_entity_counts": {"stone-furnace": 1}}, True),
    ({"force_entity_counts": {"stone-furnace": 0}}, False),
])
def test_ambiguous_placement_stays_pending_without_absence_proof(
    tmp_path, observation, retained_material
):
    class LostPlacement(FactorySimulation):
        def act(self, action):
            assert action == "idle"
            return "Synthetic observation wait"

        def execute(self, action, parameters):
            if action == "factory_place":
                self.actions.append((action, deepcopy(parameters)))
                raise RuntimeError("Synthetic ambiguous placement")
            return super().execute(action, parameters)

    backend = LostPlacement()
    backend.state.factory.update(observation)
    checkpoint = tmp_path / "checkpoint.json"
    controller = HierarchicalLoop(
        backend, policy="deterministic", target="iron_smelting",
        checkpoint=str(checkpoint), tick_seconds=0,
    )
    controller.step()
    controller.step()
    controller.step()
    if not retained_material:
        backend.state.inventory["stone-furnace"] = 0
    actions_before = list(backend.actions)

    resumed = HierarchicalLoop(
        backend, policy="deterministic", target="iron_smelting",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
    )
    record = resumed.step()
    assert record["action"] == "observe"
    assert resumed.memory.pending["dispatch"] == "ambiguous"
    assert resumed.memory.active_plan is not None
    assert backend.actions == actions_before


@pytest.mark.parametrize("dispatch", ["prepared", "ambiguous"])
def test_unacknowledged_gather_with_observed_partial_yield_replans_without_dispatching(dispatch):
    plan = Plan(
        id="factory:factory_gather:wood", goal="rocket_launch", description="gather wood",
        steps=[Step(
            action="factory_gather", effect="inventory", item="wood", threshold=20,
            parameters={"resource": "wood", "quantity": 20}, timeout_ticks=18000,
        )],
    )
    state = snapshot(inventory={"wood": 8})
    controller = HierarchicalLoop(SimpleNamespace(), policy="deterministic", tick_seconds=0)
    controller.memory = CampaignMemory(
        session_id="test-factory", target="rocket_launch", active_goal="rocket_launch",
        active_plan=plan.to_dict(), pending={
            "started_tick": 10, "polls": 1,
            "action": "factory_gather", "dispatch": dispatch,
        },
        reservations={plan.id: {}}, last_tick=10, status="uncertain",
    )

    controller.memory.attempt = make_attempt(
        controller.memory.session_id, controller.target, controller.memory.active_plan,
        0, controller.memory.pending, process_id=controller._process_id,
    )
    record = controller._verify_pending(state)

    assert record["action"] == "reconcile"
    assert record["status"] == "running"
    assert "Observed 8 wood below committed inventory threshold 20" in record["outcome"]
    assert controller.memory.pending is None
    assert controller.memory.active_plan is None
    assert controller.memory.reservations == {}
    assert controller.memory.failures == {plan.id: 1}
    receipt = next(event for event in controller.memory.history
                   if event["kind"] == "gather_partial_progress")
    assert Plan.from_dict(receipt["plan"]) == Plan.from_dict(plan.to_dict())
    assert receipt["observed_inventory"] == 8


def partial_gather_fixture():
    state = snapshot(inventory={"iron-ore": 0})
    original = FactoryPlanner(catalog(), state, "rocket_launch")._need("iron-ore", 20)
    state.inventory["iron-ore"] = 19
    controller = HierarchicalLoop(SimpleNamespace(), policy="deterministic", tick_seconds=0)
    controller.memory = CampaignMemory(
        session_id=state.session_id, target="rocket_launch", active_goal="rocket_launch",
        completed_goals={"stockpile_fuel": 1, "bootstrap_mining": 1},
        last_tick=state.tick, failures={original.id: 2},
    )
    controller.memory.event(
        "gather_partial_progress", session_id=state.session_id,
        plan=original.to_dict(), step_index=0, observed_inventory=19,
        tick=state.tick, started_tick=1, attempt_id="observed-original-attempt",
    )
    return controller, state, original


def test_partial_gather_scopes_only_the_proven_remainder_and_preserves_budgets(tmp_path, monkeypatch):
    controller, state, original = partial_gather_fixture()
    checkpoint = tmp_path / "checkpoint.json"
    controller.memory.save(checkpoint)
    controller.memory = CampaignMemory.load(checkpoint, state.session_id, "rocket_launch")
    actions = []

    def execute(action, parameters):
        actions.append((action, dict(parameters)))
        state.inventory["iron-ore"] += parameters["quantity"]
        state.tick += 1
        return "Synthetic fair gather"

    controller.backend = SimpleNamespace(observe=lambda: deepcopy(state), execute=execute)
    monkeypatch.setattr("jev_factorio.controller.compile_plans", lambda goal, current: (
        [FactoryPlanner(catalog(), current, goal)._need("iron-ore", 20)], ""
    ))
    record = controller.step()
    assert record["verified"] is True
    assert actions == [("factory_gather", {"resource": "iron-ore", "quantity": 1})]
    assert controller.memory.failures == {original.id: 2}
    assert any(event.get("plan") == original.id + ":remainder-quantity:1"
               for event in controller.memory.history if event["kind"] == "plan_committed")


def test_remainder_identity_does_not_reset_its_own_exhausted_budget(monkeypatch):
    controller, state, original = partial_gather_fixture()
    remainder = original.id + ":remainder-quantity:1"
    controller.memory.failures[remainder] = 2
    actions = []
    controller.backend = SimpleNamespace(observe=lambda: deepcopy(state),
                                         execute=lambda *args: actions.append(args))
    monkeypatch.setattr("jev_factorio.controller.compile_plans", lambda goal, current: (
        [FactoryPlanner(catalog(), current, goal)._need("iron-ore", 20)], ""
    ))
    record = controller.step()
    assert record["status"] == "blocked"
    assert actions == []
    assert controller.memory.failures == {original.id: 2, remainder: 2}


@pytest.mark.parametrize("scheduling", ["serial", "ready-work"])
def test_factory_scheduling_modes_scope_the_same_proven_remainder(monkeypatch, scheduling):
    controller, state, original = partial_gather_fixture()
    controller.catalog = catalog()
    controller.factory_scheduling = scheduling
    remainder = FactoryPlanner(catalog(), state, "rocket_launch")._need("iron-ore", 20)
    module = "factory.compile_factory" if scheduling == "serial" else "ready_work.compile_ready_factory"
    monkeypatch.setattr(f"jev_factorio.planning.{module}", lambda *args: ([remainder], ""))
    plans, blocker = controller._compile_candidates(state)
    assert not blocker
    assert plans[0].id == original.id + ":remainder-quantity:1"
    assert plans[0].steps[0].parameters == {"resource": "iron-ore", "quantity": 1}


@pytest.mark.parametrize("change", [
    "missing", "wrong_session", "future_tick", "bad_tick", "wrong_site", "wrong_goal",
    "unchanged_inventory", "wrong_observation", "wrong_target", "same_quantity",
    "missing_attempt", "malformed_plan", "wrong_step",
])
def test_remainder_authorization_fails_closed_without_matching_positive_progress(change):
    controller, state, original = partial_gather_fixture()
    receipt = controller.memory.history[-1]
    if change == "missing":
        controller.memory.history.clear()
    elif change == "wrong_session":
        receipt["session_id"] = "other-world"
    elif change == "future_tick":
        receipt["tick"] = state.tick + 1
    elif change == "bad_tick":
        receipt["started_tick"] = True
    elif change == "wrong_site":
        receipt["plan"]["id"] += "-different-site"
    elif change == "wrong_goal":
        receipt["plan"]["goal"] = "stockpile_fuel"
    elif change == "unchanged_inventory":
        state.inventory["iron-ore"] = 0
    elif change == "wrong_observation":
        receipt["observed_inventory"] = 18
    elif change == "wrong_target":
        receipt["plan"]["steps"][0]["threshold"] = 21
    elif change == "same_quantity":
        receipt["plan"]["steps"][0]["parameters"]["quantity"] = 1
    elif change == "missing_attempt":
        receipt.pop("attempt_id")
    elif change == "malformed_plan":
        receipt["plan"] = {"steps": None}
    elif change == "wrong_step":
        receipt["step_index"] = 1
    plan = FactoryPlanner(catalog(), state, "rocket_launch")._need("iron-ore", 20)
    assert controller._gather_remainder_plan(plan, state).id == original.id
    assert controller.memory.failures == {original.id: 2}


@pytest.mark.parametrize("dispatch", ["prepared", "ambiguous"])
def test_positive_preexisting_inventory_is_not_partial_gather_progress(dispatch):
    controller, state, original = partial_gather_fixture()
    step = Step("factory_gather", "inventory", "iron-ore", 20,
                parameters={"resource": "iron-ore", "quantity": 1})
    controller.memory.pending = {"dispatch": dispatch, "polls": 1, "started_tick": 1}
    assert not controller._partial_unacknowledged_gather(step, state)


def test_legacy_wood_failure_budget_identity_is_unchanged_without_progress_receipt():
    state = snapshot(inventory={"wood": 12}, nearby_resources={"wood": 3})
    state.factory["fair_resource_targets"]["wood"] = {
        "name": "tree-01", "surface_index": 1, "position": {"x": 3.0, "y": 4.0},
    }
    plan = FactoryPlanner(catalog(), state, "rocket_launch")._need("wood", 20)
    legacy_id = ('factory:factory_gather:wood:target:13:site:'
                 '{"name":"tree-01","position":{"x":3.0,"y":4.0},"surface_index":1}')
    controller = HierarchicalLoop(SimpleNamespace(), policy="deterministic")
    controller.memory = CampaignMemory(state.session_id, "rocket_launch", failures={legacy_id: 2})
    assert controller._gather_remainder_plan(plan, state).id == legacy_id
    assert controller.memory.failures[legacy_id] == 2


@pytest.mark.parametrize("change", [
    "missing_counts", "spent_material", "changed_reservation", "missing_role",
    "missing_connectors", "incomplete_connectors", "same_fluid_connector",
    "empty_connector",
])
def test_ambiguous_connection_reconciliation_fails_closed(change):
    plan = Plan(
        id="factory:factory_connect:", goal="rocket_launch", description="connect",
        steps=[Step(
            action="factory_connect", effect="connection", costs={"pipe": 41},
            parameters={
                "source": "utility:water", "target": "utility:boiler",
                "kind": "pipe", "fluid": "water",
            },
        )],
    )
    state = snapshot(inventory={"pipe": 41})
    state.factory["entities"] = {
        "utility:water": machine("offshore-pump"),
        "utility:boiler": machine("boiler"),
    }
    state.factory["force_entity_counts"] = {"pipe": 0}
    controller = object.__new__(HierarchicalLoop)
    controller.provenance = {}
    controller.memory = CampaignMemory(
        session_id="test-factory", target="rocket_launch", active_goal="rocket_launch",
        active_plan=plan.to_dict(), pending={
            "started_tick": 10, "polls": 97,
            "action": "factory_connect", "dispatch": "ambiguous",
        },
        reservations={plan.id: {"pipe": 41}}, last_tick=10, status="uncertain",
    )

    controller.memory.attempt = make_attempt(
        controller.memory.session_id, controller.memory.target, controller.memory.active_plan,
        0, controller.memory.pending,
    )
    assert controller._absent_ambiguous_connection(plan, plan.steps[0], state)
    if change == "missing_counts":
        state.factory.pop("force_entity_counts")
    elif change == "spent_material":
        state.inventory["pipe"] = 40
    elif change == "changed_reservation":
        controller.memory.reservations[plan.id]["pipe"] = 40
    elif change == "missing_role":
        state.factory["entities"].pop("utility:water")
    elif change == "missing_connectors":
        state.factory["force_entity_counts"]["pipe"] = 9
    elif change == "incomplete_connectors":
        state.factory["force_entity_counts"]["pipe"] = 9
        state.factory["connectors"] = {"pipe": [
            {"unit_number": index, "position": {"x": index + 0.5, "y": 0.5}, "fluid": "water"}
            for index in range(1, 9)
        ]}
        state.factory["entities"]["utility:water"]["fluids"] = {"water": 100}
    elif change in {"same_fluid_connector", "empty_connector"}:
        state.factory["force_entity_counts"]["pipe"] = 1
        connector_fluid = {
            "same_fluid_connector": "water", "empty_connector": "",
        }[change]
        state.factory["connectors"] = {"pipe": [{
            "unit_number": 1, "position": {"x": 0.5, "y": 0.5},
            "fluid": connector_fluid,
        }]}
    assert not controller._absent_ambiguous_connection(plan, plan.steps[0], state)


def test_ambiguous_first_connection_allows_retained_surplus_stock():
    plan = Plan(
        id="factory:factory_connect:", goal="rocket_launch", description="connect",
        steps=[Step(
            action="factory_connect", effect="connection", costs={"pipe": 12},
            parameters={
                "source": "utility:water", "target": "utility:boiler",
                "kind": "pipe", "fluid": "water",
            },
        )],
    )
    state = snapshot(inventory={"pipe": 15})
    state.factory["entities"] = {
        "utility:water": machine("offshore-pump"),
        "utility:boiler": machine("boiler"),
    }
    state.factory["force_entity_counts"] = {"pipe": 0}
    controller = object.__new__(HierarchicalLoop)
    controller.memory = CampaignMemory(
        session_id="test-factory", target="rocket_launch", active_goal="rocket_launch",
        active_plan=plan.to_dict(), pending={
            "started_tick": 10, "polls": 2,
            "action": "factory_connect", "dispatch": "ambiguous",
        },
        reservations={plan.id: {"pipe": 12}}, last_tick=10, status="uncertain",
    )

    controller.memory.attempt = make_attempt(
        controller.memory.session_id, controller.memory.target, controller.memory.active_plan,
        0, controller.memory.pending,
    )
    assert controller._absent_ambiguous_connection(plan, plan.steps[0], state)


@pytest.mark.parametrize("remaining_stock", [41, 42])
def test_ambiguous_connection_cannot_infer_connector_age_from_fluid(remaining_stock):
    plan = Plan(
        id="factory:factory_connect:", goal="rocket_launch", description="connect",
        steps=[Step(
            action="factory_connect", effect="connection", costs={"pipe": 41},
            parameters={
                "source": "utility:boiler", "target": "utility:engine",
                "kind": "pipe", "fluid": "steam",
            },
        )],
    )
    state = snapshot(inventory={"pipe": remaining_stock})
    state.factory["entities"] = {
        "utility:boiler": machine("boiler"),
        "utility:engine": machine("steam-engine"),
    }
    state.factory["force_entity_counts"] = {"pipe": 9}
    state.factory["connectors"] = {"pipe": [
        {
            "unit_number": 770 + index,
            "position": {"x": -37.5 + index, "y": -27.5},
            "fluid": "water",
        }
        for index in range(1, 10)
    ]}
    controller = object.__new__(HierarchicalLoop)
    controller.memory = CampaignMemory(
        session_id="test-factory", target="rocket_launch", active_goal="rocket_launch",
        active_plan=plan.to_dict(), pending={
            "started_tick": 10, "polls": 93,
            "action": "factory_connect", "dispatch": "ambiguous",
        },
        reservations={plan.id: {"pipe": 41}}, last_tick=10, status="uncertain",
    )

    controller.memory.attempt = make_attempt(
        controller.memory.session_id, controller.memory.target, controller.memory.active_plan,
        0, controller.memory.pending,
    )
    assert not controller._absent_ambiguous_connection(plan, plan.steps[0], state)

    state.factory["connectors"]["pipe"].append({
        "unit_number": 780,
        "position": {"x": -26.5, "y": -29.5},
        "fluid": "water",
    })
    state.factory["force_entity_counts"]["pipe"] = 10
    assert not controller._absent_ambiguous_connection(plan, plan.steps[0], state)


def test_ambiguous_connection_reconciles_without_dispatching():
    plan = Plan(
        id="factory:factory_connect:", goal="rocket_launch", description="connect",
        steps=[Step(
            action="factory_connect", effect="connection", costs={"pipe": 41},
            parameters={
                "source": "utility:water", "target": "utility:boiler",
                "kind": "pipe", "fluid": "water",
            },
        )],
    )
    state = snapshot(inventory={"pipe": 41})
    state.factory["entities"] = {
        "utility:water": machine("offshore-pump"),
        "utility:boiler": machine("boiler"),
    }
    state.factory["force_entity_counts"] = {"pipe": 0}
    controller = HierarchicalLoop(SimpleNamespace(), policy="deterministic", tick_seconds=0)
    controller.memory = CampaignMemory(
        session_id="test-factory", target="rocket_launch", active_goal="rocket_launch",
        active_plan=plan.to_dict(), pending={
            "started_tick": 10, "polls": 97,
            "action": "factory_connect", "dispatch": "ambiguous",
        },
        reservations={plan.id: {"pipe": 41}}, last_tick=10, status="uncertain",
    )

    controller.memory.attempt = make_attempt(
        controller.memory.session_id, controller.target, controller.memory.active_plan,
        0, controller.memory.pending, process_id=controller._process_id,
    )
    record = controller._verify_pending(state)

    assert record["action"] == "reconcile"
    assert record["status"] == "running"
    assert controller.memory.pending is None
    assert controller.memory.active_plan is None
    assert controller.memory.reservations == {}
    assert controller.memory.failures == {plan.id: 1}
