"""Read-only connector proof for the current, unmigrated factory installation."""
import json

from ..iteration_timing import decode_native


CURRENT_MODULES = frozenset({
    'fair_actions', 'factory', 'launch_readiness', 'observation', 'observation_v2',
    'craft_jobs', 'output_buffers', 'input_routes', 'production_sites',
    'mining_outposts', 'connector_ownership',
})


def is_current_direct_installation(result):
    native = result.get('native_installation')
    return (isinstance(native, dict) and native.get('profile') is False
            and {name for name, present in result['modules'].items() if present} == CURRENT_MODULES
            and result['connector_observer_bridge_qualified'] is False
            and result['connector_snapshot_qualified'] is False)


def current_connector_snapshot_command(result):
    """Compare the normal snapshot with its direct ledger in one native tick.

    Called only AFTER metadata/callback and every installed asset hash have
    passed native_attachment.readback. No installer, binding or receipt write.
    Deliberately limited to an idle empty connector ledger; paid routes still
    require the existing qualified owner/migration workflow.
    """
    session = json.dumps(result['session_id'])
    actor = str(result['actor_unit'])
    assets = json.dumps(json.dumps(result['native_installation']['assets'], sort_keys=True))
    return '/sc ' + (
        'local rt=assert(jev_fle_runtime);local c=assert(rt.campaign);'
        'local n=assert(rt.native_installation);local cb=assert(n.callbacks);'
        'local a=assert(rt.agent_characters and rt.agent_characters[1]);'
        'local p=assert(game.get_player(rt.jev_bound_player_index or 1));'
        'assert(rt.jev_session_id==' + session + ' and n.session_id==' + session
        + ' and n.actor_unit==' + actor + ' and a.valid and a.unit_number==' + actor + ');'
        'assert(n.schema=="jev.native-installation.v2" and not n.profile '
        'and not rt.solid_routes and not rt.coal_supply and not rt.successors '
        'and not rt.coal_manual_journal_v1 and not rt.coal_manual_cycle_v2 '
        'and not rt.connector_observer_bridge_v1 and not rt.bootstrap_output_v1);'
        'assert(p.connected and p.character==a and p.force==a.force and p.surface==a.surface '
        'and not p.cheat_mode and game.speed==1 and not game.tick_paused);'
        'assert(cb.observe==c.observe and cb.connector_observe==c.observe_connector_ownership '
        'and cb.connector_begin==c.connector_begin and cb.connector_finish==c.connector_finish '
        'and cb.connector_page==c.connector_page);'
        'local function same(x,y) if type(x)~=type(y) then return false end;'
        'if type(x)~="table" then return x==y end;'
        'for k,v in pairs(x) do if not same(v,y[k]) then return false end end;'
        'for k in pairs(y) do if x[k]==nil then return false end end;return true end;'
        'assert(same(n.assets,helpers.json_to_table(' + assets + ')));'
        'local ledger=assert(c.connector_ledger);'
        'assert(ledger.protocol==1 and ledger.active==nil and next(ledger.routes)==nil);'
        'local tick=game.tick;local factory=c.observe();'
        'local ownership=assert(factory.connector_ownership);'
        'local direct=c.observe_connector_ownership();'
        'assert(factory.tick==tick and ownership.tick==tick and ownership.protocol==1 '
        'and ownership.session_id==rt.jev_session_id and ownership.active==nil '
        'and next(ownership.routes)==nil and same(ownership,direct));'
        'assert(game.tick==tick and ledger.active==nil and next(ledger.routes)==nil);'
        'rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,'
        'actor_unit=a.unit_number,tick=tick,connector_ownership=ownership}))'
    )


def qualify_current_connector_snapshot(client, result):
    row = decode_native(client.send_command(current_connector_snapshot_command(result)))
    if not isinstance(row, dict):
        raise RuntimeError('Current connector snapshot requires reconciliation')
    ownership = row.get('connector_ownership')
    if isinstance(ownership, dict) and ownership.get('routes') == []:
        ownership = {**ownership, 'routes': {}}
    if (set(row) != {'schema', 'session_id', 'actor_unit', 'tick', 'connector_ownership'}
            or type(row['schema']) is not int or row['schema'] != 1
            or row['session_id'] != result['session_id']
            or type(row['actor_unit']) is not int or row['actor_unit'] != result['actor_unit']
            or type(row['tick']) is not int or row['tick'] < 1
            or ownership != {'protocol': 1, 'session_id': result['session_id'],
                             'tick': row['tick'], 'routes': {}}
            or type(ownership.get('protocol')) is not int
            or type(ownership.get('tick')) is not int):
        raise RuntimeError('Current connector snapshot requires reconciliation')
    return {**result, 'connector_snapshot_qualified': True,
            'connector_snapshot_tick': row['tick'], 'connector_snapshot_ownership': ownership}
