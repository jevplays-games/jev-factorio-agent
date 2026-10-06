"""Offline source-faithful craft actor failure composition controls; no live game."""
from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from jev_factorio.background import BackgroundMemory
from jev_factorio.craft_jobs import (
    CraftActorObservationFailure, InvalidCraftEvidence,
    craft_actor_observation_failure, parse_craft_actor_observation_failure,
)
from test_background_work import ReceiptBackend, controller, delay_native_craft_start


LUA_SETUP = r'''
handlers = {}
defines = {
  events = {on_pre_player_crafted_item=1, on_player_cancelled_crafting=2,
    on_player_crafted_item=3, on_tick=4, on_script_path_request_finished=5},
  inventory = {furnace_result=1, chest=2, character_main=3, fuel=4,
    furnace_source=5, assembling_machine_input=6, assembling_machine_output=7,
    lab_input=8},
  direction = {north=0,east=2,south=4,west=6,northeast=1,southeast=3,
    southwest=5,northwest=7},
  controllers = {character=1}
}
script = {
  get_event_handler=function(id) return handlers[id] end,
  on_event=function(id, callback) handlers[id]=callback end,
  on_nth_tick=function() end,
  active_mods={}
}
local counts = {['iron-plate']=1, gear=0}
local force = {index=1, rockets_launched=0, recipes={
  gear={name='gear',enabled=true,ingredients={{type='item',name='iron-plate',amount=1}},
    products={{type='item',name='gear',amount=1}}}}, technologies={}}
local surface = {index=1}
local coal = {name='coal', type='resource', valid=true, minable=true, amount=100,
  unit_number=44, position={x=1,y=2}, surface=surface, force=force}
surface.find_entities_filtered=function(filter)
  if filter and filter.force == force then return {coal} end
  if filter and filter.name == 'coal' then return {coal} end
  return {}
end
surface.find_entity=function(name, position)
  if name == coal.name and position.x == coal.position.x and position.y == coal.position.y then
    return coal
  end
end
surface.find_non_colliding_position=function(_, position) return position end
local agent = {valid=true, unit_number=9, force=force, surface=surface,
  position={x=0,y=0}}
local player = {index=1, connected=true, character=agent, force=force, surface=surface,
  position={x=0,y=0}, cheat_mode=false, crafting_queue_size=0, crafting_queue={},
  walking_state={walking=false}, mining_state={mining=false}}
player.get_item_count=function(name) return counts[name] or 0 end
player.get_main_inventory=function()
  return {get_contents=function()
    local result={}
    for name,count in pairs(counts) do
      if count > 0 then table.insert(result,{name=name,count=count}) end
    end
    return result
  end}
end
player.update_selected_entity=function(position)
  if position.x == coal.position.x and position.y == coal.position.y then player.selected=coal end
end
player.begin_crafting=function(args)
  counts['iron-plate']=counts['iron-plate']-args.count
  player.crafting_queue_size=args.count
  player.crafting_queue={{recipe=args.recipe,count=args.count,prerequisite=false}}
  return args.count
end
force.get_item_production_statistics=function()
  return {get_input_count=function() return 0 end, get_output_count=function() return 0 end}
end
game={tick=10,speed=1,tick_paused=false,get_player=function(index)
  if index == 1 then return player end
end}
prototypes={item={['iron-plate']={},gear={},coal={}},entity={character={crafting_categories={}}}}
storage={agent_characters={agent},jev_session_id='session-415',jev_player_index=1,
  campaign={entities={},connections={}}}
coal_state=coal
agent_state=agent
player_state=player
counts_state=counts
'''


class LuaHarness:
    def __init__(self, *, previous_observer_failure=False):
        lua_module = pytest.importorskip("lupa")
        self.lua = lua_module.LuaRuntime(unpack_returned_tuples=True)
        self.printed = []
        table_type = type(self.lua.table())

        def plain(value):
            if not isinstance(value, table_type):
                return value
            keys = list(value.keys())
            if keys and set(keys) == set(range(1, len(keys) + 1)):
                return [plain(value[index]) for index in range(1, len(keys) + 1)]
            return {str(key): plain(value[key]) for key in keys}

        def table_to_json(value):
            return json.dumps(plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)

        self.lua.execute(LUA_SETUP)
        self.lua.globals().jev_fle_runtime = self.lua.globals().storage
        self.lua.globals().helpers = self.lua.table_from({"table_to_json": table_to_json})
        self.lua.globals().rcon = self.lua.table_from({"print": lambda value: self.printed.append(str(value))})
        root = Path(__file__).resolve().parents[1]
        lua_dir = root / "src" / "jev_factorio" / "lua"
        self.lua.execute((lua_dir / "factory.lua").read_text(encoding="utf-8"))
        self.lua.execute((lua_dir / "fair_actions.lua").read_text(encoding="utf-8"))
        if previous_observer_failure:
            message = (previous_observer_failure if isinstance(previous_observer_failure, str)
                       else "previous observer failure")
            self.lua.execute(
                "storage.campaign.observe=function() error(" + json.dumps(message) + ") end")
        self.lua.execute((lua_dir / "craft_jobs.lua").read_text(encoding="utf-8"))
        self.lua.execute((lua_dir / "observation.lua").read_text(encoding="utf-8"))
        self.lua.execute("storage.campaign.begin_craft_job('receipt-415','gear',1)")

    def direct_observe(self):
        return self.lua.eval(r'''
          function()
            local ok, value = pcall(storage.campaign.observe)
            local job = storage.campaign.craft_jobs.job
            return {ok=ok, value=value, error=(not ok) and tostring(value) or nil,
              job_id=job.id, status=job.status, paid=job.paid, finished=job.finished,
              plate=player_state.get_item_count('iron-plate'), gear=player_state.get_item_count('gear')}
          end
        ''')()

    def atomic_observe(self):
        self.printed.clear()
        outcome = self.lua.eval(r'''
          function()
            local ok, value = pcall(storage.campaign.observation_snapshot, 0)
            local job = storage.campaign.craft_jobs.job
            return {ok=ok, error=(not ok) and tostring(value) or nil,
              status=job.status, paid=job.paid, finished=job.finished,
              plate=player_state.get_item_count('iron-plate'), gear=player_state.get_item_count('gear')}
          end
        ''')()
        return dict(outcome), list(self.printed)

    def set_actor(self, case):
        if case == "disconnect":
            self.lua.execute("player_state.connected=false")
        elif case == "replacement":
            self.lua.execute("player_state.character={valid=true,unit_number=10,force=storage.agent_characters[1].force,surface=storage.agent_characters[1].surface}")
        elif case == "invalid_character":
            self.lua.execute("agent_state.valid=false")
        else:
            raise AssertionError(case)

    def set_receipt(self, receipt):
        self.lua.globals().expected_receipt = receipt
        self.lua.execute("storage.campaign.craft_jobs.job.id=expected_receipt")


@contextmanager
def _test_fle_env():
    missing = object()
    old_fle = sys.modules.get("fle", missing)
    old_env = sys.modules.get("fle.env", missing)
    if old_env is not missing:
        yield
        return
    old_env_attr = getattr(old_fle, "env", missing) if old_fle is not missing else missing
    env_module = ModuleType("fle.env")
    env_module.Position = lambda **kw: SimpleNamespace(**kw)
    env_module.Resource = lambda **kw: SimpleNamespace(**kw)
    if old_fle is missing:
        fle_module = ModuleType("fle")
        fle_module.__path__ = []
        sys.modules["fle"] = fle_module
    else:
        fle_module = old_fle
    fle_module.env = env_module
    sys.modules["fle.env"] = env_module
    try:
        yield
    finally:
        if old_env is missing:
            sys.modules.pop("fle.env", None)
        else:
            sys.modules["fle.env"] = old_env
        if old_fle is missing:
            sys.modules.pop("fle", None)
        elif old_env_attr is missing:
            try:
                delattr(fle_module, "env")
            except AttributeError:
                pass
        else:
            fle_module.env = old_env_attr


def observed_factory_for_harness(harness, *, coherent_version=2):
    from jev_factorio.backends.observed_factory import ObservedFactory

    native = object.__new__(ObservedFactory)
    native.backend = SimpleNamespace(
        _native_attachment=None, _drill=None, _observation_profile=None)
    native.catalog = SimpleNamespace(machines={})
    native._discovery_epoch = 0
    native._coherent_identity = None
    native._coherent_drill = None
    native.coherent_observation_version = coherent_version
    if coherent_version == 2:
        harness.lua.execute("storage.campaign.observation_snapshot_v2=function(...) "
                            "return storage.campaign.observe() end")

    def execute(command):
        harness.printed.clear()
        try:
            harness.lua.execute(command)
        except Exception as error:
            raise RuntimeError("Cannot execute command. Error: " + str(error)) from error
        return "\n".join(harness.printed)

    native.command = execute
    def observe(snapshot):
        with _test_fle_env():
            return ObservedFactory.observe(native, snapshot)
    native.observe = observe
    return native


def _dict(value):
    return {str(key): value[key] for key in value.keys()}


def test_actual_factory_and_atomic_observation_positive_is_complete():
    harness = LuaHarness()
    direct = harness.direct_observe()
    assert direct["ok"] is True
    assert direct["status"] == "running" and direct["paid"] is True
    assert direct["value"]["craft_job"]["id"] == "receipt-415"
    result, output = harness.atomic_observe()
    assert result["ok"] is True
    assert result["status"] == "running" and result["paid"] is True
    row = next(value for value in output if value.startswith("JEV_SNAPSHOT|"))
    snapshot = json.loads(row.removeprefix("JEV_SNAPSHOT|"))
    assert snapshot["factory"]["exploration_radius"] == 8
    assert snapshot["targets"]["coal"]["name"] == "coal"
    assert snapshot["factory"]["craft_job"]["id"] == "receipt-415"
    assert snapshot["factory"]["craft_job_inventory"]["tick"] == snapshot["factory"]["tick"]


def _actual_capacity_observer_for_harness(harness):
    """Compose the real v2 public observer with the merged capacity wrapper."""
    from jev_factorio.observation import ObservationProfile

    native = observed_factory_for_harness(harness)
    harness.lua.execute("defines.entity_status={}")
    harness.lua.execute("player_state.surface.find_entities_filtered=function(_) return {} end")
    harness.lua.execute("player_state.surface.find_tiles_filtered=function(_) return {} end")
    harness.lua.execute(r'''
      player_state.get_main_inventory=function()
        local inventory={}
        inventory.get_contents=function()
          local result={}
          for name,count in pairs(counts_state) do
            if count>0 then table.insert(result,{name=name,count=count}) end
          end
          return result
        end
        inventory.get_insertable_count=function(spec)
          local counts={coal=17,wood=18,["iron-ore"]=19,["copper-ore"]=20,stone=21}
          return counts[spec.name]
        end
        return inventory
      end
    ''')
    lua_path = Path(__file__).resolve().parents[1] / "src/jev_factorio/lua/observation_v2_water_origin_v4.lua"
    harness.lua.execute(lua_path.read_text(encoding="utf-8"))
    native.catalog.version = "2.0.77"
    native.backend._resources = {}
    native.backend._observation_profile = ObservationProfile()
    return native


def test_composed_actor_capacity_public_observation_preserves_full_identity_bound_positive():
    from jev_factorio.state import GameSnapshot

    harness = LuaHarness()
    native = _actual_capacity_observer_for_harness(harness)
    snapshot = native.observe(GameSnapshot(session_id="session-415", tick=10))

    assert snapshot.session_id == "session-415" and snapshot.tick == 10
    evidence = snapshot.factory["inventory_insertable_evidence"]
    assert evidence["inventory"] == "character_main"
    assert evidence["method"] == "get_insertable_count"
    assert evidence["session_id"] == "session-415"
    assert evidence["actor_unit"] == 9
    assert evidence["surface_index"] == 1 and evidence["force_index"] == 1
    assert evidence["tick"] == 10
    assert evidence["items"] == {
        "coal": 17, "wood": 18, "iron-ore": 19, "copper-ore": 20, "stone": 21,
    }
    assert any(row.startswith("JEV_ACTOR_RAW_CAPACITY|") for row in harness.printed)
    assert snapshot.factory["craft_job"]["id"] == "receipt-415"

    # A stale sidecar actor identity must not extend the validated primary coal
    # estimate to other materials.
    original_command = native.command

    def wrong_capacity_actor(command):
        raw = original_command(command)
        marker = "JEV_ACTOR_RAW_CAPACITY|"
        rows = []
        for row in raw.splitlines():
            if row.startswith(marker):
                value = json.loads(row[len(marker):])
                value["actor_unit"] = 10
                row = marker + json.dumps(value, sort_keys=True, separators=(",", ":"))
            rows.append(row)
        return "\n".join(rows)

    native.command = wrong_capacity_actor
    second = native.observe(GameSnapshot(session_id="session-415", tick=10))
    assert second.factory["inventory_insertable"] == {"coal": 17}


@pytest.mark.parametrize("case,code", [
    ("disconnect", "actor_unavailable"),
    ("replacement", "actor_changed"),
    ("invalid_character", "actor_unavailable"),
])
def test_composed_actor_capacity_public_observation_emits_typed_paid_failure(case, code):
    from jev_factorio.state import GameSnapshot

    harness = LuaHarness()
    native = _actual_capacity_observer_for_harness(harness)
    harness.set_actor(case)
    with pytest.raises(CraftActorObservationFailure) as caught:
        native.observe(GameSnapshot(session_id="session-415", tick=10))

    assert caught.value.code == code
    assert caught.value.receipt == "receipt-415"
    assert len([row for row in harness.printed
                if row.startswith("JEV_CRAFT_OBSERVATION_FAILURE|")]) == 1
    assert not any(row.startswith("JEV_ACTOR_RAW_CAPACITY|") for row in harness.printed)
    job = harness.lua.globals().storage.campaign.craft_jobs.job
    assert job.id == "receipt-415" and job.status == "running"
    assert job.paid is True and job.finished == 0
    # The already-paid receipt consumed its iron input before observation.
    assert harness.lua.eval("player_state.get_item_count('iron-plate')") == 0
    assert harness.lua.eval("player_state.get_item_count('gear')") == 0


def test_composed_capacity_public_observation_does_not_promote_previous_observer_error():
    from jev_factorio.state import GameSnapshot

    harness = LuaHarness()
    native = _actual_capacity_observer_for_harness(harness)
    harness.lua.execute(
        "storage.campaign.observe=function() error('Fair play requires the original connected character') end")

    with pytest.raises(RuntimeError, match="Fair play requires the original connected character"):
        native.observe(GameSnapshot(session_id="session-415", tick=10))
    assert not any(row.startswith("JEV_CRAFT_OBSERVATION_FAILURE|") for row in harness.printed)


class _ComposedCapacityFailureBackend(ReceiptBackend):
    """Route a checkpoint observation through the real public Lua observer."""

    def __init__(self):
        super().__init__()
        self.harness = LuaHarness()
        self.native_observer = _actual_capacity_observer_for_harness(self.harness)
        self.fail_with_actual_observer = False

    def observe(self):
        if self.fail_with_actual_observer:
            self.fail_with_actual_observer = False
            from jev_factorio.state import GameSnapshot
            self.native_observer.observe(GameSnapshot(session_id="session-415", tick=10))
        return super().observe()


def test_composed_capacity_actor_failure_persists_matching_paid_receipt_and_reload(tmp_path):
    backend = _ComposedCapacityFailureBackend()
    loop = controller(backend, tmp_path)
    first = loop.step()
    assert first["background_job"] and loop.memory.pending is None
    job_before = json.loads(json.dumps(loop.memory.background_job))
    attempt_before = json.loads(json.dumps(loop.memory.background_attempt))
    receipt = job_before["parameters"]["receipt"]
    backend.harness.set_receipt(receipt)
    backend.harness.set_actor("disconnect")
    backend.fail_with_actual_observer = True

    with pytest.raises(CraftActorObservationFailure) as caught:
        loop.step()

    assert caught.value.code == "actor_unavailable" and caught.value.receipt == receipt
    assert loop.memory.status == "uncertain"
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    loaded = BackgroundMemory.load(tmp_path / "state.json", backend.state.session_id, loop.target)
    assert loaded.status == "uncertain"
    assert loaded.background_job == job_before and loaded.background_attempt == attempt_before
    assert loaded.pending is None and loaded.attempt_outcomes == []
    assert backend.harness.lua.globals().storage.campaign.craft_jobs.job.id == receipt
    assert backend.harness.lua.globals().storage.campaign.craft_jobs.job.paid is True
    calls_before = len(backend.calls)
    backend.complete()
    resumed = controller(backend, tmp_path, resume=True)
    result = resumed.step()
    assert result["status"] == "uncertain" and not result["verified"]
    assert resumed.memory.background_job == job_before
    assert resumed.memory.background_attempt == attempt_before
    assert resumed.memory.attempt_outcomes == []
    assert len(backend.calls) == calls_before


def test_composed_capacity_wrong_receipt_marker_cannot_rewrite_checkpoint(tmp_path):
    backend = _ComposedCapacityFailureBackend()
    loop = controller(backend, tmp_path)
    first = loop.step()
    assert first["background_job"] and loop.memory.pending is None
    job_before = json.loads(json.dumps(loop.memory.background_job))
    attempt_before = json.loads(json.dumps(loop.memory.background_attempt))
    durable_before = (tmp_path / "state.json").read_bytes()
    backend.harness.set_receipt(job_before["parameters"]["receipt"])
    backend.harness.set_actor("disconnect")
    original_command = backend.native_observer.command

    def wrong_receipt_command(command):
        raw = original_command(command)
        marker = "JEV_CRAFT_OBSERVATION_FAILURE|"
        rows = []
        for row in raw.splitlines():
            if row.startswith(marker):
                value = json.loads(row[len(marker):])
                value["receipt"] = "foreign-receipt"
                row = marker + json.dumps(value, sort_keys=True, separators=(",", ":"))
            rows.append(row)
        return "\n".join(rows)

    backend.native_observer.command = wrong_receipt_command
    backend.fail_with_actual_observer = True
    with pytest.raises(CraftActorObservationFailure) as caught:
        loop.step()

    assert caught.value.receipt == "foreign-receipt"
    assert loop.memory.status == "running"
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    assert (tmp_path / "state.json").read_bytes() == durable_before


@pytest.mark.parametrize("case", ["disconnect", "replacement", "invalid_character"])
def test_direct_observation_reports_paid_actor_failure_without_mutating_receipt(case):
    harness = LuaHarness()
    before = harness.direct_observe()
    assert before["ok"] and before["status"] == "running"
    harness.set_actor(case)
    result = harness.direct_observe()
    assert result["ok"] is True
    failure = _dict(result["value"]["craft_job_observation_failure"])
    expected = "actor_changed" if case == "replacement" else "actor_unavailable"
    assert failure == {"schema": 1, "receipt": "receipt-415", "code": expected}
    assert result["job_id"] == "receipt-415" and result["status"] == "running"
    assert result["paid"] is True and result["finished"] == 0
    assert result["plate"] == 0 and result["gear"] == 0


@pytest.mark.parametrize("case", ["disconnect", "replacement", "invalid_character"])
def test_atomic_observation_reports_paid_actor_failure_without_snapshot_or_mutation(case):
    harness = LuaHarness()
    result, output = harness.atomic_observe()
    assert result["ok"] is True
    assert any(row.startswith("JEV_SNAPSHOT|") for row in output)
    harness.set_actor(case)
    result, output = harness.atomic_observe()
    assert result["ok"] is True
    rows = [row for row in output if row.startswith("JEV_CRAFT_OBSERVATION_FAILURE|")]
    assert len(rows) == 1
    failure = json.loads(rows[0].removeprefix("JEV_CRAFT_OBSERVATION_FAILURE|"))
    expected = "actor_changed" if case == "replacement" else "actor_unavailable"
    assert failure == {"schema": 1, "receipt": "receipt-415", "code": expected}
    assert not any(row.startswith("JEV_SNAPSHOT|") for row in output)
    assert result["status"] == "running" and result["paid"] is True
    assert result["finished"] == 0 and result["plate"] == 0 and result["gear"] == 0


def test_previous_observer_failure_is_not_reclassified_as_actor_failure():
    harness = LuaHarness(previous_observer_failure=True)
    direct = harness.direct_observe()
    assert direct["ok"] is False and "previous observer failure" in direct["error"]
    result, output = harness.atomic_observe()
    assert result["ok"] is False and "previous observer failure" in result["error"]
    assert not any(row.startswith("JEV_CRAFT_OBSERVATION_FAILURE|") for row in output)
    assert result["status"] == "running" and result["paid"] is True


class OriginalActorFailureBackend(ReceiptBackend):
    def __init__(self):
        super().__init__()
        self.actor_failure = None
        self.actor_receipt = None
        self.actor_mode = "direct"
        self.lua_observer = LuaHarness()

    def fail_next_observation_for_actor(self, case, receipt, *, mode="direct"):
        self.actor_failure = case
        self.actor_receipt = receipt
        self.actor_mode = mode

    def observe(self):
        if self.actor_failure:
            case, self.actor_failure = self.actor_failure, None
            if case in {"previous observer failure", "previous observer actor phrase"}:
                message = ("Fair player binding changed" if case == "previous observer actor phrase"
                           else "previous observer failure")
                self.lua_observer = LuaHarness(previous_observer_failure=message)
                native = observed_factory_for_harness(self.lua_observer, coherent_version=2)
                return native.observe(self.state)
            self.lua_observer.set_receipt(self.actor_receipt)
            self.lua_observer.set_actor(case)
            version = 2 if self.actor_mode == "atomic" else 1
            native = observed_factory_for_harness(self.lua_observer, coherent_version=version)
            return native.observe(self.state)
        return super().observe()


def test_background_persists_actor_uncertainty_and_never_adopts_later_output(tmp_path):
    backend = OriginalActorFailureBackend()
    loop = controller(backend, tmp_path)
    first = loop.step()
    assert first["background_job"] and loop.memory.pending is None
    job_before = json.loads(json.dumps(loop.memory.background_job))
    attempt_before = json.loads(json.dumps(loop.memory.background_attempt))
    call_count = len(backend.calls)
    backend.fail_next_observation_for_actor(
        "disconnect", job_before["parameters"]["receipt"], mode="atomic")

    with pytest.raises(CraftActorObservationFailure) as caught:
        loop.step()

    assert caught.value.code == "actor_unavailable"
    assert caught.value.receipt == job_before["parameters"]["receipt"]
    assert loop.memory.status == "uncertain"
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    assert loop.memory.pending is None and loop.memory.attempt is None
    assert loop.memory.attempt_outcomes == []
    loaded = BackgroundMemory.load(
        tmp_path / "state.json", backend.state.session_id, loop.target)
    assert loaded.status == "uncertain" and loaded.background_job == job_before
    assert loaded.background_attempt == attempt_before and loaded.pending is None

    backend.complete()
    resumed = controller(backend, tmp_path, resume=True)
    result = resumed.step()
    assert result["status"] == "uncertain" and not result["verified"]
    assert resumed.memory.background_job == job_before
    assert resumed.memory.background_attempt == attempt_before
    assert resumed.memory.attempt_outcomes == []
    assert len(backend.calls) == call_count


def test_pending_paid_craft_actor_failure_preserves_write_ahead_and_never_admits(tmp_path, monkeypatch):
    backend = OriginalActorFailureBackend()
    delay_native_craft_start(backend, monkeypatch, corrupt=("queue_valid", False))
    loop = controller(backend, tmp_path)
    first = loop.step()
    assert first["background_job"] is None
    assert loop.memory.pending and loop.memory.pending["action"] == "factory_craft_job"
    pending_before = deepcopy(loop.memory.pending)
    attempt_before = deepcopy(loop.memory.attempt)
    plan_before = deepcopy(loop.memory.active_plan)
    reservations_before = deepcopy(loop.memory.reservations)
    receipt = plan_before["steps"][0]["parameters"]["receipt"]
    backend.fail_next_observation_for_actor("replacement", receipt, mode="direct")

    with pytest.raises(CraftActorObservationFailure) as caught:
        loop.step()

    assert caught.value.code == "actor_changed"
    assert caught.value.receipt == receipt
    assert loop.memory.status == "uncertain"
    assert loop.memory.pending == pending_before
    assert {key: value for key, value in loop.memory.attempt.items()
            if key != "observation_error"} == {
                key: value for key, value in attempt_before.items()
                if key != "observation_error"}
    assert loop.memory.attempt["observation_error"]["stage"] == "observe"
    assert loop.memory.active_plan == plan_before
    assert loop.memory.reservations == reservations_before
    assert loop.memory.background_job is None and loop.memory.background_attempt is None
    loaded = BackgroundMemory.load(
        tmp_path / "state.json", backend.state.session_id, loop.target)
    assert loaded.status == "uncertain" and loaded.pending == pending_before
    assert loaded.attempt == loop.memory.attempt
    assert loaded.active_plan == json.loads(json.dumps(plan_before))
    assert loaded.reservations == reservations_before
    calls_before = len(backend.calls)

    resumed = controller(backend, tmp_path, resume=True)
    result = resumed.step()
    assert result["status"] == "uncertain" and not result["verified"]
    assert resumed.memory.pending == pending_before and resumed.memory.attempt == loaded.attempt
    assert resumed.memory.background_job is None and resumed.memory.background_attempt is None
    assert len(backend.calls) == calls_before == 1


def test_previous_observer_failure_with_paid_job_is_not_reclassified_or_persisted(tmp_path):
    backend = OriginalActorFailureBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    job_before = json.loads(json.dumps(loop.memory.background_job))
    attempt_before = json.loads(json.dumps(loop.memory.background_attempt))
    durable_before = (tmp_path / "state.json").read_bytes()
    backend.actor_failure = "previous observer failure"

    with pytest.raises(RuntimeError, match="previous observer failure"):
        loop.step()

    assert loop.memory.status == "running"
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    assert (tmp_path / "state.json").read_bytes() == durable_before
    assert len(backend.calls) == 1


def test_prior_observer_error_containing_fair_actor_phrase_is_not_reclassified(tmp_path):
    backend = OriginalActorFailureBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    job_before = deepcopy(loop.memory.background_job)
    attempt_before = deepcopy(loop.memory.background_attempt)
    durable_before = (tmp_path / "state.json").read_bytes()
    backend.actor_failure = "previous observer actor phrase"

    with pytest.raises(RuntimeError, match="Fair player binding changed") as caught:
        loop.step()

    assert not isinstance(caught.value, CraftActorObservationFailure)
    assert loop.memory.status == "running"
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    assert (tmp_path / "state.json").read_bytes() == durable_before
    assert len(backend.calls) == 1


def test_mismatched_actor_failure_receipt_does_not_change_checkpoint(tmp_path):
    backend = OriginalActorFailureBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    job_before = deepcopy(loop.memory.background_job)
    attempt_before = deepcopy(loop.memory.background_attempt)
    durable_before = (tmp_path / "state.json").read_bytes()
    backend.fail_next_observation_for_actor(
        "disconnect", "different-receipt", mode="atomic")

    with pytest.raises(CraftActorObservationFailure) as caught:
        loop.step()

    assert caught.value.receipt == "different-receipt"
    assert loop.memory.status == "running"
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    assert (tmp_path / "state.json").read_bytes() == durable_before


def test_actor_uncertainty_checkpoint_save_failure_poison_retains_old_receipt(tmp_path, monkeypatch):
    backend = OriginalActorFailureBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    job_before = json.loads(json.dumps(loop.memory.background_job))
    attempt_before = json.loads(json.dumps(loop.memory.background_attempt))
    durable_before = (tmp_path / "state.json").read_bytes()
    call_count = len(backend.calls)
    backend.fail_next_observation_for_actor(
        "replacement", job_before["parameters"]["receipt"], mode="atomic")

    def fail_save(self, path):
        raise OSError("synthetic uncertainty checkpoint fsync failure")

    monkeypatch.setattr(BackgroundMemory, "save", fail_save)
    with pytest.raises(OSError, match="uncertainty checkpoint fsync failure"):
        loop.step()
    assert loop._save_poisoned is True
    assert (tmp_path / "state.json").read_bytes() == durable_before
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    observations = backend.observations
    with pytest.raises(RuntimeError, match="Checkpoint persistence failed"):
        loop.step()
    assert backend.observations == observations
    assert len(backend.calls) == call_count == 1


def test_actor_uncertainty_file_fsync_failure_keeps_previous_checkpoint_intact(tmp_path, monkeypatch):
    from jev_factorio import checkpoint_io

    backend = OriginalActorFailureBackend()
    loop = controller(backend, tmp_path)
    loop.step()
    job_before = json.loads(json.dumps(loop.memory.background_job))
    attempt_before = json.loads(json.dumps(loop.memory.background_attempt))
    durable_before = (tmp_path / "state.json").read_bytes()
    call_count = len(backend.calls)
    backend.fail_next_observation_for_actor(
        "disconnect", job_before["parameters"]["receipt"], mode="atomic")
    original_fsync = checkpoint_io.os.fsync
    file_syncs = []

    def fail_file_sync(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            file_syncs.append(fd)
            if len(file_syncs) == 1:
                raise OSError("synthetic uncertainty checkpoint file fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(checkpoint_io.os, "fsync", fail_file_sync)
    with pytest.raises(OSError, match="uncertainty checkpoint file fsync failure"):
        loop.step()

    assert len(file_syncs) == 1
    assert loop._save_poisoned is True
    assert loop.memory._checkpoint_metrics["status"] == "failed"
    assert (tmp_path / "state.json").read_bytes() == durable_before
    loaded = BackgroundMemory.load(
        tmp_path / "state.json", backend.state.session_id, loop.target)
    assert loaded.status == "running"
    assert loaded.reason == ""
    assert loaded.background_job == job_before
    assert loaded.background_attempt == attempt_before
    assert loaded.attempt_outcomes == []
    observations = backend.observations
    with pytest.raises(RuntimeError, match="Checkpoint persistence failed"):
        loop.step()
    assert backend.observations == observations
    assert len(backend.calls) == call_count == 1


@pytest.mark.parametrize("marker", [
    {"schema": True, "receipt": "receipt-415", "code": "actor_unavailable"},
    {"schema": 1, "receipt": "receipt-415", "code": "unknown"},
    {"schema": 1, "receipt": "", "code": "actor_unavailable"},
    {"schema": 1, "receipt": "receipt-415", "code": "actor_unavailable", "raw": "secret"},
])
def test_malformed_actor_failure_marker_is_not_trusted(marker):
    with pytest.raises(InvalidCraftEvidence):
        craft_actor_observation_failure(marker)


@pytest.mark.parametrize("case", ["disconnect", "replacement", "invalid_character"])
def test_observed_factory_atomic_public_path_decodes_actor_failure(case):
    from types import SimpleNamespace

    harness = LuaHarness()
    harness.set_receipt("receipt-415")
    harness.set_actor(case)
    native = observed_factory_for_harness(harness)

    with pytest.raises(CraftActorObservationFailure) as caught:
        native.observe(SimpleNamespace(session_id="session-415", tick=10))

    expected = "actor_changed" if case == "replacement" else "actor_unavailable"
    assert caught.value.code == expected and caught.value.receipt == "receipt-415"
    assert len(harness.printed) == 1
    assert harness.printed[0].startswith("JEV_CRAFT_OBSERVATION_FAILURE|")
    assert not any(row.startswith("JEV_SNAPSHOT|") for row in harness.printed)


def test_observed_factory_direct_public_path_decodes_lua_actor_marker():
    from types import SimpleNamespace

    harness = LuaHarness()
    harness.set_actor("replacement")
    native = observed_factory_for_harness(harness, coherent_version=1)

    with pytest.raises(CraftActorObservationFailure) as caught:
        native.observe(SimpleNamespace(session_id="session-415", tick=10))

    assert caught.value.code == "actor_changed"
    assert caught.value.receipt == "receipt-415"


def test_atomic_inner_observer_phrase_is_not_reclassified_by_public_path():
    from types import SimpleNamespace

    harness = LuaHarness()
    harness.lua.execute("storage.campaign.observation_snapshot_v2=function() "
                        "return storage.campaign.observe() end")
    harness.lua.execute("storage.campaign.observe=function() "
                        "error('Fair player binding changed') end")
    native = observed_factory_for_harness(harness)

    with pytest.raises(RuntimeError, match="Fair player binding changed") as caught:
        native.observe(SimpleNamespace(session_id="session-415", tick=10))

    assert not isinstance(caught.value, CraftActorObservationFailure)
    assert harness.printed == []


@pytest.mark.parametrize("raw", [
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":true,"receipt":"r","code":"actor_changed"}',
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"r","code":"unknown"}',
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"r","code":"actor_changed"}\nJEV_SNAPSHOT|{}',
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"r","code":"actor_changed"}\nnoise',
])
def test_public_marker_decoder_rejects_malformed_or_mixed_envelopes(raw):
    with pytest.raises(InvalidCraftEvidence):
        parse_craft_actor_observation_failure(raw)


def test_public_marker_decoder_binds_receipt_and_allows_one_exact_line():
    failure = parse_craft_actor_observation_failure(
        'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"receipt-415",'
        '"code":"actor_unavailable"}')
    assert isinstance(failure, CraftActorObservationFailure)
    assert failure.code == "actor_unavailable" and failure.receipt == "receipt-415"

    with pytest.raises(InvalidCraftEvidence):
        parse_craft_actor_observation_failure(
            'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"receipt-415",'
            '"code":"actor_unavailable"}\n'
            'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"receipt-415",'
            '"code":"actor_unavailable"}')


@pytest.mark.parametrize("code", [[], {}, None, 7], ids=["array", "object", "null", "number"])
def test_actor_failure_code_must_be_string_at_public_validation_boundaries(code):
    with pytest.raises(ValueError):
        CraftActorObservationFailure(code)

    marker = {"schema": 1, "receipt": "receipt-415", "code": code}
    with pytest.raises(InvalidCraftEvidence):
        craft_actor_observation_failure(marker)
    raw = "JEV_CRAFT_OBSERVATION_FAILURE|" + json.dumps(
        marker, separators=(",", ":"))
    with pytest.raises(InvalidCraftEvidence):
        parse_craft_actor_observation_failure(raw)


@pytest.mark.parametrize("coherent_version", [1, 2], ids=["direct-v1", "atomic-v2"])
@pytest.mark.parametrize("code", [[], {}, None, 7], ids=["array", "object", "null", "number"])
def test_observed_factory_rejects_nonstring_actor_codes_on_both_public_paths(
        coherent_version, code):
    from types import SimpleNamespace

    native = observed_factory_for_harness(
        LuaHarness(), coherent_version=coherent_version)
    raw = "JEV_CRAFT_OBSERVATION_FAILURE|" + json.dumps(
        {"schema": 1, "receipt": "receipt-415", "code": code},
        separators=(",", ":"))
    native.command = lambda _command: raw

    with pytest.raises(InvalidCraftEvidence):
        native.observe(SimpleNamespace(session_id="session-415", tick=10))


@pytest.mark.parametrize("coherent_version", [1, 2], ids=["direct-v1", "atomic-v2"])
@pytest.mark.parametrize("code", [[], {}, None, 7], ids=["array", "object", "null", "number"])
def test_malformed_actor_code_does_not_promote_paid_loop_checkpoint(
        tmp_path, coherent_version, code):
    from types import SimpleNamespace

    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    result = loop.step()
    assert result["background_job"]
    job_before = deepcopy(loop.memory.background_job)
    attempt_before = deepcopy(loop.memory.background_attempt)
    durable_before = (tmp_path / "state.json").read_bytes()
    calls_before = len(backend.calls)

    native = observed_factory_for_harness(
        LuaHarness(), coherent_version=coherent_version)
    raw = "JEV_CRAFT_OBSERVATION_FAILURE|" + json.dumps(
        {"schema": 1, "receipt": job_before["parameters"]["receipt"], "code": code},
        separators=(",", ":"))
    native.command = lambda _command: raw
    backend.observe = lambda: native.observe(SimpleNamespace(
        session_id=backend.state.session_id, tick=backend.state.tick))

    with pytest.raises(InvalidCraftEvidence):
        loop.step()

    assert loop.memory.status == "running"
    assert loop.memory.background_job == job_before
    assert loop.memory.background_attempt == attempt_before
    assert (tmp_path / "state.json").read_bytes() == durable_before
    assert len(backend.calls) == calls_before


@pytest.mark.parametrize("raw", [
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":true,"receipt":"receipt-415",'
    '"code":"actor_unavailable"}',
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"receipt-415",'
    '"code":"actor_unavailable"}\nJEV_SNAPSHOT|{}',
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"receipt-415",'
    '"code":"actor_unavailable"}\nJEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,'
    '"receipt":"receipt-415","code":"actor_unavailable"}',
    'JEV_CRAFT_OBSERVATION_FAILURE|{"schema":1,"receipt":"receipt-415",'
    '"code":"actor_unavailable","code":"actor_changed"}',
])
def test_observed_factory_public_decoder_rejects_bad_marker_responses(raw):
    from types import SimpleNamespace

    native = observed_factory_for_harness(LuaHarness())
    native.command = lambda _command: raw
    with pytest.raises(InvalidCraftEvidence):
        native.observe(SimpleNamespace(session_id="session-415", tick=10))


def test_craft_adapter_rejects_receipt_bound_native_failure_marker():
    from types import SimpleNamespace
    from jev_factorio.backends.craft_jobs import CraftJobFactory

    class MarkerNative:
        def observe(self, snapshot):
            snapshot.factory = {"craft_job_observation_failure": {
                "schema": 1, "receipt": "receipt-415", "code": "actor_changed"}}
            return snapshot

    adapter = object.__new__(CraftJobFactory)
    adapter.native = MarkerNative()
    with pytest.raises(CraftActorObservationFailure) as caught:
        adapter.observe(SimpleNamespace(factory={}))
    assert caught.value.receipt == "receipt-415" and caught.value.code == "actor_changed"


@pytest.mark.parametrize("case", ["disconnect", "replacement", "invalid_character"])
def test_unpaid_or_untracked_lua_job_does_not_emit_failure_marker(case):
    harness = LuaHarness()
    harness.lua.execute("storage.campaign.craft_jobs.job.paid=false")
    harness.set_actor(case)
    direct = harness.direct_observe()
    assert direct["ok"] is False
    assert "craft_job_observation_failure" not in str(direct["error"])
    atomic, output = harness.atomic_observe()
    assert atomic["ok"] is False
    assert not any(row.startswith("JEV_CRAFT_OBSERVATION_FAILURE|") for row in output)


class _UnhashableActorCode(str):
    __hash__ = None


def test_actor_failure_constructor_rejects_unhashable_string_subclass():
    with pytest.raises(ValueError, match="Unknown craft actor observation failure"):
        CraftActorObservationFailure(_UnhashableActorCode("actor_changed"))


def test_structured_actor_failure_parser_rejects_unhashable_string_subclass():
    marker = {"schema": 1, "receipt": "receipt-415",
              "code": _UnhashableActorCode("actor_changed")}
    with pytest.raises(InvalidCraftEvidence, match="Invalid craft actor observation marker"):
        craft_actor_observation_failure(marker)
