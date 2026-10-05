"""Composed checkpoint ownership conflicts from the retained route producers.

These are parser/controller fixtures.  They prove what the saved owners claim;
they do not establish engine adoption or native acceptance.
"""
from copy import deepcopy
from dataclasses import asdict, fields

import pytest

from jev_factorio.acceptance_io import canonical
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.coal_supply import commitment as coal_commitment, sources as coal_sources
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.input_controller import input_loop_type
from jev_factorio.memory import load_checkpoint_bytes
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.solid_controller import solid_loop_type
from jev_factorio.solid_routes import commitment as solid_commitment, routes as solid_routes

from coal_supply_fixtures import INTENTS as COAL_INTENTS, TARGETS, fixture as coal_fixture, paid_source
from coal_supply_fixtures import paid_corridor
from input_routes_fixtures import SOURCE as INPUT_SOURCE, fixture as input_fixture, full as full_input
from input_routes_fixtures import row as input_row
from solid_routes_fixtures import INTENTS as SOLID_INTENTS, ROUTE as SOLID_ROUTE
from solid_routes_fixtures import fixture as solid_fixture, full as full_solid, row as solid_row
from test_mining_outposts import RESOURCE, full as full_outpost, row as outpost_row
from test_mining_outposts import state_fixture as outpost_fixture
from test_output_buffer_integration import setup as output_fixture
from test_output_buffer_ownership import successor_fixture
from jev_factorio.planning.coal_supply import candidates as coal_candidates


def _roundtrip(memory):
    raw = canonical(asdict(memory))
    return load_checkpoint_bytes(raw, memory.session_id, memory.target)


def _direct_from_bytes(memory):
    raw = canonical(asdict(memory))
    return type(memory).from_bytes(raw, memory.session_id, memory.target)


def _output_owner(*, source="recipe:copper-plate", source_unit=3000,
                  unit_start=3100, receipt_prefix="ordinary-output"):
    _, _, row = output_fixture(True)
    row["source"] = source
    row["source_unit"] = source_unit
    owner = {key: deepcopy(row[key]) for key in ("source_unit", "layout", "parts")}
    for offset, (part, paid) in enumerate(owner["parts"].items()):
        paid.update(role=f"ordinary:{source}:{part}", unit_number=unit_start + offset,
                    receipt=f"{receipt_prefix}:{part}", paid=1)
    return {source: owner}


def _input_memory():
    state = input_fixture()
    full_input(state)
    memory_type = input_loop_type(buffered_loop_type(HierarchicalLoop)).memory_type
    memory = memory_type(state.session_id, "rocket_launch", last_tick=state.tick)
    row = input_row(state)
    memory.input_commitments = {INPUT_SOURCE: {
        key: deepcopy(row[key]) for key in ("source_unit", "layout", "parts")
    }}
    return memory


def _successor_memory(tmp_path):
    loop, _, source = successor_fixture(tmp_path)
    return loop.memory, source


def _outpost_memory():
    state, _ = outpost_fixture()
    full_outpost(state)
    memory_type = outpost_loop_type(input_loop_type(buffered_loop_type(HierarchicalLoop))).memory_type
    memory = memory_type(state.session_id, "rocket_launch", last_tick=state.tick)
    row = outpost_row(state)
    memory.outpost_commitments = {RESOURCE: {
        key: deepcopy(row[key]) for key in ("layout", "surface_index", "force_index", "steps", "parts", "flow")
    }}
    return memory


def _solid_memory():
    state = solid_fixture()
    full_solid(state)
    memory_type = solid_loop_type(buffered_loop_type(HierarchicalLoop)).memory_type
    memory = memory_type(state.session_id, "rocket_launch", last_tick=state.tick)
    epoch = state.factory["solid_routes"]
    memory.solid_intents = deepcopy(SOLID_INTENTS)
    memory.solid_epoch = {key: epoch[key] for key in ("actor_index", "surface_index", "force_index")}
    memory.solid_commitments = {SOLID_ROUTE: solid_commitment(solid_row(state))}
    return memory


def _coal_memory_with_shared_solid_endpoint():
    state = coal_fixture()
    plan = next(plan for plan in coal_candidates(state, "rocket_launch")
                if plan.steps[0].parameters["part"] == "chest")
    target = plan.steps[0].parameters["target"]
    paid_source(state, plan.steps[0].parameters)
    coal_rows = coal_sources(state)
    coal_chest = coal_rows[target]["parts"]["chest"]
    route_key = next(key for key, row in state.factory["solid_routes"]["routes"].items()
                     if row["source"]["unit_number"] == coal_chest["unit_number"])
    route = solid_routes(state)[route_key]
    first = route["steps"][0]
    paid_corridor(state, {
        "route": route_key, "layout": route["layout"], "part": first["part"],
        "receipt": "fixture:paid:coal-corridor",
    })

    memory_type = coal_loop_type(solid_loop_type(buffered_loop_type(HierarchicalLoop))).memory_type
    memory = memory_type(state.session_id, "rocket_launch", last_tick=state.tick)
    solid_epoch = state.factory["solid_routes"]
    coal_epoch = state.factory["coal_supply"]
    memory.solid_intents = deepcopy(COAL_INTENTS)
    memory.solid_epoch = {key: solid_epoch[key] for key in ("actor_index", "surface_index", "force_index")}
    memory.coal_targets = list(TARGETS)
    memory.coal_epoch = {key: coal_epoch[key] for key in ("actor_index", "surface_index", "force_index")}
    memory.coal_commitments = {key: coal_commitment(row) for key, row in coal_rows.items()}
    memory.solid_commitments = {route_key: solid_commitment(route)}
    return memory, coal_chest


def _without_output_extension(memory):
    memory_type = coal_loop_type(solid_loop_type(HierarchicalLoop)).memory_type
    projected = memory_type(memory.session_id, memory.target, last_tick=memory.last_tick)
    for item in fields(projected):
        if hasattr(memory, item.name):
            setattr(projected, item.name, deepcopy(getattr(memory, item.name)))
    return projected


def _install_output(memory, *, source_unit=3000, unit_start=3100):
    memory.output_commitments = _output_owner(source_unit=source_unit, unit_start=unit_start)
    return memory.output_commitments["recipe:copper-plate"]["parts"]["chest"]


def _alias_fields(output_part, paid_part, dimension):
    if dimension in {"unit", "unit_receipt"}:
        output_part["unit_number"] = paid_part["unit_number"]
    if dimension in {"receipt", "unit_receipt"}:
        output_part["receipt"] = paid_part["receipt"]
    if dimension == "role":
        output_part["role"] = paid_part["role"]


@pytest.mark.parametrize("dimension", ["unit", "receipt", "unit_receipt", "role"])
def test_ordinary_output_cannot_alias_retained_input_paid_owner(dimension):
    memory = _input_memory()
    output_part = _install_output(memory)
    input_part = memory.input_commitments[INPUT_SOURCE]["parts"]["inserter"]
    _alias_fields(output_part, input_part, dimension)

    with pytest.raises(ValueError, match="cross-family|paid identity|owner"):
        _roundtrip(memory)


@pytest.mark.parametrize("dimension", ["unit", "receipt", "unit_receipt", "role"])
def test_ordinary_output_cannot_alias_retained_successor_input_paid_owner(tmp_path, dimension):
    memory, source = _successor_memory(tmp_path)
    output_part = _install_output(memory)
    successor_part = memory.successor_receipts[source]["input"]["inserter"]
    _alias_fields(output_part, successor_part, dimension)

    with pytest.raises(ValueError, match="cross-family|paid identity|owner"):
        _roundtrip(memory)


@pytest.mark.parametrize("dimension", ["unit", "receipt", "unit_receipt", "role"])
def test_ordinary_output_cannot_alias_retained_outpost_paid_owner(dimension):
    memory = _outpost_memory()
    output_part = _install_output(memory)
    outpost_part = memory.outpost_commitments[RESOURCE]["parts"]["chest"]
    _alias_fields(output_part, outpost_part, dimension)

    with pytest.raises(ValueError, match="cross-family|paid identity|owner"):
        _roundtrip(memory)


def test_distinct_outpost_and_output_paid_owners_roundtrip():
    memory = _outpost_memory()
    expected_output = deepcopy(_install_output(memory))
    loaded = _roundtrip(memory)
    assert loaded.outpost_commitments == memory.outpost_commitments
    assert loaded.output_commitments["recipe:copper-plate"]["parts"]["chest"] == expected_output


@pytest.mark.parametrize("dimension", ["unit", "receipt", "unit_receipt", "role"])
def test_ordinary_output_cannot_alias_retained_solid_paid_owner(dimension):
    memory = _solid_memory()
    output_part = _install_output(memory)
    solid_part = memory.solid_commitments[SOLID_ROUTE]["parts"]["receive"]
    _alias_fields(output_part, solid_part, dimension)

    with pytest.raises(ValueError, match="cross-family|paid identity|owner"):
        _roundtrip(memory)


def test_distinct_solid_and_output_paid_owners_roundtrip():
    memory = _solid_memory()
    expected_output = deepcopy(_install_output(memory))
    loaded = _roundtrip(memory)
    assert loaded.solid_commitments == memory.solid_commitments
    assert loaded.output_commitments["recipe:copper-plate"]["parts"]["chest"] == expected_output


@pytest.mark.parametrize("dimension", ["unit", "receipt", "unit_receipt", "role"])
def test_ordinary_output_cannot_alias_retained_coal_paid_owner(dimension):
    memory, _ = _coal_memory_with_shared_solid_endpoint()
    output_part = _install_output(memory)
    coal_part = memory.coal_commitments[TARGETS[0]]["parts"]["chest"]
    _alias_fields(output_part, coal_part, dimension)

    with pytest.raises(ValueError, match="cross-family|paid identity|owner"):
        _roundtrip(memory)


def test_valid_cross_family_owners_keep_input_successor_mirror_and_shared_references(tmp_path):
    memory, source = _successor_memory(tmp_path)
    _install_output(memory)
    memory.successor_projects[source]["predecessor_unit"] = 500
    loaded = _roundtrip(memory)
    assert loaded.input_commitments[source]["parts"] == loaded.successor_receipts[source]["input"]
    assert loaded.output_commitments["recipe:copper-plate"]["parts"] == memory.output_commitments[
        "recipe:copper-plate"]["parts"]


def test_shared_source_unit_reference_does_not_alias_another_family_paid_owner():
    memory = _input_memory()
    output = _output_owner(source="recipe:iron-plate", source_unit=17, unit_start=3100)
    memory.output_commitments = output
    loaded = _roundtrip(memory)
    assert loaded.input_commitments[INPUT_SOURCE]["source_unit"] == \
        loaded.output_commitments[INPUT_SOURCE]["source_unit"] == 17


def test_resume_rejects_cross_family_alias_before_backend_attachment(tmp_path):
    memory = _input_memory()
    output_part = _install_output(memory)
    input_part = memory.input_commitments[INPUT_SOURCE]["parts"]["inserter"]
    _alias_fields(output_part, input_part, "unit_receipt")
    checkpoint = tmp_path / "ownership.json"
    checkpoint.write_bytes(canonical(asdict(memory)))

    class Backend:
        output_buffers_supported = True
        input_routes_supported = True

        def __init__(self):
            self.attached = 0

        def enable_factory(self):
            self.attached += 1
            raise AssertionError("backend initialized before composed ownership validation")

    backend = Backend()
    loop_type = input_loop_type(buffered_loop_type(HierarchicalLoop))
    with pytest.raises(ValueError, match="Aliased retained paid owner"):
        loop_type(backend, policy="deterministic", target="rocket_launch",
                  factory_scheduling="ready-work", checkpoint=str(checkpoint),
                  resume_controller=True)
    assert backend.attached == 0


def test_coal_paid_chest_remains_a_solid_source_reference_not_a_second_solid_owner():
    memory, coal_chest = _coal_memory_with_shared_solid_endpoint()
    output_part = _install_output(memory)
    # The coal source chest is the solid route's source endpoint.  Only the
    # solid route's paid `parts` are newly owned by that transport family.
    assert memory.solid_commitments[next(iter(memory.solid_commitments))]["source"]["unit_number"] == \
        coal_chest["unit_number"]
    loaded = _roundtrip(memory)
    assert loaded.coal_commitments[TARGETS[0]]["parts"]["chest"] == coal_chest
    assert loaded.output_commitments["recipe:copper-plate"]["parts"]["chest"] == output_part


def test_no_output_extension_keeps_distinct_solid_and_coal_owners_valid():
    composed, coal_chest = _coal_memory_with_shared_solid_endpoint()
    memory = _without_output_extension(composed)
    assert not hasattr(memory, "output_buffers_schema")
    loaded = _roundtrip(memory)
    direct = _direct_from_bytes(memory)
    assert not hasattr(loaded, "output_buffers_schema")
    assert loaded.solid_commitments == memory.solid_commitments
    assert loaded.coal_commitments[TARGETS[0]]["parts"]["chest"] == coal_chest
    assert direct.solid_commitments == loaded.solid_commitments
    assert direct.coal_commitments == loaded.coal_commitments


@pytest.mark.parametrize("dimension", ["unit", "receipt", "unit_receipt", "role"])
def test_no_output_extension_cannot_hide_solid_coal_paid_identity_alias(dimension):
    composed, _ = _coal_memory_with_shared_solid_endpoint()
    memory = _without_output_extension(composed)
    solid_part = memory.solid_commitments[next(iter(memory.solid_commitments))]["parts"]["receive"]
    coal_part = memory.coal_commitments[TARGETS[0]]["parts"]["chest"]
    _alias_fields(solid_part, coal_part, dimension)

    assert not hasattr(memory, "output_buffers_schema")
    with pytest.raises(ValueError, match="Aliased retained paid owner"):
        _roundtrip(memory)
    with pytest.raises(ValueError, match="Aliased retained paid owner"):
        _direct_from_bytes(memory)
