"""Opt-in consolidated read path over the unchanged native mutation contract."""
from __future__ import annotations

import math
from importlib.resources import files

from .native_factory import NativeFactory
from ..observation import parse_snapshot


class ObservedFactory(NativeFactory):
    def __init__(self, backend):
        super().__init__(backend)
        self._discovery_epoch = 0
        if getattr(backend, '_native_attachment', None) is not None:
            from .native_attachment import require_asset
            require_asset(backend._native_attachment, 'observation')
            require_asset(backend._native_attachment, 'observation_v2')
        else:
            self.command(files("jev_factorio").joinpath("lua/observation.lua").read_text())
            from .native_attachment import WATER_ORIGIN_OBSERVATION_ASSET
            ready = self.command(
                files("jev_factorio").joinpath("lua/" + WATER_ORIGIN_OBSERVATION_ASSET).read_text())
            if ready.strip() != 'JEV_ATOMIC_READY|2':
                raise RuntimeError('Native coherent observation negotiation failed')
        self.coherent_observation_version = 2

    def execute(self, action, parameters, trace=None):
        # Invalidate before attempted topology/mining changes, including ambiguous failures.
        if action not in {"factory_insert", "factory_extract", "factory_wait",
                          "factory_craft", "factory_craft_job", "factory_research"}:
            self._discovery_epoch += 1
        return super().execute(action, parameters, trace=trace)

    def observe(self, snapshot):
        if getattr(self, 'coherent_observation_version', None) == 2:
            from .atomic_observation import observe_atomic
            return observe_atomic(self, snapshot)
        from fle.env import Position, Resource
        from ..craft_jobs import parse_craft_actor_observation_failure

        profile = self.backend._observation_profile
        raw = self.call("observation_snapshot", self._discovery_epoch)
        failure = parse_craft_actor_observation_failure(raw)
        if failure is not None:
            raise failure
        result = parse_snapshot(raw, profile)
        factory = result.get("factory")
        if not isinstance(factory, dict) or result.get("session_id") != snapshot.session_id:
            raise ValueError("Consolidated observation session changed")
        runtime = factory.get("acceptance_runtime", {})
        if (not isinstance(runtime, dict) or runtime.get("session_id") != snapshot.session_id
                or factory.get("player_bound") is not True or factory.get("player_connected") is not True):
            raise ValueError("Consolidated observation player binding changed")
        for key in ("actor_unit", "surface_index"):
            if (type(result.get(key)) is not int or result[key] <= 0
                    or type(runtime.get(key)) is not int or result[key] != runtime[key]):
                raise ValueError("Consolidated observation actor changed")
        if type(factory.get("tick")) is not int or factory["tick"] < snapshot.tick:
            raise ValueError("Consolidated observation tick regressed")
        for name in ("entities", "receipts"):
            if factory.get(name) == []:
                factory[name] = {}
            if not isinstance(factory.get(name), dict):
                raise ValueError("Invalid authoritative factory observation")
        targets = result.get("targets")
        if targets == []:  # Factorio serializes an empty Lua table as an array.
            targets = {}
        if not isinstance(targets, dict) or set(targets) - {"wood", "coal", "iron-ore", "copper-ore", "stone"}:
            raise ValueError("Invalid discovery targets")
        factory["fair_resource_targets"] = {}
        for item in ("wood", "coal", "iron-ore", "copper-ore", "stone"):
            self.backend._resources.pop(item, None)
            snapshot.nearby_resources.pop(item, None)
            value = targets.get(item)
            if value is None:
                continue
            position = value.get("position", {}) if isinstance(value, dict) else {}
            coordinates = [position.get(axis) for axis in ("x", "y")]
            if (any(type(v) not in {int, float} or not math.isfinite(v) for v in coordinates)
                    or type(value.get("surface_index")) is not int or value["surface_index"] <= 0
                    or value.get("surface_index") != result["surface_index"]
                    or not isinstance(value.get("name"), str) or not value["name"]
                    or (item != "wood" and value["name"] != item)):
                raise ValueError("Invalid consolidated discovery identity")
            location = Position(x=coordinates[0], y=coordinates[1])
            self.backend._resources[item] = location
            snapshot.nearby_resources[item] = math.hypot(
                location.x - snapshot.player_position[0], location.y - snapshot.player_position[1])
            factory["fair_resource_targets"][item] = {key: value[key] for key in ("position", "surface_index", "name")}
        for item, resource in (("water", Resource.Water), ("crude-oil", Resource.CrudeOil)):
            self.backend._resources.pop(item, None)
            snapshot.nearby_resources.pop(item, None)
            try:
                location = self.backend._tools.nearest(resource)
                self.backend._resources[item] = location
                snapshot.nearby_resources[item] = math.hypot(
                    location.x - snapshot.player_position[0], location.y - snapshot.player_position[1])
            except Exception:
                pass  # Unknown discovery never becomes free material or an execution precondition.
        if self.backend._drill is not None:
            drop = self.backend._drill.drop_position
            for role, entity in factory["entities"].items():
                position = entity["position"]
                if (entity["name"] == "wooden-chest"
                        and abs(position["x"] - drop.x) < .5
                        and abs(position["y"] - drop.y) < .5):
                    factory["drill_output_role"] = role
                    break
        if not isinstance(factory.get("entities"), dict) or not isinstance(factory.get("receipts"), (dict, list)):
            raise ValueError("Invalid authoritative factory observation")
        snapshot.factory = factory
        snapshot.game_version = self.catalog.version
        snapshot.researched = factory["researched"] or []
        snapshot.victory = factory["rockets_launched"] > factory["rocket_baseline"]
        snapshot.victory_source = "native:base-game-rocket-launch" if snapshot.victory else None
        snapshot.tick = factory["tick"]
        counts = result.get("cache", {})
        for key in ("hits", "misses"):
            if type(counts.get(key)) is not int or not 0 <= counts[key] <= 5:
                raise ValueError("Invalid discovery cache diagnostics")
            profile.cache[key] += counts[key]
        return snapshot
