"""Retained paid output ownership is verified without installing Lua or observing."""
from copy import deepcopy

import pytest

from jev_factorio.backends.native_attachment import readback
from test_native_completed_attachment import settled_case


def paid_case(*, complete=False):
    lua, client, connectors = settled_case()
    lua.execute('''
      local rt=jev_fle_runtime;local p=game.get_player(1);local row=output_offer
      row.entity.get_recipe=function() return {name='iron-plate'} end
      chest={valid=true,name='wooden-chest',unit_number=888,position=row.chest_position,
        surface=p.surface,force=p.force}
      rt.campaign.entities[row.chest_role]=chest
      row.parts.chest={entity=chest,role=row.chest_role,unit_number=888,receipt='paid-chest',paid=1}
      row.built_tick=1000
      rt.output_buffers.cells[row.source]=row
      rt.output_buffers.offers[row.source]=nil
    ''')
    bound = {'recipe:iron-plate':{'source_unit':200,'layout':'output:200:joint',
        'parts':{'chest':{'role':'output-chest:200','unit_number':888,'receipt':'paid-chest','paid':1}}}}
    if complete:
        lua.execute('''
          local rt=jev_fle_runtime;local p=game.get_player(1);local row=output_offer
          arm={valid=true,name='burner-inserter',unit_number=889,position=row.inserter_position,
            surface=p.surface,force=p.force,direction=row.direction,
            pickup_target=row.entity,drop_target=chest}
          rt.campaign.entities[row.inserter_role]=arm
          row.parts.inserter={entity=arm,role=row.inserter_role,unit_number=889,receipt='paid-arm',paid=1}
        ''')
        bound['recipe:iron-plate']['parts']['inserter'] = {
            'role':'output-arm:200','unit_number':889,'receipt':'paid-arm','paid':1}
    return lua, client, connectors, bound


@pytest.mark.parametrize('complete', [False, True])
def test_paid_output_prefix_and_complete_cell_attach_read_only(complete):
    lua, client, connectors, bound = paid_case(complete=complete)
    def snapshot():
        return lua.eval('''(function()
          local seen={}
          local function copy(value)
            if type(value)=='function' then return tostring(value) end
            if type(value)~='table' then return value end
            if seen[value] then return tostring(value) end
            seen[value]=true
            local result={};local keys={}
            for key in pairs(value) do keys[#keys+1]=key end
            table.sort(keys,function(a,b) return tostring(a)<tostring(b) end)
            for _,key in ipairs(keys) do result[tostring(key)]=copy(value[key]) end
            return result
          end
          return helpers.table_to_json(copy(jev_fle_runtime))
        end)()''')
    before = snapshot()
    result = readback(client, checkpoint_binding=connectors, output_commitments=bound)
    assert result['connector_snapshot_qualified']
    assert snapshot() == before
    assert lua.eval("jev_fle_runtime.output_buffers.cells['recipe:iron-plate']==output_offer")
    assert lua.eval('output_offer.parts.chest.entity==chest and output_offer.parts.chest.paid==1')


@pytest.mark.parametrize('tamper', [
    "output_offer.parts.chest.receipt='different'", 'output_offer.parts.chest.paid=0',
    'chest.unit_number=999', 'chest.valid=false', 'chest.force={}',
    "chest.name='iron-chest'", 'chest.position={x=99,y=99}',
    "output_offer.layout='output:changed'", "output_offer.fault='changed'",
    'output_offer.pending={}', 'output_offer.parts.chest.extra=true',
    'output_offer.parts.extra={}', 'output_offer.built_tick=99999',
    "jev_fle_runtime.campaign.entities['output-chest:999']=chest",
    "jev_fle_runtime.output_buffers.cells.other=output_offer",
    "jev_fle_runtime.output_buffers.offers['recipe:iron-plate']=output_offer",
    "jev_fle_runtime.campaign.entities['output-chest:200']=nil",
    'arm.direction=8', 'arm.pickup_target={}', 'arm.drop_target={}',
])
def test_changed_or_uncheckpointed_output_owner_is_rejected(tamper):
    from lupa.lua52 import LuaError
    lua, client, connectors, bound = paid_case(complete=True)
    lua.execute(tamper)
    with pytest.raises((LuaError, ValueError, RuntimeError)):
        readback(client, checkpoint_binding=connectors, output_commitments=bound)


def test_paid_output_requires_its_checkpoint_not_only_native_presence():
    from lupa.lua52 import LuaError
    _, client, connectors, bound = paid_case()
    with pytest.raises(LuaError):
        readback(client, checkpoint_binding=connectors)
    wrong = deepcopy(bound)
    wrong['recipe:iron-plate']['parts']['chest']['receipt'] = 'wrong'
    with pytest.raises(LuaError):
        readback(client, checkpoint_binding=connectors, output_commitments=wrong)


def test_make_backend_forwards_paid_ownership_before_attachment(monkeypatch):
    from jev_factorio.main import make_backend
    from jev_factorio.backends.fle import FleBackend
    calls=[]
    monkeypatch.setattr(FleBackend, 'start', lambda self, **kw: calls.append(kw))
    bound={'captured':'checkpoint'}
    make_backend('fle', resume=True, output_commitments=bound)
    assert calls[0]['output_commitments'] is bound


def test_paid_owner_change_during_connector_detail_capture_is_rejected():
    from lupa.lua52 import LuaError
    lua, client, connectors, bound = paid_case()
    send = client.send_command
    def changing(command):
        result = send(command)
        if 'connector_page(' in command:
            lua.execute("output_offer.parts.chest.receipt='changed-during-capture'")
        return result
    client.send_command = changing
    with pytest.raises((LuaError, RuntimeError)):
        readback(client, checkpoint_binding=connectors, output_commitments=bound)
