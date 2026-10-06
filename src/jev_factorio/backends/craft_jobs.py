"""Opt-in native-factory decorator; all ordinary actions retain their adapter."""
from __future__ import annotations

from importlib.resources import files

from ..factory_contract import validate_command
from ..telemetry import Trace, phase
from ..craft_jobs import craft_actor_observation_failure


class CraftJobFactory:
    def __init__(self, native) -> None:
        self.native = native
        if getattr(native.backend, '_native_attachment', None) is not None:
            from .native_attachment import require_asset
            require_asset(native.backend._native_attachment, 'craft_jobs')
        else:
            native.command(files("jev_factorio").joinpath("lua/craft_jobs.lua").read_text())

    def __getattr__(self, name):
        return getattr(self.native, name)

    def observe(self, snapshot):
        snapshot = self.native.observe(snapshot)
        marker = snapshot.factory.get("craft_job_observation_failure")
        if marker is not None:
            raise craft_actor_observation_failure(marker)
        evidence = snapshot.factory.pop("craft_job_inventory", None)
        if not isinstance(evidence, dict) or evidence.get("tick") != snapshot.tick:
            raise ValueError("Missing atomic crafting inventory observation")
        inventory = evidence.get("items")
        if inventory == []:
            inventory = {}  # Empty native Lua map, never a non-empty array.
        if (not isinstance(inventory, dict) or len(inventory) > 4096
                or any(not isinstance(item, str) or not item or len(item) > 128
                       or type(amount) is not int or amount < 0
                       for item, amount in inventory.items())):
            raise ValueError("Invalid atomic crafting inventory observation")
        if (getattr(snapshot, '_coherent_observation_verified', None)
                == (snapshot.session_id, snapshot.tick) and snapshot.inventory != inventory):
            raise ValueError("Atomic crafting inventory disagrees with coherent snapshot")
        snapshot.inventory = dict(inventory)
        snapshot._atomic_inventory_verified = (snapshot.session_id, snapshot.tick)
        return snapshot

    def execute(self, action: str, parameters: dict, *, trace: Trace | None = None) -> str:
        if action != "factory_craft_job":
            if trace is None:
                return self.native.execute(action, parameters)
            return self.native.execute(action, parameters, trace=trace)
        validate_command(action, parameters)
        with phase("transfer_rpc", trace):
            self.native.call("begin_craft_job", parameters["receipt"],
                             parameters["recipe"], parameters["batches"])
        return "Native craft request returned; receipt and output require observation"
