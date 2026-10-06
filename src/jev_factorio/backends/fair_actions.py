"""Player-controlled actions without teleporting or synthetic harvests."""
from __future__ import annotations

import json
import math
import time
from importlib.resources import files
from types import SimpleNamespace
from typing import Any
from ..iteration_timing import native_io, decode_native, span, request_size
from .errors import ConnectionPreflightRejected, require_native_success


class NativePathNotFound(RuntimeError):
    """The native path request terminated before walking could begin."""


MAX_FLUID_PORT_PAIRS = 64
MAX_PIPE_PREFLIGHT_QUERIES = 128
MAX_PIPE_PREFLIGHT_CELLS = 65_536


def _pipe_search_exhausted() -> ConnectionPreflightRejected:
    error = ConnectionPreflightRejected("no_connection_route")
    error.search_exhausted = True
    return error


class _PipePreflightBudget:
    """One finite read-only budget shared across port pairs and final planning."""

    def __init__(self) -> None:
        self.queries = 0
        self.cells = 0

    def consume_query(self, cells: int = 0) -> None:
        if (type(cells) is not int or cells < 0
                or self.queries >= MAX_PIPE_PREFLIGHT_QUERIES
                or self.cells + cells > MAX_PIPE_PREFLIGHT_CELLS):
            raise _pipe_search_exhausted()
        self.queries += 1
        self.cells += cells


class FairActions:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        if getattr(backend, '_native_attachment', None) is not None:
            from .native_attachment import require_asset
            require_asset(backend._native_attachment, 'fair_actions')
        else:
            self.command(files("jev_factorio").joinpath("lua/fair_actions.lua").read_text())
            self.call("bind")

    def command(self, script: str) -> str:
        from .native_attachment import prepare_install_command
        script = prepare_install_command(script, getattr(self.backend, '_native_attachment', None))
        command = "/sc " + script
        result = native_io(
            "native_command",
            lambda: self.backend._instance.rcon_client.send_command(command),
            request_bytes=request_size(command), check_response=require_native_success,
        )
        return result or ""

    def call(self, function: str, *arguments: Any) -> dict:
        encoded = ", ".join(
            "helpers.json_to_table(" + json.dumps(json.dumps(value, allow_nan=False)) + ")"
            for value in arguments
        )
        return decode_native(self.command(
            f"rcon.print(helpers.table_to_json(storage.fair.{function}({encoded})))"
        ))

    @staticmethod
    def position(position: Any) -> dict:
        result = {"x": float(position.x), "y": float(position.y)}
        if not all(math.isfinite(value) for value in result.values()):
            raise ValueError("Position must be finite")
        return result

    @staticmethod
    def _approach_corridors(origin: dict, target: dict) -> list[list[dict]]:
        """Bound two ordinary walking corridors around a distant obstruction."""
        dx, dy = target["x"] - origin["x"], target["y"] - origin["y"]
        if abs(dx) + abs(dy) < 40 or max(abs(dx), abs(dy)) > 120:
            return []
        primary, secondary = ("y", "x") if abs(dy) >= abs(dx) else ("x", "y")
        distance = target[primary] - origin[primary]
        direction = 1 if distance > 0 else -1
        toward_origin = 1 if origin[secondary] >= target[secondary] else -1
        corridors = []
        for side in (toward_origin, -toward_origin):
            lane = origin[secondary] + side * 12
            coordinate = origin[primary] + direction * min(8, abs(distance))
            points = [{primary: coordinate, secondary: lane}]
            while abs(target[primary] - coordinate) > 45 and len(points) < 6:
                coordinate += direction * 20
                points.append({primary: coordinate, secondary: lane})
            near = target[primary] - direction * min(8, abs(distance))
            shoulder = target[secondary] + toward_origin * min(
                25, max(8, abs(target[secondary] - origin[secondary]) / 2)
            )
            points.append({primary: near, secondary: shoulder})
            corridors.append(points)
        return corridors

    def wait(self, timeout: float = 180) -> dict:
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                state = self.call("observe")
                if state["status"] == "completed":
                    return state
                if state["status"] == "failed":
                    if (state.get("error") == "Native pathfinder could not find a route"
                            or state.get("movement_started") is False and state.get("failure_code") in {
                                "destruction_required", "no_safe_path", "pathfinder_busy",
                                "path_deadline", "blocked_route_cooldown"}):
                        # This proves only this walk did not start. It is NOT
                        # permission to clear a compound gather/transfer that
                        # may already have had effects in earlier phases.
                        raise NativePathNotFound("No safe native path before movement")
                    raise RuntimeError(state.get("error", "Native controls failed"))
                with span("action_poll_wait"):
                    time.sleep(0.1)
            raise TimeoutError("Native action exceeded its bounded observation window")
        finally:
            self.command("storage.fair.stop()")

    def _note(self, key: str, amount: int = 1) -> None:
        if not hasattr(self, "metrics"):
            self.metrics = {}
        self.metrics[key] = self.metrics.get(key, 0) + amount

    def move_to(self, position: Any) -> Any:
        from fle.env import Position

        self._note("move_requests")
        self.call("begin_move", self.position(position))
        state = self.wait()
        result = Position(**state["position"])
        self.backend._instance.namespace.player_location = result
        return result

    def approach(self, position: Any, name: str = "character") -> None:
        """Try bounded collision-free interaction approaches, then verify reach."""
        from fle.env import Position

        center = self.position(position)
        self._note("approach_requests")
        result = decode_native(self.command(
            "local player = storage.fair.actor(); "
            "local target = helpers.json_to_table(" + json.dumps(json.dumps(center)) + "); "
            "local entity = player.surface.find_entity(" + json.dumps(name) + ", target); "
            "if not entity or not entity.valid then "
            "local box=prototypes.entity[" + json.dumps(name) + "].selection_box; "
            "target.x=target.x+math.max(math.abs(box.left_top.x),math.abs(box.right_bottom.x))+1.5; "
            "local point=player.surface.find_non_colliding_position('character',target,8,0.25); "
            "assert(point, 'No collision-free approach'); "
            "rcon.print(helpers.table_to_json(point)); return end; "
            "if player.can_reach_entity(entity) then "
            "rcon.print(helpers.table_to_json({reachable=true})); return end; "
            "local dx=player.position.x-target.x; local dy=player.position.y-target.y; "
            "local distance_squared=dx*dx+dy*dy; local reach=player.reach_distance; "
            "assert(reach>1, 'Insufficient native interaction approach margin'); "
            "if distance_squared==0 then dx=1; dy=0; distance_squared=1 end; "
            "local distance=math.sqrt(distance_squared); local positions={}; local seen={}; "
            "for inset=2,4,2 do local radius=math.max(0,reach-inset); "
            "for _,turn in ipairs({0,1,-1,2,-2,3,-3,4}) do local angle=turn*math.pi/4; "
            "local ux=(dx*math.cos(angle)-dy*math.sin(angle))/distance; "
            "local uy=(dx*math.sin(angle)+dy*math.cos(angle))/distance; "
            "local near={x=target.x+ux*radius,y=target.y+uy*radius}; "
            "local candidate=player.surface.find_non_colliding_position('character',near,1,0.25); "
            "if candidate and (candidate.x-target.x)^2+(candidate.y-target.y)^2<=(reach-1)^2 then "
            "local key=candidate.x..':'..candidate.y; if not seen[key] then "
            "seen[key]=true; table.insert(positions,candidate) end end end end; "
            "assert(#positions>0, 'No collision-free interaction approach with arrival margin'); "
            "rcon.print(helpers.table_to_json({positions=positions,unit_number=entity.unit_number}))"
        ))
        if result.get("reachable") is True:
            self._note("approaches_skipped_in_reach")
            return
        if "positions" not in result:
            # Preserve the existing generic/construction-point API when no
            # interaction entity existed. This does not establish entity reach.
            self.move_to(Position(**result))
            return
        expected_unit = "nil" if result.get("unit_number") is None else json.dumps(result["unit_number"])

        def reached_entity() -> bool:
            return decode_native(self.command(
                "local player=storage.fair.actor(); local target=helpers.json_to_table("
                + json.dumps(json.dumps(center)) + "); local entity=player.surface.find_entity("
                + json.dumps(name) + ",target); "
                "assert(entity and entity.valid, 'Interaction target is missing'); "
                "assert(entity.unit_number==" + expected_unit + ", 'Interaction target identity changed'); "
                "rcon.print(helpers.table_to_json({reachable=player.can_reach_entity(entity)}))"
            ))["reachable"] is True

        walked = False
        for candidate in result["positions"]:
            try:
                self.move_to(Position(**candidate))
            except NativePathNotFound as error:
                if type(error) is not NativePathNotFound:
                    raise
                continue
            walked = True
            if reached_entity():
                return
        origin = decode_native(self.command(
            "local p=storage.fair.actor(); "
            "rcon.print(helpers.table_to_json({x=p.position.x,y=p.position.y}))"
        ))
        # Each leg uses begin_move's native collision and no-destruction path.
        # Bound added travel separately from the direct interaction attempts.
        corridor_started = time.monotonic()
        corridor_attempts = 0
        for corridor in self._approach_corridors(origin, center):
            for waypoint in corridor:
                if corridor_attempts >= 8 or time.monotonic() - corridor_started >= 300:
                    break
                corridor_attempts += 1
                try:
                    self.move_to(Position(**waypoint))
                except NativePathNotFound as error:
                    if type(error) is not NativePathNotFound:
                        raise
                    break
                walked = True
            else:
                for candidate in result["positions"]:
                    if corridor_attempts >= 8 or time.monotonic() - corridor_started >= 300:
                        break
                    corridor_attempts += 1
                    try:
                        self.move_to(Position(**candidate))
                    except NativePathNotFound as error:
                        if type(error) is not NativePathNotFound:
                            raise
                        continue
                    walked = True
                    if reached_entity():
                        return
        if walked:
            raise RuntimeError("Native walking changed position without reaching the interaction target")
        raise NativePathNotFound("No native route to any reachable interaction approach")

    def harvest(self, resource: str, position: Any, quantity: int) -> int:
        from fle.env import Position

        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Mining quantity must be positive")
        self._note("mining_batches_started")
        gained = 0
        for attempt in range(quantity):
            if gained == 0:
                # Honor the resource coordinate from the observation that
                # authorized this action.  It is the target to which the
                # controller committed, rather than merely a same-name node
                # near the player.
                target = self.position(position)
            else:
                # After native mining depleted a node, reacquire around the
                # actor rather than searching around the stale original
                # coordinate. This only selects a target: approach() still
                # walks normally and begin_mine() enforces reach.
                target = self.call("next_mine_target", resource, 64).get("position")
                if not isinstance(target, dict):
                    raise RuntimeError("No mineable resource observed near the walking actor")
            approach = self.call("mine_approach", target, resource)
            identity = approach["identity"]
            if not approach["reachable"]:
                self.move_to(Position(**approach["position"]))
            self._note("mining_starts")
            self.call("begin_mine", target, resource, quantity - gained, identity)
            try:
                result = self.wait()
            except RuntimeError:
                result = self.call("observe")
                if result.get("error") != "Resource depleted before requested amount" or result["gained"] <= 0:
                    raise
            gained += result["gained"]
            self._note("mined_items", result["gained"])
            if gained >= quantity:
                return gained
        raise RuntimeError("Mining target budget exhausted")

    def approach_build(self, position: Any, name: str, direction: int) -> None:
        """Walk only when the native placement center is outside build reach."""
        from fle.env import Position

        center = self.position(position)
        self._note("approach_requests")
        result = decode_native(self.command(
            "local player=storage.fair.actor(); local target=helpers.json_to_table("
            + json.dumps(json.dumps(center)) + "); "
            "local dx=player.position.x-target.x; local dy=player.position.y-target.y; "
            "local distance_squared=dx*dx+dy*dy; local reach=player.build_distance; "
            "local placeable=player.surface.can_place_entity{name=" + json.dumps(name)
            + ",position=target,direction=" + json.dumps(direction)
            + ",force=player.force,build_check_type=defines.build_check_type.manual}; "
            "if distance_squared<=reach^2 and placeable then "
            "rcon.print(helpers.table_to_json({reachable=true})); return end; "
            "assert(reach>1, 'Insufficient native build approach margin'); "
            "if distance_squared==0 then dx=1; dy=0; distance_squared=1 end; "
            # Native walking may finish within 0.25 of its final waypoint,
            # and request_path permits a 0.2 endpoint radius. Leave one full
            # tile after collision search rather than merely checking reach.
            "local distance=math.sqrt(distance_squared); local positions={}; local seen={}; "
            "for inset=2,4,2 do local radius=math.max(0,reach-inset); "
            "for _,turn in ipairs({0,1,-1,2,-2,3,-3,4}) do local angle=turn*math.pi/4; "
            "local ux=(dx*math.cos(angle)-dy*math.sin(angle))/distance; "
            "local uy=(dx*math.sin(angle)+dy*math.cos(angle))/distance; "
            "local near={x=target.x+ux*radius,y=target.y+uy*radius}; "
            "local candidate=player.surface.find_non_colliding_position('character',near,1,0.25); "
            "if candidate and (candidate.x-target.x)^2+(candidate.y-target.y)^2 "
            "<=(reach-1)^2 then local key=candidate.x..':'..candidate.y; "
            "if not seen[key] then seen[key]=true; table.insert(positions,candidate) end end end end; "
            "assert(#positions>0, 'No collision-free build approach with arrival margin'); "
            "rcon.print(helpers.table_to_json({positions=positions}))"
        ))
        if result.get("reachable") is True:
            self._note("approaches_skipped_in_reach")
            return
        for candidate in result["positions"]:
            try:
                self.move_to(Position(**candidate))
                return
            except NativePathNotFound as error:
                if type(error) is not NativePathNotFound:
                    raise
        raise NativePathNotFound("No native route to any bounded build approach")

    def place_entity(self, prototype: Any, position: Any, direction: Any,
                     exact: bool = False, connector: tuple[str, int] | None = None,
                     bootstrap_owned: bool = False) -> Any:
        from fle.env import Position

        name = prototype.value[0]
        target = self.position(position)
        direction_value = direction.value
        if not exact:
            site = self.call("find_build_site", name, target, 8)
            target, direction_value = site["position"], site["direction"]
        self.approach_build(Position(**target), name, direction_value)
        result = (self.call("connector_place", connector[0], connector[1],
                            name, target, direction_value) if connector else
                  self.call("bootstrap_place" if bootstrap_owned else "place", name, target, direction_value))
        return SimpleNamespace(
            name=result["name"], position=Position(**result["position"]),
            unit_number=result.get("unit_number"),
            drop_position=Position(**result["drop_position"]) if result.get("drop_position") else None,
        )

    def insert_item(self, prototype: Any, entity: Any, quantity: int) -> int:
        self.approach(entity.position, entity.name)
        arguments = [entity.name, self.position(entity.position), prototype.value[0], quantity]
        expected_unit = getattr(entity, "unit_number", None)
        if expected_unit is not None:
            if type(expected_unit) is not int or expected_unit <= 0:
                raise ValueError("Invalid native transfer target identity")
            arguments.append(expected_unit)
        return self.call("insert", *arguments)["quantity"]

    @staticmethod
    def _connection_corridor(start: dict, end: dict, *, horizontal_first: bool) -> list[tuple[int, int, int, int]]:
        """Bound native placement discovery to one collision-aware L corridor.

        Scanning the complete rectangle between two distant endpoints makes a
        narrow, ordinary pole route depend on its bounding-box area.  The
        corridor keeps the search bounded while retaining eight cells of local
        detour room around each leg.  A caller can try the opposite L without
        widening either search into a remote-placement shortcut.
        """
        source_x, source_y = math.floor(start["x"]), math.floor(start["y"])
        target_x, target_y = math.floor(end["x"]), math.floor(end["y"])
        left, right = min(source_x, target_x) - 8, max(source_x, target_x) + 8
        top, bottom = min(source_y, target_y) - 8, max(source_y, target_y) + 8
        if horizontal_first:
            return [
                (left, right, source_y - 8, source_y + 8),
                (target_x - 8, target_x + 8, top, bottom),
            ]
        return [
            (source_x - 8, source_x + 8, top, bottom),
            (left, right, target_y - 8, target_y + 8),
        ]

    @staticmethod
    def _fallback_rectangles(start: dict, end: dict, *, searched: int,
                             margin: int) -> list[tuple[int, int, int, int]]:
        """Discover a complete detour area without exceeding the query budget.

        Narrow L corridors can each be cut even when an ordinary route exists
        around them. Keep native queries at 16,384 cells and all discovery,
        including those corridors, at 65,536 cells.
        """
        left = math.floor(min(start["x"], end["x"])) - margin
        right = math.floor(max(start["x"], end["x"])) + margin
        top = math.floor(min(start["y"], end["y"])) - margin
        bottom = math.floor(max(start["y"], end["y"])) + margin
        width = right - left + 1
        if width > 16_384 or searched + width * (bottom - top + 1) > 65_536:
            return []
        rows = 16_384 // width
        return [(left, right, y, min(bottom, y + rows - 1))
                for y in range(top, bottom + 1, rows)]

    @staticmethod
    def _pipe_fallback_rectangles(start: dict, end: dict, *, searched: int) -> list[tuple[int, int, int, int]]:
        return FairActions._fallback_rectangles(start, end, searched=searched, margin=8)

    @staticmethod
    def _pole_fallback_rectangles(start: dict, end: dict, *, searched: int) -> list[tuple[int, int, int, int]]:
        return FairActions._fallback_rectangles(start, end, searched=searched, margin=16)

    def _pipe_route(self, start: dict, end: dict, fluid: str, *,
                    budget: _PipePreflightBudget | None = None) -> tuple[list, set]:
        from ..planning.connections import shortest_pipe_path

        route = None
        existing: set = set()
        searched = 0
        for horizontal_first in (True, False):
            rectangles = self._connection_corridor(
                start, end, horizontal_first=horizontal_first,
            )
            searched += sum((right - left + 1) * (bottom - top + 1)
                            for left, right, top, bottom in rectangles)
            try:
                if budget is None:
                    buildable, existing = self._connection_cells("pipe", fluid, rectangles)
                else:
                    buildable, existing = self._connection_cells(
                        "pipe", fluid, rectangles, budget=budget,
                    )
            except ValueError as error:
                if budget is None or str(error) != "Connection search exceeds bounded area":
                    raise
                continue
            try:
                route = shortest_pipe_path(
                    (start["x"], start["y"]), (end["x"], end["y"]),
                    buildable, existing,
                )
                break
            except ValueError:
                continue

        if route is None:
            buildable, existing = set(), set()
            complete = True
            for rectangle in self._pipe_fallback_rectangles(
                start, end, searched=searched,
            ):
                try:
                    if budget is None:
                        chunk_buildable, chunk_existing = self._connection_cells(
                            "pipe", fluid, [rectangle],
                        )
                    else:
                        chunk_buildable, chunk_existing = self._connection_cells(
                            "pipe", fluid, [rectangle], budget=budget,
                        )
                except ValueError as error:
                    if budget is None or str(error) != "Connection search exceeds bounded area":
                        raise
                    complete = False
                    break
                buildable.update(chunk_buildable)
                existing.update(chunk_existing)
            if complete:
                try:
                    route = shortest_pipe_path(
                        (start["x"], start["y"]), (end["x"], end["y"]),
                        buildable, existing,
                    )
                except ValueError:
                    route = None
        if route is None:
            raise ConnectionPreflightRejected("no_connection_route")
        return route, existing

    @staticmethod
    def _fluid_port_position(point: Any) -> dict:
        values = (getattr(point, "x", None), getattr(point, "y", None))
        coordinates = []
        for value in values:
            if type(value) not in (int, float):
                raise ValueError("Invalid native fluid pipe port")
            try:
                coordinate = float(value)
            except (OverflowError, ValueError) as error:
                raise ValueError("Invalid native fluid pipe port") from error
            if (not math.isfinite(coordinate) or abs(coordinate) > 1_000_000
                    or not (coordinate * 2).is_integer()
                    or coordinate - math.floor(coordinate) != 0.5):
                raise ValueError("Invalid native fluid pipe port")
            coordinates.append(coordinate)
        return {"x": coordinates[0], "y": coordinates[1]}

    def select_feasible_pipe_pair(self, source_points: list[Any],
                                  target_points: list[Any], fluid: str
                                  ) -> tuple[Any, Any, _PipePreflightBudget]:
        """Choose the nearest deterministic native port pair with a proven route.

        Candidate evaluation is read-only. The returned budget is passed into
        ``connect`` so final route revalidation and the inventory check share
        the same global query and cell limits.
        """
        if not isinstance(source_points, list) or not isinstance(target_points, list):
            raise ValueError("Native fluid ports must be lists")
        if not source_points or not target_points:
            raise ConnectionPreflightRejected("missing_fluid_port")
        budget = _PipePreflightBudget()
        if len(source_points) * len(target_points) > MAX_FLUID_PORT_PAIRS:
            raise _pipe_search_exhausted()

        candidates = []
        for source in source_points:
            start = self._fluid_port_position(source)
            for target in target_points:
                end = self._fluid_port_position(target)
                candidates.append((
                    math.dist((start["x"], start["y"]), (end["x"], end["y"])),
                    start["x"], start["y"], end["x"], end["y"], source, target,
                ))
        candidates.sort(key=lambda row: row[:5])

        for _, _, _, _, _, source, target in candidates:
            start = self._fluid_port_position(source)
            end = self._fluid_port_position(target)
            try:
                self._pipe_route(start, end, fluid, budget=budget)
            except ConnectionPreflightRejected as error:
                if getattr(error, "search_exhausted", False):
                    raise
                if error.code == "no_connection_route":
                    continue
                raise
            except ValueError as error:
                if str(error) != "Connection search exceeds bounded area":
                    raise
                continue
            return source, target, budget
        raise ConnectionPreflightRejected("no_connection_route")

    def _connection_cells(self, name: str, fluid: str,
                          rectangles: list[tuple[int, int, int, int]], *,
                          budget: _PipePreflightBudget | None = None) -> tuple[set, set]:
        searched = sum((right - left + 1) * (bottom - top + 1)
                       for left, right, top, bottom in rectangles)
        if searched > 16_384:
            raise ValueError("Connection search exceeds bounded area")
        if budget is not None:
            budget.consume_query(searched)
        loops = "".join(
            f"for horizontal={left},{right} do for vertical={top},{bottom} do "
            "include(horizontal, vertical) end end; "
            for left, right, top, bottom in rectangles
        )
        fluid_scan = ""
        if name == "pipe":
            areas = "".join(
                f"scan({{{{{left-1},{top-1}}},{{{right+2},{bottom+2}}}}}); "
                for left, right, top, bottom in rectangles
            )
            fluid_scan = (
                "local inspected={}; local function scan(area) "
                "for _,entity in pairs(player.surface.find_entities_filtered{area=area}) do "
                "if not inspected[entity] then inspected[entity]=true; "
                "for index=1,#entity.fluidbox do "
                "local filter=entity.fluidbox.get_filter(index); "
                "local filter_name=type(filter)=='string' and filter or (filter and filter.name); "
                "local contents=entity.fluidbox[index]; "
                "if (filter_name and filter_name~='' and filter_name~=" + json.dumps(fluid)
                + ") or (contents and contents.name~=" + json.dumps(fluid) + ") then "
                "for _,port in pairs(entity.fluidbox.get_pipe_connections(index)) do "
                "if port.connection_type=='normal' and port.target_position then "
                "local p=port.target_position; blocked[p.x..':'..p.y]=true; "
                "if entity.name~='pipe' then "
                "for _,offset in ipairs({{1,0},{-1,0},{0,1},{0,-1}}) do "
                "blocked[(p.x+offset[1])..':'..(p.y+offset[2])]=true end end end end; "
                "if entity.name=='pipe' then blocked[entity.position.x..':'..entity.position.y]=true end "
                "end end end end end; " + areas
            )
        cells = decode_native(self.command(
            "local player = storage.fair.actor(); local result = {buildable={}, existing={}}; "
            "local blocked={}; " + fluid_scan
            + "local function blocked_cell(position) return blocked[position.x..':'..position.y] end; "
            "local seen = {}; local function include(horizontal, vertical) "
            "local key = horizontal .. ':' .. vertical; if seen[key] then return end; seen[key] = true; "
            "local position = {x=horizontal+0.5,y=vertical+0.5}; "
            "if blocked_cell(position) then return end; "
            # A full route must exclude unpaid coal footprints before its first
            # placement. The native fair.place guard still rechecks each build.
            "if storage.coal_supply and storage.coal_supply.placement_reserved("
            + json.dumps(name) + ",position,defines.direction.north) then return end; "
            "local entity = player.surface.find_entity(" + json.dumps(name) + ", position); "
            "if entity and entity.force == player.force then "
            "local contents = #entity.fluidbox > 0 and entity.fluidbox[1]; "
            "if not contents or contents.name == " + json.dumps(fluid) + " then "
            "table.insert(result.existing, position) end "
            "elseif player.surface.can_place_entity{name=" + json.dumps(name)
            + ", position=position, force=player.force, build_check_type=defines.build_check_type.manual} "
            "then table.insert(result.buildable, position) end; end; "
            + loops + "rcon.print(helpers.table_to_json(result))"
        ))
        if not isinstance(cells, dict) or set(cells) != {"buildable", "existing"}:
            raise ValueError("Malformed native connection cell response")

        def parse_cells(field: str) -> set[tuple[float, float]]:
            values = cells[field]
            # Factorio's table_to_json encodes an empty Lua table as an empty
            # object; non-empty sequential tables are JSON arrays.
            if isinstance(values, dict) and not values:
                return set()
            if not isinstance(values, list) or len(values) > searched:
                raise ValueError("Malformed native connection cell list")

            parsed: set[tuple[float, float]] = set()
            for value in values:
                if not isinstance(value, dict) or set(value) != {"x", "y"}:
                    raise ValueError("Malformed native connection cell coordinate")
                coordinates = []
                for axis in ("x", "y"):
                    raw = value[axis]
                    if type(raw) not in (int, float):
                        raise ValueError("Malformed native connection cell coordinate")
                    try:
                        coordinate = float(raw)
                    except (OverflowError, ValueError) as error:
                        raise ValueError("Malformed native connection cell coordinate") from error
                    if (not math.isfinite(coordinate) or abs(coordinate) > 1_000_000
                            or not (coordinate * 2).is_integer()
                            or coordinate - math.floor(coordinate) != 0.5):
                        raise ValueError("Malformed native connection cell coordinate")
                    coordinates.append(coordinate)

                point = (coordinates[0], coordinates[1])
                tile_x, tile_y = math.floor(point[0]), math.floor(point[1])
                if not any(
                    left <= tile_x <= right and top <= tile_y <= bottom
                    for left, right, top, bottom in rectangles
                ):
                    raise ValueError("Native connection cell lies outside its query bounds")
                if point in parsed:
                    raise ValueError("Duplicate native connection cell")
                parsed.add(point)
            return parsed

        buildable = parse_cells("buildable")
        existing = parse_cells("existing")
        if buildable & existing or len(buildable) + len(existing) > searched:
            raise ValueError("Inconsistent native connection cell classification")
        return buildable, existing

    @staticmethod
    def validate_pole_geometry(value: Any, *, base_version: str | None = None,
                               source_role: str | None = None,
                               target_role: str | None = None) -> dict:
        """Validate one actor/session-bound native pole endpoint observation."""
        top_fields = {
            "schema", "base_version", "session_id", "tick", "actor_unit",
            "surface_index", "force_index", "supply_area_distance",
            "maximum_wire_distance", "source", "target",
        }
        endpoint_fields = {
            "role", "name", "unit_number", "position", "direction",
            "orientation", "surface_index", "force_index", "quality",
            "bounding_box",
        }

        def exact_fields(raw: Any, expected: set[str], label: str) -> dict:
            if not isinstance(raw, dict) or set(raw) != expected:
                raise ValueError(f"Malformed native pole {label}")
            return raw

        def integer(raw: Any, low: int, high: int, label: str) -> int:
            if type(raw) is not int or not low <= raw <= high:
                raise ValueError(f"Invalid native pole {label}")
            return raw

        def finite(raw: Any, low: float, high: float, label: str) -> float:
            if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                    or not math.isfinite(raw) or not low <= raw <= high):
                raise ValueError(f"Invalid native pole {label}")
            return float(raw)

        def point(raw: Any, label: str) -> dict:
            raw = exact_fields(raw, {"x", "y"}, label)
            return {
                "x": finite(raw["x"], -1_000_000, 1_000_000, f"{label} x"),
                "y": finite(raw["y"], -1_000_000, 1_000_000, f"{label} y"),
            }

        def cardinal_orientation(raw: Any, label: str) -> float:
            orientation = finite(raw, 0, math.nextafter(1.0, 0.0), label)
            quarter_turn = round(orientation * 4)
            if not math.isclose(orientation, quarter_turn / 4, abs_tol=1e-9):
                raise ValueError(f"Unsupported non-cardinal native pole {label}")
            return orientation

        value = exact_fields(value, top_fields, "geometry response")
        if value["schema"] != "jev.native-pole-geometry.v1":
            raise ValueError("Unsupported native pole geometry schema")
        version = value["base_version"]
        if (not isinstance(version, str) or not version
                or (base_version is not None and version != base_version)):
            raise ValueError("Native pole geometry game version changed")
        session = value["session_id"]
        if not isinstance(session, str) or not session or len(session) > 128:
            raise ValueError("Invalid native pole geometry session")
        tick = integer(value["tick"], 0, 9_007_199_254_740_991, "tick")
        actor_unit = integer(value["actor_unit"], 1, 9_007_199_254_740_991,
                             "actor unit")
        surface_index = integer(value["surface_index"], 1, 2_147_483_647,
                                "surface index")
        force_index = integer(value["force_index"], 1, 2_147_483_647,
                              "force index")
        supply = finite(value["supply_area_distance"], 0.01, 64, "supply area")
        wire = finite(value["maximum_wire_distance"], 0.01, 64, "wire reach")

        endpoints = {}
        for key, expected_role in (("source", source_role), ("target", target_role)):
            raw = exact_fields(value[key], endpoint_fields, f"{key} endpoint")
            role = raw["role"]
            if (not isinstance(role, str) or not role or len(role) > 128
                    or (expected_role is not None and role != expected_role)):
                raise ValueError(f"Native pole {key} role changed")
            name = raw["name"]
            quality = raw["quality"]
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ValueError(f"Invalid native pole {key} name")
            if not isinstance(quality, str) or not quality or len(quality) > 64:
                raise ValueError(f"Invalid native pole {key} quality")
            unit = integer(raw["unit_number"], 1, 9_007_199_254_740_991,
                           f"{key} unit")
            direction = integer(raw["direction"], 0, 15, f"{key} direction")
            orientation = cardinal_orientation(raw["orientation"], f"{key} orientation")
            endpoint_surface = integer(raw["surface_index"], 1, 2_147_483_647,
                                       f"{key} surface index")
            endpoint_force = integer(raw["force_index"], 1, 2_147_483_647,
                                     f"{key} force index")
            if endpoint_surface != surface_index or endpoint_force != force_index:
                raise ValueError(f"Native pole {key} is outside the actor surface or force")
            position = point(raw["position"], f"{key} position")
            box = exact_fields(
                raw["bounding_box"],
                {"left_top", "right_bottom", "orientation"},
                f"{key} footprint",
            )
            left_top = point(box["left_top"], f"{key} footprint left top")
            right_bottom = point(box["right_bottom"], f"{key} footprint right bottom")
            box_orientation = cardinal_orientation(box["orientation"],
                                                  f"{key} footprint orientation")
            width = right_bottom["x"] - left_top["x"]
            height = right_bottom["y"] - left_top["y"]
            if (width <= 0 or height <= 0 or width > 512 or height > 512
                    or not left_top["x"] <= position["x"] <= right_bottom["x"]
                    or not left_top["y"] <= position["y"] <= right_bottom["y"]):
                raise ValueError(f"Invalid native pole {key} footprint bounds")
            FairActions._normalized_pole_bounds(
                position,
                {
                    "left_top": left_top,
                    "right_bottom": right_bottom,
                    "orientation": box_orientation,
                },
            )
            endpoints[key] = {
                "role": role,
                "name": name,
                "unit_number": unit,
                "position": position,
                "direction": direction,
                "orientation": orientation,
                "surface_index": endpoint_surface,
                "force_index": endpoint_force,
                "quality": quality,
                "bounding_box": {
                    "left_top": left_top,
                    "right_bottom": right_bottom,
                    "orientation": box_orientation,
                },
            }
        if (endpoints["source"]["role"] == endpoints["target"]["role"]
                or endpoints["source"]["unit_number"] == endpoints["target"]["unit_number"]):
            raise ValueError("Native pole endpoints are not distinct")
        return {
            "schema": value["schema"],
            "base_version": version,
            "session_id": session,
            "tick": tick,
            "actor_unit": actor_unit,
            "surface_index": surface_index,
            "force_index": force_index,
            "supply_area_distance": supply,
            "maximum_wire_distance": wire,
            **endpoints,
        }

    @staticmethod
    def _normalized_pole_bounds(position: dict, bounding_box: dict) -> dict:
        """Resolve a native oriented box to bounded world-aligned extents.

        BoundingBox corner coordinates are positioned around the entity.
        Apply only the box's own non-zero orientation about the entity position;
        a zero box orientation is already resolved and must not inherit
        LuaEntity.orientation.
        """
        left_top, right_bottom = bounding_box["left_top"], bounding_box["right_bottom"]
        orientation = bounding_box["orientation"]
        if orientation == 0:
            return {"left_top": dict(left_top), "right_bottom": dict(right_bottom)}

        turns = int(round(orientation * 4)) % 4
        rotated = []
        for x in (left_top["x"], right_bottom["x"]):
            for y in (left_top["y"], right_bottom["y"]):
                dx, dy = x - position["x"], y - position["y"]
                if turns == 1:
                    rx, ry = -dy, dx
                elif turns == 2:
                    rx, ry = -dx, -dy
                elif turns == 3:
                    rx, ry = dy, -dx
                else:
                    rx, ry = dx, dy
                world_x = position["x"] + rx
                world_y = position["y"] + ry
                if (not math.isfinite(world_x) or not math.isfinite(world_y)
                        or not -1_000_000 <= world_x <= 1_000_000
                        or not -1_000_000 <= world_y <= 1_000_000):
                    raise ValueError("Invalid normalized native pole footprint bounds")
                rotated.append((world_x, world_y))

        bounds = {
            "left_top": {
                "x": min(point[0] for point in rotated),
                "y": min(point[1] for point in rotated),
            },
            "right_bottom": {
                "x": max(point[0] for point in rotated),
                "y": max(point[1] for point in rotated),
            },
        }
        width = bounds["right_bottom"]["x"] - bounds["left_top"]["x"]
        height = bounds["right_bottom"]["y"] - bounds["left_top"]["y"]
        if (not math.isfinite(width) or not math.isfinite(height)
                or width <= 0 or height <= 0 or width > 512 or height > 512):
            raise ValueError("Invalid normalized native pole footprint extent")
        return bounds

    @staticmethod
    def _pole_supply_overlaps(bounds: dict, position: tuple[float, float],
                              supply_distance: float) -> bool:
        """Match the versioned native coal survey's square supply-area overlap."""
        left = bounds["left_top"]
        right = bounds["right_bottom"]
        return (
            left["x"] < position[0] + supply_distance
            and position[0] - supply_distance < right["x"]
            and left["y"] < position[1] + supply_distance
            and position[1] - supply_distance < right["y"]
        )

    def _pole_search_rectangles(self, start: dict, end: dict,
                                geometry: dict) -> list[tuple[int, int, int, int]]:
        """Bound a complete pole search around both native power regions."""
        radius = geometry["supply_area_distance"]
        furthest = 0.0
        for endpoint in (geometry["source"], geometry["target"]):
            position = endpoint["position"]
            bounds = self._normalized_pole_bounds(position, endpoint["bounding_box"])
            for axis, low_key, high_key in (
                ("x", "left_top", "right_bottom"),
                ("y", "left_top", "right_bottom"),
            ):
                furthest = max(
                    furthest,
                    abs(bounds[low_key][axis] - position[axis]),
                    abs(bounds[high_key][axis] - position[axis]),
                )
        margin = max(16, math.ceil(furthest + radius + 8))
        if margin > 512:
            return []
        rectangles = self._fallback_rectangles(
            start, end, searched=0, margin=margin,
        )
        if any(left < -1_000_000 or right > 999_999
               or top < -1_000_000 or bottom > 999_999
               for left, right, top, bottom in rectangles):
            return []
        return rectangles

    def _native_pole_route(self, start: dict, end: dict, fluid: str,
                           geometry: dict) -> tuple[list, set]:
        from ..planning.connections import (
            select_pole_positions,
            shortest_wire_path_between_regions,
        )

        rectangles = self._pole_search_rectangles(start, end, geometry)
        if not rectangles:
            raise ConnectionPreflightRejected("no_connection_route")
        buildable, existing = set(), set()
        for rectangle in rectangles:
            chunk_buildable, chunk_existing = self._connection_cells(
                "small-electric-pole", fluid, [rectangle],
            )
            buildable.update(chunk_buildable)
            existing.update(chunk_existing)
        candidates = buildable | existing
        source_bounds = self._normalized_pole_bounds(
            geometry["source"]["position"], geometry["source"]["bounding_box"],
        )
        target_bounds = self._normalized_pole_bounds(
            geometry["target"]["position"], geometry["target"]["bounding_box"],
        )
        origins = {
            point for point in candidates
            if self._pole_supply_overlaps(
                source_bounds, point,
                geometry["supply_area_distance"],
            )
        }
        destinations = {
            point for point in candidates
            if self._pole_supply_overlaps(
                target_bounds, point,
                geometry["supply_area_distance"],
            )
        }
        if not origins or not destinations:
            raise ConnectionPreflightRejected("no_connection_route")
        try:
            route = shortest_wire_path_between_regions(
                origins, destinations, buildable, existing,
                max_wire_distance=geometry["maximum_wire_distance"],
            )
            route = select_pole_positions(
                route, max_wire_distance=geometry["maximum_wire_distance"],
            )
        except ValueError as error:
            raise ConnectionPreflightRejected("no_connection_route") from error
        return route, existing

    @staticmethod
    def _pole_binding_script(identity: dict, geometry: dict, receipt: str,
                              route: list, existing: set) -> str:
        snapshot = json.dumps(geometry, separators=(",", ":"), allow_nan=False)
        expected = "helpers.json_to_table(" + json.dumps(snapshot) + ")"
        path = json.dumps([
            {"x": horizontal, "y": vertical,
             "existing": (horizontal, vertical) in existing}
            for horizontal, vertical in route
        ], allow_nan=False)
        return (
            "local expected=" + expected + "; local actor=assert(storage.fair.actor()); "
            "assert(actor.character and actor.character.valid "
            "and actor.character.unit_number==expected.actor_unit "
            "and actor.surface.index==expected.surface_index "
            "and actor.force.index==expected.force_index "
            "and storage.jev_session_id==expected.session_id "
            "and game.tick>=expected.tick "
            "and script.active_mods.base==expected.base_version,"
            "'Native pole actor or session changed'); "
            "local function same_point(a,b) return a and b and a.x==b.x and a.y==b.y end; "
            "local function check_endpoint(e,want) "
            "assert(e and e.valid and e.unit_number==want.unit_number and e.name==want.name "
            "and e.direction==want.direction and e.orientation==want.orientation "
            "and e.quality.name==want.quality and e.surface.index==want.surface_index "
            "and e.force.index==want.force_index and same_point(e.position,want.position),"
            "'Native pole endpoint identity changed'); "
            "local b=e.bounding_box; assert(b and same_point(b.left_top,want.bounding_box.left_top) "
            "and same_point(b.right_bottom,want.bounding_box.right_bottom) "
            "and (b.orientation or 0)==want.bounding_box.orientation,"
            "'Native pole endpoint footprint changed') end; "
            "check_endpoint(storage.campaign.entities[expected.source.role],expected.source); "
            "check_endpoint(storage.campaign.entities[expected.target.role],expected.target); "
            "local pole=assert(prototypes.entity['small-electric-pole']); "
            "assert(pole.get_supply_area_distance('normal')==expected.supply_area_distance "
            "and pole.get_max_wire_distance('normal')==expected.maximum_wire_distance,"
            "'Native pole prototype limits changed'); "
            "rcon.print(helpers.table_to_json(storage.campaign.connector_begin("
            + ",".join(json.dumps(value) for value in (
                receipt, identity["source"], identity["target"], identity["kind"],
                identity["fluid"],
            )) + ",helpers.json_to_table(" + json.dumps(path) + "))))"
        )

    def connect(self, source: Any, target: Any, prototype: Any, fluid: str = "",
                *, identity: dict | None = None,
                preflight_budget: _PipePreflightBudget | None = None,
                pole_geometry: dict | None = None) -> None:
        from fle.env import Direction, Position
        from ..planning.connections import (
            select_pole_positions,
            shortest_wire_path,
            shortest_wire_path_between_regions,
        )

        name = prototype.value[0]
        if name not in {"pipe", "small-electric-pole"}:
            raise ValueError("Unsupported fair connection type")
        start = self.position(getattr(source, "position", source))
        end = self.position(getattr(target, "position", target))
        if name == "pipe":
            if pole_geometry is not None:
                raise ValueError("Pole geometry cannot authorize a pipe connection")
            route, existing = self._pipe_route(
                start, end, fluid, budget=preflight_budget,
            )
        elif pole_geometry is not None:
            if identity is None or fluid != "electricity":
                raise ValueError("Native pole geometry requires a paid electricity identity")
            geometry = self.validate_pole_geometry(
                pole_geometry,
                source_role=identity.get("source"),
                target_role=identity.get("target"),
            )
            if (identity.get("kind") != name or identity.get("fluid") != fluid
                    or start != geometry["source"]["position"]
                    or end != geometry["target"]["position"]):
                raise ValueError("Native pole geometry is not bound to these endpoints")
            route, existing = self._native_pole_route(start, end, fluid, geometry)
        elif identity is not None:
            raise ValueError("Paid native pole connections require authenticated geometry")
        else:
            # Historical direct FairActions callers do not have campaign roles
            # or native endpoint snapshots. Preserve their point-only behavior;
            # NativeFactory's paid path always uses the geometry-bound branch.
            route = None
            route_error = None
            searched = 0
            for horizontal_first in (True, False):
                rectangles = self._connection_corridor(
                    start, end, horizontal_first=horizontal_first,
                )
                searched += sum((right - left + 1) * (bottom - top + 1)
                                for left, right, top, bottom in rectangles)
                buildable, existing = self._connection_cells(name, fluid, rectangles)
                origin = (start["x"], start["y"])
                destination = (end["x"], end["y"])
                candidates = buildable | existing
                if not candidates:
                    route_error = ValueError("No ordinary pole placement cells")
                    continue
                origin = min(candidates, key=lambda point: math.dist(point, origin))
                destination = min(candidates, key=lambda point: math.dist(point, destination))
                if math.dist(origin, (start["x"], start["y"])) > 3.5 or math.dist(
                    destination, (end["x"], end["y"])
                ) > 3.5:
                    route_error = ValueError("No nearby ordinary pole placement")
                    continue
                try:
                    route = shortest_wire_path(
                        origin, destination, buildable, existing, max_wire_distance=6
                    )
                    break
                except ValueError as error:
                    route_error = error
            if route is None:
                rectangles = self._pole_fallback_rectangles(start, end, searched=searched)
                if rectangles:
                    buildable, existing = set(), set()
                    for rectangle in rectangles:
                        chunk_buildable, chunk_existing = self._connection_cells(name, fluid, [rectangle])
                        buildable.update(chunk_buildable)
                        existing.update(chunk_existing)
                    candidates = buildable | existing
                    origin = (start["x"], start["y"])
                    destination = (end["x"], end["y"])
                    origins = {point for point in candidates if math.dist(point, origin) <= 3.5}
                    destinations = {point for point in candidates if math.dist(point, destination) <= 3.5}
                    try:
                        route = shortest_wire_path_between_regions(
                            origins, destinations, buildable, existing, max_wire_distance=6,
                        )
                    except ValueError as error:
                        route_error = error
            if route is None:
                raise ConnectionPreflightRejected("no_connection_route")
            route = select_pole_positions(route, max_wire_distance=6)
        required = sum(point not in existing for point in route)
        if preflight_budget is not None:
            preflight_budget.consume_query()
        available = decode_native(self.command(
            "rcon.print(helpers.table_to_json({count=storage.fair.actor().get_item_count("
            + json.dumps(name) + ")}))"
        ))["count"]
        if available < required:
            raise ConnectionPreflightRejected("insufficient_connection_materials")
        receipt = None
        if identity is not None:
            from ..planning.connection_identity import connection_key

            if (set(identity) != {"source", "target", "kind", "fluid"}
                    or identity["kind"] != name or identity["fluid"] != fluid):
                raise ValueError("Connector identity changed before payment")
            receipt = connection_key(identity)
            if pole_geometry is not None:
                prepared = decode_native(self.command(self._pole_binding_script(
                    identity, geometry, receipt, route, existing,
                )))
            else:
                prepared = decode_native(self.command(
                    "rcon.print(helpers.table_to_json(storage.campaign.connector_begin("
                    + ",".join(json.dumps(value) for value in (
                        receipt, identity["source"], identity["target"], name, fluid))
                    + ",helpers.json_to_table(" + json.dumps(json.dumps([
                        {"x": horizontal, "y": vertical,
                         "existing": (horizontal, vertical) in existing}
                        for horizontal, vertical in route
                    ])) + "))))"
                ))
            if prepared.get("id") != receipt:
                raise RuntimeError("Native connector preparation receipt changed")
        for index, (horizontal, vertical) in enumerate(route, 1):
            if (horizontal, vertical) not in existing:
                connector_kwargs = {"connector": (receipt, index)} if receipt else {}
                self.place_entity(prototype, Position(x=horizontal, y=vertical),
                                  direction=Direction.UP, exact=True,
                                  **connector_kwargs)
        if receipt is not None:
            completed = decode_native(self.command(
                "rcon.print(helpers.table_to_json(storage.campaign.connector_finish("
                + json.dumps(receipt) + ")))"))
            if completed.get("id") != receipt:
                raise RuntimeError("Native connector completion receipt changed")
