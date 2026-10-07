"""A resumed adapter must inspect and reuse the complete native installation."""
from __future__ import annotations

import hashlib
import json
import sys
from importlib.resources import files
from types import SimpleNamespace

import pytest

from jev_factorio.backends.fair_actions import FairActions
from jev_factorio.backends.fle import FleBackend
from jev_factorio.backends.native_attachment import (
    WATER_ORIGIN_OBSERVATION_ASSET,
    PINNED_ASSETS, PINNED_SOURCE_COMMIT, PINNED_SOURCE_TREE, PROBE,
    readback, require_asset, prepare_install_command, NATIVE_SCHEMA,
    _installer_scripts,
    _asset_source,
)
from jev_factorio.backends.output_buffers import OutputBufferFactory
from jev_factorio.backends.input_routes import InputRouteFactory
from jev_factorio.backends.mining_outposts import MiningOutpostFactory


def qualified():
    modules = dict.fromkeys(PINNED_ASSETS, True)
    modules['successors'] = False
    modules['connector_ownership'] = False
    modules['coal_manual_journal_v1'] = False
    modules['coal_manual_cycle_v2'] = False
    modules['connector_observer_bridge_v1'] = False
    modules['bootstrap_output_v1'] = False
    return {'schema': 1, 'qualified': True, 'session_id': 'synthetic-session',
            'actor_unit': 17, 'modules': modules, 'solid_intents': [],
            'coal_targets': [], 'coal_admission_evidence': False,
            'connector_observer_bridge_qualified': False,
            'connector_snapshot_qualified': False,
            'connector_snapshot_tick': 0,
            'connector_snapshot_ownership': False,
            'native_installation': False}


def test_preflight_is_fixed_read_only_query_and_rejects_partial_chain(tmp_path, monkeypatch):
    class Client:
        def __init__(self, payload):
            self.payload = payload
            self.sent = []

        def send_command(self, command):
            self.sent.append(command)
            return json.dumps(self.payload)

    receipt = {'schema': 'jev.native-attachment.v1',
               'session_id': 'synthetic-session', 'actor_unit': 17,
               'installed_source_commit': PINNED_SOURCE_COMMIT,
               'installed_source_tree': PINNED_SOURCE_TREE,
               'installed_assets': dict(PINNED_ASSETS)}
    path = tmp_path / 'attachment.json'
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    monkeypatch.setenv('JEV_NATIVE_ATTACHMENT_RECEIPT', str(path))
    client = Client(qualified())
    assert readback(client)['session_id'] == 'synthetic-session'
    assert client.sent == ['/sc ' + PROBE]
    assert 'script.on_event' not in PROBE and 'script.on_nth_tick' not in PROBE
    assert 'fair.bind(' not in PROBE and 'campaign.observe(' not in PROBE
    assert 'c.observe()' not in PROBE
    assert 'c.observe_connector_ownership()' not in PROBE
    assert 'connector_observer_bridge_qualified' in PROBE
    assert 'c.observe==i.observer and c.transfer==i.transfer' in PROBE
    assert 'c.observe==b.observer and c.transfer==b.transfer' in PROBE
    assert 'c.observe==j.observe_wrapper and c.transfer==l.transfer' in PROBE
    assert 'nc.observe==(c and c.observe)' in PROBE
    assert 'nc.snapshot_v1==(c and c.observation_snapshot)' in PROBE
    assert 'nc.snapshot_v2==(c and c.observation_snapshot_v2)' in PROBE
    assert 'nc.connector_begin==(c and c.connector_begin)' in PROBE
    for corruption in ('qualified', 'missing_module', 'wrong_schema'):
        row = qualified()
        if corruption == 'qualified':
            row['qualified'] = False
        elif corruption == 'missing_module':
            row['modules'].pop('coal_supply')
        else:
            row['schema'] = 2
        with pytest.raises(RuntimeError, match='requires reconciliation'):
            readback(Client(row))
    receipt['actor_unit'] = 18
    path.write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match='does not match'):
        readback(Client(qualified()))
    monkeypatch.delenv('JEV_NATIVE_ATTACHMENT_RECEIPT')
    with pytest.raises(RuntimeError, match='requires a source-bound'):
        readback(Client(qualified()))


def test_resume_skips_fair_bind_and_outer_lua_reinstallation():
    attachment = qualified()
    # A current-source positive control needs an exact installed manifest.
    # The historic pinned installation retains its original Lua revision.
    attachment['native_installation'] = {
        'schema': NATIVE_SCHEMA, 'profile': False,
        'session_id': attachment['session_id'], 'actor_unit': attachment['actor_unit'],
        'assets': {name: hashlib.sha256(_asset_source(name).read_bytes()).hexdigest()
                   for name, present in attachment['modules'].items() if present},
    }
    backend = SimpleNamespace(_native_attachment=attachment)
    backend._native_attachment['solid_intents'] = []
    fair = FairActions(backend)
    assert fair.backend is backend

    class Native:
        def __init__(self):
            self.backend = backend
            self.commands = []

        def command(self, script):
            self.commands.append(script)
            raise AssertionError('Native installer or mutator ran during checked reattach')

    base = Native()
    assert OutputBufferFactory(base).native is base
    assert InputRouteFactory(base).native is base
    assert MiningOutpostFactory(base).native is base
    assert base.commands == []


def test_source_change_or_missing_capability_cannot_reattach(monkeypatch):
    attachment = qualified()
    # The actor-cleanup revision must not silently replace a pinned installation.
    with pytest.raises(RuntimeError, match='Lua source differs'):
        require_asset(attachment, 'fair_actions')
    # PR #155 changed factory.lua; the retained e759 source-bound path cannot
    # silently acquire those connector changes either.
    with pytest.raises(RuntimeError, match='Lua source differs'):
        require_asset(attachment, 'factory')
    attachment['native_installation'] = {
        'assets': {'fair_actions': hashlib.sha256(
            files('jev_factorio').joinpath('lua/fair_actions.lua').read_bytes()).hexdigest()},
    }
    assert require_asset(attachment, 'fair_actions') is True
    attachment['modules']['fair_actions'] = False
    with pytest.raises(RuntimeError, match='not installed'):
        require_asset(attachment, 'fair_actions')
    attachment['modules']['fair_actions'] = True
    monkeypatch.setitem(attachment['native_installation']['assets'], 'fair_actions', '0' * 64)
    with pytest.raises(RuntimeError, match='Lua source differs'):
        require_asset(attachment, 'fair_actions')


def test_fresh_install_records_exact_assets_and_requires_connector_snapshot_witness(
        monkeypatch, tmp_path):
    monkeypatch.delenv('JEV_NATIVE_ATTACHMENT_RECEIPT', raising=False)
    root = files('jev_factorio').joinpath('lua')
    source = root.joinpath('connector_ownership.lua').read_text()
    command = prepare_install_command(source)
    sha = hashlib.sha256(root.joinpath('connector_ownership.lua').read_bytes()).hexdigest()
    assert command.startswith(source + '\n')
    assert NATIVE_SCHEMA in command and sha in command
    with pytest.raises(RuntimeError, match='reinstallation'):
        prepare_install_command(source, {'native_installation': {}})
    row = qualified()
    row['modules']['connector_ownership'] = True
    row['modules']['connector_observer_bridge_v1'] = True
    row['connector_observer_bridge_qualified'] = True
    row['native_installation'] = {
        'schema': NATIVE_SCHEMA, 'session_id': row['session_id'],
        'actor_unit': row['actor_unit'], 'profile': False,
        'assets': {name: hashlib.sha256(
            root.joinpath(WATER_ORIGIN_OBSERVATION_ASSET if name == 'observation_v2'
                          else name + '.lua').read_bytes()).hexdigest()
                   for name, enabled in row['modules'].items() if enabled},
    }
    class Client:
        def send_command(self, command):
            assert command == '/sc ' + PROBE
            return json.dumps(row)
    from native_connector_witness_helpers import write_snapshot_witness
    receipt, witness = write_snapshot_witness(tmp_path, row)
    assert readback(Client(), receipt_path=receipt,
                    connector_witness_path=witness)['native_installation'][
                        'assets']['connector_ownership'] == sha
    bridge_sha = hashlib.sha256(root.joinpath('connector_observer_bridge_v1.lua').read_bytes()).hexdigest()
    assert readback(Client(), receipt_path=receipt, connector_witness_path=witness)[
        'native_installation']['assets'][
        'connector_observer_bridge_v1'] == bridge_sha
    assert require_asset(row, 'connector_ownership') is True
    row['native_installation']['assets']['connector_ownership'] = '0' * 64
    with pytest.raises(RuntimeError, match='differs from installed manifest'):
        readback(Client())
    row['native_installation']['assets']['connector_ownership'] = sha
    row['native_installation']['assets'].pop('connector_ownership')
    with pytest.raises(RuntimeError, match='requires reconciliation'):
        readback(Client())


def test_connector_ledger_without_observer_bridge_cannot_reattach():
    row = qualified()
    row['modules'] = dict.fromkeys(PINNED_ASSETS, False)
    row['modules']['connector_ownership'] = True
    row['modules']['coal_manual_journal_v1'] = False
    row['modules']['coal_manual_cycle_v2'] = False
    row['modules']['connector_observer_bridge_v1'] = False
    row['modules']['bootstrap_output_v1'] = False
    row['connector_observer_bridge_qualified'] = False
    row['native_installation'] = {
        'schema': NATIVE_SCHEMA, 'session_id': row['session_id'],
        'actor_unit': row['actor_unit'], 'profile': False,
        'assets': {'connector_ownership': hashlib.sha256(
            files('jev_factorio').joinpath('lua/connector_ownership.lua').read_bytes()
        ).hexdigest()},
    }
    class Client:
        def send_command(self, command):
            assert command == '/sc ' + PROBE
            return json.dumps(row)

    with pytest.raises(RuntimeError, match='observer bridge requires reconciliation'):
        readback(Client())


def test_versioned_probe_rejects_replaced_observer_closure_without_mutation():
    LuaRuntime = pytest.importorskip('lupa.lua52').LuaRuntime
    lua = LuaRuntime(unpack_returned_tuples=True)
    results = []
    lua.globals().rcon = lua.table_from({'print': results.append})
    lua.globals().helpers = lua.table_from({
        'table_to_json': lambda row: row['qualified'],
        'json_to_table': lambda encoded: lua.table_from(json.loads(encoded)),
    })
    lua.execute('''
        local actor={valid=true,unit_number=17}
        local force={};local surface={}
        actor.force=force;actor.surface=surface
            local player={connected=true,character=actor,force=force,surface=surface,
                          cheat_mode=false}
            game={speed=1,tick_paused=false,get_player=function() return player end}
            local event_handlers={}
            defines={events={on_script_path_request_finished='path',
                on_player_mined_entity='mined',on_pre_player_crafted_item='pre',
                on_player_cancelled_crafting='cancel',on_player_crafted_item='crafted',
                on_tick='tick'}}
            script={get_event_handler=function(event) return event_handlers[event] end}
            local function callback() end
            local fair={actor=callback,bind=callback,observe=callback,place=callback,
                        tick_handler=callback,path_handler=callback}
            local launch={schema=1,launch=callback,craft=callback,observer=callback,
                          transfer=callback,mined_handler=callback}
            local campaign={launch=launch.launch,craft=launch.craft,
                            observe=launch.observer,transfer=launch.transfer,
                            configure=callback,observation_snapshot=callback,
                            observation_snapshot_v2=callback}
            event_handlers[defines.events.on_script_path_request_finished]=fair.path_handler
            event_handlers[defines.events.on_player_mined_entity]=launch.mined_handler
            event_handlers[defines.events.on_tick]=fair.tick_handler
            jev_fle_runtime={jev_session_id='synthetic-session',agent_characters={[1]=actor},
                             campaign=campaign,fair=fair,launch_readiness=launch}
    ''')
    source = files('jev_factorio').joinpath('lua/factory.lua').read_text()
    marker = prepare_install_command(source)[len(source) + 1:]
    lua.execute(marker)
    lua.execute(PROBE)
    assert results.pop() is True
    lua.execute('jev_fle_runtime.campaign.observation_snapshot_v2=function() end')
    lua.execute(PROBE)
    assert results.pop() is False
    lua.execute('jev_fle_runtime.campaign.observation_snapshot_v2='
                'jev_fle_runtime.native_installation.callbacks.snapshot_v2')
    lua.execute('jev_fle_runtime.campaign.observation_snapshot=function() end')
    lua.execute(PROBE)
    assert results.pop() is False


def test_install_marker_records_once_and_refuses_changed_hash():
    lua52 = pytest.importorskip('lupa.lua52')
    LuaRuntime, LuaError = lua52.LuaRuntime, lua52.LuaError

    root = files('jev_factorio').joinpath('lua')
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.globals().jev_fle_runtime = lua.table_from({
        'jev_session_id': 'synthetic-session',
        'agent_characters': lua.table_from({1: lua.table_from(
            {'valid': True, 'unit_number': 17})}),
    })
    lua.globals().helpers = lua.table_from({
        'json_to_table': lambda encoded: lua.table_from(json.loads(encoded))})
    for name in ('fair_actions', 'factory', 'connector_ownership'):
        source = root.joinpath(name + '.lua').read_text()
        prepared = prepare_install_command(source)
        assert lua.eval('load')(prepared) is not None
        marker = prepared[len(source) + 1:]
        lua.execute(marker)
        assets = lua.globals().jev_fle_runtime.native_installation.assets
        assert assets[name] == hashlib.sha256(
            root.joinpath(name + '.lua').read_bytes()).hexdigest()
        lua.execute(marker)  # Idempotent exact same source.
    assets['factory'] = '0' * 64
    with pytest.raises(LuaError, match='Native asset revision changed'):
        lua.execute(marker.replace('connector_ownership', 'factory'))


def test_every_installer_variant_and_marker_compile_as_lua():
    LuaRuntime = pytest.importorskip('lupa.lua52').LuaRuntime

    lua = LuaRuntime(unpack_returned_tuples=True)
    for source in _installer_scripts():
        loaded = lua.eval('load')(prepare_install_command(source))
        assert callable(loaded), source[:100]


def test_resume_without_installed_campaign_stops_before_fair_install(monkeypatch):
    sent = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_command(self, command):
            sent.append(command)
            if 'jev_factorio_session' in command:
                return 'true'
            if 'agent_characters' in command:
                return 'true'
            return 'false'

        def close(self):
            pass

    class Instance:
        def __init__(self, address, tcp_port, **kwargs):
            self.rcon_client, _ = self.connect_to_server(address, tcp_port)

    monkeypatch.setitem(sys.modules, 'factorio_rcon', SimpleNamespace(RCONClient=Client))
    monkeypatch.setitem(sys.modules, 'fle.env', SimpleNamespace(FactorioInstance=Instance))
    monkeypatch.setenv('FACTORIO_RCON_PASSWORD', 'synthetic-test-only')
    with pytest.raises(RuntimeError, match='No installed native campaign'):
        FleBackend().start(resume=True)
    assert not any('fair_actions' in command or 'fair.bind' in command
                   for command in sent)
