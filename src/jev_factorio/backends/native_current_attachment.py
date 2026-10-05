"""Read-only connector proof for the current, unmigrated factory installation."""
import json

from ..iteration_timing import decode_native


CURRENT_MODULES = frozenset({
    'fair_actions', 'factory', 'launch_readiness', 'observation', 'observation_v2',
    'craft_jobs', 'output_buffers', 'input_routes', 'production_sites',
    'mining_outposts', 'connector_ownership',
})

_BASE_MODULES = frozenset({
    'fair_actions', 'factory', 'launch_readiness', 'connector_ownership',
})
_OBSERVATION_MODULES = _BASE_MODULES | {'observation', 'observation_v2'}
_BUFFER_MODULES = _BASE_MODULES | {'output_buffers'}
_BUFFER_WITH_OBSERVATIONS_MODULES = _BUFFER_MODULES | {'observation', 'observation_v2'}
_BACKGROUND_BUFFER_MODULES = _BUFFER_MODULES | {'craft_jobs'}
_BACKGROUND_BUFFER_WITH_OBSERVATIONS_MODULES = _BACKGROUND_BUFFER_MODULES | {
    'observation', 'observation_v2',
}
_SOLID_MODULES = _BASE_MODULES | {'solid_routes'}
_SOLID_WITH_OBSERVATIONS_MODULES = _SOLID_MODULES | {'observation', 'observation_v2'}
_SOLID_COAL_MODULES = _SOLID_MODULES | {'coal_supply'}
_SOLID_COAL_WITH_OBSERVATIONS_MODULES = _SOLID_COAL_MODULES | {
    'observation', 'observation_v2',
}
_FULL_SOLID_COAL_MODULES = CURRENT_MODULES | {'solid_routes', 'coal_supply'}

# These are the exact source-generated launch compositions covered by the
# bundled-installer qualification tests. This is intentionally not a generic
# optional-module subset rule.
DIRECT_MODULE_PROFILES = {
    'default': _BASE_MODULES,
    'observation_only': _OBSERVATION_MODULES,
    'buffers_without_background': _BUFFER_MODULES,
    'buffers_without_background_with_observations': _BUFFER_WITH_OBSERVATIONS_MODULES,
    'background_work_with_buffers': _BACKGROUND_BUFFER_MODULES,
    'background_work_with_buffers_and_observations': _BACKGROUND_BUFFER_WITH_OBSERVATIONS_MODULES,
    'solid_only': _SOLID_MODULES,
    'solid_only_with_observations': _SOLID_WITH_OBSERVATIONS_MODULES,
    'solid_and_coal': _SOLID_COAL_MODULES,
    'solid_and_coal_with_observations': _SOLID_COAL_WITH_OBSERVATIONS_MODULES,
    'current_full': CURRENT_MODULES,
    'solid_and_coal_with_prerequisites': _FULL_SOLID_COAL_MODULES,
}


def source_bound_direct_profile(result):
    native = result.get('native_installation')
    if (not isinstance(native, dict) or native.get('profile') is not False
            or not isinstance(result.get('modules'), dict)
            or result.get('connector_observer_bridge_qualified') is not False
            or result.get('connector_snapshot_qualified') is not False):
        return None
    installed = frozenset(name for name, present in result['modules'].items() if present)
    return next((name for name, modules in DIRECT_MODULE_PROFILES.items()
                 if installed == modules), None)


def is_current_direct_installation(result):
    return source_bound_direct_profile(result) == 'current_full'


def is_supported_direct_installation(result):
    return source_bound_direct_profile(result) is not None


def current_connector_snapshot_command(result, *, completed_routes=False, completed_craft=None,
                                       background_craft=None):
    """Qualify an exact bundled direct profile without changing ownership.

    Called only AFTER metadata/callback and every installed asset hash have
    passed native_attachment.readback. No installer, binding or receipt write.
    Every installed owner registry and known optional-component role must be
    empty before a direct snapshot is allowed. Profiles with retained work
    remain on the established owner/bridge reconciliation path; no stateful
    factory observer runs during direct qualification.
    """
    profile = source_bound_direct_profile(result)
    if profile is None:
        raise RuntimeError('Native direct attachment requires an exact supported module profile')
    if completed_routes and profile != 'current_full':
        raise RuntimeError('Completed connector attachment requires the current full profile')
    craft_guard = 'j.job==nil'
    craft_recipe = ''
    background_inventory = ''
    if completed_craft is not None and background_craft is not None:
        raise ValueError('Completed and unresolved craft bindings are mutually exclusive')
    if background_craft is not None:
        from .native_completed_craft import validate_background_craft_binding
        job = validate_background_craft_binding(background_craft)
        if not completed_routes or job.session_id != result['session_id']:
            raise RuntimeError('Background craft requires checkpoint-bound connector attachment')
        item = json.dumps(next(iter(job.outputs)))
        background_inventory = (
            ',background_inventory=(function() local n=0;for _,stack in pairs('
            'p.get_main_inventory().get_contents()) do if stack.name==' + item
            + ' then n=n+stack.count end end;return {[' + item + ']=n} end)()')
        craft_guard = ('type(j.job)=="table" and j.job.id==' + json.dumps(job.parameters['receipt'])
            + ' and j.job.status=="completed" and j.job.paid==true and j.job.error==nil'
              ' and j.job.session_id==rt.jev_session_id and j.job.unit_number==a.unit_number'
              ' and j.job.player_index==p.index and j.job.surface_index==a.surface.index'
              ' and j.job.force_index==a.force.index')
    if completed_craft is not None:
        from .native_completed_craft import validate_completed_craft_binding
        bound = validate_completed_craft_binding(completed_craft)
        if not completed_routes:
            raise RuntimeError('Completed craft requires checkpoint-bound connector attachment')
        if 'step_sha256' in bound:
            craft_recipe = (
                ',completed_craft_recipe=(function() '
                'local r=assert(a.force.recipes[j.job.recipe]);'
                'assert(#r.ingredients>0 and #r.ingredients<=32 and #r.products==1);'
                'return {energy=r.energy,ingredients=r.ingredients,products=r.products} end)()')
        craft_guard = ('type(j.job)=="table" and j.job.id==' + json.dumps(bound['id'])
            + ' and j.job.status=="completed" and j.job.paid==true and j.job.error==nil'
              ' and j.job.session_id==rt.jev_session_id and j.job.unit_number==a.unit_number'
              ' and j.job.player_index==p.index and j.job.surface_index==a.surface.index'
              ' and j.job.force_index==a.force.index')
    session = json.dumps(result['session_id'])
    actor = str(result['actor_unit'])
    assets = json.dumps(json.dumps(result['native_installation']['assets'], sort_keys=True))
    modules = json.dumps(json.dumps(result['modules'], sort_keys=True))
    expected_intents = json.dumps(json.dumps(result['solid_intents'], sort_keys=True))
    expected_coal_targets = json.dumps(json.dumps(result['coal_targets'], sort_keys=True))
    prefix = (
        'local rt=assert(jev_fle_runtime);local c=assert(rt.campaign);'
        'local f=assert(rt.fair);local l=assert(rt.launch_readiness);'
        'local n=assert(rt.native_installation);local cb=assert(n.callbacks);'
        'local a=assert(rt.agent_characters and rt.agent_characters[1]);'
        'local p=assert(game.get_player(rt.jev_bound_player_index or 1));'
        'assert(rt.jev_session_id==' + session + ' and n.session_id==' + session
        + ' and n.actor_unit==' + actor + ' and a.valid and a.unit_number==' + actor + ');'
        'assert(n.schema=="jev.native-installation.v2" and not n.profile '
        'and not rt.successors '
        'and not rt.coal_manual_journal_v1 and not rt.coal_manual_cycle_v2 '
        'and not rt.connector_observer_bridge_v1 and not rt.bootstrap_output_v1);'
        'assert(p.connected and p.character==a and p.force==a.force and p.surface==a.surface '
        'and not p.cheat_mode and game.speed==1 and not game.tick_paused);'
        'local function same(x,y) if type(x)~=type(y) then return false end;'
        'if type(x)~="table" then return x==y end;'
        'for k,v in pairs(x) do if not same(v,y[k]) then return false end end;'
        'for k in pairs(y) do if x[k]==nil then return false end end;return true end;'
        'local expected_modules=helpers.json_to_table(' + modules + ');'
        'local actual_modules={fair_actions=true,factory=c~=nil,launch_readiness=l~=nil,'
        'observation=c and type(c.observation_snapshot)=="function" or false,'
        'observation_v2=c and type(c.observation_snapshot_v2)=="function" or false,'
        'craft_jobs=c and c.craft_jobs~=nil or false,output_buffers=rt.output_buffers~=nil,'
        'input_routes=rt.input_routes~=nil,production_sites=rt.production_sites~=nil,'
        'mining_outposts=rt.mining_outposts~=nil,solid_routes=rt.solid_routes~=nil,'
        'coal_supply=rt.coal_supply~=nil,successors=rt.successors~=nil,'
        'connector_ownership=c and c.connector_ledger~=nil or false,'
        'coal_manual_journal_v1=rt.coal_manual_journal_v1~=nil,'
        'coal_manual_cycle_v2=rt.coal_manual_cycle_v2~=nil,'
        'connector_observer_bridge_v1=rt.connector_observer_bridge_v1~=nil,'
        'bootstrap_output_v1=rt.bootstrap_output_v1~=nil};'
        'assert(same(actual_modules,expected_modules));'
        'assert(same(n.assets,helpers.json_to_table(' + assets + ')));'
        'local function good(x) return type(x)=="function" end;'
        'assert(good(f.actor) and good(f.bind) and good(f.observe) '
        'and good(f.place) and good(f.tick_handler));'
        'if c then assert(good(c.observe) and good(c.transfer) and good(c.configure) '
        'and l.schema==1 and c.launch==l.launch and c.craft==l.craft '
        'and good(l.observer) and (c.observation_snapshot==nil or good(c.observation_snapshot)) '
        'and (c.observation_snapshot_v2==nil or good(c.observation_snapshot_v2)));end;'
        'local j=c.craft_jobs;local b=rt.output_buffers;local i=rt.input_routes;'
        'local s=rt.solid_routes;local q=rt.coal_supply;'
        'local o=rt.mining_outposts;local sites=rt.production_sites;'
        'if j then assert(good(j.observe_wrapper) and good(j.previous_observe) '
        'and j.previous_observe==l.observer and ' + craft_guard + ' and not j.submitting '
        'and type(p.crafting_queue_size)=="number" and p.crafting_queue_size==0 '
        'and good(j.pre_handler) and good(j.cancel_handler) and good(j.crafted_handler) '
        'and script.get_event_handler(defines.events.on_pre_player_crafted_item)==j.pre_handler '
        'and script.get_event_handler(defines.events.on_player_cancelled_crafting)==j.cancel_handler '
        'and script.get_event_handler(defines.events.on_player_crafted_item)==j.crafted_handler);end;'
        'if b then assert(l and b.protocol==1 and good(b.observer) and good(b.transfer) '
        'and b.previous_observe==(j and j.observe_wrapper or l.observer) '
        'and b.previous_transfer==l.transfer '
        'and script.get_event_handler(defines.events.on_tick)==b.tick_handler);end;'
        'if i then assert(b and i.protocol==1 and i.previous_observe==b.observer '
        'and i.previous_transfer==b.transfer and good(i.observer) and good(i.transfer));end;'
        'if s then assert(s.protocol==1 and s.implementation_revision==4 '
        'and s.contract_family=="straight-solid-corridor-v1" '
        'and s.reservation_contract=="full-corridor-manhattan-v1" '
        'and type(s.coal_api)=="table" and c.observe==s.observer '
        'and c.transfer==s.transfer and c.configure==s.configure);end;'
        'if q then assert(q.revision==4 and s and s.coal==q '
        'and c.prepare_coal_source==q.prepare and c.build_coal_source==q.build);end;'
        'if c and s then assert(c.observe==s.observer and c.transfer==s.transfer);'
        'elseif c and i then assert(c.observe==i.observer and c.transfer==i.transfer);'
        'elseif c and b then assert(c.observe==b.observer and c.transfer==b.transfer);'
        'elseif c and j then assert(c.observe==j.observe_wrapper and c.transfer==l.transfer);'
        'elseif c then assert(c.observe==l.observer and c.transfer==l.transfer);end;'
        'assert(cb.fair_tick==f.tick_handler and cb.observe==c.observe '
        'and cb.snapshot_v1==(c and c.observation_snapshot) '
        'and cb.snapshot_v2==(c and c.observation_snapshot_v2) '
        'and cb.transfer==c.transfer and cb.configure==c.configure '
        'and cb.connector_observe==c.observe_connector_ownership '
        'and cb.connector_begin==c.connector_begin and cb.connector_finish==c.connector_finish '
        'and cb.connector_page==c.connector_page);'
    )
    owner_guards = [
        'local function empty(t) return type(t)=="table" and next(t)==nil end;'
        'assert(type(c.entities)=="table");'
        'for role in pairs(c.entities) do if type(role)=="string" then '
        'assert(not (string.match(role,"^output%-chest:") '
        'or string.match(role,"^output%-arm:") or string.match(role,"^input:") '
        'or string.match(role,"^outpost:")));end end;'
    ]
    if completed_routes:
        from .native_settled_factory import SETTLED_FACTORY_GUARDS
        owner_guards.append(SETTLED_FACTORY_GUARDS)
    installed = result['modules']
    if installed.get('output_buffers') and not completed_routes:
        owner_guards.append(
            'assert(b and b.protocol==1 and empty(b.cells) and empty(b.offers));'
        )
    if installed.get('input_routes') and not completed_routes:
        owner_guards.append(
            'assert(i and i.protocol==1 and empty(i.cells) and empty(i.offers));'
        )
    if installed.get('production_sites') and not completed_routes:
        owner_guards.append(
            'assert(sites and sites.protocol==1 and empty(sites.owned) '
            'and empty(sites.offers));'
        )
    if installed.get('mining_outposts') and not completed_routes:
        owner_guards.append(
            'assert(o and o.protocol==1 and empty(o.cells) and empty(o.offers) '
            'and empty(o.receipts));'
        )
    prefix += ''.join(owner_guards)
    if profile in {
        'solid_only', 'solid_only_with_observations', 'solid_and_coal',
        'solid_and_coal_with_observations', 'solid_and_coal_with_prerequisites',
    }:
        expected_coal_flag = 'false' if profile.startswith('solid_only') else 'true'
        return '/sc ' + prefix + (
            'local s=assert(rt.solid_routes);local ledger=assert(c.connector_ledger);'
            'local intents=helpers.json_to_table(' + expected_intents + ');'
            'local coal_targets=helpers.json_to_table(' + expected_coal_targets + ');'
            'assert(s.protocol==1 and s.implementation_revision==4 '
            'and s.contract_family=="straight-solid-corridor-v1" '
            'and s.reservation_contract=="full-corridor-manhattan-v1" '
            'and s.coal_api and c.observe==s.observer and c.transfer==s.transfer '
            'and c.configure==s.configure and same(s.intents,intents) '
            'and s.pending==nil and s.fault==nil and not s.committed '
            'and s.manual_pending==nil);'
            'assert(empty(s.cells) and empty(s.offers));'
            'assert((rt.coal_supply~=nil)==' + expected_coal_flag + ');'
            'if rt.coal_supply then local q=rt.coal_supply;'
            'assert(q.revision==4 and s.coal==q and not q.committed '
            'and same(q.targets,coal_targets) and type(q.rows)=="table" '
            'and q.pending==nil and q.fault==nil and q.manual_pending==nil '
            'and c.prepare_coal_source==q.prepare and c.build_coal_source==q.build);'
            'for _,row in pairs(q.rows) do assert(type(row)=="table" '
            'and type(row.parts)=="table" and next(row.parts)==nil '
            'and row.pending==nil and row.manual_pending==nil and not row.fault '
            'and not row.committed and row.receipt==nil '
            'and type(row.manual_receipts)=="table" and next(row.manual_receipts)==nil '
            'and (row.manual_total or 0)==0);end end;'
            'assert(ledger.protocol==1 and ledger.active==nil '
            'and type(ledger.routes)=="table" and next(ledger.routes)==nil);'
            'local tick=game.tick;local ownership=c.observe_connector_ownership();'
            'assert(type(ownership)=="table" and ownership.protocol==1 '
            'and ownership.session_id==rt.jev_session_id and ownership.tick==tick '
            'and ownership.active==nil and type(ownership.routes)=="table" '
            'and next(ownership.routes)==nil and game.tick==tick '
            'and ledger.active==nil and next(ledger.routes)==nil);'
            'rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,'
            'actor_unit=a.unit_number,tick=tick,connector_ownership=ownership}))'
        )
    if profile in {
        'buffers_without_background', 'buffers_without_background_with_observations',
        'background_work_with_buffers', 'background_work_with_buffers_and_observations',
    }:
        return '/sc ' + prefix + (
            'local b=assert(rt.output_buffers);local ledger=assert(c.connector_ledger);'
            'assert(b.protocol==1 and type(b.cells)=="table" and next(b.cells)==nil '
            'and type(b.offers)=="table" and next(b.offers)==nil);'
            'assert(ledger.protocol==1 and ledger.active==nil '
            'and type(ledger.routes)=="table" and next(ledger.routes)==nil);'
            'local tick=game.tick;local ownership=c.observe_connector_ownership();'
            'assert(type(ownership)=="table" and ownership.protocol==1 '
            'and ownership.session_id==rt.jev_session_id and ownership.tick==tick '
            'and ownership.active==nil and type(ownership.routes)=="table" '
            'and next(ownership.routes)==nil and game.tick==tick '
            'and ledger.active==nil and next(ledger.routes)==nil);'
            'rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,'
            'actor_unit=a.unit_number,tick=tick,connector_ownership=ownership}))'
        )
    if completed_routes:
        return '/sc ' + prefix + (
            'assert(not rt.solid_routes and not rt.coal_supply);'
            'local ledger=assert(c.connector_ledger);'
            'assert(ledger.protocol==1 and ledger.active==nil);'
            'local count=0;for id,row in pairs(ledger.routes) do '
            'count=count+1;assert(count<=128 and row.state=="complete" '
            'and row.pending==nil);end;'
            'local tick=game.tick;local ownership=c.observe_connector_ownership();'
            'assert(type(ownership)=="table" and ownership.protocol==1 '
            'and ownership.session_id==rt.jev_session_id and ownership.tick==tick '
            'and ownership.active==nil and type(ownership.routes)=="table");'
            'assert(game.tick==tick and ledger.active==nil);'
            'rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,'
            'actor_unit=a.unit_number,tick=tick,connector_ownership=ownership,settled_factory=settled,'
            'completed_craft=j and j.job or false' + craft_recipe + background_inventory + '}))'
        )
    return '/sc ' + prefix + (
        'assert(not rt.solid_routes and not rt.coal_supply);'
        'local ledger=assert(c.connector_ledger);'
        'assert(ledger.protocol==1 and ledger.active==nil and next(ledger.routes)==nil);'
        'local tick=game.tick;local ownership=c.observe_connector_ownership();'
        'assert(type(ownership)=="table" and ownership.protocol==1 '
        'and ownership.session_id==rt.jev_session_id and ownership.tick==tick '
        'and ownership.active==nil and type(ownership.routes)=="table" '
        'and next(ownership.routes)==nil);'
        'assert(game.tick==tick and ledger.active==nil and next(ledger.routes)==nil);'
        'rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,'
        'actor_unit=a.unit_number,tick=tick,connector_ownership=ownership}))'
    )


def qualify_current_connector_snapshot(client, result, *, checkpoint_binding=None, completed_craft=None,
                                       background_craft=None):
    if checkpoint_binding is not None and checkpoint_binding.get('routes'):
        from .native_completed_attachment import qualify_completed_connectors
        return qualify_completed_connectors(client, result, checkpoint_binding,
                                            completed_craft=completed_craft, background_craft=background_craft)
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
