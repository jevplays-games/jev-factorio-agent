"""Typed nonmutation only from the composed controller/FLE/Lua transfer path."""
import json
import re
from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace

import pytest

from jev_factorio.backends.fle import FleBackend
from jev_factorio.backends.native_factory import NativeFactory, TRANSFER_PREFLIGHT_MARKER
from jev_factorio.backends.observed_factory import ObservedFactory
from jev_factorio.memory import CampaignMemory
from jev_factorio.state import GameSnapshot

from attempt_helpers import controller, install_plan


class ProcessCut(BaseException):
    pass


def _to_lua(lua, value):
    if isinstance(value, dict):
        table = lua.table()
        for key, item in value.items():
            table[key] = _to_lua(lua, item)
        return table
    if isinstance(value, list):
        table = lua.table()
        for index, item in enumerate(value, 1):
            table[index] = _to_lua(lua, item)
        return table
    return value


def composed_fle_backend(*, action="factory_insert", capacity=0, accepted=20,
                         reply="normal", source=20, reachable=True,
                         refund_success=True, observed=False, factory_source=None):
    """Run the real transfer Lua under the real FLE and NativeFactory adapters."""
    lua54 = pytest.importorskip("lupa.lua54")
    lua = lua54.LuaRuntime(unpack_returned_tuples=True)
    extracting = action == "factory_extract"
    item, role, receipt = "automation-science-pack", "utility:lab", "transfer:0"
    lua.execute(f"""
        game = {{tick=11}}
        defines = {{inventory={{character_main=1, chest=2, furnace_source=3,
                                fuel=4, lab_input=5, assembling_machine_input=6}}}}
        is_extract = {str(extracting).lower()}
        source_count, target_count = {source}, 0
        mutator_calls = 0
        target_capacity, accepted_count = {capacity}, {accepted}
        reach_result = {str(reachable).lower()}
        refund_result = {str(refund_success).lower()}
        surface = {{index=1}}
        force = {{index=1, rockets_launched=0}}
        local function remove_source(stack)
            mutator_calls = mutator_calls+1
            source_count = source_count-stack.count
            return stack.count
        end
        local function return_source(stack)
            mutator_calls = mutator_calls+1
            local returned = refund_result and stack.count or 0
            source_count = source_count+returned
            return returned
        end
        local function add_target(stack)
            mutator_calls = mutator_calls+1
            local inserted = math.min(accepted_count, stack.count, target_capacity)
            target_count = target_count+inserted
            target_capacity = target_capacity-inserted
            return inserted
        end
        source_inventory = {{
            get_item_count=function() return source_count end,
            get_insertable_count=function() return 200 end,
            remove=remove_source, insert=return_source,
        }}
        target_inventory = {{
            get_item_count=function() return target_count end,
            get_insertable_count=function() return target_capacity end,
            remove=remove_source, insert=add_target,
        }}
        actor_inventory = {{
            get_item_count=function() return is_extract and target_count or source_count end,
            get_insertable_count=function() return target_capacity end,
            remove=remove_source,
            insert=function(stack)
                if is_extract then return add_target(stack) end
                return return_source(stack)
            end,
        }}
        machine_inventory = {{
            get_item_count=function() return is_extract and source_count or target_count end,
            get_insertable_count=function() return target_capacity end,
            remove=remove_source,
            insert=function(stack)
                if is_extract then return return_source(stack) end
                return add_target(stack)
            end,
        }}
        agent = {{name="character", unit_number=7, surface=surface, force=force,
            get_inventory=function() return actor_inventory end}}
        player = {{index=1, character=agent, surface=surface,
            can_reach_entity=function() return reach_result end}}
        machine = {{valid=true, name="lab", type="lab", unit_number=19,
            surface=surface, force=force, position={{x=0,y=0}},
            get_inventory=function() return machine_inventory end,
            get_output_inventory=function() return machine_inventory end}}
        storage = {{jev_session_id="receipt-session", jev_player_index=1,
            agent_characters={{agent}}, fair={{actor=function() return player end}},
            campaign={{entities={{["utility:lab"]=machine}}, receipts={{}}, receipt_order={{}}}}}}
        last_print = nil
        rcon = {{print=function(value) last_print=value end}}
        local function quote(value)
            return '"' .. value:gsub("\\\\", "\\\\\\\\")
                :gsub('"', '\\"'):gsub("\\n", "\\\\n")
                :gsub("\\r", "\\\\r"):gsub("\\t", "\\\\t") .. '"'
        end
        local function encode(value)
            local kind = type(value)
            if kind == "string" then return quote(value) end
            if kind == "number" or kind == "boolean" then return tostring(value) end
            if kind == "nil" then return "null" end
            assert(kind == "table")
            local keys, parts = {{}}, {{}}
            for key in pairs(value) do table.insert(keys, key) end
            table.sort(keys)
            for _, key in ipairs(keys) do
                table.insert(parts, quote(key) .. ":" .. encode(value[key]))
            end
            return "{{" .. table.concat(parts, ",") .. "}}"
        end
        helpers = {{json_to_table=function(_) return transfer_preflight_context end,
                    table_to_json=encode}}
    """)
    lua.execute(factory_source if factory_source is not None else
                 files("jev_factorio").joinpath("lua/factory.lua").read_text())

    backend = object.__new__(FleBackend)
    backend._instance = SimpleNamespace(namespace=SimpleNamespace())
    backend._observation_profile = None
    backend._native_attachment = None
    backend._fair = SimpleNamespace(approach=lambda *args, **kwargs: None)
    backend._drill = None
    backend._resources = {}
    backend.calls = []
    machine_output = {item: source} if extracting else {}
    backend.state = GameSnapshot(
        session_id="receipt-session", world_kind="fle", tick=10,
        inventory={item: 0 if extracting else source},
        factory={
            "player_bound": True,
            "acceptance_runtime": {
                "schema": 1, "session_id": "receipt-session", "actor_unit": 7,
                "player_index": 1, "surface_index": 1, "force_index": 1,
            },
            "entities": {role: {"name": "lab", "unit_number": 19, "input": {},
                                "output": machine_output}},
            "receipts": {},
        },
    )

    class Rcon:
        def send_command(self, command):
            backend.calls.append(command)
            assert command.startswith("/sc ")
            if reply == "text_only":
                raise RuntimeError("destination capacity is short")
            match = re.search(r'helpers\.json_to_table\(("(?:\\.|[^"\\])*")\)', command)
            if match:
                serialized = json.loads(match.group(1))
                context = json.loads(serialized)
                lua.globals().transfer_preflight_context = _to_lua(lua, context)
            lua.globals().last_print = None
            lua.execute(command[4:])
            result = lua.globals().last_print
            if reply == "lost" and isinstance(result, str) and result.startswith(TRANSFER_PREFLIGHT_MARKER):
                raise TimeoutError("simulated lost preflight response")
            if reply == "malformed" and isinstance(result, str) and result.startswith(TRANSFER_PREFLIGHT_MARKER):
                return result + "{}"
            if reply == "wrong_attempt" and isinstance(result, str) and result.startswith(TRANSFER_PREFLIGHT_MARKER):
                prefix, body = result.split("|", 1)
                proof = json.loads(body)
                proof["request"]["attempt_id"] = "0" * 32
                return prefix + "|" + json.dumps(proof, separators=(",", ":"))
            return result or ""

    backend._instance.rcon_client = Rcon()
    native_type = ObservedFactory if observed else NativeFactory
    native = object.__new__(native_type)
    native.backend = backend
    if observed:
        native._discovery_epoch = 0
    native.approach_role = lambda selected_role: None
    backend._factory = native

    def observe():
        state = backend.state
        state.tick = int(lua.globals().game.tick)
        source_count = int(lua.globals().source_count)
        target_count = int(lua.globals().target_count)
        machine_state = state.factory["entities"][role]
        if extracting:
            machine_state["output"][item] = source_count
            state.inventory[item] = target_count
        else:
            state.inventory[item] = source_count
            machine_state["input"][item] = target_count
        state.factory["receipts"].clear()
        raw = lua.globals().storage.campaign.receipts[receipt]
        if raw is not None:
            state.factory["receipts"][receipt] = {
                "role": raw["role"], "item": raw["item"],
                "quantity": int(raw["quantity"]), "unit_number": int(raw["unit_number"]),
                "extracting": bool(raw["extracting"]), "tick": int(raw["tick"]),
            }
        return deepcopy(state)

    backend.observe = observe
    backend.lua = lua
    backend.receipt_key = receipt
    backend.item = item
    backend.role = role
    return backend


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("action", ["factory_insert", "factory_extract"])
@pytest.mark.parametrize("capacity", [0, 48])
@pytest.mark.parametrize("refund_success", [True, False])
def test_public_no_context_short_capacity_rejects_before_any_mutation(
    observed, action, capacity, refund_success
):
    """Legacy FLE transfer calls must keep the pre-mutation capacity guard."""
    quantity = 50
    backend = composed_fle_backend(
        action=action, capacity=capacity, accepted=quantity, source=quantity,
        refund_success=refund_success, observed=observed,
    )
    parameters = {"role": backend.role, "item": backend.item,
                  "quantity": quantity, "receipt": backend.receipt_key}

    with pytest.raises(Exception, match="Transfer destination capacity is short"):
        backend.execute(action, parameters)

    assert backend.lua.globals().mutator_calls == 0
    assert backend.lua.globals().source_count == quantity
    assert backend.lua.globals().target_count == 0
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None
    assert len(backend.calls) == 1
    if observed:
        assert backend._factory._discovery_epoch == 0


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("action", ["factory_insert", "factory_extract"])
def test_public_no_context_full_capacity_transfer_remains_supported(observed, action):
    quantity = 50
    backend = composed_fle_backend(
        action=action, capacity=quantity, accepted=quantity, source=quantity,
        observed=observed,
    )
    parameters = {"role": backend.role, "item": backend.item,
                  "quantity": quantity, "receipt": backend.receipt_key}

    backend.execute(action, parameters)

    receipt = backend.lua.globals().storage.campaign.receipts[backend.receipt_key]
    assert backend.lua.globals().mutator_calls == 2
    assert backend.lua.globals().source_count == 0
    assert backend.lua.globals().target_count == quantity
    assert int(receipt["quantity"]) == quantity
    assert bool(receipt["extracting"]) is (action == "factory_extract")
    assert len(backend.calls) == 1
    if observed:
        assert backend._factory._discovery_epoch == 0


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("action", ["factory_insert", "factory_extract"])
@pytest.mark.parametrize("refund_success", [True, False])
def test_public_no_context_positive_capacity_partial_transfer_keeps_receipt_rules(
    observed, action, refund_success
):
    quantity = 50
    backend = composed_fle_backend(
        action=action, capacity=quantity, accepted=48, source=quantity,
        refund_success=refund_success, observed=observed,
    )
    parameters = {"role": backend.role, "item": backend.item,
                  "quantity": quantity, "receipt": backend.receipt_key}

    expected_error = "Transfer refund failed" if not refund_success else "Partial transfer"
    with pytest.raises(Exception, match=expected_error):
        backend.execute(action, parameters)

    assert backend.lua.globals().mutator_calls == 3
    assert backend.lua.globals().target_count == 48
    receipt = backend.lua.globals().storage.campaign.receipts[backend.receipt_key]
    if refund_success:
        assert backend.lua.globals().source_count == 2
        assert int(receipt["quantity"]) == 48
    else:
        assert backend.lua.globals().source_count == 0
        assert receipt is None
    assert len(backend.calls) == 1
    if observed:
        assert backend._factory._discovery_epoch == 0


def _saved(path):
    return CampaignMemory.load(path, "receipt-session", "bootstrap_mining")


@pytest.mark.parametrize("action", ["factory_insert", "factory_extract"])
@pytest.mark.parametrize("observed", [False, True])
def test_full_controller_lua_capacity_rejection_is_durable_nonmutation(
    monkeypatch, tmp_path, action, observed
):
    backend = composed_fle_backend(action=action, capacity=0, observed=observed)
    install_plan(monkeypatch, backend, action=action, reserve_transfer=action == "factory_insert")
    first = controller(tmp_path, backend, max_pending_polls=1)

    result = first.step()

    assert result["action"] == "reconcile"
    assert result["status"] == "running"
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().target_count == 0
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None
    assert len(backend.calls) == 1
    if observed:
        assert backend._factory._discovery_epoch == 0
    proof_event = next(row for row in first.memory.history
                       if row["kind"] == "transfer_preflight_rejected")
    outcome = first.memory.attempt_outcomes[-1]
    proof = outcome["dispatch_phases"]["transfer_rpc"]["proof"]
    assert proof_event["proof"] == proof
    assert proof_event["mutation_started"] is False
    assert proof["request"]["action"] == action
    assert proof["request"]["direction"] == ("extract" if action == "factory_extract" else "insert")
    assert outcome["outcome"] == "transfer_preflight_rejected"
    assert first.memory.failures == {"same-plan-id": 1}
    assert first.memory.pending is first.memory.active_plan is first.memory.attempt is None
    assert not first.memory.reservations
    restored = _saved(tmp_path / "checkpoint.json")
    assert restored.attempt_outcomes[-1] == outcome
    assert next(row for row in restored.history
                if row["kind"] == "transfer_preflight_rejected")["proof"] == proof


def test_preflight_failures_consume_existing_plan_budget_without_reset(monkeypatch, tmp_path):
    backend = composed_fle_backend(action="factory_insert", capacity=0)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    loop = controller(tmp_path, backend, max_pending_polls=1)

    loop.step()
    assert loop.memory.failures == {"same-plan-id": 1}
    loop.step()
    assert loop.memory.failures == {"same-plan-id": 2}
    assert len(backend.calls) == 2

    blocked = loop.step()
    assert blocked["status"] == "blocked"
    assert loop.memory.failures == {"same-plan-id": 2}
    assert len(backend.calls) == 2
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().target_count == 0
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None


def test_saved_preflight_survives_process_cut_and_reconciles_once(monkeypatch, tmp_path):
    backend = composed_fle_backend(action="factory_insert", capacity=0)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first._settle_transfer_preflight_rejection = lambda *args: (_ for _ in ()).throw(ProcessCut())

    with pytest.raises(ProcessCut):
        first.step()

    saved = _saved(tmp_path / "checkpoint.json")
    assert saved.pending["dispatch"] == "prepared"
    assert saved.attempt["dispatch_phases"]["transfer_rpc"]["error_code"] == "transfer_preflight_rejected"
    assert saved.attempt_outcomes == []
    assert saved.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert len(backend.calls) == 1

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["action"] == "reconcile"
    assert resumed.memory.attempt_outcomes[-1]["outcome"] == "transfer_preflight_rejected"
    assert len(backend.calls) == 1
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None


def test_failure_to_save_preflight_proof_keeps_attempt_ambiguous_and_owned(
    monkeypatch, tmp_path
):
    backend = composed_fle_backend(action="factory_insert", capacity=0)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    loop = controller(tmp_path, backend, max_pending_polls=1)
    original_save = loop._save

    def fail_proof_checkpoint():
        phase = ((loop.memory.attempt or {}).get("dispatch_phases") or {}).get("transfer_rpc")
        if isinstance(phase, dict) and phase.get("error_code") == "transfer_preflight_rejected":
            loop._persistence_failed = True
            raise OSError("simulated preflight phase checkpoint failure")
        original_save()

    loop._save = fail_proof_checkpoint
    from jev_factorio.backends.native_factory import TransferPreflightRejected
    with pytest.raises(TransferPreflightRejected):
        loop.step()

    saved = _saved(tmp_path / "checkpoint.json")
    assert saved.pending["dispatch"] == "prepared"
    assert saved.attempt["dispatch_phases"]["transfer_rpc"]["status"] == "started"
    assert "proof" not in saved.attempt["dispatch_phases"]["transfer_rpc"]
    assert saved.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert saved.failures == {}
    assert len(backend.calls) == 1
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.pending is not None
    assert resumed.memory.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert len(backend.calls) == 1


def test_checkpoint_cannot_rebind_preflight_to_another_item(monkeypatch, tmp_path):
    backend = composed_fle_backend(action="factory_insert", capacity=0)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    loop = controller(tmp_path, backend, max_pending_polls=1)
    loop._settle_transfer_preflight_rejection = lambda *args: (_ for _ in ()).throw(ProcessCut())
    with pytest.raises(ProcessCut):
        loop.step()

    path = tmp_path / "checkpoint.json"
    payload = json.loads(path.read_text())
    proof = payload["attempt"]["dispatch_phases"]["transfer_rpc"]["proof"]
    proof["request"]["item"] = "iron-plate"
    path.write_text(json.dumps(payload, sort_keys=True, allow_nan=False))
    with pytest.raises(ValueError, match="Transfer preflight proof"):
        CampaignMemory.load(path, "receipt-session", "bootstrap_mining")
    assert len(backend.calls) == 1
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None


@pytest.mark.parametrize("change", ["actor", "source", "receipt"])
def test_saved_preflight_owner_or_source_drift_stays_uncertain_without_replay(
    monkeypatch, tmp_path, change
):
    backend = composed_fle_backend(action="factory_insert", capacity=0)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first._settle_transfer_preflight_rejection = lambda *args: (_ for _ in ()).throw(ProcessCut())
    with pytest.raises(ProcessCut):
        first.step()

    if change == "actor":
        backend.state.factory["acceptance_runtime"]["actor_unit"] = 8
    elif change == "source":
        backend.lua.globals().source_count = 19
    else:
        backend.lua.globals().storage.campaign.receipts[backend.receipt_key] = _to_lua(
            backend.lua, {"role": backend.role, "item": backend.item, "quantity": 1,
                          "unit_number": 19, "extracting": False, "tick": 11})

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.pending is not None
    assert resumed.memory.attempt["id"] == _saved(tmp_path / "checkpoint.json").attempt["id"]
    assert len(backend.calls) == 1
    assert not resumed.memory.attempt_outcomes


@pytest.mark.parametrize("change", ["capacity", "reach"])
def test_saved_preflight_does_not_redispatch_after_capacity_or_reach_changes(
    monkeypatch, tmp_path, change
):
    backend = composed_fle_backend(action="factory_insert", capacity=0)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    first._settle_transfer_preflight_rejection = lambda *args: (_ for _ in ()).throw(ProcessCut())
    with pytest.raises(ProcessCut):
        first.step()

    if change == "capacity":
        backend.lua.globals().target_capacity = 20
    else:
        backend.lua.globals().reach_result = False

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "running"
    assert len(backend.calls) == 1
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().target_count == 0
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None
    assert resumed.memory.pending is None
    assert resumed.memory.attempt is None
    assert resumed.memory.failures == {"same-plan-id": 1}


@pytest.mark.parametrize("reply", ["lost", "malformed", "wrong_attempt", "text_only"])
def test_untrusted_or_lost_preflight_reply_never_releases_paid_attempt(
    monkeypatch, tmp_path, reply
):
    backend = composed_fle_backend(action="factory_insert", capacity=0, reply=reply)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)

    result = first.step()

    assert result["action"] == "factory_insert"
    assert result["status"] == "running"
    assert first.memory.pending["dispatch"] == "ambiguous"
    assert first.memory.attempt["dispatch_phases"].get("transfer_rpc", {}).get(
        "error_code") != "transfer_preflight_rejected"
    assert first.memory.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert first.memory.failures == {}
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().target_count == 0
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None
    assert len(backend.calls) == 1

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    second = resumed.step()
    assert second["status"] == "uncertain"
    assert len(backend.calls) == 1
    assert resumed.memory.pending is not None
    assert resumed.memory.reservations == {"same-plan-id": {"automation-science-pack": 20}}


@pytest.mark.parametrize("action", ["factory_insert", "factory_extract"])
def test_capacity_available_runs_original_full_transfer_and_receipt_path(
    monkeypatch, tmp_path, action
):
    backend = composed_fle_backend(action=action, capacity=20)
    install_plan(monkeypatch, backend, action=action, reserve_transfer=action == "factory_insert")
    loop = controller(tmp_path, backend, max_pending_polls=1)

    result = loop.step()

    assert result["action"] == action
    assert result["verified"] is True
    assert backend.lua.globals().source_count == 0
    assert backend.lua.globals().target_count == 20
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key]["quantity"] == 20
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "verified"
    assert "proof" not in loop.memory.attempt_outcomes[-1]["dispatch_phases"]["transfer_rpc"]


def test_partial_native_receipt_still_uses_ordinary_reconciliation(monkeypatch, tmp_path):
    backend = composed_fle_backend(action="factory_insert", capacity=20, accepted=10)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    loop = controller(tmp_path, backend, max_pending_polls=1)

    first = loop.step()
    assert first["action"] == "factory_insert"
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert loop.memory.failures == {}
    assert backend.lua.globals().source_count == 10  # The Lua refund restored the untransferred remainder.
    assert backend.lua.globals().target_count == 10
    receipt = backend.lua.globals().storage.campaign.receipts[backend.receipt_key]
    assert int(receipt["quantity"]) == 10

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    second = resumed.step()
    assert second["action"] == "reconcile"
    assert resumed.memory.attempt_outcomes[-1]["outcome"] == "partial_transfer_reconciled"
    assert resumed.memory.failures == {"same-plan-id": 1}
    assert len(backend.calls) == 1


def test_reachability_failure_is_not_a_typed_capacity_rejection(monkeypatch, tmp_path):
    backend = composed_fle_backend(action="factory_insert", capacity=0, reachable=False)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    loop = controller(tmp_path, backend, max_pending_polls=1)

    result = loop.step()

    assert result["action"] == "factory_insert"
    assert result["status"] == "running"
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert "transfer_preflight_rejected" not in {
        row["kind"] for row in loop.memory.history
    }
    assert loop.memory.attempt["dispatch_phases"]["transfer_rpc"]["error_code"] != (
        "transfer_preflight_rejected")
    assert loop.memory.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert loop.memory.failures == {}
    assert backend.lua.globals().source_count == 20
    assert backend.lua.globals().target_count == 0
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None
    assert len(backend.calls) == 1


def test_partial_refund_failure_remains_ambiguous_without_receipt_or_retry(
    monkeypatch, tmp_path
):
    backend = composed_fle_backend(action="factory_insert", capacity=20, accepted=10,
                                   refund_success=False)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    loop = controller(tmp_path, backend, max_pending_polls=1)

    result = loop.step()

    assert result["action"] == "factory_insert"
    assert result["status"] == "running"
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert loop.memory.attempt_outcomes == []
    assert loop.memory.reservations == {"same-plan-id": {"automation-science-pack": 20}}
    assert loop.memory.failures == {}
    assert backend.lua.globals().source_count == 0
    assert backend.lua.globals().target_count == 10
    assert backend.lua.globals().storage.campaign.receipts[backend.receipt_key] is None

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert len(backend.calls) == 1
    assert resumed.memory.pending is not None


def test_async_lineage_binds_terminal_preflight_to_attempt_and_event(monkeypatch, tmp_path):
    from uuid import uuid4
    from jev_factorio.skills import Plan, Step
    from jev_factorio.telemetry import fingerprint

    backend = composed_fle_backend(action="factory_insert", capacity=0)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    loop = controller(tmp_path, backend, max_pending_polls=1)
    loop.step()
    plan = Plan("same-plan-id", "stockpile_fuel", "Synthetic receipt exercise", (
        Step("factory_insert", "transfer", costs={"automation-science-pack": 20},
             parameters={"role": backend.role, "item": backend.item, "quantity": 20,
                         "receipt": backend.receipt_key}),))
    loop.async_decisions = True
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "transfer_preflight_rejected"
    outcome = loop.memory.attempt_outcomes[-1]
    loop.memory.active_plan = plan.to_dict()
    loop.memory.step_index = 0
    loop.memory.pending = {"started_tick": outcome["started_tick"], "polls": 0,
                           "action": outcome["action"], "dispatch": "prepared"}
    entry = {
        "archive_id": "a" * 64, "archive_sha256": "b" * 64,
        "request_id": str(uuid4()), "selected_plan_id": plan.id,
        "selected_plan_sha256": fingerprint(plan.to_dict()),
        "source_revision_sha256": "c" * 64, "selector_sha256": "d" * 64,
        "provider_payload_sha256": "e" * 64, "wire_body_sha256": "f" * 64,
        "state": "active", "verified_steps": [], "terminal": None,
    }
    loop._async_store_plan_lineage([entry])
    terminal = loop._async_failure_terminal(plan.to_dict(), "preflight rejected")
    assert terminal is not None
    assert terminal["attempt"]["id"] == outcome["id"]
    assert terminal["proof_event"]["proof"] == outcome["dispatch_phases"]["transfer_rpc"]["proof"]
    loop._async_close_current_plan_lineage(plan.to_dict(), terminal)
    closed = loop._async_plan_lineage_entries()[0]
    assert closed["state"] == "abandoned"
    loop._async_validate_abandoned_lineage(plan.to_dict(), closed)

    closed["terminal"]["proof_event"]["proof"]["request"]["attempt_id"] = "0" * 32
    loop._async_store_plan_lineage([closed])
    with pytest.raises(ValueError):
        loop._async_plan_lineage_entries()
