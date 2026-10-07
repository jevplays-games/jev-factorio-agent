"""Independent-review reproductions on real adapters and the actual observer Lua.

The transport and engine objects are deterministic doubles, not live Factorio.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import sys
from types import SimpleNamespace as NS

import pytest

from jev_factorio import iteration_timing as timing
from jev_factorio.backends.fair_actions import FairActions, NativePathNotFound
from jev_factorio.backends.native_factory import NativeFactory
from jev_factorio.backends.fle import SessionRcon
from jev_factorio.backends.input_routes import InputRouteFactory
from jev_factorio.backends.output_buffers import OutputBufferFactory
from jev_factorio.backends.mining_outposts import MiningOutpostFactory
from jev_factorio.backends.solid_routes import SolidRouteFactory
from jev_factorio.backends import launch_readiness
from jev_factorio.latency_report import analyze
from jev_factorio.observation import ObservationProfile
from test_atomic_observation_lua import runtime, converted
from test_atomic_observation import setup as atomic_setup
from test_iteration_timing import setup_loop
from solid_routes_fixtures import fixture, parameters


@contextmanager
def recording():
    ledger = timing.Ledger()
    token = timing._CURRENT.set(ledger)
    try:
        with timing.span('iteration'):
            yield ledger
    finally:
        timing._CURRENT.reset(token)


def command_adapter(kind, response, *, nested=False):
    calls = []
    def send(script):
        calls.append(script)
        if isinstance(response, BaseException):
            raise response
        return response
    client = NS(send_command=send)
    if nested:
        client = SessionRcon(client)
    adapter = kind.__new__(kind)
    adapter.backend = NS(_instance=NS(rcon_client=client))
    return adapter, calls


@pytest.mark.parametrize('kind', [FairActions, NativeFactory])
@pytest.mark.parametrize('nested', [False, True])
def test_command_lua_rejection_is_failed_once_without_content_leak(kind, nested):
    response = 'Cannot execute command. private-fixture \u2603'
    adapter, calls = command_adapter(kind, response, nested=nested)
    with recording() as ledger:
        with pytest.raises(RuntimeError) as error:
            adapter.command('private-script')
    assert str(error.value) == response
    assert len(calls) == 1 and ledger.io['command_calls'] == 1
    assert ledger.io['failed_calls'] == 1
    assert ledger.rows['native_command']['failed'] == 1
    assert ledger.io['response_bytes'] == len(response.encode('utf-8'))
    assert ledger.io['unknown_response_size_calls'] == 0
    assert 'private-' not in json.dumps(ledger.snapshot(1, 'error'))
    assert timing._NATIVE_DEPTH.get() == 0


@pytest.mark.parametrize('kind', [FairActions, NativeFactory])
@pytest.mark.parametrize('response', ['ok', '', None, 'ok \u2603'])
def test_native_command_success_and_empty_response_preserve_behavior(kind, response):
    adapter, calls = command_adapter(kind, response, nested=True)
    with recording() as ledger:
        assert adapter.command('query') == (response or '')
    assert len(calls) == ledger.io['command_calls'] == 1
    assert ledger.io['failed_calls'] == 0


@pytest.mark.parametrize('kind', [FairActions, NativeFactory])
def test_transport_failure_preserves_exact_exception_and_no_retry(kind):
    failure = TimeoutError('private transport error')
    adapter, calls = command_adapter(kind, failure, nested=True)
    with recording() as ledger:
        with pytest.raises(TimeoutError) as error:
            adapter.command('query')
    assert error.value is failure
    assert len(calls) == ledger.io['failed_calls'] == 1
    assert ledger.io['unknown_response_size_calls'] == 1
    assert 'private' not in json.dumps(ledger.snapshot(1, 'error'))


ADAPTERS = [
    ('input', InputRouteFactory, 'factory_input_build',
     {'source': 'recipe:iron-plate', 'layout': 'layout-1', 'part': 'inserter', 'receipt': 'r1', 'reserve_belts': 0},
     'prepare_input_route', 'build_input_route', 'burner-inserter'),
    ('site', InputRouteFactory, 'factory_place',
     {'role': 'recipe:iron-plate', 'name': 'stone-furnace', 'anchor': 'cell-site:test'},
     'prepare_production_site', 'build_production_site', 'stone-furnace'),
    ('output', OutputBufferFactory, 'factory_buffer_build',
     {'source': 'recipe:iron-plate', 'layout': 'layout-1', 'part': 'chest', 'receipt': 'r1'},
     'prepare_output_buffer', 'build_output_buffer', 'wooden-chest'),
    ('outpost', MiningOutpostFactory, 'factory_outpost_build',
     {'resource': 'iron-ore', 'layout': 'layout-1', 'part': 'chest', 'receipt': 'r1'},
     'prepare_mining_outpost', 'build_mining_outpost', 'wooden-chest'),
    ('solid', SolidRouteFactory, 'factory_solid_build', None,
     'prepare_solid_route', 'build_solid_route', 'inserter'),
    ('launch', None, 'factory_launch_pad', {'site': 's1', 'receipt': 'r1'},
     'prepare_launch_pad', 'build_launch_pad', 'cargo-landing-pad'),
]


@pytest.mark.parametrize('case', ADAPTERS, ids=lambda case: case[0])
@pytest.mark.parametrize('malformed', [False, True])
def test_every_paid_adapter_decode_has_one_span_and_preserves_order(monkeypatch, case, malformed):
    _, kind, action, args, prepare, build, name = case
    monkeypatch.setitem(sys.modules, 'fle.env', NS(Position=NS))
    events = []
    result = 'not JSON private' if malformed else json.dumps({'name': name, 'position': {'x': 1, 'y': 2}})
    def call(operation, *values):
        events.append(operation)
        return result
    native = NS(call=call, backend=NS(_fair=NS(approach=lambda *args: events.append('approach'))),
                require_launch_reconciliation=lambda: events.append('reconcile'))
    if kind:
        adapter = kind.__new__(kind)
        adapter.native = native
        execute = adapter.execute
    else:
        execute = lambda *args: launch_readiness.execute(native, *args)
    if args is None:
        args = parameters(fixture())
    with recording() as ledger:
        if malformed:
            with pytest.raises(json.JSONDecodeError):
                execute(action, args)
        else:
            execute(action, args)
    expected = [prepare] if malformed else [prepare, 'approach', build]
    if case[0] == 'launch':
        expected.insert(0, 'reconcile')
    assert events == expected
    assert ledger.rows.get('native_decode', {}).get('calls') == 1
    assert ledger.rows['native_decode']['failed'] == int(malformed)
    assert 'private' not in json.dumps(ledger.snapshot(1, 'error' if malformed else 'returned'))


def test_launch_reconciliation_rejection_precedes_rpc_and_native_decode(monkeypatch):
    monkeypatch.setitem(sys.modules, 'fle.env', NS(Position=NS))
    events = []

    def call(operation, *values):
        events.append(operation)
        return json.dumps({'name': 'cargo-landing-pad', 'position': {'x': 1, 'y': 2}})

    def reject_reconciliation():
        events.append('reconcile')
        raise RuntimeError('retained load receipt is unresolved')

    native = NS(
        call=call,
        backend=NS(_fair=NS(approach=lambda *args: events.append('approach'))),
        require_launch_reconciliation=reject_reconciliation,
    )
    with recording() as ledger:
        with pytest.raises(RuntimeError, match='retained load receipt is unresolved'):
            launch_readiness.execute(
                native, 'factory_launch_pad', {'site': 's1', 'receipt': 'r1'})

    assert events == ['reconcile']
    assert ledger.rows.get('native_decode', {}).get('calls', 0) == 0


@pytest.mark.parametrize('corridor', [False, True])
def test_reach_confirmation_and_corridor_origin_decode_are_measured(monkeypatch, corridor):
    monkeypatch.setitem(sys.modules, 'fle.env', NS(Position=NS))
    actor = FairActions.__new__(FairActions)
    outputs = [{'positions': [{'x': 2, 'y': 0}], 'unit_number': 5}]
    if corridor:
        outputs += [{'x': 0, 'y': 0}, {'reachable': True}]
    else:
        outputs += [{'reachable': True}]
    queries, walks = [], []
    def command(script):
        queries.append(script)
        return json.dumps(outputs.pop(0))
    def move(position):
        walks.append(position)
        if corridor and len(walks) == 1:
            raise NativePathNotFound('No safe path before movement')
    actor.command = command
    actor.move_to = move
    actor._approach_corridors = lambda origin, target: [[{'x': 1, 'y': 0}]]
    with recording() as ledger:
        actor.approach(NS(x=50, y=0), 'wooden-chest')
    assert not outputs
    assert ledger.rows['native_decode']['calls'] == len(queries)
    assert len(queries) == (3 if corridor else 2)


def test_observation_profile_decode_is_not_double_counted():
    profile = ObservationProfile()
    with recording() as ledger:
        assert profile.decode('{"value": 1}') == {'value': 1}
    assert ledger.rows['native_decode']['calls'] == 1
    assert profile.summary()['partition_complete']


@pytest.mark.parametrize('pinned', [False, True])
def test_bootstrap_connected_chest_just_beyond_actor_radius(pinned):
    lua = runtime()
    lua.execute('''player.position={x=0,y=0};add_drill(51,999,0);add_chest(52,1001,0)''')
    lua.execute('storage.campaign.observation_snapshot_v2(0' + (',51' if pinned else '') + ')')
    value = converted(lua.globals().captured)['bootstrap']
    assert value['drill']['unit_number'] == 51
    assert value['output_connected'] and value['iron_ore_collected'] == 7
    assert value['placed_entities'].count('wooden-chest') == 1


def test_radius_boundary_payload_passes_actual_python_decoder(monkeypatch):
    backend, _, payload, calls = atomic_setup(monkeypatch)
    from jev_factorio.backends.native_attachment import LEGACY_OBSERVATION_PROFILE
    backend._native_attachment = {
        'native_installation': {'profile': LEGACY_OBSERVATION_PROFILE}}
    lua = runtime()
    lua.execute('''player.position={x=0,y=0};add_drill(51,999,0);add_chest(52,1001,0)
        storage.campaign.observation_snapshot_v2(0,51)''')
    actual = converted(lua.globals().captured)
    for parent, key in [(actual, 'anchors'), (actual['factory'], 'entities'), (actual['factory'], 'researched')]:
        if parent[key] == {}:
            parent[key] = []
    payload.clear()
    payload.update(actual)
    state = backend.observe()
    assert len(calls) == 1 and state.drill_output_connected
    assert state.iron_ore_collected == 7


@pytest.mark.parametrize('indices,missing', [([3], 2), ([1], 0), ([2, 5], 3), ([1, 2, 3], 0)])
def test_report_counts_all_unrepresented_indices_from_process_start(monkeypatch, tmp_path, indices, missing):
    loop, clock = setup_loop(monkeypatch)
    loop.step()
    timing_row = loop.step()
    records = []
    for index in indices:
        previous = deepcopy(timing_row)
        previous['iteration_index'] = index
        records.append({'previous_iteration_timing': previous})
    path = tmp_path / 'timing.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
    result = analyze(path)
    assert result['counts'].get('iteration:unpublished_between_records', 0) == missing


@pytest.mark.parametrize('nested,enabled', [(False, False), (True, False), (True, True)])
def test_response_validation_never_depends_on_profiler_permission(nested, enabled):
    events = []
    error = RuntimeError('private response')
    def operation():
        events.append('operation')
        return 'reply'
    def validate(value):
        events.append(('check', value))
        raise error
    def execute():
        return timing.native_io('native_command', operation, check_response=validate)
    invoke = (lambda: timing.native_io('native_command', execute)) if nested else execute
    if enabled:
        with recording() as ledger:
            with pytest.raises(RuntimeError) as raised:
                invoke()
        assert ledger.io['command_calls'] == ledger.io['failed_calls'] == 1
    else:
        with pytest.raises(RuntimeError) as raised:
            invoke()
    assert raised.value is error
    assert events == ['operation', ('check', 'reply')]
    assert timing._NATIVE_DEPTH.get() == 0


@pytest.mark.parametrize('change', [
    'chest.valid=false', 'chest.force={index=99}', 'chest.surface={index=99}',
    'chest.name="steel-chest"', 'chest.position.x=1001.6',
])
def test_output_endpoint_rejects_missing_foreign_or_misaligned_chest(change):
    lua = runtime()
    lua.execute('player.position={x=0,y=0};add_drill(51,999,0);chest=add_chest(52,1001,0);' + change)
    lua.execute('storage.campaign.observation_snapshot_v2(0,51)')
    value = converted(lua.globals().captured)['bootstrap']
    assert value['output_connected'] is False and value['iron_ore_collected'] == 0


@pytest.mark.parametrize('change', [
    'add_chest(53,1001,0)', 'chest.unit_number=0', 'chest.unit_number=1.5',
    'chest.get_inventory=function() return nil end',
])
def test_output_ambiguity_or_invalid_inventory_fails_before_payload(change):
    lua = runtime()
    lua.execute('player.position={x=0,y=0};add_drill(51,999,0);chest=add_chest(52,1001,0);' + change)
    lua.execute('assert(not pcall(storage.campaign.observation_snapshot_v2,0,51));assert(captured==nil)')


def test_endpoint_included_only_once_within_existing_actor_radius():
    lua = runtime()
    lua.execute('''add_drill(51,0,0);add_chest(52,2,0);storage.campaign.observation_snapshot_v2(0,51)
        assert(#captured.bootstrap.placed_entities==2 and captured.bootstrap.iron_ore_collected==7)
        assert(query_count==4)
        local q=queries[2];assert(q.name=="wooden-chest" and q.limit==2 and q.radius==.75)
        assert(q.position.x==2 and q.position.y==0 and q.force==force)''')


def test_external_endpoint_does_not_silently_expand_total_entity_budget():
    lua = runtime()
    lua.execute('''player.position={x=0,y=0};add_drill(51,999,0);add_chest(52,1001,0)
        for i=1,127 do add_chest(i+100,0,i) end
        assert(not pcall(storage.campaign.observation_snapshot_v2,0,51));assert(captured==nil)''')


def test_endpoint_checked_fresh_on_every_snapshot_no_extra_rpc_or_discovery():
    lua = runtime()
    lua.execute('''player.position={x=0,y=0};add_drill(51,999,0);chest=add_chest(52,1001,0)
        storage.campaign.observation_snapshot_v2(0,51);assert(captured.bootstrap.output_connected)
        chest.valid=false;game.tick=11;storage.campaign.observation_snapshot_v2(0,51)
        assert(not captured.bootstrap.output_connected and captured.bootstrap.iron_ore_collected==0)
        assert(campaign_count==2 and control_count==2 and discovery_count==5 and query_count==8)''')


def test_failed_early_decorated_steps_report_their_missing_prefix(monkeypatch, tmp_path):
    class Loop:
        factory_scheduling = 'ready-work'
        @timing.profiled_iteration
        def step(self, fail=False):
            if fail:
                raise ValueError('fixture failure before record')
            return timing.previous_timing(self)
    loop = Loop()
    for _ in range(2):
        with pytest.raises(ValueError):
            loop.step(fail=True)
    # The first emitted row can carry only the most recently completed failure;
    # the earlier timing index is unrepresented, not silently counted as success.
    row = loop.step()
    assert row['iteration_index'] == 2 and row['status'] == 'error'
    path = tmp_path / 'first-published.jsonl'
    path.write_text(json.dumps({'previous_iteration_timing': row}) + '\n')
    result = analyze(path)
    assert result['counts']['iteration:unpublished_between_records'] == 1
    assert result['counts']['iteration:unobserved_before_first_sample'] == 1
    assert 'not_proof_of_runtime_loss' in result['scopes']['missing_iteration_indices']
    assert result['iteration_timing']['complete_iterations'] == 1


def test_legacy_rows_do_not_fabricate_missing_timing_counts(tmp_path):
    path = tmp_path / 'legacy.jsonl'
    path.write_text('{}\n{}\n')
    result = analyze(path)
    assert result['iteration_timing']['records_without_prior_timing'] == 2
    assert 'iteration:unpublished_between_records' not in result['counts']


def test_first_timing_index_is_bounded_before_counting_missing_prefix(monkeypatch, tmp_path):
    loop, _ = setup_loop(monkeypatch)
    loop.step()
    row = loop.step()
    row['iteration_index'] = 2**63
    with pytest.raises(ValueError, match='identity'):
        timing.validate_timing(row)
    path = tmp_path / 'oversized-prefix.jsonl'
    path.write_text(json.dumps({'previous_iteration_timing': row}) + '\n')
    with pytest.raises(ValueError, match='Invalid latency record'):
        analyze(path)


@pytest.mark.parametrize('key,bad', [
    ('bootstrap_output_radius', .2), ('bootstrap_output_limit', 3),
    ('bootstrap_output_limit', 2.0),
])
def test_endpoint_query_bounds_are_attested_and_type_checked(monkeypatch, key, bad):
    backend, _, payload, _ = atomic_setup(monkeypatch)
    payload['bounds'][key] = bad
    with pytest.raises(ValueError, match='query bounds'):
        backend.observe()


def test_lua_endpoint_query_bounds_match_decoder_contract():
    lua = runtime()
    lua.execute('add_drill(51,0,0);add_chest(52,2,0);storage.campaign.observation_snapshot_v2(0,51)')
    bounds = converted(lua.globals().captured)['bounds']
    assert bounds['bootstrap_output_radius'] == .75
    assert bounds['bootstrap_output_limit'] == 2


@pytest.mark.parametrize('kind', [FairActions, NativeFactory])
def test_nested_session_counts_scoped_request_bytes_once(kind):
    sent = []
    client = SessionRcon(NS(send_command=lambda command: sent.append(command) or 'ok'))
    adapter = kind.__new__(kind)
    adapter.backend = NS(_instance=NS(rcon_client=client))
    with recording() as ledger:
        assert adapter.command('fixture') == 'ok'
    assert sent == [SessionRcon.scoped('/sc fixture')]
    assert ledger.io['command_calls'] == 1
    assert ledger.io['request_bytes'] == len(sent[0].encode('utf-8'))
    assert ledger.io['unknown_request_size_calls'] == 0


def test_ordered_delegates_retain_known_bytes_and_unknown_evidence():
    with recording() as ledger:
        def ordered():
            timing.native_io('native_command', lambda: 'first', request_bytes=7)
            return timing.native_io('native_command', lambda: 'second', request_bytes=None)
        assert timing.native_io('native_batch', ordered, request_bytes=1234) == 'second'
    assert ledger.io['batch_calls'] == 1 and ledger.io['command_calls'] == 0
    assert ledger.io['request_bytes'] == 7
    assert ledger.io['unknown_request_size_calls'] == 1
    assert timing._NATIVE_REQUEST.get() is None


def test_request_frames_reset_between_logical_calls():
    with recording() as ledger:
        timing.native_io('native_command',
            lambda: timing.native_io('native_command', lambda: 'ok', request_bytes=31),
            request_bytes=2)
        timing.native_io('native_command', lambda: 'ok', request_bytes=11)
    assert ledger.io['request_bytes'] == 42
    assert ledger.io['command_calls'] == 2
    assert timing._NATIVE_REQUEST.get() is None


def test_nested_batch_uses_scoped_request_content_once():
    sent = []
    commands = {'first': '/sc print("\u2603")', 'second': '/c return 1'}
    client = SessionRcon(NS(send_commands=lambda value: sent.append(value) or {'first': 'ok', 'second': '1'}))
    with recording() as ledger:
        timing.native_io('native_batch', lambda: client.send_commands(commands),
                         request_bytes=timing.request_size(commands))
    assert ledger.io['batch_calls'] == len(sent) == 1
    assert ledger.io['command_calls'] == 0
    assert ledger.io['request_bytes'] == timing.request_size(sent[0])
    assert ledger.io['response_bytes'] == 3
