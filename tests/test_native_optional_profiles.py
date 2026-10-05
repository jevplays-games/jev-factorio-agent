"""Exercise qualification against the bundled optional Lua installers."""
from __future__ import annotations

import json
import sys
from importlib.resources import files

import pytest

from jev_factorio.backends.native_attachment import (
    PROBE, _installer_scripts, prepare_install_command, readback,
)
from jev_factorio.backends.native_current_attachment import (
    current_connector_snapshot_command,
    source_bound_direct_profile,
)
from lupa.lua52 import LuaRuntime, lua_type


SESSION = "offline-optional-profile-session"
ACTOR = 17
ROOT = files("jev_factorio").joinpath("lua")
TARGETS = ["utility:boiler", "recipe:copper-plate"]
SOLID_INTENTS = [
    {"source": f"coal:{target}:chest", "target": target,
     "item": "coal", "destination": "fuel"}
    for target in TARGETS
]


def _from_lua(value):
    if lua_type(value) != "table":
        return value
    pairs = list(value.items())
    if all(type(key) is int and key >= 1 for key, _ in pairs):
        ordered = sorted(pairs)
        if [key for key, _ in ordered] == list(range(1, len(ordered) + 1)):
            return [_from_lua(item) for _, item in ordered]
    return {key: _from_lua(item) for key, item in pairs}


def _runtime():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(r'''
        local event_handlers, nth_handlers = {}, {}
        script = {active_mods={base="2.0.77",core="2.0.77"}}
        function script.on_event(event, handler)
            event_handlers[event] = handler
        end
        function script.get_event_handler(event) return event_handlers[event] end
        function script.on_nth_tick(tick, handler) nth_handlers[tick] = handler end
        defines = {
            events={on_tick=1,on_script_path_request_finished=2,on_player_mined_entity=3,
                on_pre_player_crafted_item=4,on_player_cancelled_crafting=5,
                on_player_crafted_item=6},
            controllers={character=1},
            direction={north=0,east=4,south=8,west=12,northeast=2,southeast=6,
                southwest=10,northwest=14},
            build_check_type={manual=1},
            inventory={fuel=1,assembling_machine_input=2,assembling_machine_output=3,
                furnace_source=4,furnace_result=5,lab_input=6,chest=7,
                character_main=8,cargo_landing_pad_main=9,rocket_silo_rocket=10},
            rocket_silo_status={rocket_ready=1},entity_status={}
        }
        local force={index=1,rockets_launched=0,technologies={}}
        function force.get_item_production_statistics()
            return {get_input_count=function() return 0 end}
        end
        local surface={index=1}
        function surface.find_entities_filtered() return {} end
        local actor={valid=true,unit_number=17,force=force,surface=surface}
        local player={index=1,connected=true,character=actor,force=force,surface=surface,
                      cheat_mode=false,position={x=0,y=0},crafting_queue_size=0}
        function player.get_main_inventory()
            return {get_contents=function() return {} end}
        end
        game={speed=1,tick_paused=false,tick=1234,
              get_player=function(index) if index==1 then return player end end}
        prototypes={item={}}
        jev_fle_runtime={jev_session_id="offline-optional-profile-session",
                         jev_bound_player_index=1,agent_characters={[1]=actor}}
        storage=jev_fle_runtime
        local function copy(value)
            if type(value) ~= "table" then return value end
            local result={};for key,item in pairs(value) do result[copy(key)]=copy(item) end
            return result
        end
        helpers={json_to_table=function(raw) return copy(require_json_to_table(raw)) end}
    ''')

    def json_to_lua(raw):
        return lua.table_from(json.loads(raw), recursive=True)

    def json_encode(value):
        return json.dumps(_from_lua(value), sort_keys=True, separators=(",", ":"))

    output = []
    lua.globals().require_json_to_table = json_to_lua
    lua.globals().helpers.table_to_json = json_encode
    lua.globals().rcon = lua.table_from({"print": output.append})
    return lua, output


def _asset_text(name):
    if name == "launch_readiness":
        return "do\n" + ROOT.joinpath("launch_readiness.lua").read_text() + "\nend"
    if name == "input_routes":
        return "\n".join(
            "do\n" + ROOT.joinpath(part).read_text() + "\nend"
            for part in ("input_routes.lua", "production_sites.lua"))
    if name == "observation_v2":
        from jev_factorio.backends.native_attachment import WATER_ORIGIN_OBSERVATION_ASSET
        return ROOT.joinpath(WATER_ORIGIN_OBSERVATION_ASSET).read_text()
    return ROOT.joinpath(name + ".lua").read_text()


def _install(lua, name):
    source = _asset_text(name)
    prepared = prepare_install_command(source)
    assert prepared.startswith(source + "\n") or prepared == source, name
    lua.execute(prepared)


def _case(*, observations=False, craft=False, buffers=False, inputs=False,
          outposts=False, solid=False, coal=False):
    lua, output = _runtime()
    _install(lua, "fair_actions")
    lua.execute("jev_fle_runtime.fair.bind()")
    _install(lua, "factory")
    _install(lua, "connector_ownership")
    _install(lua, "launch_readiness")
    if observations:
        _install(lua, "observation")
        _install(lua, "observation_v2")
    if craft:
        _install(lua, "craft_jobs")
    if buffers:
        _install(lua, "output_buffers")
    if inputs:
        _install(lua, "input_routes")
    if outposts:
        _install(lua, "mining_outposts")
    if solid:
        _install(lua, "solid_routes")
        payload = SOLID_INTENTS if coal else [{
            "source": "recipe:iron-plate", "target": "utility:boiler",
            "item": "iron-plate", "destination": "input",
        }]
        lua.execute("jev_fle_runtime.campaign.set_solid_intents(helpers.json_to_table(" +
                    json.dumps(json.dumps(payload, separators=(",", ":"))) + "))")
    if coal:
        _install(lua, "coal_supply")
        lua.execute("jev_fle_runtime.campaign.set_coal_targets(helpers.json_to_table(" +
                    json.dumps(json.dumps(TARGETS, separators=(",", ":"))) + "))")
    return lua, output


def _probe(case):
    lua, output = _case(**case)
    output.clear()
    lua.execute(PROBE)
    assert len(output) == 1
    value = output.pop()
    return json.loads(value) if isinstance(value, str) else value


class _ReadbackClient:
    def __init__(self, lua, output, *, execute_snapshot):
        self.lua = lua
        self.output = output
        self.execute_snapshot = execute_snapshot
        self.commands = []
        self.row = None

    def send_command(self, command):
        self.commands.append(command)
        self.output.clear()
        if len(self.commands) == 1:
            assert command == "/sc " + PROBE
            self.lua.execute(PROBE)
            raw = self.output.pop()
            self.row = json.loads(raw) if isinstance(raw, str) else raw
            return json.dumps(self.row)
        assert len(self.commands) == 2
        assert command == current_connector_snapshot_command(self.row)
        if self.execute_snapshot:
            self.lua.execute(command.removeprefix("/sc "))
            return self.output.pop()
        return json.dumps({
            "schema": 1,
            "session_id": self.row["session_id"],
            "actor_unit": self.row["actor_unit"],
            "tick": 1234,
            "connector_ownership": {
                "protocol": 1, "session_id": self.row["session_id"],
                "tick": 1234, "routes": {},
            },
        })


@pytest.mark.parametrize("case", [
    pytest.param({}, id="default-without-observation"),
    pytest.param({"observations": True}, id="observation-only"),
    pytest.param({"buffers": True}, id="buffers-without-observation-or-background"),
    pytest.param({"observations": True, "buffers": True}, id="buffers-without-background"),
    pytest.param({"craft": True, "buffers": True}, id="background-buffers-without-observation"),
    pytest.param({"observations": True, "craft": True, "buffers": True},
                 id="background-buffers-with-observation"),
    pytest.param({"solid": True}, id="minimal-solid-without-observation"),
    pytest.param({"observations": True, "solid": True}, id="minimal-solid"),
    pytest.param({"solid": True, "coal": True}, id="minimal-solid-and-coal-without-observation"),
    pytest.param({"observations": True, "solid": True, "coal": True}, id="minimal-solid-and-coal"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True}, id="existing-current-module-profile"),
])
def test_actual_bundled_optional_profile_has_a_qualified_installed_callback_chain(case):
    row = _probe(case)
    assert row["qualified"] is True
    assert row["session_id"] == SESSION
    assert row["actor_unit"] == ACTOR
    assert row["native_installation"]["session_id"] == SESSION
    assert row["native_installation"]["actor_unit"] == ACTOR


def test_actual_optional_callback_replacement_is_rejected():
    lua, output = _case(observations=True, buffers=True)
    lua.execute("jev_fle_runtime.output_buffers.previous_observe=function() end")
    output.clear()
    lua.execute(PROBE)
    assert json.loads(output.pop())["qualified"] is False


def test_actual_observation_callback_replacement_is_rejected():
    lua, output = _case(observations=True)
    lua.execute("jev_fle_runtime.campaign.observation_snapshot_v2=function() end")
    output.clear()
    lua.execute(PROBE)
    assert json.loads(output.pop())["qualified"] is False


def test_actual_solid_callback_replacement_is_rejected():
    lua, output = _case(observations=True, solid=True)
    lua.execute("jev_fle_runtime.solid_routes.observer=function() end")
    output.clear()
    lua.execute(PROBE)
    assert json.loads(output.pop())["qualified"] is False


@pytest.mark.parametrize("case", [
    pytest.param({}, id="default"),
    pytest.param({"observations": True}, id="observation-only"),
    pytest.param({"buffers": True}, id="buffers-without-observation-or-background"),
    pytest.param({"observations": True, "buffers": True}, id="buffers-without-background"),
    pytest.param({"craft": True, "buffers": True},
                 id="background-work-with-buffers"),
    pytest.param({"observations": True, "craft": True, "buffers": True},
                 id="background-work-with-buffers-and-observations"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True}, id="existing-current-full-profile"),
])
def test_exact_non_solid_source_profile_completes_public_readback(case):
    lua, output = _case(**case)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    attached = readback(client)
    assert len(client.commands) == 2
    assert attached["connector_snapshot_qualified"] is True
    assert attached["connector_snapshot_ownership"]["routes"] == {}
    profile = source_bound_direct_profile(client.row)
    assert profile in {
        "default", "observation_only", "buffers_without_background",
        "buffers_without_background_with_observations", "current_full",
        "background_work_with_buffers", "background_work_with_buffers_and_observations",
    }
    if profile in {"buffers_without_background", "buffers_without_background_with_observations",
                   "background_work_with_buffers", "background_work_with_buffers_and_observations"}:
        assert "c.observe()" not in client.commands[1]
    assert "c.observe()" not in client.commands[1]


def test_background_work_with_output_buffers_has_standalone_public_readback():
    # BackgroundWorkLoop is a Python controller wrapper: it introduces no Lua
    # installer asset. Exercise its actual production composition independently
    # from input routes/outposts, then run the same public native readback path.
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type

    background_with_buffers = buffered_loop_type(BackgroundWorkLoop)
    assert issubclass(background_with_buffers, BackgroundWorkLoop)
    lua, output = _case(craft=True, buffers=True)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    attached = readback(client)
    assert source_bound_direct_profile(client.row) == "background_work_with_buffers"
    assert attached["connector_snapshot_qualified"] is True
    assert attached["connector_snapshot_ownership"]["routes"] == {}
    assert "c.observe()" not in client.commands[1]


def test_live_cli_selects_background_and_buffer_composition_without_starting_a_world(
        monkeypatch, tmp_path):
    # Exercise the real CLI parser/selection path while replacing only the
    # backend and loop execution. Native installer/readback behavior is covered
    # independently above against the actual bundled Lua assets.
    from jev_factorio import controller, main

    selected = []

    class BackendStub:
        craft_jobs_supported = True
        output_buffers_supported = True

    def capture_loop(self, *args, **kwargs):
        selected.append(type(self).__mro__)
        self.catalog = object()

    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: BackendStub())
    monkeypatch.setattr(controller.HierarchicalLoop, "__init__", capture_loop)
    monkeypatch.setattr(controller.HierarchicalLoop, "run", lambda self, **kwargs: None)
    monkeypatch.setattr(sys, "argv", [
        "jev-factorio", "--backend", "fle", "--controller", "hierarchical",
        "--policy", "deterministic", "--target", "iron_smelting",
        "--factory-scheduling", "ready-work", "--background-work",
        "--furnace-output-buffers", "--checkpoint", str(tmp_path / "controller.json"),
        "--tick-seconds", "1", "--steps", "0",
    ])

    main.cli()

    assert selected
    names = {kind.__name__ for kind in selected[0]}
    assert "BackgroundWorkLoop" in names
    assert "OutputBufferLoop" in names


@pytest.mark.parametrize("change", ["paid_cell", "pending_cell", "offer"])
def test_buffer_readback_preserves_existing_owner_state(change):
    lua, output = _case(craft=True, buffers=True)
    if change == "paid_cell":
        lua.execute("jev_fle_runtime.output_buffers.cells.retained={parts={chest={paid=1,receipt='paid'}}}")
    elif change == "pending_cell":
        lua.execute("jev_fle_runtime.output_buffers.cells.retained={parts={},pending={phase='prepared'}}")
    elif change == "offer":
        lua.execute("jev_fle_runtime.output_buffers.offers.retained={parts={},layout='output:stale'}")
    retained = ("helpers.table_to_json({cells=jev_fle_runtime.output_buffers.cells,"
                "offers=jev_fle_runtime.output_buffers.offers})")
    before = lua.eval(retained)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert not output
    assert lua.eval(retained) == before


@pytest.mark.parametrize(("job", "queue"), [
    pytest.param({"status": "running", "paid": True, "accepted": 1}, 1,
                 id="paid-running-job"),
    pytest.param({"status": "completed", "paid": True, "finished": 1}, 0,
                 id="retained-completed-job"),
    pytest.param({"status": "invalid", "paid": True, "error": "queue_mismatch"}, 0,
                 id="ambiguous-invalid-job"),
])
def test_background_direct_readback_preserves_paid_craft_job_for_reconciliation(job, queue):
    lua, output = _case(craft=True, buffers=True)
    lua.globals().jev_fle_runtime.campaign.craft_jobs.job = lua.table_from(job)
    lua.globals().game.get_player(1).crafting_queue_size = queue
    retained = ("helpers.table_to_json({job=jev_fle_runtime.campaign.craft_jobs.job,"
                "queue=game.get_player(1).crafting_queue_size})")
    before = lua.eval(retained)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert not output
    assert lua.eval(retained) == before


def test_background_direct_readback_rejects_untracked_native_crafting_queue():
    lua, output = _case(observations=True, craft=True, buffers=True)
    lua.globals().game.get_player(1).crafting_queue_size = 1
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert lua.globals().game.get_player(1).crafting_queue_size == 1


@pytest.mark.parametrize("case", [
    pytest.param({"observations": True, "solid": True}, id="minimal-solid"),
    pytest.param({"solid": True}, id="minimal-solid-without-observation"),
    pytest.param({"solid": True, "coal": True}, id="minimal-solid-and-coal-without-observation"),
    pytest.param({"observations": True, "solid": True, "coal": True}, id="minimal-solid-and-coal"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True, "solid": True, "coal": True},
                 id="existing-prerequisites-solid-and-coal"),
])
def test_solid_readback_uses_non_mutating_source_bound_owner_query(case):
    lua, output = _case(**case)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    attached = readback(client)
    assert len(client.commands) == 2
    assert attached["connector_snapshot_qualified"] is True
    assert "c.observe()" not in client.commands[1]
    assert lua.eval("next(jev_fle_runtime.solid_routes.cells)==nil") is True
    if case.get("coal"):
        assert lua.eval("not jev_fle_runtime.coal_supply.committed") is True


@pytest.mark.parametrize("change", [
    "solid_cell", "solid_offer_paid", "solid_offer_pending", "solid_offer_fault",
    "solid_offer_committed", "solid_offer_manual", "coal_committed", "coal_pending_part",
    "coal_paid_part", "coal_manual_pending", "coal_manual_receipt", "coal_manual_total",
    "coal_fault", "coal_row_committed", "connector_active", "connector_route",
])
def test_solid_readback_preserves_paid_or_pending_owner_state(change):
    case = {"solid": True, "coal": change.startswith("coal_")}
    lua, output = _case(**case)
    if change == "solid_cell":
        lua.execute("jev_fle_runtime.solid_routes.cells.retained={}")
    elif change == "solid_offer_paid":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={paid={}}}")
    elif change == "solid_offer_pending":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},pending={phase='prepared'}}")
    elif change == "solid_offer_fault":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},fault='receipt_reconciliation_failed'}")
    elif change == "solid_offer_committed":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},committed=true}")
    elif change == "solid_offer_manual":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},manual_pending={receipt='pending'}}")
    elif change == "coal_committed":
        lua.execute("jev_fle_runtime.coal_supply.committed=true")
    elif change == "coal_pending_part":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},pending={phase='prepared'},manual_receipts={},manual_total=0}")
    elif change == "coal_paid_part":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={chest={receipt='paid'}},manual_receipts={},manual_total=0}")
    elif change == "coal_manual_pending":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_pending={receipt='pending'},manual_receipts={},manual_total=0}")
    elif change == "coal_manual_receipt":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={paid=1},manual_total=1}")
    elif change == "coal_manual_total":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={},manual_total=1}")
    elif change == "coal_fault":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={},manual_total=0,fault='manual_transfer_ambiguous'}")
    elif change == "coal_row_committed":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={},manual_total=0,committed=true}")
    elif change == "connector_active":
        lua.execute("jev_fle_runtime.campaign.connector_ledger.active='retained'")
    elif change == "connector_route":
        lua.execute("jev_fle_runtime.campaign.connector_ledger.routes.retained={}")
    retained_state = (
        "helpers.table_to_json({solid_cells=jev_fle_runtime.solid_routes "
        "and jev_fle_runtime.solid_routes.cells or {},solid_offers=jev_fle_runtime.solid_routes "
        "and jev_fle_runtime.solid_routes.offers or {},coal_committed=jev_fle_runtime.coal_supply "
        "and jev_fle_runtime.coal_supply.committed or false,coal_rows=jev_fle_runtime.coal_supply "
        "and jev_fle_runtime.coal_supply.rows or {},connector_active=jev_fle_runtime.campaign "
        "and jev_fle_runtime.campaign.connector_ledger.active or false,connector_routes="
        "jev_fle_runtime.campaign.connector_ledger.routes or {}})"
    )
    before = lua.eval(retained_state)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert not output
    assert lua.eval(retained_state) == before


@pytest.mark.parametrize(("case", "expected"), [
    pytest.param({}, "default", id="default"),
    pytest.param({"observations": True}, "observation_only", id="observation-only"),
    pytest.param({"buffers": True}, "buffers_without_background", id="buffers-without-observation"),
    pytest.param({"observations": True, "buffers": True},
                 "buffers_without_background_with_observations", id="buffers-with-background-observation"),
    pytest.param({"craft": True, "buffers": True},
                 "background_work_with_buffers", id="background-work-with-buffers"),
    pytest.param({"observations": True, "craft": True, "buffers": True},
                 "background_work_with_buffers_and_observations", id="background-work-with-buffers-and-observations"),
    pytest.param({"observations": True, "solid": True}, "solid_only_with_observations", id="solid-only-with-observations"),
    pytest.param({"solid": True}, "solid_only", id="solid-only-without-observation"),
    pytest.param({"observations": True, "solid": True}, "solid_only_with_observations", id="solid-only-with-observations"),
    pytest.param({"observations": True, "solid": True, "coal": True},
                 "solid_and_coal_with_observations", id="solid-and-coal-with-observations"),
    pytest.param({"solid": True, "coal": True}, "solid_and_coal", id="solid-and-coal-without-observation"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True}, "current_full", id="current-full"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True, "solid": True, "coal": True},
                 "solid_and_coal_with_prerequisites", id="solid-coal-with-prerequisites"),
])
def test_only_exact_bundled_installer_module_profiles_are_directly_eligible(case, expected):
    row = _probe(case)
    assert row["qualified"] is True
    assert source_bound_direct_profile(row) == expected


def test_unlisted_optional_combinations_remain_bridge_or_reconciliation_gated():
    row = _probe({"observations": True, "craft": True})
    assert row["qualified"] is True
    assert source_bound_direct_profile(row) is None

    current = _probe({"observations": True, "craft": True, "buffers": True,
                      "inputs": True, "outposts": True})
    current["modules"]["mining_outposts"] = False
    assert source_bound_direct_profile(current) is None


def test_installer_inventory_is_bounded_to_supported_manifests():
    manifests = list(_installer_scripts().values())
    assert ("factory",) in manifests
    assert ("solid_routes",) in manifests


_FULL_OWNER_CASES = [
    pytest.param(False, id="current-full"),
    pytest.param(True, id="solid-coal-with-prerequisites"),
]

_RETAINED_OWNER_FIXTURES = {
    "output_paid_offer": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19001}
        c.entities['output-chest:19001']=e
        jev_fle_runtime.output_buffers.offers['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:17:retained',state='building',parts={chest={entity=e,
            role='output-chest:19001',unit_number=19001,receipt='paid-output',paid=1}}}
    """,
    "output_paid_cell": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19002}
        c.entities['output-arm:19002']=e
        jev_fle_runtime.output_buffers.cells['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:17:retained',state='building',parts={inserter={entity=e,
            role='output-arm:19002',unit_number=19002,receipt='paid-output-cell',paid=1}}}
    """,
    "output_stale_offer": """
        jev_fle_runtime.output_buffers.offers['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:stale',state='proposed',parts={}}
    """,
    "output_fault_cell": """
        jev_fle_runtime.output_buffers.cells['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:retained',fault='buffer_identity_or_handler_changed',parts={}}
    """,
    "input_paid_cell": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19003}
        c.entities['input:recipe:iron-plate:drill']=e
        jev_fle_runtime.input_routes.cells['recipe:iron-plate']={source='recipe:iron-plate',
            parts={drill={entity=e,role='input:recipe:iron-plate:drill',unit_number=19003,
            receipt='paid-input',paid=1}}}
    """,
    "input_stale_offer": """
        jev_fle_runtime.input_routes.offers['recipe:iron-plate']={source='recipe:iron-plate',
            layout='input:stale',parts={}}
    """,
    "input_fault_cell": """
        jev_fle_runtime.input_routes.cells['recipe:iron-plate']={source='recipe:iron-plate',
            layout='input:retained',fault='input_identity_changed',parts={}}
    """,
    "outpost_paid_cell": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19004}
        c.entities['outpost:iron-ore:drill']=e
        jev_fle_runtime.mining_outposts.cells['iron-ore']={resource='iron-ore',
            layout='outpost:iron-ore:retained',parts={drill={entity=e,
            role='outpost:iron-ore:drill',unit_number=19004,receipt='paid-outpost',paid=1}}}
        jev_fle_runtime.mining_outposts.receipts['paid-outpost']=19004
    """,
    "outpost_stale_offer": """
        jev_fle_runtime.mining_outposts.offers['iron-ore']={resource='iron-ore',
            layout='outpost:iron-ore:stale',parts={}}
    """,
    "outpost_fault_cell": """
        jev_fle_runtime.mining_outposts.cells['iron-ore']={resource='iron-ore',
            layout='outpost:iron-ore:retained',fault='outpost_identity_topology_or_flow_mismatch',parts={}}
    """,
    "outpost_receipt": """
        jev_fle_runtime.mining_outposts.receipts['already-paid-outpost']=19005
    """,
    "production_owned": """
        local c=jev_fle_runtime.campaign
        local e={valid=true,name='stone-furnace',unit_number=19006,position={x=4.5,y=5.5}}
        c.entities['recipe:iron-plate']=e
        jev_fle_runtime.production_sites.owned['recipe:iron-plate']={
            role='recipe:iron-plate',entity=e,source_unit=19006,
            position={x=4.5,y=5.5},specs={}}
    """,
    "production_stale_offer": """
        jev_fle_runtime.production_sites.offers['recipe:iron-plate']={
            role='recipe:iron-plate',anchor='cell-site:iron-ore:stale',specs={}}
    """,
    "orphan_output_role": """
        jev_fle_runtime.campaign.entities['output-chest:19007']={valid=true,unit_number=19007}
    """,
    "orphan_input_role": """
        jev_fle_runtime.campaign.entities['input:recipe:iron-plate:drill']={valid=true,unit_number=19008}
    """,
    "orphan_outpost_role": """
        jev_fle_runtime.campaign.entities['outpost:iron-ore:drill']={valid=true,unit_number=19009}
    """,
    "paid_craft_job": """
        jev_fle_runtime.campaign.craft_jobs.job={status='running',paid=true,accepted=1}
        game.get_player(1).crafting_queue_size=1
    """,
    "untracked_crafting_queue": "game.get_player(1).crafting_queue_size=1",
    "connector_active": "jev_fle_runtime.campaign.connector_ledger.active='retained'",
    "connector_route": "jev_fle_runtime.campaign.connector_ledger.routes.retained={state='building',paid=1}",
}

_SOLID_COAL_OWNER_FIXTURES = {
    "solid_stale_offer": """
        jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={}}
    """,
    "solid_pending": "jev_fle_runtime.solid_routes.pending={receipt='pending'}",
    "coal_pending": "jev_fle_runtime.coal_supply.pending={receipt='pending'}",
    "coal_fault": "jev_fle_runtime.coal_supply.fault='ambiguous_transfer'",
}


def _owner_state_json(lua):
    return lua.eval("""
        helpers.table_to_json({
            output_cells=jev_fle_runtime.output_buffers.cells,
            output_offers=jev_fle_runtime.output_buffers.offers,
            input_cells=jev_fle_runtime.input_routes.cells,
            input_offers=jev_fle_runtime.input_routes.offers,
            outpost_cells=jev_fle_runtime.mining_outposts.cells,
            outpost_offers=jev_fle_runtime.mining_outposts.offers,
            outpost_receipts=jev_fle_runtime.mining_outposts.receipts,
            production_owned=jev_fle_runtime.production_sites.owned,
            production_offers=jev_fle_runtime.production_sites.offers,
            solid_cells=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.cells or {},
            solid_offers=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.offers or {},
            solid_pending=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.pending or false,
            solid_fault=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.fault or false,
            coal_rows=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.rows or {},
            coal_pending=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.pending or false,
            coal_fault=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.fault or false,
            coal_committed=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.committed or false,
            craft_job=jev_fle_runtime.campaign.craft_jobs.job or false,
            crafting_queue=game.get_player(1).crafting_queue_size,
            connector_active=jev_fle_runtime.campaign.connector_ledger.active or false,
            connector_routes=jev_fle_runtime.campaign.connector_ledger.routes,
            entity_owners={
                output=jev_fle_runtime.campaign.entities['output-chest:19001'] or false,
                output_arm=jev_fle_runtime.campaign.entities['output-arm:19002'] or false,
                orphan_output=jev_fle_runtime.campaign.entities['output-chest:19007'] or false,
                input=jev_fle_runtime.campaign.entities['input:recipe:iron-plate:drill'] or false,
                outpost=jev_fle_runtime.campaign.entities['outpost:iron-ore:drill'] or false,
                production_site_recipe=jev_fle_runtime.campaign.entities['recipe:iron-plate'] or false,
            },
        })
    """)


@pytest.mark.parametrize("full_solid_coal", _FULL_OWNER_CASES)
@pytest.mark.parametrize("fixture", sorted(_RETAINED_OWNER_FIXTURES))
def test_full_direct_profiles_reject_retained_module_owners_before_readback_mutation(
        full_solid_coal, fixture):
    case = {"observations": True, "craft": True, "buffers": True,
            "inputs": True, "outposts": True}
    if full_solid_coal:
        case.update(solid=True, coal=True)
    lua, output = _case(**case)
    lua.execute(_RETAINED_OWNER_FIXTURES[fixture])
    before = _owner_state_json(lua)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    with pytest.raises(Exception):
        readback(client)

    assert len(client.commands) == 2
    assert "c.observe()" not in client.commands[1]
    assert not output
    assert _owner_state_json(lua) == before


@pytest.mark.parametrize("fixture", sorted(_SOLID_COAL_OWNER_FIXTURES))
def test_full_solid_coal_profile_rejects_all_retained_route_state_before_readback_mutation(
        fixture):
    lua, output = _case(observations=True, craft=True, buffers=True, inputs=True,
                        outposts=True, solid=True, coal=True)
    lua.execute(_SOLID_COAL_OWNER_FIXTURES[fixture])
    before = _owner_state_json(lua)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    with pytest.raises(Exception, match="assertion failed"):
        readback(client)

    assert len(client.commands) == 2
    assert not output
    assert _owner_state_json(lua) == before


def test_full_solid_coal_prerequisite_profile_has_empty_owner_public_readback():
    lua, output = _case(observations=True, craft=True, buffers=True, inputs=True,
                        outposts=True, solid=True, coal=True)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    attached = readback(client)

    assert source_bound_direct_profile(client.row) == "solid_and_coal_with_prerequisites"
    assert attached["connector_snapshot_qualified"] is True
    assert attached["connector_snapshot_ownership"]["routes"] == {}
    assert "c.observe()" not in client.commands[1]


def test_current_full_keeps_ordinary_recipe_role_eligible_without_site_owner():
    # production_sites.lua:210-212 classifies an entity under recipe:* as an
    # existing manual cell when sites.owned has no corresponding entry. The
    # direct connector query must leave that ordinary factory role untouched.
    lua, output = _case(observations=True, craft=True, buffers=True,
                        inputs=True, outposts=True)
    lua.execute("""
        local e={valid=true,name='stone-furnace',unit_number=19010,
            position={x=4.5,y=5.5}}
        jev_fle_runtime.campaign.entities['recipe:iron-plate']=e
    """)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    attached = readback(client)

    assert source_bound_direct_profile(client.row) == "current_full"
    assert attached["connector_snapshot_qualified"] is True
    assert lua.eval("jev_fle_runtime.campaign.entities['recipe:iron-plate'].unit_number") == 19010
    assert lua.eval("next(jev_fle_runtime.production_sites.owned)==nil") is True
    assert "c.observe()" not in client.commands[1]
