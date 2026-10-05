"""Fixed read query tested in Lua; decoder and atomic publication fail closed."""
import json

import pytest

from jev_factorio.backends.native_actor_capacity import ITEMS, MARKER, decode, observation_command


def payload():
    return {'schema': 1, 'tick': 10, 'session_id': 'atomic-fixture',
            'actor_unit': 17, 'surface_index': 1, 'force_index': 2,
            'inventory': 'character_main', 'quality': 'normal',
            'method': 'get_insertable_count', 'complete': True,
            'items': {item: 100 for item in ITEMS}}


def primary():
    return {key: payload()[key] for key in ('schema', 'tick', 'inventory', 'quality', 'method')} | {
        'items': {'coal': 100}}


def raw(value):
    return MARKER + json.dumps(value)


def test_decode_retains_zero_and_requires_matching_primary():
    p = payload(); p['items']['wood'] = 0
    result = decode(raw(p), payload(), primary())
    assert result['items']['wood'] == 0 and set(result['items']) == ITEMS
    assert primary()['items'] == {'coal': 100}
    assert decode(raw(p), payload(), None) is None
    assert decode('JEV_SNAPSHOT|{}', payload(), primary()) is None
    p['complete'] = False
    assert decode(raw(p), payload(), primary()) is None


@pytest.mark.parametrize(('key', 'value'), [
    ('schema', True), ('tick', 9), ('tick', True), ('session_id', 'other'),
    ('actor_unit', 18), ('actor_unit', True), ('surface_index', 2), ('force_index', 1),
    ('inventory', 'chest'), ('quality', 'rare'), ('method', 'estimate'),
    ('complete', 1), ('items', {'coal': 100}),
])
def test_decode_rejects_wrong_identity_or_incomplete_shape(key, value):
    p = payload(); p[key] = value
    with pytest.raises(ValueError): decode(raw(p), payload(), primary())


@pytest.mark.parametrize('value', [-1, True, 1.5, 2**32, float('nan'), float('inf')])
def test_decode_rejects_malformed_counts(value):
    p = payload(); p['items']['wood'] = value
    with pytest.raises(ValueError): decode(raw(p), payload(), primary())


def test_ambiguous_foreign_or_conflicting_readings_reject():
    p = payload()
    for response in (raw(p) + '\n' + raw(p), MARKER + ' ' * 4097,
                     raw({**p, 'unbounded': True}),
                     raw({**p, 'items': {**p['items'], 'coal': 99}}),
                     raw({**p, 'items': {**p['items'], 'unknown-item': 1}})):
        with pytest.raises(ValueError): decode(response, p, primary())


def lua_query(change=''):
    lua = pytest.importorskip('lupa.lua52').LuaRuntime(unpack_returned_tuples=True)
    lua.execute('''
game={tick=10};queries={};calls=0
actor={valid=true,unit_number=17,surface={index=1},force={index=2}}
inventory={get_insertable_count=function(spec)
    assert(spec.quality=="normal");table.insert(queries,spec.name);return 100 end}
player={character=actor,get_main_inventory=function() return inventory end}
jev_fle_runtime={jev_session_id="atomic-fixture",fair={actor=function() return player end}}
helpers={table_to_json=function(value) captured=value;return "{}" end}
rcon={print=function(value) printed=value end}
''')
    lua.execute(change)
    return lua


def test_lua_reads_exactly_five_items_after_primary_without_action_or_callback_installation():
    lua = lua_query()
    lua.execute(observation_command('calls=calls+1'))
    assert lua.globals().calls == 1
    assert list(lua.globals().queries.values()) == ['coal', 'wood', 'iron-ore', 'copper-ore', 'stone']
    row = lua.globals().captured
    assert row.complete is True and row.tick == 10 and row.actor_unit == 17
    assert dict(row['items']) == {item: 100 for item in ITEMS}


@pytest.mark.parametrize('change', [
    'inventory.get_insertable_count=nil',
    'inventory.get_insertable_count=function() error("unavailable") end',
    'inventory.get_insertable_count=function() return -1 end',
])
def test_lua_unsupported_or_invalid_capacity_is_not_complete(change):
    lua = lua_query(change)
    lua.execute(observation_command('calls=calls+1'))
    assert lua.globals().calls == 1 and lua.globals().captured.complete is False


@pytest.mark.parametrize('command', [
    'game.tick=11', 'actor.unit_number=18', 'actor.surface.index=2',
    'actor.force.index=3', 'jev_fle_runtime.jev_session_id="foreign"',
    'player.character={}', 'actor.valid=false',
])
def test_lua_identity_change_aborts_before_publishing(command):
    lua = lua_query()
    with pytest.raises(Exception, match='crossed observation identity'):
        lua.execute(observation_command(command))
    assert lua.globals().captured is None


@pytest.mark.parametrize('valid', [True, False])
def test_atomic_publication_preserves_primary_when_sidecar_invalid(monkeypatch, valid):
    from test_atomic_observation import setup
    backend, native, source, calls = setup(monkeypatch, craft=True)
    source['inventory_capacity'] = primary()
    client = backend._instance.rcon_client
    original = client.send_command
    p = payload()
    if not valid: p['tick'] -= 1
    client.send_command = lambda command: original(command) + '\n' + raw(p)
    state = backend.observe()
    assert len(calls) == 1
    assert calls[0].count('observation_snapshot_v2(') == 1
    assert state.factory['inventory_insertable'] == (p['items'] if valid else {'coal': 100})
    assert state.factory['inventory_insertable_evidence']['tick'] == state.tick
    assert state._atomic_inventory_verified == (state.session_id, state.tick)
