"""FLE adapter for a dedicated, explicitly marked Factorio agent world."""
from __future__ import annotations

import json
import math
import os
from uuid import uuid4

from ..planning.catalog import Catalog
from ..state import GameSnapshot
from ..telemetry import Trace
from ..iteration_timing import native_io, request_size, decode_native


class SessionRcon:
    """Keep FLE's executable Lua state out of Factorio's saved storage."""

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    @staticmethod
    def scoped(command):
        for prefix in ("/sc ", "/c ", "/silent-command ", "/command "):
            if command.startswith(prefix):
                return prefix + "local storage = jev_fle_runtime; " + command[len(prefix):]
        return command

    def send_command(self, command):
        scoped = self.scoped(command)
        return native_io("native_command", lambda: self.client.send_command(scoped),
                         request_bytes=request_size(scoped))

    def send_commands(self, commands):
        scoped = {key: self.scoped(command) for key, command in commands.items()}
        return native_io("native_batch", lambda: self.client.send_commands(scoped),
                         request_bytes=request_size(scoped))


class FleBackend:
    def __init__(self) -> None:
        self._instance = None
        self._resources = {}
        self._drill = None
        self._error = ""
        self._factory = None
        self._fair = None
        self.profile_observations = False
        self.consolidated_observations = False
        self.last_observation_profile = None
        self._observation_profile = None
        self._native_attachment = None

    def enable_factory(self) -> Catalog:
        from .native_factory import NativeFactory

        if self._factory is None:
            if self.consolidated_observations:
                from .observed_factory import ObservedFactory
                self._factory = ObservedFactory(self)
            else:
                self._factory = NativeFactory(self)
        return self._factory.catalog

    def execute(self, action: str, parameters: dict) -> str:
        if self._factory is None:
            raise RuntimeError("Native factory capabilities have not been enabled")
        return self._factory.execute(action, parameters)

    def execute_traced(self, action: str, parameters: dict, trace: Trace) -> str:
        """Same execution contract, with optional phase evidence for the controller."""
        if self._factory is None:
            raise RuntimeError("Native factory capabilities have not been enabled")
        return self._factory.execute(action, parameters, trace=trace)

    def native_mine_target(self, resource: str):
        """Return a fresh, cursor-selectable native raw-resource target.

        FLE's ``nearest`` cache may outlive a depleted resource entity.  Raw
        gathering must therefore be admitted by the fair Lua selector, which
        checks the original player, 1x speed, minability, and cursor
        visibility.  This observation does not move or mine; FairActions
        still walks and checks normal reach immediately before mining.
        """
        from fle.env import Position

        selected = self._fair.call("next_mine_target", resource, 128)
        candidate = selected.get("position") if isinstance(selected, dict) else None
        name = selected.get("name") if isinstance(selected, dict) else None
        surface_index = selected.get("surface_index") if isinstance(selected, dict) else None
        if not isinstance(candidate, dict):
            raise RuntimeError(f"No fair native {resource} target observed")
        horizontal, vertical = candidate.get("x"), candidate.get("y")
        if (
            name != resource
            or type(surface_index) is not int
            or surface_index <= 0
            or type(horizontal) not in {int, float}
            or type(vertical) not in {int, float}
            or not math.isfinite(horizontal)
            or not math.isfinite(vertical)
        ):
            raise RuntimeError(f"Invalid fair native {resource} target")
        return Position(x=float(horizontal), y=float(vertical))

    @staticmethod
    def _adopt_session(client) -> str:
        session_id = uuid4().hex
        observed = client.send_command(
            "/sc assert(storage.jev_factorio_session == true); "
            "assert(jev_fle_runtime and jev_fle_runtime.agent_characters and "
            "jev_fle_runtime.agent_characters[1] and "
            "jev_fle_runtime.agent_characters[1].valid); "
            "assert(jev_fle_runtime.jev_session_id == nil or "
            "jev_fle_runtime.jev_session_id == ''); "
            "jev_fle_runtime.jev_session_id = " + json.dumps(session_id) + "; "
            "rcon.print(jev_fle_runtime.jev_session_id)"
        )
        if (observed or "").strip() != session_id:
            raise RuntimeError("Session adoption failed; refusing to reset or overwrite identity")
        return session_id

    def start(self, resume: bool = False, adopt_session: bool = False,
              setup_timing=None, connector_witness_path=None, connector_binding=None,
              completed_craft=None, background_craft=None, output_commitments=None) -> None:
        if adopt_session and not resume:
            raise ValueError("Session adoption requires resume; never initializes a world")
        if setup_timing:
            setup_timing.mark_backend('attach_start')
        from factorio_rcon import RCONClient
        from fle.env import FactorioInstance

        password = os.environ.get("FACTORIO_RCON_PASSWORD")
        if not password:
            raise ValueError("Set FACTORIO_RCON_PASSWORD in .env")

        class DedicatedInstance(FactorioInstance):
            @staticmethod
            def connect_to_server(address, tcp_port):
                client = RCONClient(address, tcp_port, password, timeout=120)
                marker = (client.send_command(
                    "/sc rcon.print(storage.jev_factorio_session == true)"
                ) or "").strip()
                if marker != "true":
                    client.close()
                    raise RuntimeError(
                        "Refusing to initialize an unmarked world. Use a dedicated "
                        "agent save and set storage.jev_factorio_session = true via RCON."
                    )
                if resume:
                    ready = client.send_command(
                        "/sc rcon.print(jev_fle_runtime ~= nil and "
                        "jev_fle_runtime.agent_characters ~= nil and "
                        "jev_fle_runtime.agent_characters[1] ~= nil and "
                        "jev_fle_runtime.agent_characters[1].valid)"
                    )
                    if (ready or "").strip() != "true":
                        client.close()
                        raise RuntimeError("No live agent session to resume; refusing to reset.")
                    if adopt_session:
                        try:
                            FleBackend._adopt_session(client)
                        except Exception:
                            client.close()
                            raise
                else:
                    client.send_command("/sc jev_fle_runtime = {jev_session_id="
                                        + json.dumps(uuid4().hex) + "}")
                return SessionRcon(client), address

            def initialise(self, *args, **kwargs):
                if not resume:
                    return super().initialise(*args, **kwargs)

            def _generate_chunks(self, center_x=0, center_y=0, chunk_radius=25):
                """Bound initial terrain generation for the bootstrap scenario."""
                return super()._generate_chunks(center_x, center_y, min(chunk_radius, 8))

        self._instance = DedicatedInstance(
            address=os.environ.get("FACTORIO_RCON_HOST", "127.0.0.1"),
            tcp_port=int(os.environ.get("FACTORIO_RCON_PORT", "27018")),
            fast=False,
            inventory={"burner-mining-drill": 1, "wooden-chest": 1},
            all_technologies_researched=False,
            clear_entities=True,
            peaceful=True,
            reset_speed=1,
        )
        if setup_timing:
            setup_timing.mark_backend('instance_ready')
        if resume:
            existing_campaign = self._instance.rcon_client.send_command(
                "/sc rcon.print(jev_fle_runtime ~= nil and jev_fle_runtime.campaign ~= nil)"
            )
            if (existing_campaign or "").strip() == "true":
                from .native_attachment import readback
                self._native_attachment = readback(
                    self._instance.rcon_client,
                    receipt_path=os.environ.get('JEV_NATIVE_ATTACHMENT_RECEIPT'),
                    connector_witness_path=connector_witness_path,
                    checkpoint_binding=connector_binding, completed_craft=completed_craft,
                    background_craft=background_craft,
                    **({'output_commitments': output_commitments} if output_commitments is not None else {}))
            elif (existing_campaign or "").strip() == "false":
                partial = self._instance.rcon_client.send_command(
                    "/sc rcon.print(jev_fle_runtime ~= nil and "
                    "(jev_fle_runtime.native_installation ~= nil or "
                    "jev_fle_runtime.fair ~= nil))"
                )
                if (partial or "").strip() == "true":
                    raise RuntimeError("Partial native installation requires reconciliation")
                raise RuntimeError("No installed native campaign to resume")
            elif (existing_campaign or "").strip() != "false":
                raise RuntimeError("Cannot determine whether the native campaign is installed")
        if setup_timing:
            setup_timing.mark_backend('installation_ready')
        from .fair_actions import FairActions

        self._fair = FairActions(self)
        if setup_timing:
            setup_timing.mark_backend('fair_ready')

    @property
    def _tools(self):
        if self._instance is None:
            raise RuntimeError("Start the FLE backend before using it")
        tools = self._instance.namespace
        if self._observation_profile is not None:
            from ..observation import ProfiledTools
            return ProfiledTools(tools, self._observation_profile)
        return tools

    def observe(self) -> GameSnapshot:
        if self.profile_observations or self.consolidated_observations:
            from ..observation import profile_backend
            with profile_backend(self):
                if self._has_coherent_observation():
                    return self._observe_coherent()
                return self._observe_legacy()
        return self._observe_legacy()

    def _has_coherent_observation(self) -> bool:
        from .observed_factory import ObservedFactory
        native = self._factory
        while native is not None:
            if isinstance(native, ObservedFactory):
                return (self.consolidated_observations
                        and getattr(native, 'coherent_observation_version', None) == 2)
            native = getattr(native, 'native', None)
        return False

    def _observe_coherent(self) -> GameSnapshot:
        from . import has_adapter
        from .craft_jobs import CraftJobFactory
        snapshot = GameSnapshot(world_kind='fle', alerts=[self._error] if self._error else [])
        snapshot = self._factory.observe(snapshot)
        identity = (snapshot.session_id, snapshot.tick)
        if getattr(snapshot, '_coherent_observation_verified', None) != identity:
            raise ValueError('Coherent observation provider did not validate this snapshot')
        if (has_adapter(self._factory, CraftJobFactory)
                and getattr(snapshot, '_atomic_inventory_verified', None) != identity):
            raise ValueError('Atomic inventory provider did not validate this snapshot')
        return snapshot

    def _observe_legacy(self) -> GameSnapshot:
        from fle.env import Prototype

        native_controls = self._fair.call("observe")
        tools = self._tools
        raw = self._instance.rcon_client.send_command(
            "/sc local agent = storage.agent_characters[1]; "
            "rcon.print(helpers.table_to_json({tick=game.tick,"
            "session_id=storage.jev_session_id,"
            "position={agent.position.x,agent.position.y}}))"
        )
        live = self._observation_profile.decode(raw) if self._observation_profile else decode_native(raw)
        position = tuple(live["position"])
        # The installed craft-job adapter requires and validates native inventory
        # from the same observation as its receipt/tick. Unsupported configurations
        # retain the legacy helper; failed validation never silently falls back.
        from . import has_adapter
        from .craft_jobs import CraftJobFactory
        atomic_inventory = has_adapter(self._factory, CraftJobFactory)
        inventory = {} if atomic_inventory else dict(tools.inspect_inventory().items())
        nearby = {}
        alerts = [self._error] if self._error else []
        self._resources = {}
        for name in (() if self.consolidated_observations and self._factory else ("coal", "iron-ore")):
            try:
                target = self.native_mine_target(name)
                self._resources[name] = target
                nearby[name] = math.hypot(target.x - position[0], target.y - position[1])
            except Exception as error:
                alerts.append(f"{name}: {error}")
        entities = tools.get_entities({Prototype.BurnerMiningDrill, Prototype.WoodenChest})
        drills = [entity for entity in entities if entity.name == "burner-mining-drill"]
        self._drill = drills[0] if drills else None
        output_chests = [
            entity for entity in entities
            if self._drill is not None and entity.name == "wooden-chest"
            # Factorio places the chest at its tile center, while the drill's
            # output position can be offset within that tile (0.203125 in the
            # native iron-drill case). Compare the occupied tile, not centers.
            and abs(entity.position.x - self._drill.drop_position.x) < 0.5
            and abs(entity.position.y - self._drill.drop_position.y) < 0.5
        ]
        collected = sum(tools.inspect_inventory(entity).get("iron-ore", 0)
                        for entity in output_chests)
        snapshot = GameSnapshot(
            tick=live["tick"],
            session_id=live.get("session_id", ""),
            world_kind="fle",
            player_position=position,
            inventory=inventory,
            nearby_resources=nearby,
            placed_entities=[entity.name for entity in entities],
            alerts=alerts,
            drill_status=self._drill.status.value if self._drill else "",
            drill_fuel=self._drill.fuel.get("coal", 0) if self._drill else 0,
            drill_output_connected=bool(output_chests),
            iron_ore_collected=collected,
        )
        snapshot = self._factory.observe(snapshot) if self._factory else snapshot
        if atomic_inventory and getattr(snapshot, '_atomic_inventory_verified', None) != (snapshot.session_id, snapshot.tick):
            raise ValueError("Atomic inventory provider did not validate this observation")
        snapshot._native_controls = native_controls
        return snapshot

    def act(self, action: str) -> str:
        if action != 'idle' and getattr(self, '_bootstrap_output_pending', False):
            raise ValueError('Native bootstrap journal requires reconciliation before actor mutation')
        from fle.env import Direction, Prototype

        tools = self._tools
        self._error = ""
        if action != "idle" and self._has_coherent_observation():
            from .observed_factory import ObservedFactory
            native = self._factory
            while native is not None:
                if isinstance(native, ObservedFactory):
                    native._discovery_epoch += 1
                    break
                native = getattr(native, "native", None)
        try:
            if action == "idle":
                return "Waiting for production"
            if action in ("walk_to_coal", "walk_to_iron"):
                resource = "coal" if action == "walk_to_coal" else "iron-ore"
                position = self._fair.move_to(self._resources[resource])
                return f"Moved to {resource} at ({position.x}, {position.y})"
            if action in ("mine_coal", "mine_iron"):
                resource = "coal" if action == "mine_coal" else "iron-ore"
                amount = self._fair.harvest(resource, self._resources[resource], quantity=5)
                return f"Harvested {amount} {resource}"
            if action == "place_burner_drill":
                attachment = getattr(self, '_native_attachment', None)
                bootstrap_owned = bool(attachment and attachment['modules'].get('bootstrap_output_v1'))
                if bootstrap_owned:
                    from .native_attachment import require_asset
                    require_asset(attachment, 'bootstrap_output_v1')
                self._drill = self._fair.place_entity(
                    Prototype.BurnerMiningDrill,
                    direction=Direction.UP,
                    position=self._resources["iron-ore"],
                    exact=False,
                    **({'bootstrap_owned': True} if bootstrap_owned else {}),
                )
                chest = self._fair.place_entity(Prototype.WoodenChest, position=self._drill.drop_position,
                                        direction=Direction.UP, exact=True,
                                        **({'bootstrap_owned': True} if bootstrap_owned else {}))
                if bootstrap_owned:
                    self._fair.command('storage.bootstrap_output_v1.bind_paid('
                        + str(self._drill.unit_number) + ',' + str(chest.unit_number) + ')')
                return "Placed burner drill on iron with an output chest"
            if action == "fuel_drill":
                if self._drill is None:
                    raise ValueError("No burner drill exists")
                amount = min(5, tools.inspect_inventory().get("coal", 0))
                if amount == 0:
                    raise ValueError("No coal in inventory")
                self._fair.insert_item(Prototype.Coal, self._drill, quantity=amount)
                return f"Fueled burner drill with {amount} coal"
            if action == "craft_stone_furnace":
                self.enable_factory()
                self._factory.call("craft", "stone-furnace", 1)
                return "Crafted a stone furnace"
            raise ValueError(f"Unsupported action: {action}")
        except Exception as error:
            self._error = f"{action} failed: {error}"
            return self._error
