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
                           'tick': ownership['tick'], 'connector_ownership': ownership})


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
