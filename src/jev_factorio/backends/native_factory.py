"""Native production adapter: ordinary inventories, crafting, research and pipes."""
from __future__ import annotations

import json
import math
from importlib.resources import files
from typing import Any
from ..iteration_timing import native_io, decode_native, span, request_size

from ..factory_contract import validate_command
from ..planning.catalog import Catalog
from ..state import GameSnapshot
from ..telemetry import Trace, phase
from .errors import ConnectionPreflightRejected, require_native_success

MAX_NATIVE_FLUID_PORTS = 16


def _fluid_port_search_exhausted() -> ConnectionPreflightRejected:
    error = ConnectionPreflightRejected("no_connection_route")
    error.search_exhausted = True
    return error


class NativeFactory:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        raw = self.command(files("jev_factorio").joinpath("lua/catalog.lua").read_text())
        self.catalog = Catalog.from_dict(decode_native(raw))
        if getattr(backend, '_native_attachment', None) is not None:
            from .native_attachment import require_asset
            require_asset(backend._native_attachment, 'factory')
            if backend._native_attachment['modules']['connector_ownership']:
                require_asset(backend._native_attachment, 'connector_ownership')
            require_asset(backend._native_attachment, 'launch_readiness')
        else:
            self.command(files("jev_factorio").joinpath("lua/factory.lua").read_text())
            self.command(files("jev_factorio").joinpath("lua/connector_ownership.lua").read_text())
            self.command("do\n" + files("jev_factorio").joinpath("lua/launch_readiness.lua").read_text() + "\nend")
            self.command("storage.campaign.discover()")

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

    def call(self, function: str, *arguments: Any) -> str:
        encoded = ", ".join(
            "helpers.json_to_table(" + json.dumps(json.dumps(value, allow_nan=False)) + ")"
            if isinstance(value, (dict, list)) else json.dumps(value, allow_nan=False)
            for value in arguments
        )
        return self.command(f"storage.campaign.{function}({encoded})")

    def approach(self, position: Any) -> None:
        self.backend._fair.approach(position)

    def approach_role(self, role: str, *, bootstrap_parameters=None) -> None:
        from fle.env import Position

        preflight = ''
        if bootstrap_parameters is not None:
            from ..bootstrap_output import ROLE
            if role != ROLE:
                raise ValueError('Bootstrap preflight cannot authorize another endpoint')
            item = json.dumps(bootstrap_parameters['item'])
            receipt = json.dumps(bootstrap_parameters['receipt'])
            quantity = str(bootstrap_parameters['quantity'])
            preflight = (
                'local output=assert(storage.bootstrap_output_v1.observe());'
                'assert(not output.native_pending and output.role==' + json.dumps(ROLE) + ');'
                'assert(' + item + '=="iron-ore" and output.output["iron-ore"]>=' + quantity
                + ' and output.capacity.count>=' + quantity + ');'
                'assert(not storage.campaign.receipts[' + receipt + ']);')
        state = decode_native(self.command(
            preflight + "local entity = storage.campaign.entities[" + json.dumps(role) + "]; "
            "assert(entity and entity.valid); "
            "rcon.print(helpers.table_to_json({name=entity.name, position=entity.position}))"
        ))
        self.backend._fair.approach(Position(**state["position"]), state["name"])

    def observe(self, snapshot: GameSnapshot) -> GameSnapshot:
        from fle.env import Position, Resource

        raw = self.command("rcon.print(helpers.table_to_json(storage.campaign.observe()))")
        factory = decode_native(raw)
        if self.backend._drill is not None:
            drop = self.backend._drill.drop_position
            for role, entity in factory["entities"].items():
                position = entity["position"]
                if (entity["name"] == "wooden-chest"
                        and abs(position["x"] - drop.x) < 0.5
                        and abs(position["y"] - drop.y) < 0.5):
                    factory["drill_output_role"] = role
                    break
        snapshot.factory = factory
        snapshot.game_version = self.catalog.version
        snapshot.researched = factory["researched"] or []
        snapshot.victory = factory["rockets_launched"] > factory["rocket_baseline"]
        snapshot.victory_source = "native:base-game-rocket-launch" if snapshot.victory else None
        snapshot.tick = factory["tick"]
        factory.pop("fair_resource_targets", None)
        generated_radius = factory.get("exploration_radius", 8)
        if type(generated_radius) is not int or not 1 <= generated_radius <= 32:
            raise RuntimeError("Invalid bounded exploration radius")
        # ``factory_explore`` generates terrain around the campaign origin in
        # chunk units.  Discovery inspects that already-generated area only;
        # it only probes cursor selectability, never moving or mining. FairActions walks
        # to the selected entity and checks native reach before mining.
        discovery_radius = generated_radius * 32
        for resource in ("wood", "coal", "iron-ore", "copper-ore", "stone"):
            self.backend._resources.pop(resource, None)
            snapshot.nearby_resources.pop(resource, None)
            observed = self.backend._fair.call(
                "discover_mine_target", resource, {"x": 0, "y": 0}, discovery_radius
            )
            candidate = observed.get("position") if isinstance(observed, dict) else None
            name = observed.get("name") if isinstance(observed, dict) else None
            surface_index = observed.get("surface_index") if isinstance(observed, dict) else None
            if not isinstance(candidate, dict):
                continue
            horizontal, vertical = candidate.get("x"), candidate.get("y")
            if (isinstance(horizontal, (int, float)) and not isinstance(horizontal, bool)
                    and isinstance(vertical, (int, float)) and not isinstance(vertical, bool)
                    and math.isfinite(horizontal) and math.isfinite(vertical)
                    and isinstance(name, str) and name.strip()
                    and (resource == "wood" or name == resource)
                    and type(surface_index) is int and surface_index > 0):
                location = Position(x=float(horizontal), y=float(vertical))
                self.backend._resources[resource] = location
                snapshot.nearby_resources[resource] = math.hypot(
                    location.x - snapshot.player_position[0],
                    location.y - snapshot.player_position[1],
                )
                factory.setdefault("fair_resource_targets", {})[resource] = {
                    "name": name,
                    "surface_index": surface_index,
                    "position": {"x": location.x, "y": location.y},
                }
        for name, resource in (("water", Resource.Water), ("crude-oil", Resource.CrudeOil)):
            try:
                location = self.backend._tools.nearest(resource)
                self.backend._resources[name] = location
                snapshot.nearby_resources[name] = math.hypot(
                    location.x - snapshot.player_position[0], location.y - snapshot.player_position[1]
                )
            except Exception:
                pass
        return snapshot

    @staticmethod
    def prototype(name: str) -> Any:
        from fle.env import Prototype

        for prototype in Prototype:
            if prototype.value[0] == name:
                return prototype
        raise ValueError(f"FLE has no supported prototype for {name}")

    def entity(self, role: str) -> Any:
        from fle.env import Position

        state = decode_native(self.command(
            "local entity = storage.campaign.entities[" + json.dumps(role) + "]; "
            "assert(entity and entity.valid, 'Campaign entity disappeared'); "
            "rcon.print(helpers.table_to_json({name=entity.name, position=entity.position}))"
        ))
        # FLE's opaque lookup can perform its own waits/retries. Keep its
        # inclusive helper wall/CPU time visible during action execution while
        # SessionRcon continues to count each actual client call separately.
        with span('fle_helper'):
            return self.backend._tools.get_entity(
                self.prototype(state["name"]), Position(**state["position"])
            )

    def native_pole_geometry(self, source_role: str, target_role: str) -> dict:
        """Read actor-bound pole coverage geometry from the current native entities."""
        if (not isinstance(source_role, str) or not source_role
                or not isinstance(target_role, str) or not target_role
                or source_role == target_role):
            raise ValueError("Invalid native pole endpoint roles")
        raw = self.command(
            "local actor=assert(storage.fair.actor()); "
            "assert(actor.character and actor.character.valid and actor.character.unit_number); "
            "local function point(p) assert(p and p.x and p.y); return {x=p.x,y=p.y} end; "
            "local function box(b) assert(b and b.left_top and b.right_bottom); "
            "return {left_top=point(b.left_top),right_bottom=point(b.right_bottom),"
            "orientation=b.orientation or 0} end; "
            "local function endpoint(role) "
            "local e=storage.campaign.entities[role]; "
            "assert(e and e.valid and e.unit_number and e.surface==actor.surface "
            "and e.force==actor.force); "
            "return {role=role,name=e.name,unit_number=e.unit_number,position=point(e.position),"
            "direction=e.direction,orientation=e.orientation,surface_index=e.surface.index,"
            "force_index=e.force.index,quality=e.quality.name,bounding_box=box(e.bounding_box)} end; "
            "local source=endpoint(" + json.dumps(source_role) + "); "
            "local target=endpoint(" + json.dumps(target_role) + "); "
            "assert(source.unit_number~=target.unit_number); "
            "local pole=assert(prototypes.entity['small-electric-pole']); "
            "local result={schema='jev.native-pole-geometry.v1',"
            "base_version=script.active_mods.base,session_id=storage.jev_session_id,"
            "tick=game.tick,actor_unit=actor.character.unit_number,"
            "surface_index=actor.surface.index,force_index=actor.force.index,"
            "supply_area_distance=pole.get_supply_area_distance('normal'),"
            "maximum_wire_distance=pole.get_max_wire_distance('normal'),"
            "source=source,target=target}; "
            "rcon.print(helpers.table_to_json(result))"
        )
        result = decode_native(raw)
        if not isinstance(result, dict):
            raise ValueError("Malformed native pole geometry response")
        return result

    @staticmethod
    def fluid_connection_points(entity: Any, fluid: str, *, output: bool) -> list[Any]:
        """Return FLE-observed pipe cells for one fluid without mutating the world."""
        if output and fluid == "steam":
            steam_output = getattr(entity, "steam_output_point", None)
            if steam_output is not None:
                return [steam_output]
        attribute = "output_connection_points" if output else "input_connection_points"
        typed = list(getattr(entity, attribute, []) or [])
        if typed:
            accepted = {fluid} if output else {"", fluid}
            matches = [point for point in typed if getattr(point, "type", "") in accepted]
            if matches:
                return matches
            direction = "output" if output else "input"
            raise ValueError(f"Requested {direction} fluid has no native connection point")
        generic = list(getattr(entity, "connection_points", []) or [])
        if generic:
            # FLE 0.4.3 serializes generator ports at the edge of the entity
            # collision box.  On the axis of connection that is an integer tile
            # boundary, not the half-integer center where a one-tile pipe can be
            # built.  Move only those boundary coordinates one half-tile away
            # from the generator; other handlers already report pipe-cell centers.
            if getattr(entity, "name", "") in {"steam-engine", "steam-turbine"}:
                center = entity.position
                normalized = []
                for point in generic:
                    coordinates = []
                    for coordinate, origin in (
                        (float(point.x), float(center.x)),
                        (float(point.y), float(center.y)),
                    ):
                        fraction = coordinate - math.floor(coordinate)
                        if math.isclose(fraction, 0.5):
                            coordinates.append(coordinate)
                            continue
                        if not math.isclose(fraction, 0.0) or math.isclose(coordinate, origin):
                            raise ValueError("Generator connection point is not on a pipe-cell boundary")
                        coordinates.append(coordinate + math.copysign(0.5, coordinate - origin))
                    normalized.append(type(point)(x=coordinates[0], y=coordinates[1]))
                generic = normalized
            return generic
        direction = "output" if output else "input"
        raise ValueError(f"Requested {direction} fluid has no native connection point")

    def native_fluid_connection_points(self, role: str, fluid: str, *, output: bool) -> list[Any]:
        """Read rotated native pipe cells, avoiding FLE's inferred port geometry."""
        from fle.env import Position

        direction = "output" if output else "input"
        raw = self.command(
            "local entity=storage.campaign.entities[" + json.dumps(role) + "]; "
            "assert(entity and entity.valid, 'Campaign entity disappeared'); "
            "local result={}; for index=1,#entity.fluidbox do "
            "local filter=entity.fluidbox.get_filter(index); "
            "local contents=entity.fluidbox[index]; "
            "local name=type(filter)=='string' and filter or "
            "(filter and filter.name) or (contents and contents.name) or ''; "
            "if name==" + json.dumps(fluid) + (" then " if output else " or name=='' then ")
            + "for _,connection in pairs(entity.fluidbox.get_pipe_connections(index)) do "
            "if connection.connection_type=='normal' and "
            "(connection.flow_direction==" + json.dumps(direction)
            + " or connection.flow_direction=='input-output') then "
            "table.insert(result,connection.target_position) end end end end; "
            "rcon.print(helpers.table_to_json({points=result}))"
        )
        points = decode_native(raw)["points"]
        if points == [] or points == {}:
            raise ConnectionPreflightRejected("missing_fluid_port")
        if not isinstance(points, list):
            raise ValueError("Invalid native fluid port response")
        if len(points) > MAX_NATIVE_FLUID_PORTS:
            raise _fluid_port_search_exhausted()
        parsed = []
        for point in points:
            if not isinstance(point, dict) or set(point) != {"x", "y"}:
                raise ValueError("Invalid native fluid port response")
            coordinates = {}
            for axis in ("x", "y"):
                value = point[axis]
                if type(value) not in (int, float):
                    raise ValueError("Invalid native pipe cell coordinate")
                try:
                    coordinate = float(value)
                except (OverflowError, ValueError) as error:
                    raise ValueError("Invalid native pipe cell coordinate") from error
                if (not math.isfinite(coordinate) or abs(coordinate) > 1_000_000
                        or not (coordinate * 2).is_integer()
                        or coordinate - math.floor(coordinate) != 0.5):
                    raise ValueError("Invalid native pipe cell coordinate")
                coordinates[axis] = coordinate
            parsed.append(Position(**coordinates))
        return parsed

    def position(self, name: str, anchor: str) -> Any:
        from fle.env import Position

        if anchor in {"water", "crude-oil"}:
            if anchor not in self.backend._resources:
                raise ValueError(f"No observed {anchor} placement anchor")
            return self.backend._resources[anchor]
        raw = self.command(
            "local campaign = storage.campaign; local count = 0; "
            "for role in pairs(campaign.entities) do "
            "if string.sub(role, 1, 6) ~= 'stock:' then count = count + 1 end end; "
            "local reference = campaign.entities[" + json.dumps(anchor) + "]; "
            "local center = reference and "
            "{x=reference.position.x + 10, y=reference.position.y} or "
            "{x=(count % 6)*12, y=32 + math.floor(count/6)*12}; "
            "local position = storage.agent_characters[1].surface.find_non_colliding_position("
            + json.dumps(name) + ", center, 48, 0.5); "
            "assert(position, 'No collision-free factory site'); "
            "rcon.print(helpers.table_to_json(position))"
        )
        return Position(**decode_native(raw))

    def execute(self, action: str, parameters: dict, *, trace: Trace | None = None) -> str:
        validate_command(action, parameters)
        from ..launch_readiness import COMMANDS
        if action in COMMANDS:
            from .launch_readiness import execute
            return execute(self, action, parameters, trace)
        tools = self.backend._tools
        if action == "factory_wait":
            return "Waiting for native production or research"
        if action == "factory_bind":
            self.call("bind_player")
            return "Bound the existing agent character for native crafting"
        if action == "factory_explore":
            self.call("explore", parameters["radius"])
            return f"Generated normal map terrain to radius {parameters['radius']} chunks"
        if action == "factory_gather":
            resource = parameters["resource"]
            position = self.backend._resources[resource]
            receipt = parameters.get("receipt")
            if receipt is not None:
                attachment = getattr(self.backend, '_native_attachment', None)
                if attachment is None:
                    raise RuntimeError('Journaled coal gather requires a qualified native attachment')
                from .native_attachment import require_asset
                require_asset(attachment, 'coal_manual_journal_v1')
                self.command('storage.coal_manual_journal_v1.begin(' + json.dumps(receipt) + ')')
            harvested = self.backend._fair.harvest(resource, position, parameters["quantity"])
            if receipt is not None:
                result = decode_native(self.command(
                    'rcon.print(helpers.table_to_json(storage.coal_manual_journal_v1.finish('
                    + json.dumps(receipt) + ')))'))
                if (not isinstance(result, dict) or result.get('status') != 'complete'
                        or result.get('coal_after', 0) - result.get('coal_before', 0) != harvested):
                    raise RuntimeError('Native coal gather journal did not complete exactly')
            return f"Harvested {harvested} {resource}"
        if action == "factory_craft":
            self.call("craft", parameters["recipe"], parameters["batches"])
            return f"Queued native crafting: {parameters['recipe']}"
        if action == "factory_place":
            from fle.env import Direction

            position = self.position(parameters["name"], parameters["anchor"])
            entity = self.backend._fair.place_entity(self.prototype(parameters["name"]),
                                        position=position, direction=Direction.UP, exact=False)
            self.call("register", parameters["role"], parameters["name"],
                      {"x": entity.position.x, "y": entity.position.y})
            return f"Placed {parameters['name']} for {parameters['role']}"
        if action == "factory_configure":
            self.approach_role(parameters["role"])
            self.call("configure", parameters["role"], parameters["recipe"])
            return f"Configured {parameters['role']}"
        if action in {"factory_insert", "factory_extract"}:
            from ..bootstrap_output import ROLE as BOOTSTRAP_ROLE
            if parameters['role'] == BOOTSTRAP_ROLE:
                if action != 'factory_extract':
                    raise ValueError('Bootstrap output only permits bounded raw ore pickup')
                from .native_attachment import require_asset
                attachment = getattr(self.backend, '_native_attachment', None)
                if attachment is None or not require_asset(attachment, 'bootstrap_output_v1'):
                    raise RuntimeError('Bootstrap pickup requires its qualified native attachment')
                with phase('approach', trace):
                    self.approach_role(parameters['role'], bootstrap_parameters=parameters)
                with phase('transfer_rpc', trace):
                    self.command('storage.bootstrap_output_v1.extract('
                        + json.dumps(parameters['item']) + ',' + str(parameters['quantity'])
                        + ',' + json.dumps(parameters['receipt']) + ')')
                return f"Collected {parameters['quantity']} native bootstrap ore ({parameters['receipt']})"
            with phase("approach", trace):
                self.approach_role(parameters["role"])
            with phase("transfer_rpc", trace):
                self.call("transfer", parameters["role"], parameters["item"],
                          parameters["quantity"], parameters["receipt"], action == "factory_extract")
            return f"Transferred {parameters['quantity']} {parameters['item']} ({parameters['receipt']})"
        if action == "factory_connect":
            attachment = getattr(self.backend, '_native_attachment', None)
            if attachment is not None and not attachment['modules']['connector_ownership']:
                raise RuntimeError('Retained native campaign has no paid connector ledger')
            from fle.env import Position

            if parameters["kind"] == "pipe":
                fair = self.backend._fair
                branch = decode_native(self.call("pipe_source", parameters["source"],
                                              parameters["target"], parameters["fluid"]))
                if branch:
                    source = Position(**branch)
                    source_points = [source]
                else:
                    source_points = self.native_fluid_connection_points(
                        parameters["source"], parameters["fluid"], output=True
                    )
                target_points = self.native_fluid_connection_points(
                    parameters["target"], parameters["fluid"], output=False
                )
                source, target, preflight_budget = fair.select_feasible_pipe_pair(
                    source_points, target_points, parameters["fluid"],
                )
                source = Position(x=source.x, y=source.y)
                target = Position(x=target.x, y=target.y)
                fair.connect(source, target, self.prototype(parameters["kind"]),
                             parameters["fluid"], identity=parameters,
                             preflight_budget=preflight_budget)
            elif parameters["kind"] == "small-electric-pole":
                fair = self.backend._fair
                geometry = fair.validate_pole_geometry(
                    self.native_pole_geometry(parameters["source"], parameters["target"]),
                    base_version=self.catalog.version,
                    source_role=parameters["source"],
                    target_role=parameters["target"],
                )
                source = Position(**geometry["source"]["position"])
                target = Position(**geometry["target"]["position"])
                fair.connect(
                    source, target, self.prototype(parameters["kind"]),
                    parameters["fluid"], identity=parameters,
                    pole_geometry=geometry,
                )
            else:
                source, target = self.entity(parameters["source"]), self.entity(parameters["target"])
                source, target = source.position, target.position
                self.backend._fair.connect(source, target, self.prototype(parameters["kind"]),
                                           parameters["fluid"], identity=parameters)
            return f"Constructed {parameters['kind']} connection; native topology must verify"
        if action == "factory_research":
            self.call("research", parameters["technology"])
            return f"Started native research {parameters['technology']}"
        if action == "factory_launch":
            self.approach_role(parameters["role"])
            self.call("launch", parameters["role"])
            return "Requested native rocket launch; awaiting force launch counter"
        raise ValueError(f"Unsupported factory command: {action}")
