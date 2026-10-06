"""Offline dispatch and route-selection regressions for native fluid ports.

The fake port/collision responses stand in for read-only native observations.
The tests exercise NativeFactory.execute, FairActions.connect, and the real
shortest_pipe_path planner; they do not make native calls or claim gameplay
acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest

from jev_factorio.backends.errors import ConnectionPreflightRejected
from jev_factorio.backends.fair_actions import FairActions
from jev_factorio.backends.native_factory import NativeFactory
from jev_factorio.planning.connection_identity import connection_key


@dataclass(frozen=True)
class Position:
    x: float
    y: float


@pytest.fixture
def fle_boundary(monkeypatch):
    env = ModuleType("fle.env")
    env.Position = Position
    env.Direction = SimpleNamespace(UP=SimpleNamespace(value=0))
    monkeypatch.setitem(sys.modules, "fle", ModuleType("fle"))
    monkeypatch.setitem(sys.modules, "fle.env", env)
    return env


def _identity():
    return {
        "source": "source",
        "target": "target",
        "kind": "pipe",
        "fluid": "water",
    }


def _pipeline(monkeypatch, *, source_ports, target_ports, buildable,
              count=100, branch=None, wrong_begin_receipt=False,
              wrong_finish_receipt=False, fail_placement_at=None,
              source_response=None, target_response=None, existing=()):
    events = []
    placements = []
    receipt = connection_key(_identity())

    fair = object.__new__(FairActions)

    def fair_command(script):
        if "local result = {buildable={}, existing={}}" in script:
            rectangles = [
                tuple(int(value) for value in match.groups())
                for match in re.finditer(
                    r"for horizontal=(-?\d+),(-?\d+) do for vertical=(-?\d+),(-?\d+) do include\(horizontal, vertical\) end end;",
                    script,
                )
            ]
            assert rectangles
            queried_cells = sum(
                (right - left + 1) * (bottom - top + 1)
                for left, right, top, bottom in rectangles
            )
            assert queried_cells <= 16_384
            events.append(("readonly_cells", script, queried_cells, rectangles))

            def in_query(point):
                tile_x, tile_y = math.floor(point[0]), math.floor(point[1])
                return any(
                    left <= tile_x <= right and top <= tile_y <= bottom
                    for left, right, top, bottom in rectangles
                )

            return json.dumps({
                "buildable": [
                    {"x": x, "y": y} for x, y in sorted(buildable) if in_query((x, y))
                ],
                "existing": [
                    {"x": x, "y": y} for x, y in sorted(existing) if in_query((x, y))
                ],
            })
        if "get_item_count" in script:
            events.append(("inventory_read", script))
            return json.dumps({"count": count})
        if "storage.campaign.connector_begin(" in script:
            events.append(("connector_begin", script))
            return json.dumps({"id": "wrong-receipt" if wrong_begin_receipt else receipt})
        if "storage.campaign.connector_finish(" in script:
            events.append(("connector_finish", script))
            return json.dumps({"id": "wrong-receipt" if wrong_finish_receipt else receipt})
        raise AssertionError(f"Unexpected FairActions command: {script}")

    fair.command = fair_command

    def place_entity(prototype, position, direction, exact=False, connector=None,
                     bootstrap_owned=False):
        event = ("place", position, connector, direction, exact)
        events.append(event)
        placements.append(event)
        if fail_placement_at == len(placements):
            raise RuntimeError("modeled placement acknowledgement was lost")
        return SimpleNamespace(position=position)

    fair.place_entity = place_entity

    factory = object.__new__(NativeFactory)
    factory.backend = SimpleNamespace(_tools=object(), _fair=fair, _native_attachment=None)

    def factory_call(function, *arguments):
        assert function == "pipe_source"
        events.append(("branch_read", arguments))
        return json.dumps(branch or {})

    factory.call = factory_call

    def factory_command(script):
        if 'storage.campaign.entities["source"]' in script:
            role = "source"
            response = source_response
            if response is None:
                response = {"points": [{"x": x, "y": y} for x, y in source_ports]}
        elif 'storage.campaign.entities["target"]' in script:
            role = "target"
            response = target_response
            if response is None:
                response = {"points": [{"x": x, "y": y} for x, y in target_ports]}
        else:
            raise AssertionError(f"Unexpected NativeFactory command: {script}")
        events.append(("port_read", role, script))
        return json.dumps(response, allow_nan=True)

    factory.command = factory_command
    factory.prototype = lambda kind: SimpleNamespace(value=(kind, object()))
    return factory, fair, events, placements


def _dispatch(factory):
    return factory.execute("factory_connect", _identity())


def test_factory_dispatch_selects_a_routable_native_port_pair_before_actuation(
    monkeypatch, fle_boundary,
):
    source_ports = [(0.5, 0.5), (0.5, 1.5)]
    target_ports = [(2.5, 0.5), (2.5, 1.5)]
    route = {(0.5, 1.5), (1.5, 1.5), (2.5, 1.5)}
    factory, _, events, placements = _pipeline(
        monkeypatch,
        source_ports=source_ports,
        target_ports=target_ports,
        buildable=route,
    )

    outcome = _dispatch(factory)

    assert outcome.startswith("Constructed pipe connection")
    assert placements
    placed_cells = [(event[1].x, event[1].y) for event in placements]
    assert placed_cells == [(0.5, 1.5), (1.5, 1.5), (2.5, 1.5)]
    names = [event[0] for event in events]
    assert names.count("branch_read") == 1
    assert names.count("port_read") == 2
    assert names.count("connector_begin") == 1
    assert names.count("connector_finish") == 1
    receipt = connection_key(_identity())
    assert [event[2] for event in placements] == [
        (receipt, 1), (receipt, 2), (receipt, 3),
    ]
    assert all(event[4] is True for event in placements)
    begin_script = next(event[1] for event in events if event[0] == "connector_begin")
    assert all(json.dumps(value) in begin_script for value in (
        receipt, "source", "target", "pipe", "water",
    ))
    discovery_scripts = [event[1] for event in events if event[0] == "readonly_cells"]
    assert discovery_scripts
    assert all("storage.fair.actor()" in script for script in discovery_scripts)
    assert all("storage.coal_supply" in script for script in discovery_scripts)
    first_begin = names.index("connector_begin")
    assert all(name in {"branch_read", "port_read", "readonly_cells", "inventory_read"}
               for name in names[:first_begin])
    assert names.index("inventory_read") < first_begin


def test_equal_distance_routes_choose_a_stable_native_port_pair(monkeypatch, fle_boundary):
    # Reverse the input order: the result must follow the documented coordinate
    # tie-break, not the order returned by Factorio's fluidbox table.
    source_ports = [(0.5, 2.5), (0.5, 0.5)]
    target_ports = [(2.5, 2.5), (2.5, 0.5)]
    route = {
        (0.5, 0.5), (1.5, 0.5), (2.5, 0.5),
        (0.5, 2.5), (1.5, 2.5), (2.5, 2.5),
    }
    factory, _, _, placements = _pipeline(
        monkeypatch, source_ports=source_ports, target_ports=target_ports,
        buildable=route,
    )

    _dispatch(factory)

    assert [(event[1].x, event[1].y) for event in placements] == [
        (0.5, 0.5), (1.5, 0.5), (2.5, 0.5),
    ]


def test_branch_source_uses_its_native_position_and_still_selects_target_port(
    monkeypatch, fle_boundary,
):
    factory, _, events, placements = _pipeline(
        monkeypatch,
        source_ports=[],
        target_ports=[(2.5, 1.5), (2.5, 0.5)],
        buildable={(0.5, 0.5), (1.5, 0.5), (2.5, 0.5)},
        branch={"x": 0.5, "y": 0.5},
    )

    assert _dispatch(factory).startswith("Constructed pipe connection")

    assert [event[1] for event in events if event[0] == "port_read"] == ["target"]
    assert len([event for event in events if event[0] == "branch_read"]) == 1
    assert [(event[1].x, event[1].y) for event in placements] == [
        (0.5, 0.5), (1.5, 0.5), (2.5, 0.5),
    ]


def test_no_reachable_pair_fails_before_inventory_or_connector_preparation(
    monkeypatch, fle_boundary,
):
    factory, _, events, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5), (0.5, 1.5)],
        target_ports=[(2.5, 0.5), (2.5, 1.5)],
        buildable=set(),
    )

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route"):
        _dispatch(factory)

    names = [event[0] for event in events]
    assert names.count("readonly_cells") > 0
    assert "inventory_read" not in names
    assert "connector_begin" not in names
    assert placements == []


@pytest.mark.parametrize(
    "malformed",
    [
        {"buildable": [{"x": float("nan"), "y": 0.5}], "existing": []},
        {"buildable": [{"x": 0.0, "y": 0.5}], "existing": []},
        {"buildable": [{"x": 0.4999999995, "y": 0.5},
                        {"x": 1.5, "y": 0.5}, {"x": 2.5, "y": 0.5}],
         "existing": []},
        {"buildable": [{"x": 1000.5, "y": 1000.5}], "existing": []},
        {"buildable": [{"x": 0.5, "y": 0.5, "unexpected": 1}], "existing": []},
        {"buildable": [], "existing": [], "unexpected": True},
    ],
    ids=["non-finite", "off-grid", "near-half-grid", "outside-query",
         "unknown-cell-field", "unknown-response-field"],
)
def test_invalid_collision_evidence_is_not_retried_as_no_route(
    monkeypatch, fle_boundary, malformed,
):
    factory, fair, events, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5)],
        target_ports=[(2.5, 0.5)],
        buildable={(0.5, 0.5), (1.5, 0.5), (2.5, 0.5)},
    )
    original_command = fair.command
    collision_queries = 0

    def malformed_then_valid(script):
        nonlocal collision_queries
        if "local result = {buildable={}, existing={}}" in script:
            collision_queries += 1
            if collision_queries == 1:
                return json.dumps(malformed, allow_nan=True)
        return original_command(script)

    fair.command = malformed_then_valid

    with pytest.raises(ValueError) as raised:
        _dispatch(factory)

    assert type(raised.value) is ValueError
    assert collision_queries == 1
    assert "inventory_read" not in [event[0] for event in events]
    assert "connector_begin" not in [event[0] for event in events]
    assert placements == []


def test_empty_factorio_collision_tables_are_valid_no_route_evidence(
    monkeypatch, fle_boundary,
):
    factory, fair, events, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5)],
        target_ports=[(2.5, 0.5)],
        buildable=set(),
    )
    original_command = fair.command
    collision_queries = 0

    def empty_native_tables(script):
        nonlocal collision_queries
        if "local result = {buildable={}, existing={}}" in script:
            collision_queries += 1
            # Factorio helpers.table_to_json serializes empty Lua tables as {}.
            return json.dumps({"buildable": {}, "existing": {}})
        return original_command(script)

    fair.command = empty_native_tables

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route"):
        _dispatch(factory)

    assert collision_queries > 0
    names = [event[0] for event in events]
    assert "inventory_read" not in names
    assert "connector_begin" not in names
    assert placements == []


def test_port_pair_search_does_not_hide_unrelated_preflight_errors(monkeypatch, fle_boundary):
    factory, fair, events, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5), (0.5, 1.5)],
        target_ports=[(2.5, 0.5)],
        buildable=set(),
    )
    attempted = []

    def raise_unrelated_preflight(start, end, fluid, *, budget=None):
        attempted.append((start, end, fluid))
        raise ConnectionPreflightRejected("insufficient_connection_materials")

    fair._pipe_route = raise_unrelated_preflight

    with pytest.raises(ConnectionPreflightRejected, match="insufficient_connection_materials"):
        _dispatch(factory)

    assert len(attempted) == 1
    names = [event[0] for event in events]
    assert "inventory_read" not in names
    assert "connector_begin" not in names
    assert placements == []


@pytest.mark.parametrize(
    "response",
    [
        {"points": [{"x": 0.5, "y": 0.5, "unexpected": 1}]},
        {"points": [{"x": float("nan"), "y": 0.5}]},
        {"points": [{"x": 0.0, "y": 0.5}]},
    ],
    ids=["extra-field", "non-finite", "wrong-grid"],
)
def test_malformed_native_source_ports_fail_before_route_or_mutation(
    monkeypatch, fle_boundary, response,
):
    factory, _, events, placements = _pipeline(
        monkeypatch, source_ports=[], target_ports=[(2.5, 0.5)],
        source_response=response, buildable={(0.5, 0.5), (1.5, 0.5), (2.5, 0.5)},
    )

    with pytest.raises(ValueError):
        _dispatch(factory)

    names = [event[0] for event in events]
    assert "readonly_cells" not in names
    assert "inventory_read" not in names
    assert "connector_begin" not in names
    assert placements == []


@pytest.mark.parametrize("role", ["source", "target"])
def test_near_half_native_port_aborts_before_alternative_route_or_mutation(
    monkeypatch, fle_boundary, role,
):
    route = {(0.5, 1.5), (1.5, 1.5), (2.5, 1.5)}
    if role == "source":
        factory, _, events, placements = _pipeline(
            monkeypatch,
            source_ports=[],
            target_ports=[(2.5, 0.5), (2.5, 1.5)],
            source_response={"points": [
                {"x": 0.4999999995, "y": 0.5}, {"x": 0.5, "y": 1.5},
            ]},
            buildable=route,
        )
    else:
        factory, _, events, placements = _pipeline(
            monkeypatch,
            source_ports=[(0.5, 0.5), (0.5, 1.5)],
            target_ports=[],
            target_response={"points": [
                {"x": 2.50000000025, "y": 0.5}, {"x": 2.5, "y": 1.5},
            ]},
            buildable=route,
        )

    with pytest.raises(ValueError):
        _dispatch(factory)

    names = [event[0] for event in events]
    assert names.count("port_read") >= 1
    assert "readonly_cells" not in names
    assert "inventory_read" not in names
    assert "connector_begin" not in names
    assert placements == []


def test_native_port_pair_count_exhaustion_fails_closed_before_route_queries(
    monkeypatch, fle_boundary,
):
    source_ports = [(0.5 + index, 0.5) for index in range(9)]
    target_ports = [(100.5 + index, 0.5) for index in range(8)]
    factory, _, events, placements = _pipeline(
        monkeypatch, source_ports=source_ports, target_ports=target_ports,
        buildable=set(),
    )

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route") as raised:
        _dispatch(factory)

    assert raised.value.search_exhausted is True
    names = [event[0] for event in events]
    assert names.count("port_read") == 2
    assert "readonly_cells" not in names
    assert "connector_begin" not in names
    assert placements == []


@pytest.mark.parametrize("role", ["source", "target"])
def test_native_port_observation_count_limit_fails_closed(monkeypatch, fle_boundary, role):
    ports = [(0.5 + index, 0.5) for index in range(17)]
    kwargs = {f"{role}_response": {"points": [{"x": x, "y": y} for x, y in ports]}}
    factory, _, events, placements = _pipeline(
        monkeypatch, source_ports=[(0.5, 0.5)], target_ports=[(2.5, 0.5)],
        buildable=set(), **kwargs,
    )

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route") as raised:
        _dispatch(factory)

    assert raised.value.search_exhausted is True
    names = [event[0] for event in events]
    assert "readonly_cells" not in names
    assert "connector_begin" not in names
    assert placements == []


def test_global_collision_cell_budget_exhaustion_never_starts_connector(
    monkeypatch, fle_boundary,
):
    source_ports = [(0.5, 0.5 + index) for index in range(4)]
    target_ports = [(80.5, 0.5 + index) for index in range(4)]
    factory, _, events, placements = _pipeline(
        monkeypatch, source_ports=source_ports, target_ports=target_ports,
        buildable=set(),
    )

    with pytest.raises(ConnectionPreflightRejected, match="no_connection_route") as raised:
        _dispatch(factory)

    assert raised.value.search_exhausted is True
    names = [event[0] for event in events]
    queries = [event for event in events if event[0] == "readonly_cells"]
    assert 0 < len(queries) < 4 * 4 * 5
    assert all(event[2] <= 16_384 for event in queries)
    assert sum(event[2] for event in queries) <= 65_536
    assert len(queries) <= 128
    assert "inventory_read" not in names
    assert "connector_begin" not in names
    assert placements == []


def test_insufficient_pipe_materials_rejects_before_connector_preparation(
    monkeypatch, fle_boundary,
):
    route = {(0.5, 1.5), (1.5, 1.5), (2.5, 1.5)}
    factory, _, events, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5), (0.5, 1.5)],
        target_ports=[(2.5, 0.5), (2.5, 1.5)],
        buildable=route,
        count=2,
    )

    with pytest.raises(ConnectionPreflightRejected, match="insufficient_connection_materials"):
        _dispatch(factory)

    names = [event[0] for event in events]
    assert names.count("inventory_read") == 1
    assert "connector_begin" not in names
    assert placements == []


def test_existing_pipe_cells_reduce_material_need_without_losing_route_indices(
    monkeypatch, fle_boundary,
):
    route = {(0.5, 0.5), (1.5, 0.5), (2.5, 0.5)}
    reused = {(1.5, 0.5)}
    factory, _, _, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5)],
        target_ports=[(2.5, 0.5)],
        buildable=route - reused,
        existing=reused,
        count=2,
    )

    assert _dispatch(factory).startswith("Constructed pipe connection")

    receipt = connection_key(_identity())
    assert [(event[2], event[1].x, event[1].y) for event in placements] == [
        ((receipt, 1), 0.5, 0.5),
        ((receipt, 3), 2.5, 0.5),
    ]


def test_connector_receipt_mismatch_never_places_or_retries_another_pair(
    monkeypatch, fle_boundary,
):
    route = {(0.5, 1.5), (1.5, 1.5), (2.5, 1.5)}
    factory, _, events, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5), (0.5, 1.5)],
        target_ports=[(2.5, 0.5), (2.5, 1.5)],
        buildable=route,
        wrong_begin_receipt=True,
    )

    with pytest.raises(RuntimeError, match="preparation receipt changed"):
        _dispatch(factory)

    names = [event[0] for event in events]
    assert names.count("connector_begin") == 1
    assert "connector_finish" not in names
    assert "place" not in names
    assert names[names.index("connector_begin") + 1:] == []
    assert placements == []


def test_lost_placement_acknowledgement_does_not_retry_an_alternate_port_pair(
    monkeypatch, fle_boundary,
):
    route = {(0.5, 1.5), (1.5, 1.5), (2.5, 1.5)}
    factory, _, events, placements = _pipeline(
        monkeypatch,
        source_ports=[(0.5, 0.5), (0.5, 1.5)],
        target_ports=[(2.5, 0.5), (2.5, 1.5)],
        buildable=route,
        fail_placement_at=2,
    )

    with pytest.raises(RuntimeError, match="acknowledgement was lost"):
        _dispatch(factory)

    names = [event[0] for event in events]
    begin_index = names.index("connector_begin")
    assert names.count("connector_begin") == 1
    assert names.count("place") == 2
    assert "connector_finish" not in names
    assert "readonly_cells" not in names[begin_index + 1:]
    assert len(placements) == 2


def test_connector_finish_receipt_mismatch_preserves_completed_route_identity(
    monkeypatch, fle_boundary,
):
    route = {(0.5, 0.5), (1.5, 0.5), (2.5, 0.5)}
    factory, _, events, placements = _pipeline(
        monkeypatch, source_ports=[(0.5, 0.5)], target_ports=[(2.5, 0.5)],
        buildable=route, wrong_finish_receipt=True,
    )

    with pytest.raises(RuntimeError, match="completion receipt changed"):
        _dispatch(factory)

    assert [(event[2][1], event[1].x, event[1].y) for event in placements] == [
        (1, 0.5, 0.5), (2, 1.5, 0.5), (3, 2.5, 0.5),
    ]
    names = [event[0] for event in events]
    assert names.count("connector_begin") == 1
    assert names.count("connector_finish") == 1
