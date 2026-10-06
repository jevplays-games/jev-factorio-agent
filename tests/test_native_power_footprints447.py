"""Offline production-path regressions for native pole coverage planning.

Most cases supply read-only native responses; the Lupa cases also execute the
generated geometry and prepayment Lua against API-shaped offline entities.
None loads a game or mutates a native world. NativeFactory.execute and
FairActions.connect remain the production Python entry points under test.
"""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

from jev_factorio.backends.fair_actions import FairActions
from jev_factorio.backends.errors import ConnectionPreflightRejected
from jev_factorio.backends.native_factory import NativeFactory
from jev_factorio.planning.connection_identity import connection_key


SOURCE_ROLE = "utility:source"
TARGET_ROLE = "utility:target"
POLE_IDENTITY = {
    "source": SOURCE_ROLE,
    "target": TARGET_ROLE,
    "kind": "small-electric-pole",
    "fluid": "electricity",
}


@dataclass(frozen=True)
class Position:
    x: float
    y: float


def _install_fle_stub(monkeypatch):
    """Satisfy the tiny Position/Direction adapter surface without FLE."""
    package = ModuleType("fle")
    package.__path__ = []
    env = ModuleType("fle.env")
    env.Position = Position
    env.Direction = SimpleNamespace(UP=0)
    monkeypatch.setitem(sys.modules, "fle", package)
    monkeypatch.setitem(sys.modules, "fle.env", env)


def _point(x, y):
    return {"x": float(x), "y": float(y)}


def _endpoint(role, unit, position, bounds, *, direction=0, orientation=0.0,
              name="assembling-machine-1"):
    return {
        "role": role,
        "name": name,
        "unit_number": unit,
        "position": _point(*position),
        "direction": direction,
        "orientation": float(orientation),
        "surface_index": 1,
        "force_index": 1,
        "quality": "normal",
        "bounding_box": {
            "left_top": _point(bounds[0], bounds[1]),
            "right_bottom": _point(bounds[2], bounds[3]),
            "orientation": float(orientation),
        },
    }


def _geometry(source=None, target=None, *, supply=2.5, wire=7.5,
              actor_unit=7, tick=120):
    return {
        "schema": "jev.native-pole-geometry.v1",
        "base_version": "2.0.77",
        "session_id": "offline-session-447",
        "tick": tick,
        "actor_unit": actor_unit,
        "surface_index": 1,
        "force_index": 1,
        "supply_area_distance": float(supply),
        "maximum_wire_distance": float(wire),
        "source": source or _endpoint(
            SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, -4.0, 5.0, 5.0),
        ),
        "target": target or _endpoint(
            TARGET_ROLE, 202, (22.5, 0.5), (18.0, -4.0, 27.0, 5.0),
        ),
    }


def _route_fixture(monkeypatch, cells, *, geometry=None, inventory=20,
                   stale_binding=False, fail_place_at=None):
    _install_fle_stub(monkeypatch)
    geometry = geometry or _geometry()
    events = []
    scripts = {"native_geometry": [], "cell_queries": [], "binding": []}
    expected_receipt = connection_key(POLE_IDENTITY)

    factory = object.__new__(NativeFactory)
    factory.catalog = SimpleNamespace(version="2.0.77")
    factory.backend = SimpleNamespace(
        _tools=SimpleNamespace(), _native_attachment=None,
    )

    def native_command(script):
        scripts["native_geometry"].append(script)
        if "bounding_box" not in script and "get_supply_area_distance" not in script:
            raise AssertionError("Unexpected NativeFactory command in pole fixture")
        return json.dumps(geometry, allow_nan=True)

    factory.command = native_command
    factory.entity = lambda role: SimpleNamespace(
        name="assembling-machine-1",
        position=Position(*(geometry["source" if role == SOURCE_ROLE else "target"]["position"][axis]
                            for axis in ("x", "y"))),
    )
    factory.prototype = lambda name: SimpleNamespace(value=(name, object()))

    fair = object.__new__(FairActions)
    fair.backend = factory.backend

    def fair_command(script):
        if "local result = {buildable={}, existing={}}" in script:
            scripts["cell_queries"].append(script)
            events.append("cells")
            return json.dumps({
                "buildable": [_point(*cell) for cell in cells],
                "existing": [],
            })
        if "get_item_count(" in script:
            events.append("inventory")
            return json.dumps({"count": inventory})
        if "connector_begin(" in script:
            scripts["binding"].append(script)
            events.append("binding_check")
            if stale_binding:
                raise RuntimeError("Native endpoint identity changed")
            events.append("begin")
            return json.dumps({"id": expected_receipt, "paid": 0, "external": 0})
        if "connector_finish(" in script:
            events.append("finish")
            return json.dumps({"id": expected_receipt, "paid": len(placed), "owned": True})
        raise AssertionError("Unexpected FairActions command in pole fixture")

    fair.command = fair_command
    placed = []

    def place_entity(prototype, position, direction, exact=False, **kwargs):
        assert prototype.value[0] == "small-electric-pole"
        assert exact is True
        if "connector" in kwargs:
            assert kwargs["connector"] == (expected_receipt, len(placed) + 1)
        placed.append((position.x, position.y))
        events.append("place")
        if fail_place_at == len(placed):
            raise RuntimeError("ambiguous paid placement")
        return SimpleNamespace(name="small-electric-pole", position=position)

    fair.place_entity = place_entity
    factory.backend._fair = fair
    return factory, fair, events, scripts, placed


def _execute_pole(factory):
    return factory.execute("factory_connect", dict(POLE_IDENTITY))


def _lua_api_route_fixture(monkeypatch, cells, *, geometry=None, inventory=20,
                           binding_mutation=None, advance_tick=False):
    """Run the generated geometry and prepayment Lua through an offline API model."""
    from lupa import LuaRuntime, lua_type

    geometry = deepcopy(geometry or _geometry())
    factory, fair, events, scripts, placed = _route_fixture(
        monkeypatch, cells, geometry=geometry, inventory=inventory,
    )
    lua = LuaRuntime(unpack_returned_tuples=True)
    output = []
    begin_calls = []
    finish_calls = []

    def to_lua(value):
        if isinstance(value, dict):
            table = lua.table()
            for key, item in value.items():
                table[key] = to_lua(item)
            return table
        if isinstance(value, (list, tuple)):
            table = lua.table()
            for index, item in enumerate(value, 1):
                table[index] = to_lua(item)
            return table
        return value

    def from_lua(value):
        if lua_type(value) != "table":
            return value
        keys = list(value.keys())
        if keys and all(type(key) is int and key > 0 for key in keys):
            if set(keys) == set(range(1, len(keys) + 1)):
                return [from_lua(value[index]) for index in range(1, len(keys) + 1)]
        return {key: from_lua(value[key]) for key in keys}

    def table_to_json(value):
        return json.dumps(from_lua(value), separators=(",", ":"), allow_nan=False)

    def json_to_table(raw):
        return to_lua(json.loads(raw))

    surface = lua.table(index=geometry["surface_index"])
    force = lua.table(index=geometry["force_index"])
    actor = lua.table()
    actor.character = lua.table(valid=True, unit_number=geometry["actor_unit"])
    actor.surface = surface
    actor.force = force
    actor.get_item_count = lambda _name: inventory

    entities = lua.table()
    endpoint_tables = {}
    for role_key in ("source", "target"):
        endpoint = geometry[role_key]
        entity = lua.table()
        entity.valid = True
        entity.unit_number = endpoint["unit_number"]
        entity.name = endpoint["name"]
        entity.position = to_lua(endpoint["position"])
        entity.direction = endpoint["direction"]
        entity.orientation = endpoint["orientation"]
        entity.surface = surface
        entity.force = force
        entity.quality = lua.table(name=endpoint["quality"])
        entity.bounding_box = to_lua(endpoint["bounding_box"])
        entities[endpoint["role"]] = entity
        endpoint_tables[role_key] = entity

    storage = lua.table()
    fair_api = lua.table()
    fair_api.actor = lambda: actor
    storage.fair = fair_api
    storage.jev_session_id = geometry["session_id"]
    campaign = lua.table()
    campaign.entities = entities

    expected_receipt = connection_key(POLE_IDENTITY)

    def connector_begin(receipt, source_role, target_role, kind, fluid, route):
        begin_calls.append((receipt, source_role, target_role, kind, fluid,
                            from_lua(route)))
        events.append("lua_begin")
        return to_lua({"id": receipt, "paid": 0, "external": 0})

    def connector_finish(receipt):
        finish_calls.append(receipt)
        events.append("lua_finish")
        return to_lua({"id": receipt, "paid": len(placed), "owned": True})

    campaign.connector_begin = connector_begin
    campaign.connector_finish = connector_finish
    storage.campaign = campaign
    pole = lua.table()
    pole.supply_area_distance = geometry["supply_area_distance"]
    pole.maximum_wire_distance = geometry["maximum_wire_distance"]
    pole.get_supply_area_distance = lambda _quality: pole.supply_area_distance
    pole.get_max_wire_distance = lambda _quality: pole.maximum_wire_distance
    prototypes = lua.table(entity=lua.table())
    prototypes.entity["small-electric-pole"] = pole
    active_mods = lua.table(base=geometry["base_version"])
    lua.globals().storage = storage
    lua.globals().prototypes = prototypes
    lua.globals().script = lua.table(active_mods=active_mods)
    lua.globals().game = lua.table(tick=geometry["tick"])
    lua.globals().helpers = lua.table(
        table_to_json=table_to_json,
        json_to_table=json_to_table,
    )
    lua.globals().rcon = lua.table(print=lambda value: output.append(value))

    def execute_lua(script_text):
        output.clear()
        lua.execute(script_text)
        if len(output) != 1:
            raise AssertionError(f"Expected one modeled RCON response, received {len(output)}")
        return output[0]

    def mutate_before_binding():
        if advance_tick:
            lua.globals().game.tick = geometry["tick"] + 1
        if binding_mutation is None:
            return
        source, target = endpoint_tables["source"], endpoint_tables["target"]
        other_surface = lua.table(index=geometry["surface_index"] + 1)
        other_force = lua.table(index=geometry["force_index"] + 1)
        mutations = {
            "actor_unit": lambda: setattr(actor.character, "unit_number", geometry["actor_unit"] + 1),
            "actor_character_invalid": lambda: setattr(actor.character, "valid", False),
            "actor_surface": lambda: setattr(actor, "surface", other_surface),
            "actor_force": lambda: setattr(actor, "force", other_force),
            "session": lambda: setattr(storage, "jev_session_id", "replaced-session"),
            "tick_rewind": lambda: setattr(lua.globals().game, "tick", geometry["tick"] - 1),
            "base_version": lambda: setattr(active_mods, "base", "2.0.78"),
            "supply_limit": lambda: setattr(pole, "supply_area_distance", pole.supply_area_distance + 0.5),
            "wire_limit": lambda: setattr(pole, "maximum_wire_distance", pole.maximum_wire_distance + 0.5),
            "source_valid": lambda: setattr(source, "valid", False),
            "source_unit": lambda: setattr(source, "unit_number", source.unit_number + 1),
            "target_unit": lambda: setattr(target, "unit_number", target.unit_number + 1),
            "source_name": lambda: setattr(source, "name", "chemical-plant"),
            "source_direction": lambda: setattr(source, "direction", (source.direction + 4) % 16),
            "source_orientation": lambda: setattr(source, "orientation", 0.5),
            "source_quality": lambda: setattr(source.quality, "name", "uncommon"),
            "source_surface": lambda: setattr(source, "surface", other_surface),
            "source_force": lambda: setattr(source, "force", other_force),
            "source_position": lambda: setattr(source.position, "x", source.position.x + 1),
            "source_box_left": lambda: setattr(source.bounding_box.left_top, "x", source.bounding_box.left_top.x - 1),
            "source_box_right": lambda: setattr(source.bounding_box.right_bottom, "x", source.bounding_box.right_bottom.x + 1),
            "source_box_orientation": lambda: setattr(source.bounding_box, "orientation", 0.5),
            "target_valid": lambda: setattr(target, "valid", False),
            "target_name": lambda: setattr(target, "name", "chemical-plant"),
            "target_direction": lambda: setattr(target, "direction", (target.direction + 4) % 16),
            "target_orientation": lambda: setattr(target, "orientation", 0.5),
            "target_quality": lambda: setattr(target.quality, "name", "uncommon"),
            "target_surface": lambda: setattr(target, "surface", other_surface),
            "target_force": lambda: setattr(target, "force", other_force),
            "target_position": lambda: setattr(target.position, "x", target.position.x + 1),
            "target_box_left": lambda: setattr(target.bounding_box.left_top, "x", target.bounding_box.left_top.x - 1),
            "target_box_right": lambda: setattr(target.bounding_box.right_bottom, "x", target.bounding_box.right_bottom.x + 1),
            "target_box_orientation": lambda: setattr(target.bounding_box, "orientation", 0.5),
        }
        try:
            mutations[binding_mutation]()
        except KeyError as error:
            raise AssertionError(f"Unknown modeled binding mutation: {binding_mutation}") from error

    def native_command(script_text):
        assert "bounding_box=box(e.bounding_box)" in script_text
        assert "get_supply_area_distance" in script_text
        scripts["native_geometry"].append(script_text)
        raw = execute_lua(script_text)
        scripts.setdefault("native_rows", []).append(json.loads(raw))
        return raw

    factory.command = native_command
    original_fair_command = fair.command

    def fair_command(script_text):
        if "local expected=helpers.json_to_table" in script_text and "connector_begin(" in script_text:
            scripts["binding"].append(script_text)
            events.append("binding_check")
            mutate_before_binding()
            return execute_lua(script_text)
        if "connector_finish(" in script_text:
            return execute_lua(script_text)
        return original_fair_command(script_text)

    fair.command = fair_command
    return factory, events, scripts, placed, begin_calls, finish_calls, geometry, lua


def test_native_factory_large_machine_uses_authenticated_footprint_for_pole_route(monkeypatch):
    """A silo-sized endpoint is covered at 5.5 tiles although its center is not."""
    factory, _, events, scripts, placed = _route_fixture(
        monkeypatch, [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
    )

    result = _execute_pole(factory)

    assert result.startswith("Constructed small-electric-pole connection")
    assert placed == [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)]
    assert events.index("inventory") < events.index("begin") < events.index("place")
    assert events[-1] == "finish"
    assert len(scripts["native_geometry"]) == 1
    assert "bounding_box=box(e.bounding_box)" in scripts["native_geometry"][0]
    assert "get_supply_area_distance" in scripts["native_geometry"][0]
    assert "get_max_wire_distance" in scripts["native_geometry"][0]
    assert len(scripts["cell_queries"]) <= 4


def test_quarter_turn_box_rejects_cell_covered_only_by_unrotated_bounds(monkeypatch):
    """A rotated 9x1 footprint does not keep its unrotated x reach."""
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, 0.0, 5.0, 1.0),
        direction=4, orientation=0.25,
    )
    geometry = _geometry(source=source)
    factory, _, events, _, placed = _route_fixture(
        monkeypatch,
        [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
    )

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route"):
        _execute_pole(factory)

    # The native box rotates from x[-4, 5], y[0, 1] to x[0, 1], y[-4, 5].
    # The former source cell at x=5.5 is therefore outside actual supply coverage.
    assert "inventory" not in events
    assert "begin" not in events
    assert placed == []


def test_quarter_turn_coverage_accepts_only_cells_in_rotated_bounds(monkeypatch):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, 0.0, 5.0, 1.0),
        direction=4, orientation=0.25,
    )
    geometry = _geometry(source=source)
    factory, _, events, _, placed = _route_fixture(
        monkeypatch,
        [(1.5, 0.5), (7.5, 0.5), (13.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
    )

    result = _execute_pole(factory)

    assert result.startswith("Constructed small-electric-pole connection")
    assert placed[0] == (1.5, 0.5)
    assert placed[-1] == (17.5, 0.5)
    assert events.index("inventory") < events.index("begin") < events.index("place")
    assert events[-1] == "finish"


def test_half_turn_shifted_asymmetric_box_uses_entity_position_as_pivot(monkeypatch):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-1.5, 0.0, 3.5, 2.0),
        direction=8, orientation=0.5,
    )
    geometry = _geometry(source=source, supply=1.0)
    factory, _, _, _, placed = _route_fixture(
        monkeypatch,
        [(-2.5, 0.5), (4.5, 0.5), (10.5, 0.5), (16.5, 0.5), (22.5, 0.5)],
        geometry=geometry,
    )

    _execute_pole(factory)

    # Rotating around the entity center expands the left reach to -2.5;
    # (-2.5, 0.5) overlaps its 1-tile square supply, while the unrotated
    # footprint only touches the pole square at x=-1.5 and does not overlap.
    assert placed[0] == (-2.5, 0.5)
    assert placed[-1] == (22.5, 0.5)


@pytest.mark.parametrize(
    ("orientation", "source_cell", "cells"),
    [
        (0.25, (-2.5, 0.5),
         [(-2.5, 0.5), (3.5, 0.5), (9.5, 0.5), (15.5, 0.5), (21.5, 0.5)]),
        (0.75, (3.5, 0.5),
         [(3.5, 0.5), (9.5, 0.5), (15.5, 0.5), (21.5, 0.5)]),
    ],
)
def test_quarter_and_three_quarter_turns_route_from_transformed_rectangle(
    monkeypatch, orientation, source_cell, cells,
):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-0.5, 0.0, 1.5, 4.0),
        direction=4 if orientation == 0.25 else 12,
        orientation=orientation,
    )
    geometry = _geometry(source=source, supply=1.0)
    factory, _, _, _, placed = _route_fixture(monkeypatch, cells, geometry=geometry)

    _execute_pole(factory)

    # These first cells overlap only the narrow rectangle after its own
    # cardinal turn around the native entity position.
    assert placed[0] == source_cell


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        (0.0, (-1.5, 0.0, 3.5, 2.0)),
        (0.25, (-1.0, -1.5, 1.0, 3.5)),
        (0.5, (-2.5, -1.0, 2.5, 1.0)),
        (0.75, (0.0, -2.5, 2.0, 2.5)),
    ],
)
def test_normalized_shifted_box_matches_cardinal_orientation_matrix(orientation, expected):
    bounds = FairActions._normalized_pole_bounds(
        _point(0.5, 0.5),
        {
            "left_top": _point(-1.5, 0.0),
            "right_bottom": _point(3.5, 2.0),
            "orientation": orientation,
        },
    )
    assert (
        bounds["left_top"]["x"], bounds["left_top"]["y"],
        bounds["right_bottom"]["x"], bounds["right_bottom"]["y"],
    ) == expected


def test_zero_bounding_box_orientation_is_not_rotated_by_entity_orientation(monkeypatch):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, 0.0, 5.0, 1.0),
        direction=4, orientation=0.25,
    )
    source["bounding_box"]["orientation"] = 0.0
    geometry = _geometry(source=source)
    factory, _, _, _, placed = _route_fixture(
        monkeypatch,
        [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
    )

    _execute_pole(factory)

    assert placed[0] == (5.5, 0.5)


def test_rotated_box_cannot_escape_native_world_coordinate_limit():
    source = _endpoint(
        SOURCE_ROLE, 101, (999999.8, 999999.8),
        (999999.3, 999999.5, 1000000.0, 1000000.0),
        direction=4, orientation=0.25,
    )

    with pytest.raises(ValueError, match="normalized native pole footprint bounds"):
        FairActions.validate_pole_geometry(
            _geometry(source=source), base_version="2.0.77",
            source_role=SOURCE_ROLE, target_role=TARGET_ROLE,
        )


def test_generated_native_geometry_and_binding_lua_accept_coherent_rotated_endpoints(monkeypatch):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, 0.0, 5.0, 1.0),
        direction=4, orientation=0.25,
    )
    target = _endpoint(
        TARGET_ROLE, 202, (22.5, 0.5), (18.0, -4.0, 27.0, 5.0),
        direction=12, orientation=0.75,
    )
    geometry = _geometry(source=source, target=target)
    factory, events, scripts, placed, begin_calls, finish_calls, _, _ = _lua_api_route_fixture(
        monkeypatch,
        [(1.5, 0.5), (7.5, 0.5), (13.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
        advance_tick=True,
    )

    result = _execute_pole(factory)

    assert result.startswith("Constructed small-electric-pole connection")
    native_row = scripts["native_rows"][0]
    assert native_row["source"]["bounding_box"]["orientation"] == 0.25
    assert native_row["target"]["bounding_box"]["orientation"] == 0.75
    assert len(scripts["native_geometry"]) == len(scripts["binding"]) == 1
    assert events.index("inventory") < events.index("binding_check") < events.index("lua_begin")
    assert placed[0] == (1.5, 0.5) and placed[-1] == (17.5, 0.5)
    assert begin_calls[0][:5] == (
        connection_key(POLE_IDENTITY), SOURCE_ROLE, TARGET_ROLE,
        "small-electric-pole", "electricity",
    )
    assert len(begin_calls[0][5]) == len(placed)
    assert finish_calls == [connection_key(POLE_IDENTITY)]


def test_generated_query_keeps_zero_box_orientation_without_entity_rotation(monkeypatch):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, 0.0, 5.0, 1.0),
        direction=4, orientation=0.25,
    )
    source["bounding_box"]["orientation"] = 0.0
    geometry = _geometry(source=source)
    factory, _, scripts, placed, begin_calls, _, _, _ = _lua_api_route_fixture(
        monkeypatch,
        [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
    )

    _execute_pole(factory)

    row = scripts["native_rows"][0]
    assert row["source"]["orientation"] == 0.25
    assert row["source"]["bounding_box"]["orientation"] == 0.0
    assert placed[0] == (5.5, 0.5)
    assert len(begin_calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "actor_unit", "actor_character_invalid", "actor_surface", "actor_force", "session", "tick_rewind",
        "base_version", "supply_limit", "wire_limit", "source_valid",
        "source_unit", "target_unit", "source_name", "source_direction",
        "source_orientation", "source_quality", "source_surface", "source_force",
        "source_position", "source_box_left", "source_box_right",
        "source_box_orientation", "target_valid", "target_name", "target_direction",
        "target_orientation", "target_quality", "target_surface", "target_force",
        "target_position", "target_box_left", "target_box_right", "target_box_orientation",
    ],
)
def test_generated_prepayment_lua_rejects_actor_session_endpoint_and_limit_drift(
    monkeypatch, mutation,
):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, 0.0, 5.0, 1.0),
        direction=4, orientation=0.25,
    )
    geometry = _geometry(source=source)
    factory, events, scripts, placed, begin_calls, _, _, _ = _lua_api_route_fixture(
        monkeypatch,
        [(1.5, 0.5), (7.5, 0.5), (13.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
        binding_mutation=mutation,
    )

    with pytest.raises(Exception, match="Native pole"):
        _execute_pole(factory)

    assert len(scripts["native_geometry"]) == 1
    assert len(scripts["binding"]) == 1
    assert "inventory" in events and "binding_check" in events
    assert begin_calls == []
    assert "lua_begin" not in events
    assert not placed


def test_generated_native_query_rejects_noncardinal_box_before_route_or_inventory(monkeypatch):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 0.5), (-4.0, 0.0, 5.0, 1.0),
        direction=4, orientation=0.25,
    )
    source["bounding_box"]["orientation"] = 0.125
    geometry = _geometry(source=source)
    factory, events, scripts, placed, begin_calls, _, _, _ = _lua_api_route_fixture(
        monkeypatch,
        [(1.5, 0.5), (7.5, 0.5), (13.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
    )

    with pytest.raises(ValueError, match="non-cardinal native pole source footprint orientation"):
        _execute_pole(factory)

    assert len(scripts["native_geometry"]) == 1
    assert scripts["binding"] == []
    assert "cells" not in events and "inventory" not in events
    assert begin_calls == []
    assert not placed


def test_small_machine_endpoints_keep_ordinary_native_coverage(monkeypatch):
    geometry = _geometry(
        _endpoint(SOURCE_ROLE, 101, (0.5, 0.5), (0.1, 0.1, 0.9, 0.9)),
        _endpoint(TARGET_ROLE, 202, (8.5, 0.5), (8.1, 0.1, 8.9, 0.9)),
    )
    factory, _, events, _, placed = _route_fixture(
        monkeypatch, [(2.5, 0.5), (6.5, 0.5)], geometry=geometry,
    )

    _execute_pole(factory)

    assert placed == [(2.5, 0.5), (6.5, 0.5)]
    assert events.index("begin") < events.index("place")
    assert events[-1] == "finish"


def test_shifted_rectangular_rotated_footprints_cover_both_route_ends(monkeypatch):
    source = _endpoint(
        SOURCE_ROLE, 101, (0.5, 10.5), (-2.0, 6.0, 3.0, 15.0),
        direction=4, orientation=0.25,
    )
    target = _endpoint(
        TARGET_ROLE, 202, (24.5, 10.5), (22.0, 6.0, 27.0, 15.0),
        direction=12, orientation=0.75,
    )
    geometry = _geometry(source, target)
    factory, _, _, _, placed = _route_fixture(
        monkeypatch,
        [(4.5, 10.5), (10.5, 10.5), (16.5, 10.5), (20.5, 10.5)],
        geometry=geometry,
    )

    _execute_pole(factory)

    assert placed == [
        (4.5, 10.5), (10.5, 10.5), (16.5, 10.5), (20.5, 10.5),
    ]


def test_nearby_cell_outside_actual_power_area_is_rejected(monkeypatch):
    geometry = _geometry(
        _endpoint(SOURCE_ROLE, 101, (0.5, 0.5), (0.1, 0.1, 0.9, 0.9)),
        _endpoint(TARGET_ROLE, 202, (9.5, 0.5), (9.1, 0.1, 9.9, 0.9)),
    )
    factory, _, events, _, placed = _route_fixture(
        monkeypatch, [(3.5, 0.5), (7.5, 0.5)], geometry=geometry,
    )

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route"):
        _execute_pole(factory)

    # (3.5, 0.5) is only 3 tiles from the source center, inside the old 3.5
    # center gate, but its square supply area does not overlap the 0.8x0.8 box.
    assert "inventory" not in events
    assert "begin" not in events
    assert placed == []


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda value: value.__setitem__("base_version", "2.1.0"),
                     id="wrong-game-version"),
        pytest.param(lambda value: value.__setitem__("supply_area_distance", float("nan")),
                     id="nonfinite-supply-area"),
        pytest.param(lambda value: value.__setitem__("maximum_wire_distance", 0),
                     id="invalid-wire-distance"),
        pytest.param(lambda value: value["source"].__setitem__("orientation", 0.125),
                     id="unsupported-orientation"),
        pytest.param(lambda value: value["target"].__setitem__("surface_index", 2),
                     id="wrong-endpoint-surface"),
        pytest.param(lambda value: value["source"]["bounding_box"]["right_bottom"].__setitem__("x", 1_000_001),
                     id="out-of-bounds-footprint"),
        pytest.param(lambda value: value["source"]["bounding_box"].__setitem__("left_top", {"x": 4, "y": 0}),
                     id="inverted-footprint"),
        pytest.param(lambda value: value.__setitem__("unknown", True),
                     id="unknown-native-field"),
    ],
)
def test_malformed_native_geometry_fails_before_cell_search_or_payment(monkeypatch, mutate):
    geometry = deepcopy(_geometry())
    mutate(geometry)
    factory, _, events, scripts, placed = _route_fixture(
        monkeypatch, [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
    )

    with pytest.raises((TypeError, ValueError)):
        _execute_pole(factory)

    assert scripts["native_geometry"]
    assert scripts["cell_queries"] == []
    assert "inventory" not in events and "begin" not in events
    assert placed == []


def test_endpoint_snapshot_is_rechecked_with_receipt_before_payment(monkeypatch):
    factory, _, events, scripts, placed = _route_fixture(
        monkeypatch, [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
        stale_binding=True,
    )

    with pytest.raises(RuntimeError, match="endpoint identity changed"):
        _execute_pole(factory)

    assert events.index("inventory") < events.index("binding_check")
    assert "begin" not in events and "place" not in events and "finish" not in events
    assert placed == []
    script = scripts["binding"][0]
    for bound_field in (
        "expected.actor_unit", "expected.session_id", "expected.surface_index",
        "expected.force_index", "expected.source", "expected.target",
        "unit_number==want.unit_number", "same_point(e.position,want.position)",
        "e.bounding_box", "get_supply_area_distance", "get_max_wire_distance",
    ):
        assert bound_field in script


def test_wire_limit_and_global_search_bounds_are_native_and_finite(monkeypatch):
    geometry = _geometry(wire=5.5)
    factory, _, events, scripts, placed = _route_fixture(
        monkeypatch, [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
        geometry=geometry,
    )

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route"):
        _execute_pole(factory)

    assert len(scripts["cell_queries"]) <= 4
    total_cells = 0
    for script in scripts["cell_queries"]:
        rectangles = []
        for match in re.finditer(
            r"for horizontal=(-?\d+),(-?\d+) do for vertical=(-?\d+),(-?\d+) do",
            script,
        ):
            left, right, top, bottom = map(int, match.groups())
            assert left >= -1_000_000 and right <= 999_999
            assert top >= -1_000_000 and bottom <= 999_999
            cells = (right - left + 1) * (bottom - top + 1)
            assert 0 < cells <= 16_384
            rectangles.append(cells)
        assert rectangles
        total_cells += sum(rectangles)
    assert total_cells <= 65_536
    assert "inventory" not in events and "begin" not in events
    assert placed == []


def test_insufficient_inventory_rejects_before_connector_receipt(monkeypatch):
    factory, _, events, _, placed = _route_fixture(
        monkeypatch, [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)], inventory=0,
    )

    with pytest.raises(ConnectionPreflightRejected, match="insufficient_connection_materials"):
        _execute_pole(factory)

    assert events[-1] == "inventory"
    assert "begin" not in events and "place" not in events
    assert placed == []


def test_blocked_route_rejects_before_inventory_or_receipt(monkeypatch):
    factory, _, events, _, placed = _route_fixture(monkeypatch, [])

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route"):
        _execute_pole(factory)

    assert events == ["cells"]
    assert placed == []


def test_ambiguous_partial_payment_keeps_receipt_open_without_retry(monkeypatch):
    factory, _, events, _, placed = _route_fixture(
        monkeypatch, [(5.5, 0.5), (11.5, 0.5), (17.5, 0.5)],
        fail_place_at=2,
    )

    with pytest.raises(RuntimeError, match="ambiguous paid placement"):
        _execute_pole(factory)

    assert events.count("begin") == 1
    assert events.count("place") == 2
    assert "finish" not in events
    assert placed == [(5.5, 0.5), (11.5, 0.5)]


def test_point_only_direct_fair_actions_call_keeps_legacy_behavior(monkeypatch):
    factory, fair, events, scripts, placed = _route_fixture(
        monkeypatch, [(1.5, 0.5), (5.5, 0.5)],
    )
    prototype = SimpleNamespace(value=("small-electric-pole", object()))

    fair.connect(Position(0.5, 0.5), Position(6.5, 0.5), prototype, "electricity")

    assert placed == [(1.5, 0.5), (5.5, 0.5)]
    assert scripts["native_geometry"] == []
    assert "begin" not in events and "finish" not in events
