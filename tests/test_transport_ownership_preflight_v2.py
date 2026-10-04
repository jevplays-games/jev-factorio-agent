"""Ownership protocol fixtures, not native evidence or payment authentication."""
from copy import deepcopy
from dataclasses import asdict
from importlib.resources import files
import json

import pytest

from jev_factorio.acceptance_io import canonical
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.dev_preflight import checkpoint_read, probe
from jev_factorio.dev_preflight_v2 import inspect_native, query_sha256, REPORT_SCHEMA, SCOPE
from jev_factorio.solid_controller import solid_loop_type
from test_coal_supply_lua import runtime
from test_mining_outposts import lua_runtime as outpost_runtime


QUERY = files('jev_factorio').joinpath('lua/acceptance_probe_v2.lua').read_text()


def plain(value):
    if not hasattr(value, 'items'):
        return value
    items = dict(value.items())
    if items and set(items) == set(range(1, len(items) + 1)):
        return [plain(items[i]) for i in range(1, len(items) + 1)]
    return {key: plain(item) for key, item in items.items()}


def prepared_runtime(paid=False):
    lua = runtime()
    if paid is True:
        lua.execute('coal_all()')
    elif paid == 'prefix':
        lua.execute('coal_build("alpha", "chest")')
    lua.execute('''
        jev_fle_runtime=storage;storage.jev_factorio_session=true
        storage.jev_bound_player_index=1
        player.walking_state={walking=false};player.mining_state={mining=false}
        player.cheat_mode=false;fair.quarantined=false;game.tick_paused=false
        script={active_mods={base="2.0.77"}}
        helpers={table_to_json=function(value) return value end}
        rcon={print=function(value) probe_result=value end}
        -- Actual extension methods have already made fixture ownership. The
        -- fixed query may not invoke any of them, even to refresh observations.
        local function forbidden() error("query called mutating callback") end
        campaign.observe=forbidden;fair.actor=forbidden;fair.place=forbidden
        campaign.prepare_solid_route=forbidden;campaign.build_solid_route=forbidden
        campaign.prepare_coal_source=forbidden;campaign.build_coal_source=forbidden
        campaign.transfer=forbidden
    ''')
    return lua


def projected(lua):
    lua.execute(QUERY)
    return plain(lua.globals().probe_result)


def wire_native(value):
    """Defensive empty-array wire fixture; current native 2.0.77 emitted {}."""
    def encoded(value):
        if isinstance(value, dict):
            return {key: encoded(item) for key, item in value.items()} if value else []
        if isinstance(value, list):
            return [encoded(item) for item in value]
        return value
    return json.loads(json.dumps(encoded(value)))


def checkpoint(native):
    epoch = {'actor_index': native['player_index'], 'surface_index': native['surface_index'],
             'force_index': native['force_index']}
    Memory = coal_loop_type(solid_loop_type(HierarchicalLoop)).memory_type
    return asdict(Memory(native['session_id'], 'rocket_launch', last_tick=native['tick'],
        solid_intents=native['solid_routes']['intents'], solid_epoch=epoch,
        solid_commitments=native['solid_routes']['commitments'],
        coal_targets=native['coal_supply']['targets'], coal_epoch=epoch,
        coal_commitments=native['coal_supply']['commitments']))


def native_for_checkpoint(checkpoint):
    """An explicit empty-ownership fixture for capture parser tests."""
    value = projected(prepared_runtime())
    value.update(session_id=checkpoint['session_id'], tick=checkpoint['last_tick'])
    value['entities'] = {}
    intents = checkpoint['solid_intents']
    value['solid_routes'].update(intents=deepcopy(intents), binding='\n'.join(
        ''.join(str(len(i[k])) + ':' + i[k] for k in ('source', 'target', 'item', 'destination'))
        for i in intents))
    targets = checkpoint['coal_targets']
    value['coal_supply'].update(targets=list(targets), binding='\n'.join(str(len(t)) + ':' + t for t in targets),
        admission_evidence=checkpoint.get('coal_economic_admission', False))
    return value


@pytest.mark.parametrize('paid', [False, True])
def test_fixed_query_projects_real_extension_owners_without_callbacks_or_mutation(tmp_path, paid):
    lua = prepared_runtime(paid)
    before = lua.eval('{paid_calls,transfers,game.tick,storage.solid_routes.serial,storage.coal_supply.serial}')
    before = plain(before)
    native = projected(lua)
    assert native['ownership_complete'] is True
    retained = checkpoint(native)
    path = tmp_path / 'checkpoint.json'
    path.write_bytes(canonical(retained))
    assert checkpoint_read(path)[0] == retained
    assert inspect_native(native, retained, retained['session_id']) == []
    assert projected(lua) == native
    assert plain(lua.eval('{paid_calls,transfers,game.tick,storage.solid_routes.serial,storage.coal_supply.serial}')) == before
    assert bool(native['coal_supply']['commitments']) is paid
    assert bool(native['solid_routes']['commitments']) is paid


@pytest.mark.parametrize('mutation', [
    'storage.coal_supply.rows.alpha.pending={phase="prepared"}',
    'storage.coal_supply.rows.alpha.manual_pending={phase="dispatching"}',
    'storage.solid_routes.cells[next(storage.solid_routes.cells)].pending={phase="placed"}',
    'fair.job={status="walking"}',
    'campaign.craft_jobs={submitting=true}',
    'storage.walking_queues={[1]={}}',
    'player.walking_state.walking=true',
])
def test_native_pending_or_ambiguous_work_is_not_an_idle_boundary(mutation):
    lua = prepared_runtime(True)
    retained = checkpoint(projected(lua))
    lua.execute(mutation)
    assert inspect_native(projected(lua), retained, retained['session_id']) == ['native_work_not_reconciled']


@pytest.mark.parametrize('mutation', [
    'storage.coal_supply.rows.alpha.parts.chest.entity=burner1',
    'storage.coal_supply.rows.alpha.parts.chest.unit_number=999999',
    'storage.coal_supply.rows.alpha.parts.chest.entity.position.x=100',
    'storage.coal_supply.rows.alpha.parts.chest.paid=true',
    'storage.coal_supply.rows.alpha.fault="ambiguous"',
    'storage.coal_supply.committed=false',
    'campaign.entities.alias=burner1',
    'storage.solid_routes.implementation_revision=99',
    'storage.coal_supply.rows.alpha.parts.chest.receipt=storage.coal_supply.rows.beta.parts.chest.receipt',
    'storage.coal_supply.rows.alpha.parts.chest.entity.valid=false',
])
def test_unknown_or_changed_raw_owner_cannot_qualify(mutation):
    lua = prepared_runtime(True)
    retained = checkpoint(projected(lua))
    lua.execute(mutation)
    native = projected(lua)
    assert native['ownership_complete'] is False
    assert inspect_native(native, retained, retained['session_id']) == ['ownership_probe_incomplete']


def test_missing_runtime_is_not_initialized_and_oversized_projection_is_incomplete():
    lua = prepared_runtime()
    lua.execute('jev_fle_runtime=nil')
    assert projected(lua)['ownership_complete'] is False
    assert lua.eval('jev_fle_runtime==nil')
    lua = prepared_runtime()
    lua.execute('for i=1,2049 do campaign.entities["fixture"..i]=burner1 end')
    value = projected(lua)
    assert value['truncated'] is True and value['ownership_complete'] is False


@pytest.mark.parametrize(('path', 'value', 'reason'), [
    (('solid_routes', 'binding'), 'changed', 'solid_treatment_mismatch'),
    (('coal_supply', 'admission_evidence'), True, 'coal_treatment_mismatch'),
    (('coal_supply', 'committed'), False, 'owned_coal_mismatch'),
    (('bound_player_index',), 2, 'invalid_actor_epoch'),
    (('surface_index',), 2, 'checkpoint_actor_epoch_mismatch'),
    (('mods',), {'base': '2.0.78'}, 'unsupported_native_mod_version'),
    (('ownership_complete',), False, 'ownership_probe_incomplete'),
    (('tick_paused',), True, 'simulation_not_normal_running'),
])
def test_python_revalidates_report_instead_of_trusting_success_flag(path, value, reason):
    native = projected(prepared_runtime(True))
    retained = checkpoint(native)
    target = native
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert inspect_native(native, retained, retained['session_id']) == [reason]


def test_python_rejects_paid_alias_bool_counter_and_unretained_output():
    native = projected(prepared_runtime(True))
    retained = checkpoint(native)
    p = native['coal_supply']['commitments']['alpha']['parts']['chest']
    p['paid'] = True
    assert inspect_native(native, retained, retained['session_id']) == ['owned_coal_mismatch']
    p['paid'] = 1
    native['output_buffers'] = {'present': True, 'protocol': 1, 'commitments': {
        'alpha': {'source_unit': 1, 'layout': 'unretained', 'parts': {}}}}
    assert inspect_native(native, retained, retained['session_id']) == ['ordinary_output_ownership_not_retained']


def test_private_v2_config_selects_fixed_query_and_preserves_non_authorization(tmp_path):
    native = projected(prepared_runtime())
    retained = checkpoint(native)
    checkpoint_path = tmp_path / 'checkpoint.json'
    checkpoint_path.write_bytes(canonical(retained))
    dev_uuid, prod_uuid = '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002'
    password = tmp_path / 'password'
    password.write_text('fixture-only'); password.chmod(0o600)
    config = tmp_path / 'config.json'
    config.write_bytes(canonical({'schema': 2, 'vm_uuid': dev_uuid, 'production_vm_uuid': prod_uuid,
        'session_id': retained['session_id'], 'port': 27015, 'password_file': str(password)}))
    config.chmod(0o600)
    dmi = tmp_path / 'dmi'; dmi.write_text(dev_uuid)
    commands = []
    class Client:
        def __init__(self, host, port, credential, timeout):
            assert (host, port, credential, timeout) == ('127.0.0.1', 27015, 'fixture-only', 10)
        def send_command(self, command):
            commands.append(command)
            return canonical(wire_native(native)).decode()
        def close(self):
            pass
    report = probe(config, checkpoint_path, client_factory=Client, dmi_path=dmi)
    assert commands == ['/sc ' + QUERY]
    assert report['schema'] == REPORT_SCHEMA and report['ownership_scope'] == SCOPE
    assert report['query_sha256'] == query_sha256()
    assert report['native']['solid_routes']['commitments'] == {}
    assert report['ready_for_coordinated_validation'] is True
    assert all(report[k] is False for k in ('gameplay_started', 'deployment_authorized', 'native_acceptance_proven'))
    retained['solid_funding'] = {'ambiguous': True}
    # A malformed or retained funding checkpoint is rejected before connecting.
    checkpoint_path.write_bytes(canonical(retained))
    with pytest.raises(ValueError):
        probe(config, checkpoint_path, client_factory=Client, dmi_path=dmi)
    assert len(commands) == 1


def prepared_outpost_runtime():
    lua = outpost_runtime.__wrapped__()
    lua.execute('''
        build_outpost()
        -- These fixtures began with an abbreviated, unpaid output buffer;
        -- remove that fixture scaffold before testing the real paid outpost.
        storage.output_buffers=nil
        storage.campaign.entities['out:arm']=nil;storage.campaign.entities['out:chest']=nil
        character.surface=source.surface;character.force=force
        player.index=1;player.walking_state={walking=false};player.mining_state={mining=false}
        game.connected_players={player};game.tick_paused=false
        game.get_player=function(i) return i==1 and player or nil end
        script.active_mods={base='2.0.77'}
        storage.jev_player_index=1;storage.jev_bound_player_index=1;storage.jev_factorio_session=true
        storage.fair.quarantined=false;jev_fle_runtime=storage
        for _,e in pairs(storage.campaign.entities) do
            e.quality={name='normal'}
            e.type=e.name=='stone-furnace' and 'furnace' or e.name=='wooden-chest' and 'container' or 'mining-drill'
        end
        helpers.table_to_json=function(value) return value end
        rcon.print=function(value) probe_result=value end
    ''')
    lua.execute(files('jev_factorio').joinpath('lua/solid_routes.lua').read_text())
    lua.execute(files('jev_factorio').joinpath('lua/coal_supply.lua').read_text())
    lua.execute('''
        storage.campaign.set_solid_intents({{source='coal:alpha:chest',target='alpha',item='coal',destination='fuel'},
            {source='coal:beta:chest',target='beta',item='coal',destination='fuel'}})
        storage.campaign.set_coal_targets({'alpha','beta'})
        local function forbidden() error('query invoked callback') end
        storage.campaign.observe=forbidden;storage.campaign.observe_mining_outposts=forbidden
        storage.fair.actor=forbidden
    ''')
    return lua


def test_actual_outpost_paid_cells_and_receipt_map_match_existing_checkpoint_loader(tmp_path):
    lua = prepared_outpost_runtime()
    native = projected(lua)
    assert native['ownership_complete'] is True
    retained = checkpoint(native)
    retained.update(input_routes_schema=1, input_commitments={}, outposts_schema=1,
                    outpost_commitments=deepcopy(native['outposts']['commitments']))
    # Outpost ownership depends on the composed input adapter. This actual
    # fixture has that runtime; it has no output-buffer schema or runtime.
    assert native['input_routes'] == {'present': True, 'protocol': 1, 'commitments': {}}
    assert native['output_buffers'] == {'present': False, 'protocol': 0, 'commitments': {}}
    path = tmp_path / 'checkpoint.json'; path.write_bytes(canonical(retained))
    assert checkpoint_read(path)[0] == retained
    assert inspect_native(native, retained, retained['session_id']) == []
    lua.execute("storage.mining_outposts.receipts['build:chest']=123456")
    assert inspect_native(projected(lua), retained, retained['session_id']) == ['outpost_receipt_mismatch']


@pytest.mark.parametrize(('family', 'schema', 'commitments'), [
    ('input_routes', 'input_routes_schema', 'input_commitments'),
    ('output_buffers', 'output_buffers_schema', 'output_commitments'),
])
def test_schema_enabled_empty_transport_rejects_absent_runtime_with_full_present_control(
        family, schema, commitments):
    native = projected(prepared_runtime())
    retained = checkpoint(native)
    retained.update({schema: 1, commitments: {}})
    assert native[family] == {'present': False, 'protocol': 0, 'commitments': {}}
    assert inspect_native(native, retained, retained['session_id']) == [
        'invalid_legacy_transport_projection']

    native[family] = {'present': True, 'protocol': 1, 'commitments': {}}
    assert inspect_native(native, retained, retained['session_id']) == []


def test_legacy_empty_transport_without_schema_may_remain_absent():
    native = projected(prepared_runtime())
    retained = checkpoint(native)
    assert inspect_native(native, retained, retained['session_id']) == []


def test_untracked_output_roles_cannot_hide_outside_output_cells():
    native = projected(prepared_runtime())
    retained = checkpoint(native)
    native['entities']['output-chest:17'] = native['entities'].pop('witness1')
    assert inspect_native(native, retained, retained['session_id']) == ['untracked_transport_entity']


@pytest.mark.parametrize('mode', ['empty', 'prefix', 'outpost'])
def test_empty_lua_tables_use_declared_map_normalization_at_json_validation_boundary(mode):
    native = projected(prepared_outpost_runtime() if mode == 'outpost'
                       else prepared_runtime('prefix' if mode == 'prefix' else False))
    if mode == 'empty':
        native['entities'] = {}
    retained = checkpoint(native)
    if mode == 'outpost':
        retained.update(outposts_schema=1, outpost_commitments=deepcopy(native['outposts']['commitments']))
    wire = wire_native(native)
    if mode == 'empty':
        assert wire['entities'] == [] and wire['solid_routes']['commitments'] == []
    elif mode == 'prefix':
        assert wire['coal_supply']['commitments']['beta']['parts'] == []
    else:
        assert wire['outposts']['commitments']['iron-ore']['flow'] == []
    before = deepcopy(wire)
    assert inspect_native(wire, retained, retained['session_id']) == []
    assert wire == before


@pytest.mark.parametrize('field', ['entities', 'parts', 'intents', 'targets'])
def test_map_normalization_never_coerces_nonempty_arrays_or_ordered_arrays(field):
    native = projected(prepared_runtime('prefix'))
    retained = checkpoint(native)
    native = wire_native(native)
    if field == 'entities':
        native['entities'] = [{'unit_number': 1}]
    elif field == 'parts':
        native['coal_supply']['commitments']['beta']['parts'] = [{'paid': 1}]
    elif field == 'intents':
        native['solid_routes']['intents'] = {}
    else:
        native['coal_supply']['targets'] = {}
    assert inspect_native(native, retained, retained['session_id'])
