"""Native Lua checks for proposals; never clear offers to make restart pass."""
import pytest

from jev_factorio.backends.native_attachment import readback
from test_native_paid_output_attachment import paid_case
from test_steel_offer_reattachment import _owner_state_json


def offered_case():
    lua, client, connectors, bound = paid_case(complete=True)
    # Hypothetical offer using the native captured V36 shape and mock identities.
    lua.execute('''
      local rt=jev_fle_runtime;local row=output_offer
      row.flow={layout=row.layout,source_unit=row.source_unit,conservation=true,
        positive_samples=3,received=3,first_tick=100,last_tick=300}
      input_offer={source=row.source,source_unit=row.source_unit,
        source_position=row.source_position,entity=row.entity,item=row.item,ore='iron-ore',
        output_layout=row.layout,layout='input:'..row.source_unit..':joint',parts={},
        belt_count=1,reserve_belts=0,steps={
          {part='inserter',name='burner-inserter',position={x=10,y=10},direction=8},
          {part='belt:1',name='transport-belt',position={x=10,y=11},direction=0},
          {part='drill',name='burner-mining-drill',position={x=10,y=12},direction=0}}}
      rt.input_routes.offers[row.source]=input_offer
      rt.campaign.observe_input_routes=function() error('stateful observer called') end
    ''')
    return lua, client, connectors, bound


def test_unspent_input_offer_attaches_without_mutating_native_state():
    lua, client, connectors, bound = offered_case()
    before = _owner_state_json(lua)
    result = readback(client, checkpoint_binding=connectors, output_commitments=bound)
    assert result['connector_snapshot_qualified']
    assert _owner_state_json(lua) == before


@pytest.mark.parametrize('tamper', [
    'input_offer.source_unit=999', 'input_offer.source_position={x=99,y=99}',
    'input_offer.entity={}', "input_offer.item='copper-plate'", "input_offer.ore='copper-ore'",
    "input_offer.output_layout='wrong'", "input_offer.layout='input:999:joint'",
    'input_offer.parts.inserter={paid=1}', 'input_offer.pending={}', 'input_offer.reserve_belts=1',
    'input_offer.belt_count=2', 'input_offer.steps[1].direction=3',
    "input_offer.steps[2].part='belt:2'", 'input_offer.steps[2].position={x=10,y=10}',
    'input_offer.steps.extra={}', 'input_offer.steps[1].extra=true',
    'output_offer.flow=nil', 'output_offer.flow.conservation=false',
    'output_offer.flow.positive_samples=2', 'output_offer.flow.last_tick=999999',
    "jev_fle_runtime.input_routes.cells['recipe:iron-plate']=input_offer",
    "jev_fle_runtime.campaign.entities['input:200:inserter']={valid=true}",
])
def test_paid_partial_stale_or_malformed_input_proposal_is_rejected(tamper):
    from lupa.lua52 import LuaError
    lua, client, connectors, bound = offered_case()
    lua.execute(tamper)
    before = _owner_state_json(lua)
    with pytest.raises((LuaError, RuntimeError, ValueError)):
        readback(client, checkpoint_binding=connectors, output_commitments=bound)
    assert _owner_state_json(lua) == before


def test_input_offer_change_between_connector_readbacks_is_rejected():
    lua, client, connectors, bound = offered_case()
    original = client.send_command
    def changing(command):
        result = original(command)
        if 'connector_page(' in command:
            lua.execute("input_offer.layout='input:200:changed'")
        return result
    client.send_command = changing
    with pytest.raises(RuntimeError, match='ledger changed'):
        readback(client, checkpoint_binding=connectors, output_commitments=bound)
