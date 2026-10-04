"""Qualify the fresh current observer; keep legacy bridge gates independent."""
from copy import deepcopy
import hashlib
from importlib.resources import files
import json

import pytest

from jev_factorio.backends.native_attachment import PROBE, readback, _asset_source
from jev_factorio.backends.native_current_attachment import (
    CURRENT_MODULES, current_connector_snapshot_command,
)
from test_native_reattach import qualified


def current():
    row = qualified()
    row['modules'] = {name: name in CURRENT_MODULES for name in row['modules']}
    row['native_installation'] = {
        'schema': 'jev.native-installation.v2', 'profile': False,
        'session_id': row['session_id'], 'actor_unit': row['actor_unit'],
        'assets': {name: hashlib.sha256(_asset_source(name).read_bytes()).hexdigest()
                   for name in CURRENT_MODULES},
    }
    return row


def snapshot(row):
    return {'schema': 1, 'session_id': row['session_id'], 'actor_unit': row['actor_unit'],
            'tick': 100, 'connector_ownership': {'protocol': 1, 'session_id': row['session_id'],
                                               'tick': 100, 'routes': {}}}


class Client:
    def __init__(self, row, observed=None):
        self.row, self.observed, self.sent = row, observed or snapshot(row), []

    def send_command(self, command):
        self.sent.append(command)
        if len(self.sent) == 1:
            assert command == '/sc ' + PROBE
            return json.dumps(self.row)
        assert len(self.sent) == 2
        assert command == current_connector_snapshot_command(self.row)
        return json.dumps(self.observed)


def test_fresh_source_reattaches_only_after_coherent_native_snapshot():
    client = Client(current())
    before = deepcopy(client.row)
    result = readback(client)
    assert len(client.sent) == 2
    assert result['connector_snapshot_qualified'] is True
    assert result['connector_snapshot_tick'] == 100
    assert result['connector_observer_bridge_qualified'] is False
    assert result['modules']['connector_observer_bridge_v1'] is False
    assert client.row == before


@pytest.mark.parametrize('change', ['asset', 'profile', 'qualified', 'missing_module', 'solid_module'])
def test_source_or_callback_mismatch_stops_before_observation(change):
    row = current()
    if change == 'asset': row['native_installation']['assets']['factory'] = '0' * 64
    elif change == 'profile': row['native_installation']['profile'] = 'unknown'
    elif change == 'qualified': row['qualified'] = False
    elif change == 'missing_module':
        row['modules']['input_routes'] = False
        row['native_installation']['assets'].pop('input_routes')
    elif change == 'solid_module':
        row['modules']['solid_routes'] = True
        row['native_installation']['assets']['solid_routes'] = '0' * 64
    client = Client(row)
    with pytest.raises(RuntimeError):
        readback(client)
    assert len(client.sent) == 1


@pytest.mark.parametrize('change', ['session', 'actor', 'tick', 'inner_tick', 'active', 'routes',
                                  'protocol_bool', 'unexpected_field', 'absent_ownership'])
def test_invalid_coherent_snapshot_is_not_attachment_authority(change):
    row = current()
    observed = snapshot(row)
    if change == 'session': observed['session_id'] = 'other'
    elif change == 'actor': observed['actor_unit'] += 1
    elif change == 'tick': observed['tick'] = False
    elif change == 'inner_tick': observed['connector_ownership']['tick'] += 1
    elif change == 'active': observed['connector_ownership']['active'] = {'id': 'paid-route'}
    elif change == 'routes': observed['connector_ownership']['routes'] = {'route': {}}
    elif change == 'protocol_bool': observed['connector_ownership']['protocol'] = True
    elif change == 'unexpected_field': observed['other'] = 1
    elif change == 'absent_ownership': observed['connector_ownership'] = None
    with pytest.raises(RuntimeError, match='snapshot requires reconciliation'):
        readback(Client(row, observed))


def lua_case():
    lua = pytest.importorskip('lupa.lua52').LuaRuntime(unpack_returned_tuples=True)
    row = current()
    results = []
    lua.globals().rcon = lua.table_from({'print': results.append})
    lua.globals().helpers = lua.table_from({
        'json_to_table': lambda raw: lua.table_from(json.loads(raw), recursive=True),
        'table_to_json': lambda value: value,
    })
    lua.globals().assets = lua.table_from(row['native_installation']['assets'], recursive=True)
    lua.execute('''
        local a={valid=true,unit_number=17,force={},surface={}}
        local p={connected=true,character=a,force=a.force,surface=a.surface,cheat_mode=false}
        game={speed=1,tick=100,tick_paused=false,get_player=function() return p end}
        local ownership={protocol=1,session_id='synthetic-session',tick=100,routes={}}
        local c={connector_ledger={protocol=1,routes={}}}
        c.observe=function() return {tick=game.tick,connector_ownership=ownership} end
        c.observe_connector_ownership=function() return ownership end
        c.connector_begin=function() end;c.connector_finish=function() end;c.connector_page=function() end
        jev_fle_runtime={jev_session_id='synthetic-session',agent_characters={[1]=a},campaign=c,
          native_installation={schema='jev.native-installation.v2',session_id='synthetic-session',
            actor_unit=17,assets=assets,callbacks={observe=c.observe,connector_observe=c.observe_connector_ownership,
              connector_begin=c.connector_begin,connector_finish=c.connector_finish,connector_page=c.connector_page}}}
    ''')
    return lua, row, results


def test_lua_observation_proves_direct_snapshot_without_installing_or_marking_runtime():
    lua, row, results = lua_case()
    lua.execute(current_connector_snapshot_command(row).removeprefix('/sc '))
    assert len(results) == 1 and results[0]['tick'] == 100
    assert lua.eval('jev_fle_runtime.connector_observer_bridge_v1') is None
    assert lua.eval('jev_fle_runtime.native_installation.profile') is None
    assert lua.eval('jev_fle_runtime.campaign.connector_ledger.active') is None


@pytest.mark.parametrize('tamper', [
    "jev_fle_runtime.jev_session_id='other'",
    "jev_fle_runtime.native_installation.assets.factory='other'",
    "jev_fle_runtime.campaign.observe=function() error('replaced') end",
    "jev_fle_runtime.campaign.connector_ledger.active={id='paid'}",
    "jev_fle_runtime.campaign.connector_ledger.routes.paid={}",
    "jev_fle_runtime.solid_routes={}",
    "game.speed=2",
    "game.get_player().connected=false",
    "local c=jev_fle_runtime.campaign;local prior=c.observe;c.observe=function() local f=prior();"
    "f.connector_ownership=nil;return f end;jev_fle_runtime.native_installation.callbacks.observe=c.observe",
])
def test_lua_rejects_changed_native_identity_callbacks_or_nonempty_ledger(tamper):
    lua, row, results = lua_case()
    lua.execute(tamper)
    with pytest.raises(Exception, match='assertion failed'):
        lua.execute(current_connector_snapshot_command(row).removeprefix('/sc '))
    assert results == []
