"""Execute attachment Lua with the captured three unspent output proposals."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.backends.native_attachment import readback
from test_native_completed_attachment import installed_case


def _owner_state_json(lua):
    # Mock entities refer to Lua force/surface tables with callbacks. Preserve
    # reference identities and table contents without serializing functions.
    return lua.eval('''(function()
      local seen={}
      local function capture(value)
        if type(value)=='function' or type(value)=='userdata' then return tostring(value) end
        if type(value)~='table' then return value end
        if seen[value] then return {reference=tostring(value)} end
        seen[value]=true;local out={}
        for key,item in pairs(value) do out[tostring(key)]=capture(item) end
        return out
      end
      local rt=jev_fle_runtime
      return helpers.table_to_json(capture({sites=rt.production_sites,
        outputs=rt.output_buffers,inputs=rt.input_routes,outposts=rt.mining_outposts,
        entities=rt.campaign.entities,connectors=rt.campaign.connector_ledger}))
    end)()''')


def captured_offers():
    saved = json.loads((Path(__file__).parent / 'fixtures/native-v28-unspent-output-offers.json').read_text())
    assert set(saved['sites']) == {'recipe:iron-plate', 'recipe:copper-plate'}
    lua, client, binding = installed_case()
    # Reconstruct native userdata references in the Lua harness. Proposal
    # identities, units, positions and parts below are the captured values.
    lua.globals().captured = lua.table_from(deepcopy(saved['offers']), recursive=True)
    lua.execute('''
      local rt=jev_fle_runtime;local p=game.get_player(1)
      for role,row in pairs(captured) do
        assert(row.entity_same and row.entity_valid and row.source_unit==row.entity_unit)
        local entity={valid=true,name=row.entity_name,unit_number=row.entity_unit,
                      position=row.entity_position,surface=p.surface,force=p.force}
        rt.campaign.entities[role]=entity
        local offer={}
        for _,key in ipairs({'source','item','source_unit','source_position','layout',
            'chest_position','inserter_position','direction','chest_role','inserter_role','parts'}) do
          offer[key]=row[key]
        end
        offer.entity=entity;rt.output_buffers.offers[role]=offer
      end
      steel=rt.output_buffers.offers['recipe:steel-plate']
      rt.campaign.observe_production_sites=function() error('stateful observer called') end
    ''')
    return lua, client, binding


def test_captured_three_output_offers_attach_without_mutation():
    lua, client, binding = captured_offers()
    before = _owner_state_json(lua)
    result = readback(client, checkpoint_binding=binding)
    assert result['connector_snapshot_qualified'] is True
    assert _owner_state_json(lua) == before
    assert lua.eval("jev_fle_runtime.output_buffers.offers['recipe:steel-plate']==steel and next(steel.parts)==nil")


@pytest.mark.parametrize('tamper', [
    'steel.source_unit=999', 'steel.source_position.x=99', 'steel.entity.force={}',
    'steel.parts.chest={unit_number=1}', "steel.fault='lost'", 'steel.pending={}',
    "steel.item='iron-plate'", 'steel.entity={}',
    "jev_fle_runtime.campaign.entities['output-chest:2588']={valid=true}",
    "jev_fle_runtime.output_buffers.offers['recipe:stone-brick']=steel",
    "jev_fle_runtime.production_sites.owned['recipe:steel-plate']=steel",
])
def test_steel_offer_cannot_admit_paid_faulted_unknown_or_replaced_ownership(tamper):
    from lupa.lua52 import LuaError
    lua, client, binding = captured_offers()
    lua.execute(tamper)
    before = _owner_state_json(lua)
    with pytest.raises((LuaError, RuntimeError, ValueError)):
        readback(client, checkpoint_binding=binding)
    assert _owner_state_json(lua) == before


def test_steel_offer_change_between_readbacks_is_rejected():
    lua, client, binding = captured_offers()
    send = client.send_command
    def changing(command):
        result = send(command)
        if 'connector_page(' in command:
            lua.execute("steel.layout='output:2588:changed'")
        return result
    client.send_command = changing
    with pytest.raises(RuntimeError, match='ledger changed'):
        readback(client, checkpoint_binding=binding)
