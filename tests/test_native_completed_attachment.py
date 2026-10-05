"""Completed routes attach only with exact checkpoint-owned paid cell evidence."""
from copy import deepcopy
import json

import pytest

from jev_factorio.backends.native_attachment import PROBE, readback
from jev_factorio.backends.native_current_attachment import current_connector_snapshot_command
from jev_factorio.connector_checkpoint import reconcile
from test_connector_checkpoint import fixture, RECEIPT
from test_native_current_attachment import current


class Client:
    def __init__(self):
        memory, snapshot, native = fixture()
        reconcile(memory, snapshot, native, resume=True)
        self.binding = memory.connector_ownership
        self.row = current()
        self.row['session_id'] = 'test-session'
        self.row['actor_unit'] = 13
        self.row['native_installation'].update(session_id='test-session', actor_unit=13)
        self.ownership = deepcopy(snapshot.factory['connector_ownership'])
        self.ownership.pop('active')
        self.ownership['tick'] = 100
        self.native = native
        self.commands = []
        self.reads = 0
        self.after = None

    def send_command(self, command):
        self.commands.append(command)
        if command == '/sc ' + PROBE:
            return json.dumps(self.row)
        if 'connector_page(' in command:
            return self.native.command(command.removeprefix('/sc '))
        assert command == current_connector_snapshot_command(self.row, completed_routes=True)
        self.reads += 1
        ownership = deepcopy(self.ownership)
        if self.reads == 2 and self.after:
            self.after(ownership)
        return json.dumps({'schema': 1, 'session_id': 'test-session', 'actor_unit': 13,
                           'tick': ownership['tick'], 'connector_ownership': ownership,
                           'completed_craft': False,
                           'settled_factory': {'sites': {}, 'output_offers': {}, 'outpost_offers': {}}})


def test_completed_routes_attach_read_only_after_both_summary_and_paid_cells_match():
    client = Client()
    original = deepcopy(client.binding)
    result = readback(client, checkpoint_binding=client.binding)
    assert result['connector_snapshot_qualified'] is True
    assert result['connector_snapshot_ownership'] == client.ownership
    assert client.binding == original
    assert len(client.commands) == 4
    assert not any('c.observe()' in command for command in client.commands)


@pytest.mark.parametrize('change', ['unit', 'position', 'payment', 'endpoint', 'missing',
                                  'extra', 'active', 'fault', 'regression', 'changed_after'])
def test_retained_route_mismatch_cannot_become_attachment_authority(change):
    client = Client()
    original = deepcopy(client.binding)
    row = client.ownership['routes'][RECEIPT]
    if change == 'unit': client.native.cells[0]['unit_number'] += 1
    elif change == 'position': client.native.cells[0]['position']['x'] += 1
    elif change == 'payment': client.native.cells[0].update(paid=0, external=True)
    elif change == 'endpoint': row['target_unit'] += 1
    elif change == 'missing': client.ownership['routes'].clear()
    elif change == 'extra': client.ownership['routes']['other'] = {}
    elif change == 'active': client.ownership['active'] = RECEIPT
    elif change == 'fault': row['state'] = 'fault'
    elif change == 'regression': client.after = lambda owned: owned.update(tick=99)
    elif change == 'changed_after':
        client.after = lambda owned: owned['routes'][RECEIPT].update(paid=0)
    with pytest.raises((RuntimeError, ValueError)):
        readback(client, checkpoint_binding=client.binding)
    assert client.binding == original


@pytest.mark.parametrize('change', ['actor', 'partial', 'external', 'pending'])
def test_unqualified_checkpoint_rejected_before_native_snapshot(change):
    client = Client()
    row = client.binding['routes'][RECEIPT]
    if change == 'actor': row['actor_unit'] += 1
    elif change == 'partial': row.update(state='building', owned=False)
    elif change == 'external':
        row.update(owned=False, paid=0, external=1)
        row['cells'][0].update(paid=False, external=True)
    elif change == 'pending': row['pending'] = 1
    with pytest.raises((ValueError, RuntimeError)):
        readback(client, checkpoint_binding=client.binding)
    assert len(client.commands) == 1


def test_completed_path_still_requires_exact_full_source_profile():
    client = Client()
    client.row['native_installation']['assets']['factory'] = '0' * 64
    with pytest.raises(RuntimeError):
        readback(client, checkpoint_binding=client.binding)
    assert len(client.commands) == 1


def installed_case():
    from test_native_optional_profiles import _case, SESSION, ACTOR
    from jev_factorio.connector_checkpoint import capture
    lua, output = _case(observations=True, craft=True, buffers=True,
                        inputs=True, outposts=True)
    client = Client()
    row = deepcopy(client.native.row)
    row.update(session_id=SESSION, actor_unit=ACTOR, cells=deepcopy(client.native.cells))
    lua.globals().retained = lua.table_from(row, recursive=True)
    lua.execute('''
        local p=game.get_player(1);local c=jev_fle_runtime.campaign
        c.entities[retained.source]={valid=true,unit_number=retained.source_unit}
        c.entities[retained.target]={valid=true,unit_number=retained.target_unit}
        p.surface.find_entity=function(name,pos)
          return {valid=true,unit_number=retained.cells[1].unit_number,force=p.force}
        end
        c.connector_ledger.routes[retained.id]=retained
    ''')
    class Installed:
        def send_command(self, command):
            output.clear()
            lua.execute(command.removeprefix('/sc '))
            return output.pop()
    installed = Installed()
    from types import SimpleNamespace
    native = SimpleNamespace(command=lambda script: installed.send_command('/sc '+script))
    summary = {k:v for k,v in row.items() if k != 'cells'}
    binding = {'protocol': 1, 'session_id': SESSION,
               'routes': {RECEIPT: capture(native, RECEIPT, summary)}}
    return lua, installed, binding


def test_actual_installed_lua_completed_route_attaches_without_owner_mutation():
    from test_native_optional_profiles import _owner_state_json
    lua, client, binding = installed_case()
    before = _owner_state_json(lua)
    result = readback(client, checkpoint_binding=binding)
    assert result['connector_snapshot_qualified'] is True
    assert _owner_state_json(lua) == before


@pytest.mark.parametrize('tamper', [
    "retained.state='building'", "retained.pending=1",
    "jev_fle_runtime.campaign.connector_ledger.active=retained.id",
    "jev_fle_runtime.production_sites.owned.retained={}",
    "jev_fle_runtime.output_buffers.cells.retained={}",
    "jev_fle_runtime.input_routes.offers.retained={}",
    "jev_fle_runtime.mining_outposts.receipts.retained={}",
    "jev_fle_runtime.campaign.entities[retained.target].unit_number=99",
])
def test_actual_lua_rejects_pending_or_optional_owner_state_without_mutating_it(tamper):
    from test_native_optional_profiles import _owner_state_json
    lua, client, binding = installed_case()
    lua.execute(tamper)
    before = _owner_state_json(lua)
    with pytest.raises(Exception):
        readback(client, checkpoint_binding=binding)
    assert _owner_state_json(lua) == before


def test_make_backend_forwards_checkpoint_binding_before_start(monkeypatch):
    from jev_factorio.main import make_backend
    from jev_factorio.backends.fle import FleBackend
    calls = []
    monkeypatch.setattr(FleBackend, 'start', lambda self, **kwargs: calls.append(kwargs))
    binding = Client().binding
    make_backend('fle', resume=True, connector_binding=binding)
    assert calls == [{'resume': True, 'adopt_session': False,
                      'connector_witness_path': None, 'connector_binding': binding}]


def settled_case():
    lua, client, binding = installed_case()
    lua.execute('''
      local rt=jev_fle_runtime;local p=game.get_player(1)
      local e={valid=true,name='stone-furnace',unit_number=200,position={x=8,y=9},
               surface=p.surface,force=p.force}
      rt.campaign.entities['recipe:iron-plate']=e
      site={role='recipe:iron-plate',item='iron-plate',ore='iron-ore',
            anchor='cell-site:iron-ore:8:9:0:1',source_unit=200,entity=e,
            position={x=8,y=9},surface=p.surface,force=p.force,belt_count=8}
      output_offer={source='recipe:iron-plate',item='iron-plate',source_unit=200,
        source_position={x=8,y=9},entity=e,layout='output:200:joint',
        chest_position={x=10.5,y=9.5},inserter_position={x=9.5,y=9.5},direction=4,
        chest_role='output-chest:200',inserter_role='output-arm:200',parts={}}
      outpost_offer={resource='iron-ore',layout='outpost:iron-ore:1',
        surface=p.surface,force=p.force,parts={},patch={},steps={
         {part='chest',name='wooden-chest',direction=0,position={x=12.5,y=3.5}},
         {part='drill',name='burner-mining-drill',direction=4,position={x=12,y=4}}}}
      rt.production_sites.owned['recipe:iron-plate']=site
      rt.output_buffers.offers['recipe:iron-plate']=output_offer
      rt.mining_outposts.offers['iron-ore']=outpost_offer
      -- Observer execution would clear or regenerate proposals. It must never run.
      rt.campaign.observe_production_sites=function() error('stateful observer called') end
    ''')
    return lua, client, binding


def test_settled_manual_furnace_and_unspent_offers_attach_without_observer_or_mutation():
    lua, client, binding = settled_case()
    result = readback(client, checkpoint_binding=binding)
    assert result['connector_snapshot_qualified'] is True
    assert lua.eval("jev_fle_runtime.production_sites.owned['recipe:iron-plate']==site")
    assert lua.eval("jev_fle_runtime.output_buffers.offers['recipe:iron-plate']==output_offer")
    assert lua.eval("jev_fle_runtime.mining_outposts.offers['iron-ore']==outpost_offer")
    assert lua.eval('next(output_offer.parts)==nil and next(outpost_offer.parts)==nil')


@pytest.mark.parametrize('tamper', [
    'site.source_unit=999', 'site.position.x=99', 'site.force={}',
    "site.role='growth:iron-plate'", "site.pending={}", 'site.belt_count=65',
    'output_offer.parts.chest={unit_number=888}', "output_offer.fault='lost'",
    'output_offer.source_unit=999', 'output_offer.source_position.y=99',
    'outpost_offer.parts.drill={unit_number=888}', 'outpost_offer.pending={}',
    'outpost_offer.steps[1].unit_number=888', 'outpost_offer.force={}',
    "jev_fle_runtime.campaign.entities['output-chest:200']={valid=true,unit_number=888}",
    "jev_fle_runtime.production_sites.offers['recipe:copper-plate']={}",
])
def test_settled_qualification_refuses_paid_pending_faulted_or_mismatched_owners(tamper):
    from lupa.lua52 import LuaError
    lua, client, binding = settled_case()
    lua.execute(tamper)
    with pytest.raises((LuaError, ValueError, RuntimeError)):
        readback(client, checkpoint_binding=binding)


def test_settled_owner_change_between_detail_pages_is_rejected():
    lua, client, binding = settled_case()
    send = client.send_command
    def changing(command):
        result = send(command)
        if 'connector_page(' in command:
            lua.execute("output_offer.layout='output:200:changed'")
        return result
    client.send_command = changing
    with pytest.raises(RuntimeError, match='ledger changed'):
        readback(client, checkpoint_binding=binding)
