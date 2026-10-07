"""Crash-window regressions for the launch payload receipt contract."""
from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace

import pytest

from jev_factorio import launch_readiness as contract
from jev_factorio.factory_contract import allowed
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.planning.factory import compile_factory
from test_launch_readiness import lua_case, scenario


def _load_attempt(row, receipt="load:100"):
    row["attempts"]["load"] = {
        "receipt": receipt,
        "silo_unit": row["silo"]["unit_number"],
        "rocket_unit": row["silo"]["rocket_unit"],
        "item": "raw-fish",
        "tick": row["tick"],
    }


def _load_receipt(row, receipt="load:100"):
    row["receipts"][receipt] = {
        "kind": "load",
        "session_id": row["session_id"],
        "actor_unit": row["actor_unit"],
        "tick": row["tick"],
        "item": "raw-fish",
        "quantity": 1,
        "silo_unit": row["silo"]["unit_number"],
        "rocket_unit": row["silo"]["rocket_unit"],
    }


def _to_python(value):
    from lupa import lua_type

    if lua_type(value) == "table":
        return {_to_python(key): _to_python(item) for key, item in value.items()}
    return value


class _LuaBackendAdapter:
    """Exercise NativeFactory.execute while keeping all game calls in Lupa."""

    def __init__(self, lua):
        self.lua = lua
        self.backend = SimpleNamespace(_tools={})
        self.approaches = []
        self.launch_calls = []

    def approach_role(self, role):
        self.approaches.append(role)

    def require_launch_reconciliation(self):
        # The adapter is paired with the current candidate Lua module above.
        return None

    def call(self, name, *args):
        assert name == "launch"
        self.launch_calls.append(args[0])
        return self.lua.globals().launch_from_adapter(args[0])


def _adapter_launch(lua):
    from jev_factorio.backends.native_factory import NativeFactory

    lua.execute("function launch_from_adapter(role) return storage.campaign.launch(role) end")
    adapter = _LuaBackendAdapter(lua)
    result = NativeFactory.execute(adapter, "factory_launch", {"role": contract.SILO})
    return adapter, result


def test_loaded_cargo_with_unresolved_paid_attempt_blocks_planner_and_launch():
    data, state = scenario(cargo={"raw-fish": 1})
    row = state.factory["launch_readiness"]
    _load_attempt(row)

    assert not contract.ready(state)
    assert not allowed("factory_launch", {"role": contract.SILO}, state)
    plans, reason = compile_factory("rocket_launch", state, data)
    assert plans == []
    assert "load" in reason.lower() or "receipt" in reason.lower()


def test_exact_load_receipt_and_legacy_loaded_payload_remain_launchable():
    data, legacy = scenario(cargo={"raw-fish": 1})
    legacy_row = legacy.factory["launch_readiness"]
    assert not legacy_row["attempts"].get("load")
    assert contract.ready(legacy)
    assert compile_factory("rocket_launch", legacy, data)[0][0].steps[0].action == "factory_launch"

    data, settled = scenario(cargo={"raw-fish": 1})
    row = settled.factory["launch_readiness"]
    _load_attempt(row)
    _load_receipt(row)
    assert contract.ready(settled)
    assert allowed("factory_launch", {"role": contract.SILO}, settled)
    assert compile_factory("rocket_launch", settled, data)[0][0].steps[0].action == "factory_launch"


def test_actual_lua_emitted_load_record_reconciles_python_planner():
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        storage.campaign.load_launch_payload({role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"})
        emitted=observe()''')
    data, state = scenario(cargo={"raw-fish": 1})
    state.tick = 100
    state.factory["launch_readiness"] = deepcopy(_to_python(lua.globals().emitted))

    assert contract.ready(state)
    assert allowed("factory_launch", {"role": contract.SILO}, state)
    assert compile_factory("rocket_launch", state, data)[0][0].steps[0].action == "factory_launch"


def test_actual_lua_observation_missing_receipt_blocks_after_reload_and_never_replays():
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        setmetatable(storage.launch_readiness.receipts,{__newindex=function() error("receipt-store-failure") end})
        local loaded=pcall(storage.campaign.load_launch_payload,{role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"})
        setmetatable(storage.launch_readiness.receipts,nil)
        assert(not loaded and main.get_item_count("raw-fish")==0 and cargo.get_item_count("raw-fish")==1)
        assert(observe().attempts.load and not observe().receipts["load:100"])
        observed_row=observe()''')
    emitted = _to_python(lua.globals().observed_row)
    data, state = scenario(cargo={"raw-fish": 1})
    state.factory["launch_readiness"] = deepcopy(emitted)
    assert not contract.ready(state)
    assert not allowed("factory_launch", {"role": contract.SILO}, state)
    plans, _ = compile_factory("rocket_launch", state, data)
    assert plans == []

    lua.execute(files("jev_factorio").joinpath("lua/launch_readiness.lua").read_text())
    lua.execute('''local p={role="recipe:rocket-part",silo_unit=30,rocket_unit=31,item="raw-fish",receipt="load:100"}
        assert(not pcall(storage.campaign.load_launch_payload,p))
        assert(main.get_item_count("raw-fish")==0 and cargo.get_item_count("raw-fish")==1)
        function launch_from_adapter(role) return storage.campaign.launch(role) end''')
    from jev_factorio.backends.native_factory import NativeFactory
    adapter = _LuaBackendAdapter(lua)
    with pytest.raises(Exception, match="Paid payload load is unresolved"):
        NativeFactory.execute(adapter, "factory_launch", {"role": contract.SILO})
    lua.execute('''assert(launches==0 and observe().attempts.load and not observe().attempts.launch
            and not observe().receipts["load:100"])''')


@pytest.mark.parametrize("field,value", [
    ("kind", "pad"),
    ("session_id", "other-session"),
    ("actor_unit", 11),
    ("tick", 101),
    ("item", "satellite"),
    ("quantity", True),
    ("silo_unit", 33),
    ("rocket_unit", 34),
])
def test_actual_lua_receipt_drift_is_rejected_by_native_factory_dispatch(field, value):
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        storage.campaign.load_launch_payload({role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"})''')
    lua.globals().receipt_field = field
    lua.globals().receipt_value = value
    lua.execute('storage.launch_readiness.receipts["load:100"][receipt_field]=receipt_value')
    with pytest.raises(Exception, match="Paid payload load is unresolved"):
        _adapter_launch(lua)
    assert lua.globals().launches == 0
    assert lua.eval('storage.launch_readiness.attempts.launch==nil')


def test_exact_actual_lua_receipt_passes_native_factory_adapter_after_attachment_reload():
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        storage.campaign.load_launch_payload({role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"})''')
    lua.execute(files("jev_factorio").joinpath("lua/launch_readiness.lua").read_text())
    adapter, _ = _adapter_launch(lua)
    assert adapter.approaches == [contract.SILO]
    assert adapter.launch_calls == [contract.SILO]
    assert lua.globals().launches == 1
    assert lua.eval('storage.launch_readiness.receipts.launch~=nil')


def test_legacy_loaded_payload_without_load_attempt_still_passes_native_factory_adapter():
    lua = lua_case('''observe();add_pad();cargo.insert{name="raw-fish",count=1}''')
    adapter, _ = _adapter_launch(lua)
    assert adapter.launch_calls == [contract.SILO]
    assert lua.globals().launches == 1


@pytest.mark.parametrize("mutation", [
    "missing_receipt",
    "wrong_receipt_id",
    "missing_item",
    "unknown_attempt_field",
    "wrong_silo",
    "wrong_rocket",
    "wrong_item",
    "mismatched_tick",
    "future_attempt_and_receipt",
    "null_attempt",
    "orphan_receipt",
])
def test_malformed_or_orphan_python_load_history_fails_closed(mutation):
    _, state = scenario(cargo={"raw-fish": 1})
    row = state.factory["launch_readiness"]
    _load_attempt(row)
    _load_receipt(row)
    if mutation == "missing_receipt":
        row["receipts"].pop("load:100")
    elif mutation == "wrong_receipt_id":
        row["attempts"]["load"]["receipt"] = "load:other"
    elif mutation == "missing_item":
        row["attempts"]["load"].pop("item")
    elif mutation == "unknown_attempt_field":
        row["attempts"]["load"]["source"] = "actor-main-inventory"
    elif mutation == "wrong_silo":
        row["attempts"]["load"]["silo_unit"] = 99
    elif mutation == "wrong_rocket":
        row["attempts"]["load"]["rocket_unit"] = 98
    elif mutation == "wrong_item":
        row["attempts"]["load"]["item"] = "satellite"
    elif mutation == "mismatched_tick":
        row["attempts"]["load"]["tick"] += 1
    elif mutation == "future_attempt_and_receipt":
        row["attempts"]["load"]["tick"] += 1
        row["receipts"]["load:100"]["tick"] += 1
    elif mutation == "null_attempt":
        row["attempts"]["load"] = None
    elif mutation == "orphan_receipt":
        row["attempts"].pop("load")
    assert not contract.ready(state)
    assert not allowed("factory_launch", {"role": contract.SILO}, state)


def test_python_load_receipt_rejects_boolean_entity_ids_even_when_one_is_current():
    _, state = scenario(cargo={"raw-fish": 1})
    row = state.factory["launch_readiness"]
    row["silo"]["unit_number"] = 1
    row["silo"]["rocket_unit"] = 1
    state.factory["entities"][contract.SILO]["unit_number"] = 1
    _load_attempt(row)
    row["attempts"]["load"]["silo_unit"] = 1
    row["attempts"]["load"]["rocket_unit"] = 1
    _load_receipt(row)
    row["receipts"]["load:100"]["silo_unit"] = True
    row["receipts"]["load:100"]["rocket_unit"] = True
    assert not contract.ready(state)


@pytest.mark.parametrize("mutation", [
    'storage.launch_readiness.attempts.load.item="satellite"',
    'storage.launch_readiness.attempts.load.silo_unit=99',
    'storage.launch_readiness.attempts.load.rocket_unit=98',
    'storage.launch_readiness.attempts.load.tick=101',
    'storage.launch_readiness.attempts.load.tick=101;storage.launch_readiness.receipts["load:100"].tick=101',
    'storage.launch_readiness.attempts.load.extra="unknown"',
    'storage.launch_readiness.attempts.load=nil',
    'storage.launch_readiness.receipts["load:100"]=nil',
])
def test_actual_lua_attempt_drift_is_rejected_before_launch_intent(mutation):
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        storage.campaign.load_launch_payload({role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"})''')
    lua.execute(mutation)
    with pytest.raises(Exception):
        _adapter_launch(lua)
    assert lua.globals().launches == 0
    assert lua.eval('storage.launch_readiness.attempts.launch==nil')


def _controller_for_state(tmp_path, state, data, *, checkpoint=None, resume=False):
    class Backend:
        def __init__(self):
            self.actions = []

        def enable_factory(self):
            return data

        def observe(self):
            return deepcopy(state)

        def execute(self, action, parameters):
            self.actions.append((action, deepcopy(parameters)))
            return "offline fixture dispatch"

        def act(self, action):
            self.actions.append((action, {}))
            return "offline fixture idle"

    backend = Backend()
    path = checkpoint or str(tmp_path / "controller.json")
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                           checkpoint=path, resume_controller=resume, tick_seconds=0)
    if not resume:
        loop.memory = loop.memory_type(
            state.session_id, "rocket_launch", active_goal="rocket_launch",
            completed_goals={goal: 1 for goal in loop.order[:-1]}, last_tick=state.tick)
    return loop, backend


def test_public_hierarchical_loop_blocks_repeated_resume_with_unresolved_observed_attempt(tmp_path):
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        setmetatable(storage.launch_readiness.receipts,{__newindex=function() error("receipt-store-failure") end})
        assert(not pcall(storage.campaign.load_launch_payload,{role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"}))
        setmetatable(storage.launch_readiness.receipts,nil)
        game.tick=101;emitted=observe()''')
    data, state = scenario(cargo={"raw-fish": 1})
    state.tick = 101
    state.factory["launch_readiness"] = deepcopy(_to_python(lua.globals().emitted))
    checkpoint = str(tmp_path / "controller.json")

    first, backend = _controller_for_state(tmp_path, state, data, checkpoint=checkpoint)
    first.memory.save(checkpoint)
    for _ in range(2):
        resumed, backend = _controller_for_state(tmp_path, state, data, checkpoint=checkpoint, resume=True)
        resumed.step()
        assert backend.actions == []
        assert resumed.memory.attempt is None and resumed.memory.pending is None
        assert resumed.memory.status == "blocked"


def test_public_hierarchical_loop_valid_receipt_is_still_gated_by_checkpoint_save(tmp_path):
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        storage.campaign.load_launch_payload({role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"})
        emitted=observe()''')
    data, state = scenario(cargo={"raw-fish": 1})
    state.tick = 100
    state.factory["launch_readiness"] = deepcopy(_to_python(lua.globals().emitted))
    loop, backend = _controller_for_state(tmp_path, state, data)
    loop.memory.save = lambda *_: (_ for _ in ()).throw(OSError("checkpoint-save-failed"))
    with pytest.raises(OSError, match="checkpoint-save-failed"):
        loop.step()
    assert backend.actions == []


def test_public_hierarchical_loop_dispatches_valid_exact_receipt_after_checkpoint(tmp_path):
    lua = lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        storage.campaign.load_launch_payload({role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="load:100"})
        emitted=observe()''')
    data, state = scenario(cargo={"raw-fish": 1})
    state.tick = 100
    state.factory["launch_readiness"] = deepcopy(_to_python(lua.globals().emitted))
    loop, backend = _controller_for_state(tmp_path, state, data)
    record = loop.step()
    assert backend.actions and backend.actions[0][0] == "factory_launch"
    assert record["action"] == "factory_launch"


@pytest.mark.parametrize("field,value", [
    ("kind", "pad"),
    ("session_id", "other-session"),
    ("actor_unit", 11),
    ("tick", 101),
    ("item", "satellite"),
    ("quantity", True),
    ("silo_unit", 33),
    ("rocket_unit", 34),
])
def test_mismatched_load_receipt_cannot_authorize_launch(field, value):
    data, state = scenario(cargo={"raw-fish": 1})
    row = state.factory["launch_readiness"]
    _load_attempt(row)
    _load_receipt(row)
    row["receipts"]["load:100"][field] = value
    assert not contract.ready(state)
    assert not allowed("factory_launch", {"role": contract.SILO}, state)
    assert compile_factory("rocket_launch", state, data)[0] == []
