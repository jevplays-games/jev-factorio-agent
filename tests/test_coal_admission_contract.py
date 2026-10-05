"""Admission evidence must stay bound to the exact native observation."""
from copy import deepcopy

import pytest
from lupa.lua52 import LuaRuntime

from coal_supply_fixtures import fixture
from jev_factorio import coal_supply
from jev_factorio.planning import coal_admission


BOUND = ("session_id", "tick", "actor_index", "surface_index", "force_index")
REASON = "electric_conversion_and_construction_cost_unknown"


def v2():
    snapshot = fixture()
    data = snapshot.factory["coal_supply"]
    data["protocol"] = 2
    data["admission"] = {"protocol": 1, **{key: data[key] for key in BOUND},
                         "qualified": False, "reason": REASON}
    return snapshot


def test_v1_and_bound_v2_defer_without_payback_claim():
    assert coal_admission.evaluate(fixture())["reason"] == "coal_admission_v2_unavailable"
    evidence = coal_admission.evaluate(v2())
    assert evidence["eligible"] is False and evidence["reason"] == REASON


@pytest.mark.parametrize("change", [
    lambda data: data.pop("admission"),
    lambda data: data["admission"].update(tick=data["tick"] - 1),
    lambda data: data["admission"].update(actor_index=data["actor_index"] + 1),
    lambda data: data["admission"].update(qualified=True),
    lambda data: data["admission"].update(reason="payback_proven"),
    lambda data: data["admission"].update(extra=0),
])
def test_incomplete_or_unbound_v2_is_rejected(change):
    snapshot = v2()
    change(snapshot.factory["coal_supply"])
    with pytest.raises(ValueError):
        coal_supply.sources(snapshot)


def test_lua_v2_is_explicit_and_carries_same_epoch():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    lua = LuaRuntime(unpack_returned_tuples=True)
    for file in ("tests/fixtures/solid_routes_runtime.lua", "tests/fixtures/coal_supply_runtime.lua",
                 "src/jev_factorio/lua/solid_routes.lua", "src/jev_factorio/lua/coal_supply.lua"):
        lua.execute((root / file).read_text())
    lua.execute("configure_coal()")
    assert lua.eval("coal_offer().state") == "proposed"
    assert lua.eval("storage.coal_supply.snapshot().protocol") == 1
    lua.execute("campaign.set_coal_admission_evidence(true)")
    assert lua.eval("storage.coal_supply.snapshot().protocol") == 2
    assert lua.eval("storage.coal_supply.snapshot().admission.qualified") is False
    assert lua.eval("storage.coal_supply.snapshot().admission.tick == game.tick") is True
    lua.execute("campaign.set_coal_admission_evidence(false)")
    assert lua.eval("storage.coal_supply.snapshot().protocol") == 1
    lua.execute((root / "src/jev_factorio/lua/coal_supply.lua").read_text())
    lua.execute("campaign.set_coal_admission_evidence(true)")
    assert lua.eval("storage.coal_supply.snapshot().protocol") == 2
    lua.execute("campaign.set_coal_admission_evidence(false)")
    assert lua.eval("storage.coal_supply.snapshot().protocol") == 1


def test_controller_direct_opt_in_defers_new_kit_on_unqualified_v2(tmp_path):
    from test_coal_kit_funding import Backend, controller, offers
    backend = Backend()
    backend.state = v2()
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    _, plans = offers(loop)
    assert all(not plan.id.startswith("coal-kit:") for plan in plans)
    assert loop._coal_kit_evidence["eligible"] is False
    assert not backend.calls


def test_economic_checkpoint_roundtrip_rejects_treatment_downgrade(tmp_path):
    from test_coal_kit_funding import Backend, controller, offers

    backend = Backend()
    backend.state = v2()
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    offers(loop)
    saved = backend.checkpoint.read_text()
    assert '"coal_economic_admission": true' in saved
    assert '"coal_supply_schema": 2' in saved
    resumed = controller(backend, tmp_path, resume=True, coal_economic_admission=True)
    resumed._observe()
    assert resumed.memory.coal_economic_admission is True
    with pytest.raises(ValueError, match='unbound checkpoint'):
        controller(backend, tmp_path, resume=True)


def test_existing_paid_funding_rejects_in_place_admission_upgrade(tmp_path):
    from jev_factorio.planning import coal_funding
    from test_coal_kit_funding import Backend, controller, offers
    backend = Backend()
    loop = controller(backend, tmp_path)
    snapshot, plans = offers(loop)
    plan = next(plan for plan in plans if coal_funding.MARKER in (plan.materials or {}))
    loop._commit_solid(plan, snapshot)
    assert loop.memory.coal_funding is not None
    paid = deepcopy(loop.memory.coal_funding)
    checkpoint_before = backend.checkpoint.read_bytes()
    calls_before = deepcopy(backend.calls)
    loop._coal_economic_admission = True
    with pytest.raises(ValueError, match="protocol"):
        loop._observe()
    assert loop.memory.status == 'uncertain'
    assert loop.memory.coal_funding == paid
    assert backend.checkpoint.read_bytes() == checkpoint_before
    assert backend.calls == calls_before
