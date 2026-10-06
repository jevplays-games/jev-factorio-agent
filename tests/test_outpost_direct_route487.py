"""A fresh direct-route offer beats unpaid outpost work, not paid prefixes."""

import pytest

from jev_factorio import mining_outposts
from jev_factorio.input_routes import sources as input_route_sources
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from test_mining_outposts import build, lua_runtime, row, state_fixture


def _direct_route(state, *, route_state="proposed"):
    return {
        "source": "recipe:iron-plate",
        "source_unit": state.factory["entities"]["recipe:iron-plate"]["unit_number"],
        "ore": "iron-ore",
        "item": "iron-plate",
        "layout": "input-route:iron-plate:1",
        "state": route_state,
        "topology": False,
        "reserve_belts": 0,
        "parts": {},
        "flow": {},
        "steps": [
            {"part": "inserter", "name": "burner-inserter",
             "position": {"x": 1.5, "y": -1.5}, "direction": 0},
            {"part": "belt:1", "name": "transport-belt",
             "position": {"x": 2.5, "y": -1.5}, "direction": 0},
            {"part": "drill", "name": "burner-mining-drill",
             "position": {"x": 3, "y": 0}, "direction": 0},
        ],
    }


def _add_direct_route(state, *, route_state="proposed"):
    direct = _direct_route(state, route_state=route_state)
    state.factory["input_routes"]["sources"][direct["source"]] = direct
    assert input_route_sources(state)[direct["source"]] == direct
    return direct


def test_valid_direct_route_offer_rejects_stale_unpaid_python_step_and_wins_replan():
    state, catalog = state_fixture()
    selected = MiningOutpostPlanner(catalog, state, "rocket_launch")._need("iron-ore", 20)
    stale_step = selected.steps[0]
    assert stale_step.action == mining_outposts.COMMAND
    assert stale_step.parameters["part"] == "chest"
    assert stale_step.allowed(state)

    _add_direct_route(state)
    assert not stale_step.allowed(state)

    fresh = MiningOutpostPlanner(catalog, state, "rocket_launch")._need("iron-ore", 20)
    assert fresh.steps[0].action == "factory_gather"
    assert fresh.steps[0].parameters["resource"] == "iron-ore"


@pytest.mark.parametrize("outpost_state", ["proposed", "building"])
def test_unpaid_outpost_state_is_rejected_even_after_prepare_froze_geometry(outpost_state):
    state, catalog = state_fixture()
    row(state)["state"] = outpost_state
    assert row(state)["parts"] == {}
    stale_step = MiningOutpostPlanner(catalog, state, "rocket_launch")._need(
        "iron-ore", 20).steps[0]
    _add_direct_route(state)
    assert not stale_step.allowed(state)


def test_valid_direct_route_offer_preserves_paid_outpost_prefix():
    state, catalog = state_fixture()
    build(state, "chest", receipt="paid-chest-prefix")
    _add_direct_route(state)

    continuation = MiningOutpostPlanner(catalog, state, "rocket_launch")._need("iron-ore", 20)
    assert continuation.steps[0].action == mining_outposts.COMMAND
    assert continuation.steps[0].parameters["part"] == "drill"
    assert continuation.steps[0].allowed(state)


def test_committed_direct_route_still_rejects_outpost_work():
    state, catalog = state_fixture()
    stale_step = MiningOutpostPlanner(catalog, state, "rocket_launch")._need("iron-ore", 20).steps[0]
    _add_direct_route(state, route_state="building")
    assert not stale_step.allowed(state)


def _lua_attempt_prepare(lua, *, route_offer=None, mutation=None):
    lua.execute("""
        local row=observed()
        p={resource='iron-ore',layout=row.layout,part='chest',receipt='outpost-chest-487'}
    """)
    if mutation:
        lua.execute(mutation)
    if route_offer is not None:
        lua.execute("storage.input_routes.offers['recipe:iron-plate']=" + route_offer)
    lua.execute("""
        local ok,err=pcall(storage.campaign.prepare_mining_outpost,p)
        lua_prepare_result={accepted=ok,error=err,placements=placements,
                            chest_count=stock['wooden-chest'],cell_frozen=storage.mining_outposts.cells['iron-ore']~=nil}
    """)


def _lua_valid_direct_offer():
    # Match the complete cached input_routes.survey offer shape. The standalone
    # producer itself is covered independently by test_input_routes_lua.py.
    return """{source='recipe:iron-plate',source_unit=17,source_position={x=10,y=0},
        entity=source,item='iron-plate',ore='iron-ore',output_layout='output:17',
        layout='input:17:1',steps={
            {part='inserter',name='burner-inserter',position={x=1.5,y=-1.5},direction=0},
            {part='belt:1',name='transport-belt',position={x=2.5,y=-1.5},direction=0},
            {part='drill',name='burner-mining-drill',position={x=3,y=0},direction=0}},
        parts={},belt_count=1,reserve_belts=0}"""


@pytest.mark.parametrize("malformation", ["empty_object", "unknown_state", "non_table"])
def test_actual_lua_unpaid_preflight_fails_closed_on_present_malformed_offer(lua_runtime, malformation):
    malformed = {
        "empty_object": "{}",
        "unknown_state": "{state='future',layout='unknown'}",
        "non_table": "true",
    }[malformation]
    _lua_attempt_prepare(lua_runtime, route_offer=malformed)
    lua_runtime.execute("""
        assert(not lua_prepare_result.accepted)
        assert(lua_prepare_result.placements==0 and lua_prepare_result.chest_count==1)
        assert(not lua_prepare_result.cell_frozen)
    """)


@pytest.mark.parametrize("offer_table", ["missing", "wrong_type"])
def test_actual_lua_unpaid_preflight_fails_closed_on_malformed_offer_registry(lua_runtime, offer_table):
    mutation = {
        "missing": "storage.input_routes.offers=nil",
        "wrong_type": "storage.input_routes.offers='malformed'",
    }[offer_table]
    _lua_attempt_prepare(lua_runtime, mutation=mutation)
    lua_runtime.execute("""
        assert(not lua_prepare_result.accepted)
        assert(lua_prepare_result.placements==0 and lua_prepare_result.chest_count==1)
        assert(not lua_prepare_result.cell_frozen)
    """)


def test_actual_lua_route_offer_before_prepare_rejects_without_placement_or_payment(lua_runtime):
    _lua_attempt_prepare(lua_runtime, route_offer=_lua_valid_direct_offer())
    lua_runtime.execute("""
        assert(not lua_prepare_result.accepted)
        assert(string.find(lua_prepare_result.error,'direct route',1,true))
        assert(lua_prepare_result.placements==0 and lua_prepare_result.chest_count==1)
        assert(not lua_prepare_result.cell_frozen)
    """)


def test_actual_lua_route_offer_between_prepare_and_build_rejects_before_payment(lua_runtime):
    lua_runtime.execute("""
        local row=observed();p={resource='iron-ore',layout=row.layout,part='chest',receipt='outpost-chest-487'}
        storage.campaign.prepare_mining_outpost(p)
        assert(placements==0 and stock['wooden-chest']==1)
        player.position=row.steps[1].position
    """)
    lua_runtime.execute("storage.input_routes.offers['recipe:iron-plate']=" + _lua_valid_direct_offer())
    lua_runtime.execute("""
        local ok,err=pcall(storage.campaign.build_mining_outpost,p)
        assert(not ok and string.find(err,'direct route',1,true))
        assert(placements==0 and stock['wooden-chest']==1)
        assert(storage.mining_outposts.cells['iron-ore'].parts.chest==nil)
    """)


def test_actual_lua_proposed_route_does_not_interrupt_paid_chest_prefix(lua_runtime):
    lua_runtime.execute("""
        c=storage.campaign;row=observed()
        p={resource='iron-ore',layout=row.layout,part='chest',receipt='paid-chest-487'}
        c.prepare_mining_outpost(p);player.position=row.steps[1].position;c.build_mining_outpost(p)
        assert(placements==1 and stock['wooden-chest']==0 and stock['burner-mining-drill']==1)
        p.part='drill';p.receipt='paid-drill-487'
    """)
    lua_runtime.execute("storage.input_routes.offers['recipe:iron-plate']=" + _lua_valid_direct_offer())
    lua_runtime.execute("""
        c.prepare_mining_outpost(p);player.position=row.steps[2].position;c.build_mining_outpost(p)
        assert(placements==2 and stock['wooden-chest']==0 and stock['burner-mining-drill']==0)
        local saved=storage.mining_outposts.cells['iron-ore']
        assert(saved.parts.chest.paid==1 and saved.parts.chest.receipt=='paid-chest-487')
        assert(saved.parts.drill.paid==1 and saved.parts.drill.receipt=='paid-drill-487')
    """)


def test_actual_lua_committed_route_still_rejects_unpaid_outpost(lua_runtime):
    lua_runtime.execute("""
        local row=observed();p={resource='iron-ore',layout=row.layout,part='chest',receipt='committed-487'}
        storage.input_routes.cells['recipe:iron-plate']={state='building',layout='committed-layout'}
        local ok,err=pcall(storage.campaign.prepare_mining_outpost,p)
        assert(not ok and string.find(err,'direct route is already committed',1,true))
        assert(placements==0 and stock['wooden-chest']==1)
    """)


@pytest.mark.parametrize("case", ["actor", "resource", "layout", "receipt", "stale_offer"])
def test_actual_lua_outpost_identity_and_replay_guards_still_prevent_payment(lua_runtime, case):
    lua_runtime.execute("""
        local row=observed();p={resource='iron-ore',layout=row.layout,part='chest',receipt='guard-487'}
    """)
    mutations = {
        "actor": "player.character={valid=true,unit_number=10}",
        "resource": "p.resource='copper-ore'",
        "layout": "p.layout='stale-layout'",
        "receipt": "storage.mining_outposts.receipts['guard-487']=99",
        "stale_offer": "storage.mining_outposts.offers['iron-ore'].fault='stale'",
    }
    lua_runtime.execute(mutations[case])
    lua_runtime.execute("""
        local ok=pcall(storage.campaign.prepare_mining_outpost,p)
        assert(not ok and placements==0 and stock['wooden-chest']==1)
    """)
