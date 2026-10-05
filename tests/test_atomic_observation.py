"""Negotiated snapshot tests; fake native transport, not a running game."""
import copy
import json
import sys
from types import SimpleNamespace as NS

import pytest

from jev_factorio.backends.fle import FleBackend
from jev_factorio.backends.observed_factory import ObservedFactory
from jev_factorio.backends.craft_jobs import CraftJobFactory


def envelope():
    return {
        'schema': 2, 'tick': 10, 'session_id': 'atomic-fixture',
        'actor_unit': 17, 'surface_index': 1, 'force_index': 2,
        'position': {'x': 3, 'y': 4}, 'inventory': {'coal': 8},
        'controls': {'tick': 10, 'position': {'x': 3, 'y': 4}, 'status': 'idle',
                     'walking': False, 'mining': False, 'movement_started': False,
                     'path_requests': 0, 'gained': 0},
        'factory': {'tick': 10, 'entities': [], 'receipts': [], 'researched': [],
                    'rockets_launched': 0, 'rocket_baseline': 0,
                    'player_bound': True, 'player_connected': True,
                    'acceptance_runtime': {'schema': 1, 'session_id': 'atomic-fixture',
                        'actor_unit': 17, 'surface_index': 1, 'force_index': 2,
                        'speed': 1, 'tick_paused': False}},
        'bootstrap': {'placed_entities': [], 'drill': False, 'output_connected': False,
                      'iron_ore_collected': 0, 'query_limit': 129},
        'targets': [], 'anchors': [], 'cache': {'hits': 0, 'misses': 5},
        'anchor_diagnostics': {
            'oil': {'radius': 1024, 'saturated': False},
            'water': {'radius': 256, 'saturated': False},
            'selection': 'bounded_witness_not_global_nearest',
        },
        'bounds': {'anchor_radius': 1024, 'water_radius': 256,
                   'oil_query_radii': [256, 512, 1024], 'anchor_limit': 129,
                   'bootstrap_radius': 1000, 'bootstrap_limit': 129,
                   'bootstrap_output_radius': .75, 'bootstrap_output_limit': 2},
    }


def setup(monkeypatch, craft=False):
    monkeypatch.setitem(sys.modules, 'fle.env', NS(Position=lambda **kw: NS(**kw), Prototype=NS(BurnerMiningDrill='drill', WoodenChest='chest')))
    payload = envelope()
    calls = []
    class Tools:
        def __getattr__(self, name):
            raise AssertionError(f'Unexpected FLE helper: {name}')
    class Client:
        def send_command(self, command):
            calls.append(command)
            snapshot_payload = copy.deepcopy(payload)
            capacity_payload = snapshot_payload.pop('_receiver_input_capacity_payload', None)
            bootstrap_payload = snapshot_payload.pop('_bootstrap_output_payload', None)
            response = 'JEV_SNAPSHOT|' + json.dumps(snapshot_payload)
            if capacity_payload is not None:
                from jev_factorio.backends.native_input_capacity import MARKER
                response += '\n' + MARKER + json.dumps(capacity_payload)
            if bootstrap_payload is not None:
                response += '\nJEV_BOOTSTRAP_OUTPUT|' + json.dumps(bootstrap_payload)
            return response
    backend = FleBackend()
    backend.consolidated_observations = True
    backend._instance = NS(namespace=Tools(), rcon_client=Client())
    backend._fair = NS(call=lambda *args: (_ for _ in ()).throw(AssertionError('duplicate fair call')))
    native = ObservedFactory.__new__(ObservedFactory)
    native.backend = backend
    native.catalog = NS(version='2.0.77', machines={})
    native._discovery_epoch = 0
    native.coherent_observation_version = 2
    backend._factory = native
    if craft:
        wrapper = CraftJobFactory.__new__(CraftJobFactory)
        wrapper.native = native
        backend._factory = wrapper
        payload['factory']['craft_job_inventory'] = {'tick': 10, 'items': {'coal': 8}}
    return backend, native, payload, calls


@pytest.mark.parametrize('craft', [False, True])
def test_atomic_path_one_transport_no_fle_helpers(monkeypatch, craft):
    backend, native, payload, calls = setup(monkeypatch, craft)
    state = backend.observe()
    assert state.inventory == {'coal': 8}
    assert state.player_position == (3, 4) and state.tick == 10
    assert state.session_id == 'atomic-fixture' and state._native_controls['tick'] == 10
    assert len(calls) == 1 and 'observation_snapshot_v2' in calls[0]
    assert state._coherent_observation_verified == ('atomic-fixture', 10)
    assert not backend.last_observation_profile['subcalls']


def test_receiver_capacity_stays_private_and_uses_same_single_observation_command(monkeypatch):
    backend, native, payload, calls = setup(monkeypatch)
    from jev_factorio.backends.native_input_capacity import MARKER

    payload['factory']['entities'] = {'recipe:iron-plate': {
        'name': 'stone-furnace', 'unit_number': 2547,
        'position': {'x': 1, 'y': 2}, 'fuel': {'coal': 2},
    }}
    payload['factory']['production_sites'] = {
        'protocol': 1, 'session_id': 'atomic-fixture', 'tick': 10,
        'sources': {'recipe:iron-plate': {
            'state': 'owned', 'reason': 'owned legacy furnace',
            'anchor': 'cell-site:legacy-iron-furnace',
            'position': {'x': 1, 'y': 2}, 'belt_count': 1,
            'bill': {'stone-furnace': 1, 'burner-mining-drill': 1,
                     'burner-inserter': 2, 'wooden-chest': 1,
                     'transport-belt': 1},
            'source_unit': 2547,
        }},
    }
    native.catalog.machines = {'stone-furnace': {'burner': True}}
    payload['_receiver_input_capacity_payload'] = {
        'schema': 1, 'tick': 10, 'session_id': 'atomic-fixture',
        'actor_unit': 17, 'surface_index': 1, 'force_index': 2,
        'actor_inventory': {'coal': 8}, 'complete': True,
        'eligible_count': 1, 'item_count': 1,
        'receivers': {'recipe:iron-plate': {
            'unit_number': 2547, 'name': 'stone-furnace', 'type': 'furnace',
            'burner': True, 'surface_index': 1, 'force_index': 2,
            'items': {'coal': {
                'inventory': 'fuel', 'actor_count': 8,
                'insertable_count': 48, 'method': 'get_insertable_count',
            }},
        }},
    }
    state = backend.observe()
    assert len(calls) == 1
    assert calls[0].count('observation_snapshot_v2(') == 1
    assert MARKER in calls[0]
    assert state._receiver_input_capacity['receivers']['recipe:iron-plate'][
        'items']['coal'] == 48
    assert type(state._receiver_input_capacity['receivers']['recipe:iron-plate'][
        'items']['coal']) is int
    assert 'receiver_input_capacity' not in state.factory
    assert '_receiver_input_capacity' not in state.for_jev()

    # Exercise the consumer through the real atomic decoder's private attr;
    # do not hand-build an alternate capacity sample for this helper.
    from jev_factorio.planning.decision_support import _receiver_capacity_start_evidence
    catalog = NS(machines={'stone-furnace': {'burner': True}})
    evidence = _receiver_capacity_start_evidence(
        state, catalog, 'recipe:iron-plate', 'coal', 3, 2547)
    assert evidence['insertable_count_now'] == 48
    assert evidence['actor_count_now'] == 8
    assert evidence['actor_unit'] == 17
    assert evidence['surface_index'] == 1 and evidence['force_index'] == 2
    assert evidence['source_unit'] == 2547

    # A foreign helper-shaped row is not the decoder's integer result and
    # cannot qualify a transfer even after the native sidecar was decoded.
    state._receiver_input_capacity['receivers']['recipe:iron-plate'][
        'items']['coal'] = {
            'inventory': 'fuel', 'actor_count': 8,
            'insertable_count': 48, 'method': 'get_insertable_count',
        }
    assert _receiver_capacity_start_evidence(
        state, catalog, 'recipe:iron-plate', 'coal', 3, 2547) is None


def test_atomic_snapshot_survives_unavailable_or_overbudget_optional_capacity(monkeypatch):
    backend, native, payload, calls = setup(monkeypatch)
    del native.catalog.machines  # Benchmark/legacy adapter shape has no capacity catalog.

    state = backend.observe()

    assert state.tick == 10 and state.inventory == {'coal': 8}
    assert state._receiver_input_capacity is None
    assert len(calls) == 1
    assert 'observation_snapshot_v2(' in calls[0]
    assert 'JEV_RECEIVER_INPUT_CAPACITY' not in calls[0]

    # A supported query may still report incomplete coverage. The coherent
    # primary observation remains usable while the advisory capacity is unknown.
    native.catalog.machines = {'stone-furnace': {'burner': True}}
    payload['_receiver_input_capacity_payload'] = {
        'schema': 1, 'tick': 10, 'session_id': 'atomic-fixture',
        'actor_unit': 17, 'surface_index': 1, 'force_index': 2,
        'actor_inventory': {'coal': 8}, 'complete': False,
        'eligible_count': 65, 'item_count': 1, 'receivers': {},
    }
    second = backend.observe()
    assert second.tick == 10 and second.inventory == {'coal': 8}
    assert second._receiver_input_capacity is None
    assert len(calls) == 2 and 'JEV_RECEIVER_INPUT_CAPACITY' in calls[1]


@pytest.mark.parametrize('field,value', [('session_id','other'), ('actor_unit',18),
    ('surface_index',2), ('force_index',3), ('tick',9), ('inventory',{'coal':True})])
def test_atomic_rejects_identity_tick_or_inventory_changes(monkeypatch, field, value):
    backend, native, payload, calls = setup(monkeypatch)
    backend.observe()
    payload[field] = value
    with pytest.raises(ValueError):
        backend.observe()
    assert len(calls) == 2  # no fallback helper or second read after invalid data


@pytest.mark.parametrize('part', ['controls', 'factory'])
def test_mixed_ticks_rejected(monkeypatch, part):
    backend, native, payload, _ = setup(monkeypatch)
    payload[part]['tick'] = 11
    with pytest.raises(ValueError): backend.observe()


@pytest.mark.parametrize('where', ['position', 'controls'])
def test_position_mismatch_or_nonfinite_rejected(monkeypatch, where):
    backend, native, payload, _ = setup(monkeypatch)
    if where == 'position': payload['position']['x'] = float('inf')
    else: payload['controls']['position']['y'] = 5
    with pytest.raises(ValueError): backend.observe()


def test_crafting_wrapper_must_agree_with_coherent_inventory(monkeypatch):
    backend, native, payload, _ = setup(monkeypatch, craft=True)
    payload['factory']['craft_job_inventory']['items']['coal'] = 7
    with pytest.raises(ValueError, match='inventory'): backend.observe()


def test_empty_native_maps_are_valid_but_nonempty_arrays_are_not(monkeypatch):
    backend, native, payload, _ = setup(monkeypatch, craft=True)
    payload['inventory'] = []
    payload['factory']['craft_job_inventory']['items'] = []
    assert backend.observe().inventory == {}
    payload['inventory'] = ['coal']
    with pytest.raises(ValueError): backend.observe()


def test_bad_wrapper_cannot_return_placeholder_state(monkeypatch):
    backend, native, _, _ = setup(monkeypatch)
    native.observe = lambda snapshot: snapshot
    with pytest.raises(ValueError, match='Coherent'): backend.observe()


def test_bootstrap_without_fle_entity_conversion(monkeypatch):
    backend, native, payload, _ = setup(monkeypatch)
    payload['bootstrap'] = {
        'query_limit': 129, 'placed_entities': ['burner-mining-drill', 'wooden-chest'],
        'drill': {'name':'burner-mining-drill', 'unit_number':51,
            'position':{'x':0,'y':0}, 'drop_position':{'x':2,'y':-.296875},
            'status':'working', 'fuel':{'coal':3}},
        'output_connected':True, 'iron_ore_collected':7}
    payload['factory']['entities'] = {
        'bootstrap:output': {'name': 'wooden-chest',
                             'position': {'x': 2, 'y': -.5}}}
    result = backend.observe()
    assert result.drill_fuel == 3 and result.drill_status == 'working'
    assert result.iron_ore_collected == 7 and result.drill_output_connected
    assert result.factory['drill_output_role'] == 'bootstrap:output'
    assert backend._drill.unit_number == 51
    backend.observe()
    assert 'observation_snapshot_v2(0,51,{x=0,y=0})' in _[-1]
    payload['bootstrap']['drill']['unit_number'] = 52
    with pytest.raises(ValueError, match='bootstrap'): backend.observe()


def test_resources_and_anchors_are_fresh_and_never_cached_client_side(monkeypatch):
    backend, native, payload, _ = setup(monkeypatch)
    payload['targets'] = {'iron-ore': {'name':'iron-ore','surface_index':1,
                                     'position':{'x':6,'y':8}}}
    payload['anchors'] = {'water': {'name':'water','surface_index':1,
                                   'position':{'x':3,'y':6}}}
    result = backend.observe()
    assert result.nearby_resources == {'iron-ore':5,'water':2}
    payload['targets'] = payload['anchors'] = []
    result = backend.observe()
    assert not result.nearby_resources and not backend._resources


def test_expanded_oil_anchor_bound_preserves_legacy_and_water_limits(monkeypatch):
    backend, native, payload, _ = setup(monkeypatch)
    payload['anchors'] = {'crude-oil': {'name':'crude-oil','surface_index':1,
                                      'position':{'x':15.5,'y':383.5}}}
    assert backend.observe().nearby_resources['crude-oil'] > 256
    payload['anchors'] = {'water': {'name':'water','surface_index':1,
                                   'position':{'x':3,'y':300}}}
    with pytest.raises(ValueError, match='outside query bounds'):
        backend.observe()
    payload['bounds']['oil_query_radii'] = [256, True, 1024]
    with pytest.raises(ValueError, match='query bounds'):
        backend.observe()
    payload['bounds']['oil_query_radii'] = [256, 512, 1024]
    payload['anchor_diagnostics']['oil']['radius'] = 256
    payload['anchors'] = {'crude-oil': {'name':'crude-oil','surface_index':1,
                                      'position':{'x':15.5,'y':383.5}}}
    with pytest.raises(ValueError, match='outside query bounds'):
        backend.observe()
    payload['anchor_diagnostics']['oil']['radius'] = 1024
    payload['anchors'] = {'crude-oil': {'name':'crude-oil','surface_index':1,
                                      'position':{'x':1050,'y':4}}}
    with pytest.raises(ValueError, match='outside query bounds'):
        backend.observe()


def test_legacy_profile_refuses_expanded_wire_and_accepts_exact_old_bound(monkeypatch):
    from jev_factorio.backends.native_attachment import LEGACY_OBSERVATION_PROFILE
    backend, native, payload, _ = setup(monkeypatch)
    backend._native_attachment = {
        'native_installation': {'profile': LEGACY_OBSERVATION_PROFILE}}
    with pytest.raises(ValueError, match='query bounds'):
        backend.observe()
    payload['bounds'] = {'anchor_radius': 256, 'anchor_limit': 129,
                         'bootstrap_radius': 1000, 'bootstrap_limit': 129,
                         'bootstrap_output_radius': .75, 'bootstrap_output_limit': 2}
    payload.pop('anchor_diagnostics')
    assert backend.observe().tick == 10
    payload['anchors'] = {'crude-oil': {'name':'crude-oil','surface_index':1,
                                      'position':{'x':15.5,'y':383.5}}}
    with pytest.raises(ValueError, match='outside query bounds'):
        backend.observe()


def test_fresh_versioned_installation_uses_expanded_wire(monkeypatch):
    backend, native, payload, _ = setup(monkeypatch)
    backend._native_attachment = {'native_installation': {'profile': False}}
    assert backend.observe().tick == 10
    backend._native_attachment = {'native_installation': False}
    with pytest.raises(ValueError, match='Unqualified atomic observer profile'):
        backend.observe()


@pytest.mark.parametrize('profile_name', ['MANUAL_CYCLE_PROFILE', 'CLOSED_WORLD_PROFILE'])
def test_resumed_manual_cycle_profiles_keep_expanded_atomic_bounds(monkeypatch, profile_name):
    from jev_factorio.backends import native_attachment

    backend, native, payload, calls = setup(monkeypatch)
    backend._native_attachment = {'native_installation': {
        'profile': getattr(native_attachment, profile_name)}}
    assert backend.observe().tick == 10
    assert len(calls) == 1
    payload['bounds']['anchor_radius'] = 256
    with pytest.raises(ValueError, match='query bounds'):
        backend.observe()


def test_unknown_resumed_atomic_profile_is_rejected_before_native_query(monkeypatch):
    backend, native, payload, calls = setup(monkeypatch)
    backend._native_attachment = {'native_installation': {
        'profile': 'unknown-observation-profile'}}
    with pytest.raises(ValueError, match='Unqualified atomic observer profile'):
        backend.observe()
    assert calls == []


@pytest.mark.parametrize('bad', [
    {'selection': 'globally_nearest'},
    {'oil': {'radius': 2048, 'saturated': False}},
    {'oil': {'radius': 512, 'saturated': 1}},
    {'water': {'radius': 512, 'saturated': False}},
])
def test_expanded_anchor_provenance_must_be_exact(monkeypatch, bad):
    backend, native, payload, _ = setup(monkeypatch)
    payload['anchor_diagnostics'].update(bad)
    with pytest.raises(ValueError, match='anchor diagnostics'):
        backend.observe()


@pytest.mark.parametrize('bad', [True, 0, 130])
def test_native_query_bounds_not_silently_relaxed(monkeypatch, bad):
    backend, native, payload, _ = setup(monkeypatch)
    payload['bounds']['bootstrap_limit'] = bad
    with pytest.raises(ValueError): backend.observe()


def test_unknown_or_oversized_bootstrap_does_not_replace_valid_state(monkeypatch):
    backend, native, payload, _ = setup(monkeypatch)
    backend.observe()
    payload['bootstrap']['placed_entities'] = ['wooden-chest'] * 129
    with pytest.raises(ValueError): backend.observe()


def test_no_invented_fallback_when_atomic_envelope_version_changes(monkeypatch):
    backend, native, payload, calls = setup(monkeypatch)
    payload['schema'] = 1
    with pytest.raises(ValueError): backend.observe()
    assert len(calls) == 1


def test_installation_requires_exact_native_readback(monkeypatch):
    from jev_factorio.backends.native_factory import NativeFactory
    def init(self, backend): self.backend=backend
    monkeypatch.setattr(NativeFactory, '__init__', init)
    commands=[]
    monkeypatch.setattr(ObservedFactory, 'command', lambda self, script: commands.append(script) or
                        ('JEV_ATOMIC_READY|2' if 'JEV_ATOMIC_READY|2' in script else ''))
    native=ObservedFactory(NS())
    assert native.coherent_observation_version==2 and len(commands)==2
    monkeypatch.setattr(ObservedFactory, 'command', lambda self, script: 'unsupported')
    with pytest.raises(RuntimeError, match='negotiation'): ObservedFactory(NS())


def test_bootstrap_fuel_uses_unit_binding_at_native_insert(monkeypatch):
    from jev_factorio.backends.fair_actions import FairActions
    fair=FairActions.__new__(FairActions)
    fair.approach=lambda *args: None
    calls=[]
    fair.call=lambda *args: calls.append(args) or {'quantity':3}
    entity=NS(name='burner-mining-drill',unit_number=51,position=NS(x=1,y=2))
    assert fair.insert_item(NS(value=['coal']),entity,3)==3
    assert calls==[('insert','burner-mining-drill',{'x':1.0,'y':2.0},'coal',3,51)]


@pytest.mark.parametrize('name',['water','deepwater'])
def test_native_water_anchor_names_are_explicitly_supported(monkeypatch,name):
    backend,native,payload,_=setup(monkeypatch)
    payload['anchors']={'water':{'name':name,'surface_index':1,'position':{'x':3,'y':6}}}
    assert backend.observe().nearby_resources['water']==2


@pytest.mark.parametrize('profile_name', [
    'WATER_ORIGIN_OBSERVATION_PROFILE', 'MANUAL_CYCLE_PROFILE', 'CLOSED_WORLD_PROFILE',
])
def test_water_origin_profiles_reject_half_tile_without_replacing_prior_view(monkeypatch, profile_name):
    from jev_factorio.backends import native_attachment

    backend, native, payload, _ = setup(monkeypatch)
    backend._native_attachment = {
        'native_installation': {'profile': getattr(native_attachment, profile_name)}}
    payload['anchors'] = {'water': {'name': 'water', 'surface_index': 1,
                                    'position': {'x': 3, 'y': 6}}}
    prior = backend.observe()
    payload['anchors']['water']['position'] = {'x': 3.5, 'y': 6.5}
    with pytest.raises(ValueError, match='non-tile anchor'):
        backend.observe()
    assert backend._resources['water'].x == 3
    backend._native_attachment['native_installation']['profile'] = (
        native_attachment.EXPANDED_OBSERVATION_PROFILE)
    assert backend.observe().nearby_resources['water'] > prior.nearby_resources['water']


def test_arbitrary_native_tile_cannot_claim_water(monkeypatch):
    backend,native,payload,_=setup(monkeypatch)
    payload['anchors']={'water':{'name':'grass-1','surface_index':1,'position':{'x':3,'y':6}}}
    with pytest.raises(ValueError,match='discovery identity'):backend.observe()


@pytest.mark.parametrize(('wire', 'expected'), [
    ({}, []),
    ([], []),
    (['automation'], ['automation']),
])
def test_researched_is_normalized_at_both_public_snapshot_boundaries_and_serialization(
        monkeypatch, wire, expected):
    backend, _, payload, calls = setup(monkeypatch)
    payload['factory']['researched'] = wire

    state = backend.observe()
    rendered = state.for_jev()
    decoded = json.loads(json.dumps(rendered, allow_nan=False))

    assert state.researched == expected
    assert state.factory['researched'] == expected
    assert rendered['researched'] == expected
    assert rendered['factory']['researched'] == expected
    assert decoded['researched'] == decoded['factory']['researched'] == expected
    assert payload['factory']['researched'] == wire  # preserve the captured wire object
    assert len(calls) == 1


@pytest.mark.parametrize('wire', [
    {'automation': True},
    {'0': 'automation'},
    [''],
    [True],
    [None],
    ['automation'] * 4097,
])
def test_researched_rejects_malformed_or_oversized_values_without_replacing_prior_observation(
        monkeypatch, wire):
    backend, _, payload, calls = setup(monkeypatch)
    previous = backend.observe()
    identity = backend._factory._coherent_identity
    tick = backend._factory._coherent_tick
    resources = copy.deepcopy(backend._resources)
    payload['factory']['researched'] = wire

    with pytest.raises(ValueError, match='Invalid atomic research state'):
        backend.observe()

    assert len(calls) == 2
    assert backend._factory._coherent_identity == identity
    assert backend._factory._coherent_tick == tick
    assert backend._resources == resources
    assert previous.factory['researched'] == previous.researched == []
