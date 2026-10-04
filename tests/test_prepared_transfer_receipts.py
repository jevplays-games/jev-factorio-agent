"""Crash-window receipt reconciliation through the production trace/save path."""
import json
from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace

import pytest

from jev_factorio.backends.fle import FleBackend
from jev_factorio.backends.native_factory import NativeFactory
from jev_factorio.memory import CampaignMemory
from jev_factorio.state import GameSnapshot

from attempt_helpers import controller, install_plan


class SimulatedProcessStop(BaseException):
    """Stop dispatch before the outer handler rewrites its write-ahead row."""


def load(path):
    return CampaignMemory.load(path, "receipt-session", "bootstrap_mining")


def native_transfer_backend(*, action, quantity, rpc_status):
    """Compose real FleBackend/NativeFactory/Lua transfer code without a game."""
    lua54 = pytest.importorskip("lupa.lua54")
    lua = lua54.LuaRuntime(unpack_returned_tuples=True)
    item, role, receipt = "automation-science-pack", "utility:lab", "transfer:0"
    extracting = action == "factory_extract"
    lua.execute("""
        game = {tick = 11}
        defines = {inventory = {character_main = 1, chest = 2, furnace_source = 3}}
        source_count, target_count, accepted_count = 20, 0, 0
        source_inventory = {
            get_item_count = function() return source_count end,
            remove = function(stack) source_count = source_count-stack.count; return stack.count end,
            insert = function(stack) source_count = source_count+stack.count; return stack.count end
        }
        target_inventory = {
            get_insertable_count = function() return 20 end,
            insert = function(stack)
                local inserted = math.min(accepted_count, stack.count)
                target_count = target_count + inserted
                return inserted
            end
        }
        storage = {agent_characters = {{
            force = {rockets_launched = 0}, position = {x = 0, y = 0},
            get_inventory = function() return agent_inventory end
        }}, fair = {actor = function()
            return {can_reach_entity = function() return true end}
        end}}
        agent_inventory = source_inventory
        rcon = {print = function() end}
    """)
    lua.execute(files("jev_factorio").joinpath("lua/factory.lua").read_text())
    lua.globals().extracting = extracting
    if extracting:
        lua.execute("agent_inventory = target_inventory")
    lua.globals().accepted_count = quantity
    lua.globals().storage.campaign.entities[role] = lua.table_from({
        "valid": True,
        "type": "lab",
        "unit_number": 7,
        "position": lua.table_from({"x": 0, "y": 0}),
        "get_inventory": lua.eval("function() return target_inventory end"),
        "get_output_inventory": lua.eval("function() return source_inventory end"),
    })

    backend = object.__new__(FleBackend)
    backend._instance = SimpleNamespace(namespace=SimpleNamespace())
    backend._observation_profile = None
    backend._factory = None
    backend.calls = []
    backend.state = GameSnapshot(
        session_id="receipt-session", world_kind="fle", tick=10,
        inventory={item: 0 if extracting else 20},
        factory={
            "player_bound": True,
            "entities": {role: {
                "name": "lab", "unit_number": 7, "input": {},
                "output": {item: 20} if extracting else {},
            }},
            "receipts": {},
        },
    )
    native = object.__new__(NativeFactory)
    native.backend = backend
    native.approach_role = lambda selected_role: None
    lua_transfer = lua.eval("storage.campaign.transfer")

    def call(function, *arguments):
        assert function == "transfer"
        backend.calls.append((function, tuple(arguments)))
        lua_transfer(*arguments)
        # A response can be lost after the native endpoint committed its full receipt.
        if rpc_status == "failed" and quantity == 20:
            raise RuntimeError("synthetic lost response after native transfer")
        return ""

    native.call = call
    backend._factory = native

    def observe():
        state = backend.state
        state.tick = int(lua.globals().game.tick)
        machine = state.factory["entities"][role]
        source_count, target_count = int(lua.globals().source_count), int(lua.globals().target_count)
        if extracting:
            machine["output"][item] = source_count
            state.inventory[item] = target_count
        else:
            state.inventory[item] = source_count
            machine["input"][item] = target_count
        state.factory["receipts"].clear()
        raw = lua.globals().storage.campaign.receipts[receipt]
        if raw is not None:
            state.factory["receipts"][receipt] = {
                "role": raw["role"], "item": raw["item"], "quantity": int(raw["quantity"]),
                "unit_number": int(raw["unit_number"]), "extracting": bool(raw["extracting"]),
                "tick": int(raw["tick"]),
            }
        return deepcopy(state)

    backend.observe = observe
    backend.lua = lua
    backend.receipt_key = receipt
    backend.item = item
    backend.role = role
    expected = {
        "role": role, "item": item, "quantity": 20, "receipt": receipt,
    }

    # Keep the real FleBackend.execute_traced and NativeFactory.execute methods,
    # injecting a stop only at durable checkpoint boundaries. Started suppresses
    # the terminal callback; failed is converted after the native phase saves it;
    # returned is stopped after NativeFactory has persisted its returned phase.
    def execute_traced(selected_action, parameters, trace):
        assert selected_action == action and parameters == expected
        if rpc_status == "started":
            def crash_cut_trace(event):
                if event["stage"] == "transfer_rpc" and event["status"] != "started":
                    return
                trace(event)
            try:
                result = FleBackend.execute_traced(backend, action, parameters, crash_cut_trace)
            except Exception:
                raise SimulatedProcessStop from None
            raise SimulatedProcessStop
        if rpc_status == "failed":
            try:
                result = FleBackend.execute_traced(backend, action, parameters, trace)
            except Exception:
                # FleBackend/NativeFactory has already sent and saved the failed
                # transfer_rpc phase before control reaches this outer boundary.
                raise SimulatedProcessStop from None
            raise SimulatedProcessStop
        result = FleBackend.execute_traced(backend, action, parameters, trace)
        assert result.startswith("Transferred ")
        raise SimulatedProcessStop

    backend.execute_traced = execute_traced
    return backend


CASES = [
    *((action, quantity, status)
      for action in ("factory_insert", "factory_extract")
      for quantity in (0, 10, 20)
      for status in ("started", "failed")),
    ("factory_insert", 20, "returned"),
    ("factory_extract", 20, "returned"),
]


@pytest.mark.parametrize("action,quantity,rpc_status", CASES)
def test_prepared_native_receipt_reconciles_after_durable_rpc_phase(
    monkeypatch, tmp_path, action, quantity, rpc_status
):
    backend = native_transfer_backend(action=action, quantity=quantity, rpc_status=rpc_status)
    install_plan(monkeypatch, backend, action=action, reserve_transfer=action == "factory_insert")

    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()  # Refresh the offline observer from Lua's durable endpoint state.

    before_resume = load(tmp_path / "checkpoint.json")
    assert before_resume.pending["dispatch"] == "prepared"
    phase_status = before_resume.attempt["dispatch_phases"]["transfer_rpc"]["status"]
    assert phase_status == rpc_status
    assert before_resume.attempt["dispatch_phases"]["approach"]["status"] == "returned"
    assert before_resume.attempt["id"] == first.memory.attempt["id"]
    assert before_resume.reservations == (
        {"same-plan-id": {"automation-science-pack": 20}}
        if action == "factory_insert" else {"same-plan-id": {}}
    )
    receipt = before_resume.attempt["receipt"]
    assert receipt == "transfer:0"
    native_receipt = backend.state.factory["receipts"][receipt]
    assert native_receipt == {
        "role": "utility:lab", "item": "automation-science-pack", "quantity": quantity,
        "unit_number": 7, "extracting": action == "factory_extract", "tick": 11,
    }
    assert backend.calls == [("transfer", (
        "utility:lab", "automation-science-pack", 20, "transfer:0", action == "factory_extract",
    ))]

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["action"] == ("verify" if quantity == 20 else "reconcile")
    assert len(backend.calls) == 1
    outcome = load(tmp_path / "checkpoint.json").attempt_outcomes[-1]
    assert outcome["id"] == before_resume.attempt["id"]
    assert outcome["receipt"] == receipt
    assert outcome["dispatch_phases"]["transfer_rpc"]["status"] == rpc_status
    if quantity == 20:
        assert outcome["outcome"] == "verified"
        assert resumed.memory.failures == {}
        assert not any(event["kind"] in {
            "partial_transfer_reconciled", "zero_effect_transfer_reconciled",
        } for event in resumed.memory.history)
    else:
        expected = "zero_effect_transfer_reconciled" if quantity == 0 else "partial_transfer_reconciled"
        assert outcome["outcome"] == expected
        assert resumed.memory.failures == {"same-plan-id": 1}
        event = next(event for event in resumed.memory.history if event["kind"] == expected)
        assert event["attempt_id"] == before_resume.attempt["id"]
        assert event["receipt"] == receipt
        assert event["transferred_quantity"] == quantity
        assert event["requested_quantity"] == 20
    saved = load(tmp_path / "checkpoint.json")
    assert saved.pending is saved.active_plan is saved.attempt is None
    assert saved.attempt_outcomes[-1] == outcome


@pytest.mark.parametrize("rpc_status", ["started", "failed"])
@pytest.mark.parametrize("unit_change", ["receipt_only", "machine_only", "both_replaced"])
def test_prepared_partial_native_receipt_rejects_wrong_pinned_unit(
    monkeypatch, tmp_path, rpc_status, unit_change
):
    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status=rpc_status)
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    saved = load(tmp_path / "checkpoint.json")

    if unit_change in {"receipt_only", "both_replaced"}:
        backend.lua.globals().storage.campaign.receipts["transfer:0"]["unit_number"] = 8
    if unit_change in {"machine_only", "both_replaced"}:
        backend.state.factory["entities"]["utility:lab"]["unit_number"] = 8
    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()

    assert result["status"] == "uncertain"
    assert resumed.memory.pending["dispatch"] == "prepared"
    assert resumed.memory.attempt["id"] == saved.attempt["id"]
    assert not resumed.memory.attempt_outcomes
    assert len(backend.calls) == 1


@pytest.mark.parametrize("mutation", ["missing_receipt", "wrong_role", "wrong_item", "wrong_direction",
                                      "stale_tick", "future_tick", "actor_unbound", "mock_transport"])
def test_prepared_receipt_fails_closed_without_exact_live_evidence(monkeypatch, tmp_path, mutation):
    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    saved = load(tmp_path / "checkpoint.json")

    receipt = backend.lua.globals().storage.campaign.receipts["transfer:0"]
    if mutation == "missing_receipt":
        backend.lua.globals().storage.campaign.receipts["transfer:0"] = None
    elif mutation == "wrong_role":
        receipt["role"] = "wrong:machine"
    elif mutation == "wrong_item":
        receipt["item"] = "iron-plate"
    elif mutation == "wrong_direction":
        receipt["extracting"] = True
    elif mutation == "stale_tick":
        receipt["tick"] = saved.attempt["started_tick"] - 1
    elif mutation == "future_tick":
        receipt["tick"] = backend.state.tick + 1
    elif mutation == "actor_unbound":
        backend.state.factory["player_bound"] = False
    elif mutation == "mock_transport":
        backend.state.world_kind = "mock"

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.pending["dispatch"] == "prepared"
    assert resumed.memory.attempt["id"] == saved.attempt["id"]
    assert not resumed.memory.attempt_outcomes
    assert len(backend.calls) == 1


def test_prepared_partial_receipt_with_returned_phase_is_schema_only_control(monkeypatch, tmp_path):
    """Returned+partial is not emitted by today's Lua assert; predicate remains status-neutral."""
    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    saved = CampaignMemory.load(tmp_path / "checkpoint.json", "receipt-session", "bootstrap_mining")
    saved.attempt["dispatch_phases"]["transfer_rpc"]["status"] = "returned"
    saved.attempt["dispatch_phases"]["transfer_rpc"]["seconds"] = 0.001
    saved.attempt["dispatch_phases"]["transfer_rpc"]["error_code"] = None
    saved.save(tmp_path / "checkpoint.json")

    # Current factory.lua throws after publishing partial receipts, so this is a
    # schema-valid policy control only. It proves the reducer uses effect/identity,
    # not a requirement for the failed label; it is not producer-reachability evidence.
    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["action"] == "reconcile"
    assert resumed.memory.attempt_outcomes[-1]["outcome"] == "partial_transfer_reconciled"
    assert len(backend.calls) == 1


def test_prepared_partial_receipt_rejects_legacy_attempt(monkeypatch, tmp_path):
    from jev_factorio.telemetry import make_attempt

    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    memory = CampaignMemory.load(tmp_path / "checkpoint.json", "receipt-session", "bootstrap_mining")
    memory.attempt = make_attempt(
        memory.session_id, "bootstrap_mining", memory.active_plan, memory.step_index,
        memory.pending, unit_number=7,
    )
    memory.save(tmp_path / "checkpoint.json")

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.attempt["origin"] == "legacy"
    assert not resumed.memory.attempt_outcomes
    assert len(backend.calls) == 1


def test_prepared_partial_receipt_rejects_malformed_phase_before_dispatch(monkeypatch, tmp_path):
    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()

    path = tmp_path / "checkpoint.json"
    payload = json.loads(path.read_text())
    payload["attempt"]["dispatch_phases"]["transfer_rpc"]["status"] = "queued"
    path.write_text(json.dumps(payload, sort_keys=True, allow_nan=False))
    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    with pytest.raises(ValueError, match="Invalid diagnostic phase"):
        resumed.step()
    assert len(backend.calls) == 1


def test_prepared_partial_receipt_rejects_checkpoint_session_mismatch(monkeypatch, tmp_path):
    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    backend.state.session_id = "other-session"

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    with pytest.raises(ValueError, match="session"):
        resumed.step()
    assert len(backend.calls) == 1


def test_prepared_zero_receipt_requires_all_reserved_insert_source(monkeypatch, tmp_path):
    backend = native_transfer_backend(action="factory_insert", quantity=0, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    backend.lua.globals().source_count = 19

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.pending["dispatch"] == "prepared"
    assert not resumed.memory.attempt_outcomes
    assert len(backend.calls) == 1


def test_prepared_no_rpc_with_exact_receipt_does_not_retry_or_reconcile(monkeypatch, tmp_path):
    # A partial transfer cannot return from current factory.lua. Use the producer's
    # failed path, then remove the terminal subphase record while retaining effect.
    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    saved = CampaignMemory.load(tmp_path / "checkpoint.json", "receipt-session", "bootstrap_mining")
    saved.attempt["dispatch_phases"].pop("transfer_rpc")
    saved.save(tmp_path / "checkpoint.json")

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.pending["dispatch"] == "prepared"
    assert resumed.memory.attempt["id"] == saved.attempt["id"]
    assert not resumed.memory.attempt_outcomes
    assert len(backend.calls) == 1


def test_ambiguous_partial_receipt_rejects_tick_after_current_observation(monkeypatch, tmp_path):
    backend = native_transfer_backend(action="factory_insert", quantity=10, rpc_status="failed")
    install_plan(monkeypatch, backend, reserve_transfer=True)
    first = controller(tmp_path, backend, max_pending_polls=1)
    with pytest.raises(SimulatedProcessStop):
        first.step()
    backend.observe()
    saved = CampaignMemory.load(tmp_path / "checkpoint.json", "receipt-session", "bootstrap_mining")
    saved.pending["dispatch"] = "ambiguous"
    saved.save(tmp_path / "checkpoint.json")
    backend.lua.globals().storage.campaign.receipts["transfer:0"]["tick"] = backend.state.tick + 1

    resumed = controller(tmp_path, backend, resume=True, max_pending_polls=1)
    result = resumed.step()
    assert result["status"] == "uncertain"
    assert resumed.memory.attempt["id"] == saved.attempt["id"]
    assert not resumed.memory.attempt_outcomes
    assert len(backend.calls) == 1
