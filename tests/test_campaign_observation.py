"""Offline protocol fixtures, not measurements of native Factorio speed."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from jev_factorio.observation import (ObservationProfile, ProfiledRcon, ProfiledTools,
                                     profile_backend, parse_snapshot, host_pressure, MAX_PAYLOAD_BYTES)
from jev_factorio.backends.observed_factory import ObservedFactory
from jev_factorio import iteration_timing as timing
from test_factory import snapshot


class Clock:
    value = 0
    def __call__(self):
        self.value += 10
        return self.value


def test_rpc_metrics_are_content_free_and_preserve_results_and_failures():
    profile = ObservationProfile(Clock())
    secret = 'secret-native-error-token'
    assert profile.rpc('discovery', lambda: secret, 4) == secret
    with pytest.raises(ValueError):
        profile.rpc(secret, lambda: (_ for _ in ()).throw(ValueError(secret)))
    result = profile.summary()
    assert result['calls']['discovery']['response_bytes'] == len(secret)
    assert result['calls']['other']['failed'] == 1
    assert secret not in json.dumps(result)
    assert result['rpc_includes_native_and_transport'] is True


def test_tool_helpers_are_profiled_even_with_private_transports():
    values = {'inspect_inventory': {'iron': 2}, 'get_entities': [1], 'nearest': (1, 2)}
    tools = NS(**{key: (lambda *args, _value=value: _value) for key, value in values.items()})
    profile = ObservationProfile(Clock())
    wrapped = ProfiledTools(tools, profile)
    assert wrapped.inspect_inventory() == {'iron': 2}
    assert wrapped.inspect_inventory(object()) == {'iron': 2}
    assert wrapped.get_entities({'drill'}) == [1]
    assert wrapped.nearest('water') == (1, 2)
    assert set(profile.summary()['subcalls']) == {'inventory', 'output_inventory', 'entities', 'nearest'}
    assert not profile.calls  # Helper time is not mislabeled transport time.


def test_profile_context_restores_transport_even_when_native_read_fails():
    client = NS(send_command=lambda command: 'same', send_commands=lambda commands: {k: 'ok' for k in commands})
    backend = NS(_instance=NS(rcon_client=client))
    with pytest.raises(RuntimeError):
        with profile_backend(backend):
            assert backend._instance.rcon_client.send_command('private') == 'same'
            assert backend._instance.rcon_client.send_commands({'a': 'one', 'b': 'two'}) == {'a': 'ok', 'b': 'ok'}
            raise RuntimeError('native fault')
    assert backend._instance.rcon_client is client
    assert backend._observation_profile is None
    assert backend.last_observation_profile['calls']['other']['count'] == 2


def test_installed_fle_helper_manager_uses_the_profiled_shared_client():
    # FLE 0.4.3 tools call connection.rcon_client, where connection is the
    # LuaScriptManager captured when each tool is constructed.
    client = NS(send_command=lambda command: 'native-result')
    manager = NS(rcon_client=client)
    tools = NS(get_entities=lambda: manager.rcon_client.send_command('get_entities'))
    backend = NS(_instance=NS(rcon_client=client, lua_script_manager=manager),
                 _observation_profile=None)
    with pytest.raises(RuntimeError, match='native fault'):
        with profile_backend(backend):
            assert ProfiledTools(tools, backend._observation_profile).get_entities() == 'native-result'
            assert manager.rcon_client is backend._instance.rcon_client
            raise RuntimeError('native fault')
    assert manager.rcon_client is client
    assert backend._instance.rcon_client is client
    profile = backend.last_observation_profile
    assert profile['subcalls']['entities']['count'] == 1
    assert profile['calls']['entities']['count'] == 1
    assert profile['wall_partition_ns']['rpc'] <= profile['total_ns']
    assert 'native fault' not in json.dumps(profile)


def test_unshared_helper_manager_is_not_claimed_as_profiled_rpc():
    instance_client = NS(send_command=lambda command: 'instance')
    helper_client = NS(send_command=lambda command: 'helper')
    manager = NS(rcon_client=helper_client)
    tools = NS(get_entities=lambda: manager.rcon_client.send_command('get_entities'))
    backend = NS(_instance=NS(rcon_client=instance_client, lua_script_manager=manager),
                 _observation_profile=None)
    with profile_backend(backend):
        assert ProfiledTools(tools, backend._observation_profile).get_entities() == 'helper'
        assert manager.rcon_client is helper_client
    assert backend._instance.rcon_client is instance_client
    assert backend.last_observation_profile['subcalls']['entities']['count'] == 1
    assert backend.last_observation_profile['calls'] == {}
    assert backend.last_observation_profile['helper_retry_attempts'] is None


def test_native_entity_lookup_profiles_opaque_helpers_and_preserves_error(monkeypatch):
    from jev_factorio.backends.fle import FleBackend, SessionRcon
    from jev_factorio.backends.native_factory import NativeFactory

    position = lambda **values: NS(**values)
    monkeypatch.setitem(sys.modules, 'fle.env', NS(Position=position))
    expected_error = RuntimeError('private native detail')
    helper_calls = 0
    sent = []
    entity_lookups = []

    def send_command(command):
        nonlocal helper_calls
        sent.append(command)
        if 'get_entity_fixture' in command:
            helper_calls += 1
            if helper_calls == 4:
                raise expected_error
            return 'same-entity-result'
        if 'entities["source"]' in command:
            return '{"name":"small-electric-pole","position":{"x":1,"y":2}}'
        if 'entities["target"]' in command:
            return '{"name":"small-electric-pole","position":{"x":3,"y":4}}'
        raise AssertionError('unexpected fixture command')

    session = SessionRcon(NS(send_command=send_command))
    def get_entity(prototype, point):
        entity_lookups.append((prototype, point.x, point.y))
        assert session.send_command('/sc get_entity_fixture') == 'same-entity-result'
        return NS(position=point)

    tools = NS(get_entity=get_entity)
    backend = FleBackend()
    backend._instance = NS(namespace=tools, rcon_client=session, _native_attachment=None)
    factory = NativeFactory.__new__(NativeFactory)
    factory.backend = backend
    factory.prototype = lambda name: name

    ledger = timing.Ledger()
    token = timing._CURRENT.set(ledger)
    try:
        with pytest.raises(RuntimeError) as caught:
            with ledger.span('iteration'):
                for role in ('source', 'target', 'source', 'target'):
                    assert factory.entity(role).position.x in (1, 3)
    finally:
        timing._CURRENT.reset(token)

    assert caught.value is expected_error
    report = ledger.snapshot(1, 'error')
    assert report['partition_complete'] is True
    assert report['phases']['fle_helper']['calls'] == 4
    assert report['phases']['fle_helper']['failed'] == 1
    assert report['native_io']['command_calls'] == 8
    assert report['native_io']['failed_calls'] == 1
    assert len(sent) == 8
    assert len(entity_lookups) == 4
    assert 'private native detail' not in json.dumps(report)


def test_action_factory_connect_profiles_native_geometry_and_preserves_error(monkeypatch):
    from jev_factorio.backends.fair_actions import FairActions
    from jev_factorio.backends.fle import FleBackend, SessionRcon
    from jev_factorio.backends.native_factory import NativeFactory

    monkeypatch.setitem(sys.modules, 'fle.env', NS(Position=lambda **values: NS(**values)))
    expected_error = RuntimeError('private native geometry detail')
    sent, connected = [], []

    def endpoint(role, unit, x):
        return {'role': role, 'name': 'assembling-machine-1', 'unit_number': unit,
                'position': {'x': x, 'y': 0.5}, 'direction': 0, 'orientation': 0.0,
                'surface_index': 1, 'force_index': 1, 'quality': 'normal',
                'bounding_box': {'left_top': {'x': x - 1, 'y': -0.5},
                                 'right_bottom': {'x': x + 1, 'y': 1.5},
                                 'orientation': 0.0}}

    geometry = {'schema': 'jev.native-pole-geometry.v1', 'base_version': '2.0.77',
                'session_id': 'offline-geometry-profile', 'tick': 10, 'actor_unit': 7,
                'surface_index': 1, 'force_index': 1,
                'supply_area_distance': 2.5, 'maximum_wire_distance': 7.5,
                'source': endpoint('source', 101, 0.5),
                'target': endpoint('target', 202, 8.5)}

    def send_command(command):
        sent.append(command)
        assert 'bounding_box=box(e.bounding_box)' in command
        if len(sent) == 2:
            raise expected_error
        return json.dumps(geometry)

    backend = FleBackend()
    backend._instance = NS(namespace=NS(), rcon_client=SessionRcon(NS(send_command=send_command)))
    backend._fair = NS(validate_pole_geometry=FairActions.validate_pole_geometry,
                       connect=lambda *args, **kwargs: connected.append((args, kwargs)))
    factory = NativeFactory.__new__(NativeFactory)
    factory.backend, factory.catalog = backend, NS(version='2.0.77')
    factory.prototype = lambda name: name
    parameters = {'source': 'source', 'target': 'target',
                  'kind': 'small-electric-pole', 'fluid': 'electricity'}
    ledger = timing.Ledger()
    token = timing._CURRENT.set(ledger)
    try:
        with pytest.raises(RuntimeError) as caught:
            with ledger.span('iteration'):
                assert factory.execute('factory_connect', parameters).startswith(
                    'Constructed small-electric-pole connection')
                factory.execute('factory_connect', parameters)
    finally:
        timing._CURRENT.reset(token)

    assert caught.value is expected_error
    report = ledger.snapshot(1, 'error')
    assert report['partition_complete'] is True
    assert report['native_io']['command_calls'] == 2
    assert report['native_io']['failed_calls'] == 1
    assert report['phases'].get('fle_helper', {}).get('calls', 0) == 0
    assert len(sent) == 2 and len(connected) == 1
    assert connected[0][1]['pole_geometry'] == geometry
    assert connected[0][1]['identity'] == parameters
    assert 'private native geometry detail' not in json.dumps(report)


@pytest.mark.parametrize('amount,unit,nanos', [('2.5','ms',2500000), ('3','us',3000), ('1','s',1000000000)])
def test_native_profiler_units_are_explicit(amount, unit, nanos):
    profile = ObservationProfile(Clock())
    assert parse_snapshot(f'JEV_NATIVE_PROFILE|discovery|{amount} {unit}\nJEV_SNAPSHOT|{{"schema":1}}', profile) == {'schema': 1}
    assert profile.native_ns['discovery'] == nanos
    assert profile.decode_ns > 0


def test_unknown_localized_native_time_is_unknown_not_zero():
    profile = ObservationProfile()
    parse_snapshot('JEV_NATIVE_PROFILE|discovery|0,4 ms\nJEV_SNAPSHOT|{"schema":1}', profile)
    assert not profile.summary()['native_timing_available']


@pytest.mark.parametrize('raw', ['', 'JEV_SNAPSHOT|[]', 'JEV_SNAPSHOT|{"schema":2}',
    'JEV_SNAPSHOT|{"schema":1}\nJEV_SNAPSHOT|{"schema":1}', 'x' * (MAX_PAYLOAD_BYTES + 1)],
    ids=['empty', 'array', 'wrong-schema', 'duplicate', 'oversized'])
def test_missing_ambiguous_invalid_or_oversized_envelopes_fail_closed(raw):
    with pytest.raises(ValueError):
        parse_snapshot(raw, ObservationProfile())


def test_host_sampling_reads_only_fixed_counters_and_handles_unsupported_hosts(tmp_path):
    assert host_pressure(tmp_path)['available'] is False
    (tmp_path/'meminfo').write_text('MemTotal: 100 kB\nMemAvailable: 30 kB\nPrivate: 999 kB\n')
    (tmp_path/'pressure').mkdir()
    (tmp_path/'pressure'/'cpu').write_text('some avg10=1.2 avg60=0 avg300=0 total=25\n')
    result = host_pressure(tmp_path)
    assert result['memory'] == {'MemTotal_kib': 100, 'MemAvailable_kib': 30}
    assert result['pressure']['cpu']['some']['avg10'] == 1.2
    assert 'Private' not in json.dumps(result)


def observation_fixture(monkeypatch):
    env = NS(Position=lambda **kw: NS(**kw), Resource=NS(Water='water', CrudeOil='crude-oil'))
    monkeypatch.setitem(sys.modules, 'fle.env', env)
    factory = ObservedFactory.__new__(ObservedFactory)
    factory._discovery_epoch = 0
    factory.catalog = NS(version='2.0.77')
    factory.backend = NS(_observation_profile=ObservationProfile(), _resources={'wood': 'old'}, _drill=None,
                         _tools=NS(nearest=lambda resource: (_ for _ in ()).throw(ValueError('absent'))))
    state = snapshot(world_kind='fle', tick=10)
    payload = {'schema': 1, 'session_id': state.session_id, 'actor_unit': 17, 'surface_index': 1,
        'factory': {'tick': 10, 'entities': [], 'receipts': [], 'researched': [],
            'rockets_launched': 0, 'rocket_baseline': 0, 'player_connected': True, 'player_bound': True,
            'acceptance_runtime': {'session_id': state.session_id, 'actor_unit': 17, 'surface_index': 1}},
        'targets': [], 'cache': {'hits': 0, 'misses': 5}}
    factory.call = lambda *args: 'JEV_SNAPSHOT|' + json.dumps(payload)
    return factory, state, payload


def test_empty_discovery_clears_stale_targets_and_keeps_empty_receipts(monkeypatch):
    factory, state, payload = observation_fixture(monkeypatch)
    result = factory.observe(state)
    assert not factory.backend._resources
    assert result.factory['receipts'] == {} and result.factory['entities'] == {}
    assert result.victory is False


@pytest.mark.parametrize('field,value', [('session_id','other'), ('actor_unit',18), ('actor_unit',True), ('surface_index',2)])
def test_observation_rejects_identity_changes(monkeypatch, field, value):
    factory, state, payload = observation_fixture(monkeypatch)
    payload[field] = value
    with pytest.raises(ValueError):
        factory.observe(state)


@pytest.mark.parametrize('position', [{'x': True, 'y': 1}, {'x': float('nan'), 'y': 0}, {'x': '1', 'y': 0}])
def test_observation_rejects_malformed_resource_coordinates(monkeypatch, position):
    factory, state, payload = observation_fixture(monkeypatch)
    payload['targets'] = {'iron-ore': {'name': 'iron-ore', 'position': position, 'surface_index': 1}}
    with pytest.raises(ValueError):
        factory.observe(state)


@pytest.mark.parametrize('surface', [True, '1', 0, -1])
def test_observation_rejects_malformed_resource_surface(monkeypatch, surface):
    factory, state, payload = observation_fixture(monkeypatch)
    payload['targets'] = {'iron-ore': {'name': 'iron-ore', 'position': {'x': 1, 'y': 1},
                                     'surface_index': surface}}
    with pytest.raises(ValueError, match='discovery identity'):
        factory.observe(state)


def test_observation_rejects_boolean_runtime_surface(monkeypatch):
    factory, state, payload = observation_fixture(monkeypatch)
    payload['factory']['acceptance_runtime']['surface_index'] = True
    with pytest.raises(ValueError, match='actor changed'):
        factory.observe(state)


def test_fallback_observation_attributes_chest_in_drill_drop_tile(monkeypatch):
    factory, state, payload = observation_fixture(monkeypatch)
    factory.backend._drill = NS(drop_position=NS(x=2, y=-.296875))
    payload['factory']['entities'] = {
        'bootstrap:output': {'name': 'wooden-chest',
                             'position': {'x': 2, 'y': -.5}}}
    result = factory.observe(state)
    assert result.factory['drill_output_role'] == 'bootstrap:output'


def test_receipts_and_capability_fields_are_fresh_not_cached(monkeypatch):
    factory, state, payload = observation_fixture(monkeypatch)
    for tick in (10, 11):
        payload['factory'].update(tick=tick, receipts={'fresh': {'quantity': tick}}, input_routes={'revision': tick})
        result = factory.observe(state)
        assert result.factory['receipts']['fresh']['quantity'] == tick
        assert result.factory['input_routes']['revision'] == tick


@pytest.mark.parametrize('action,invalidated', [('factory_gather',True), ('factory_place',True),
    ('factory_connect',True), ('factory_explore',True), ('factory_insert',False),
    ('factory_extract',False), ('factory_craft_job',False), ('factory_wait',False)])
def test_topology_cache_invalidates_before_even_an_ambiguous_mutation(monkeypatch, action, invalidated):
    factory = ObservedFactory.__new__(ObservedFactory)
    factory._discovery_epoch = 0
    def execute(self, name, params, *, trace=None):
        assert self._discovery_epoch == int(invalidated)
        raise TimeoutError('ambiguous native mutation')
    monkeypatch.setattr('jev_factorio.backends.native_factory.NativeFactory.execute', execute)
    with pytest.raises(TimeoutError):
        factory.execute(action, {})


@pytest.fixture
def lua_snapshot():
    from lupa import LuaRuntime
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute('''
        storage = {jev_session_id="native-test", campaign={}, fair={}}
        game = {tick=1}; helpers = {}; output = {}; calls=0; observed=0; radius=8; absent=false
        resources={}
        for i,name in ipairs({"wood","coal","iron-ore","copper-ore","stone"}) do
            resources[name]={name=name,position={x=i,y=0},valid=true,minable=true,type="resource",amount=100,unit_number=3}
        end
        player={character={unit_number=1},surface={index=1}}
        player.surface.find_entity=function(name,position) return resources[name] end
        player.update_selected_entity=function(position)
            player.selected=nil
            for _,entity in pairs(resources) do
                if entity.position == position and not entity.obscured then player.selected=entity end
            end
        end
        storage.fair.actor=function() return player end
        storage.fair.discover_mine_target=function(item,origin,distance)
            calls=calls+1
            if absent then return {} end
            local e=resources[item]
            if e.amount==0 or not e.valid or not e.minable or e.obscured then return {} end
            return {name=e.name,position=e.position,unit_number=e.unit_number,surface_index=player.surface.index}
        end
        storage.campaign.observe=function()
            observed=observed+1
            return {exploration_radius=radius, receipts={fresh=observed}}
        end
        helpers.table_to_json=function(value) captured=value; return "{}" end
        rcon={print=function(value) output[#output+1]=value end}
    ''')
    lua.execute(Path('src/jev_factorio/lua/observation.lua').read_text())
    return lua


def test_lua_positive_discovery_is_cached_but_campaign_receipts_are_not(lua_snapshot):
    lua = lua_snapshot
    lua.execute('storage.campaign.observation_snapshot(0); storage.campaign.observation_snapshot(0)')
    assert lua.globals().calls == 5
    assert lua.globals().observed == 2
    assert lua.globals().captured.cache.hits == 5
    assert lua.globals().captured.factory.receipts.fresh == 2


@pytest.mark.parametrize('change', ['game.tick=1802', 'game.tick=0', 'radius=9',
    'player.character.unit_number=2', 'player.surface.index=2', 'storage.jev_session_id="replacement"'])
def test_lua_discovery_invalidates_on_expiry_identity_radius_and_tick_epoch(lua_snapshot, change):
    lua = lua_snapshot
    lua.execute('storage.campaign.observation_snapshot(0); '+change+'; storage.campaign.observation_snapshot(0)')
    assert lua.globals().calls == 10


@pytest.mark.parametrize('change', ['resources.coal.amount=0', 'resources.coal.valid=false',
    'resources.coal.minable=false', 'resources.coal.unit_number=100'])
def test_lua_stale_or_depleted_target_is_not_reused(lua_snapshot, change):
    lua = lua_snapshot
    lua.execute('storage.campaign.observation_snapshot(0); '+change+'; storage.campaign.observation_snapshot(0)')
    assert lua.globals().calls == 6
    assert lua.globals().captured.cache.hits == 4


def test_lua_no_negative_cache_and_generation_invalidation(lua_snapshot):
    lua = lua_snapshot
    lua.execute('absent=true; storage.campaign.observation_snapshot(0); storage.campaign.observation_snapshot(0)')
    assert lua.globals().calls == 10
    lua.execute('absent=false; storage.campaign.observation_snapshot(0); storage.campaign.observation_snapshot(1)')
    assert lua.globals().calls == 20


def test_lua_new_building_obscures_cached_node_without_generation_change(lua_snapshot):
    lua = lua_snapshot
    lua.execute('storage.campaign.observation_snapshot(0); resources.coal.obscured=true; storage.campaign.observation_snapshot(0)')
    assert lua.globals().calls == 6
    assert lua.globals().captured.cache.hits == 4
    assert lua.globals().captured.targets.coal is None
    lua.execute('resources.coal.obscured=false; storage.campaign.observation_snapshot(0)')
    assert lua.globals().calls == 7
    assert lua.globals().captured.targets.coal.name == 'coal'
