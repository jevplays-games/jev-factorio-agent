"""Strict decoder for the negotiated, single-command native observation v2.

No inventory, receipt or actor control survives across freshness boundaries.
Discovery is advisory: returned coordinates never grant native action permission.
"""
from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

from ..craft_jobs import parse_craft_actor_observation_failure
from ..observation import parse_snapshot
from ..state import GameSnapshot

RAW_ITEMS = frozenset({'wood', 'coal', 'iron-ore', 'copper-ore', 'stone'})
BOUNDS = {'anchor_radius': 256, 'anchor_limit': 129,
          'bootstrap_radius': 1000, 'bootstrap_limit': 129,
          'bootstrap_output_radius': .75, 'bootstrap_output_limit': 2}
EXPANDED_ANCHOR_BOUNDS = {
    **BOUNDS, 'anchor_radius': 1024, 'water_radius': 256,
    'oil_query_radii': [256, 512, 1024],
}


def _actor_guarded_command(command: str) -> str:
    """Bind the paid-craft marker to the fair.actor call at this command boundary."""
    return '''local storage=jev_fle_runtime
local actor_ok, actor_or_error=pcall(storage.fair.actor)
if not actor_ok then
    local message=type(actor_or_error)=="string" and actor_or_error or ""
    local code
    if string.find(message,"Fair play requires the original connected character",1,true) then
        code="actor_unavailable"
    elseif string.find(message,"Fair player binding changed",1,true) then
        code="actor_changed"
    elseif string.find(message,"Fair play requires normal game speed",1,true) then
        code="actor_policy_changed"
    end
    local jobs=storage.campaign and storage.campaign.craft_jobs
    local job=type(jobs)=="table" and jobs.job or nil
    if code and type(job)=="table" and job.paid==true
            and type(job.id)=="string" and #job.id>0 and #job.id<=128 then
        rcon.print("JEV_CRAFT_OBSERVATION_FAILURE|"..helpers.table_to_json({
            schema=1,receipt=job.id,code=code
        }))
    else
        error(actor_or_error)
    end
else
''' + command + '''
end'''


def _map(value: Any, label: str, limit: int = 4096) -> dict:
    if value == []:
        value = {}
    if not isinstance(value, dict) or len(value) > limit:
        raise ValueError(f'Invalid atomic {label}')
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError(f'Invalid atomic {label}')
    return value


def _position(value: Any) -> tuple[float, float]:
    if (not isinstance(value, dict) or set(value) != {'x', 'y'}
            or any(type(v) not in {int, float} or not math.isfinite(v)
                   or abs(v) > 1_000_000 for v in value.values())):
        raise ValueError('Invalid atomic position')
    return value['x'], value['y']


def _inventory(value: Any) -> dict[str, int]:
    result = _map(value, 'inventory')
    for item, quantity in result.items():
        if not isinstance(item, str) or not item or len(item) > 128:
            raise ValueError('Invalid atomic inventory')
        _integer(quantity, 'inventory')
    return dict(result)


def _capacity(value: Any) -> dict:
    result = _map(value, 'capacity')
    for item, quantity in result.items():
        if not isinstance(item, str) or not item or len(item) > 128:
            raise ValueError('Invalid atomic capacity item')
        _integer(quantity, 'capacity')
    return dict(result)


def _actor_capacity(value: Any, tick: int) -> dict | None:
    """Accept only a fresh, explicitly identified main-inventory coal reading."""
    if value is False:
        return None
    if (not isinstance(value, dict)
            or set(value) != {'schema', 'tick', 'inventory', 'quality', 'method', 'items'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['tick']) is not int or value['tick'] != tick
            or value['inventory'] != 'character_main' or value['quality'] != 'normal'
            or value['method'] != 'get_insertable_count'
            or not isinstance(value['items'], dict) or set(value['items']) != {'coal'}):
        raise ValueError('Invalid atomic inventory capacity')
    count = value['items']['coal']
    if type(count) is not int or not 0 <= count <= 2**32 - 1:
        raise ValueError('Invalid atomic inventory capacity')
    return {**value, 'items': {'coal': count}}


def observe_atomic(native: Any, snapshot: GameSnapshot) -> GameSnapshot:
    """Read and validate one v2 snapshot before exposing any of its native facts."""
    from fle.env import Position
    from .native_attachment import (
        CLOSED_WORLD_PROFILE, EXPANDED_OBSERVATION_PROFILE,
        LEGACY_OBSERVATION_PROFILE, MANUAL_CYCLE_PROFILE,
        WATER_ORIGIN_OBSERVATION_PROFILE,
    )
    from ..bootstrap_output import (PROFILE as BOOTSTRAP_PROFILE,
                                   MANUAL_CYCLE_PROFILE as MANUAL_CYCLE_BOOTSTRAP_PROFILE)

    backend = native.backend
    attachment = getattr(backend, '_native_attachment', None)
    if attachment is None:
        expected_bounds = EXPANDED_ANCHOR_BOUNDS  # Fresh installations use v3.
    else:
        installed = attachment.get('native_installation')
        profile = installed.get('profile') if isinstance(installed, dict) else None
        if profile == LEGACY_OBSERVATION_PROFILE:
            expected_bounds = BOUNDS
        elif profile in {EXPANDED_OBSERVATION_PROFILE, WATER_ORIGIN_OBSERVATION_PROFILE,
                         MANUAL_CYCLE_PROFILE, CLOSED_WORLD_PROFILE, BOOTSTRAP_PROFILE,
                         MANUAL_CYCLE_BOOTSTRAP_PROFILE} or (
                profile is False and isinstance(installed, dict)):
            expected_bounds = EXPANDED_ANCHOR_BOUNDS
        else:
            raise ValueError('Unqualified atomic observer profile')
    prior = getattr(native, '_coherent_identity', None)
    prior_drill = getattr(native, '_coherent_drill', None)
    # A just-built fair bootstrap drill is also identity-bound, when available.
    if prior_drill is None and getattr(backend._drill, 'unit_number', None) is not None:
        prior_drill = _integer(backend._drill.unit_number, 'bootstrap identity', 1)
    # Keep the installed v5 callback/profile/witness bytes unchanged. The
    # source-built capacity sidecar follows that callback in the same /sc
    # command so actor, receiver, item, and tick facts share one RPC boundary.
    from .native_input_capacity import decode as decode_receiver_capacity
    from .native_input_capacity import observation_command

    from .native_bootstrap_output import observation_command as bootstrap_command
    from .native_bootstrap_output import decode as decode_bootstrap
    from .native_actor_capacity import observation_command as actor_capacity_command
    from .native_actor_capacity import decode as decode_actor_capacity
    command = actor_capacity_command(bootstrap_command(observation_command(native)))
    raw = native.command(_actor_guarded_command(command))
    actor_failure = parse_craft_actor_observation_failure(raw)
    if actor_failure is not None:
        raise actor_failure
    result = parse_snapshot(raw, backend._observation_profile, schemas=(2,))
    session = result.get('session_id')
    if not isinstance(session, str) or not session or len(session) > 128:
        raise ValueError('Invalid atomic session identity')
    identity = (session, *(_integer(result.get(k), 'identity', 1)
                           for k in ('actor_unit', 'surface_index', 'force_index')))
    if prior is not None and prior != identity:
        raise ValueError('Atomic observation identity changed')
    if snapshot.session_id and snapshot.session_id != session:
        raise ValueError('Atomic snapshot session changed')
    tick = _integer(result.get('tick'), 'tick')
    if tick < max(snapshot.tick, getattr(native, '_coherent_tick', 0)):
        raise ValueError('Atomic observation tick regressed')
    position = _position(result.get('position'))
    inventory = _inventory(result.get('inventory'))
    capacity = _actor_capacity(result.get('inventory_capacity', False), tick)
    try:
        expanded_capacity = decode_actor_capacity(raw, result, capacity)
    except (KeyError, TypeError, ValueError):
        # Optional raw-item headroom must not make an otherwise valid snapshot
        # unavailable. Retain only the independently validated primary reading.
        expanded_capacity = None
    if expanded_capacity is not None:
        capacity = expanded_capacity
    controls = result.get('controls')
    if (not isinstance(controls, dict) or type(controls.get('tick')) is not int
            or controls['tick'] != tick or _position(controls.get('position')) != position
            or not isinstance(controls.get('status'), str) or len(controls['status']) > 64
            or any(type(controls.get(k)) is not bool
                   for k in ('walking', 'mining', 'movement_started'))):
        raise ValueError('Invalid atomic control boundary')
    for field in ('path_requests', 'gained'):
        # Mining gains may be negative while a machine/player spends material;
        # no positive gain or action completion is inferred by this decoder.
        if field == 'gained':
            if type(controls.get(field)) is not int or abs(controls[field]) > 2**53 - 1:
                raise ValueError('Invalid atomic controls')
        else:
            _integer(controls.get(field), 'controls')
    factory = _map(result.get('factory'), 'factory')
    runtime = factory.get('acceptance_runtime')
    if (not isinstance(runtime, dict) or runtime.get('schema') != 1
            or type(runtime.get('schema')) is not int
            or factory.get('player_bound') is not True
            or factory.get('player_connected') is not True
            or type(factory.get('tick')) is not int or factory['tick'] != tick
            or runtime.get('session_id') != session
            or type(runtime.get('speed')) not in {int, float} or runtime['speed'] != 1
            or runtime.get('tick_paused') is not False):
        raise ValueError('Invalid atomic runtime binding')
    for key in ('actor_unit', 'surface_index', 'force_index'):
        if type(runtime.get(key)) is not int or runtime[key] != result[key]:
            raise ValueError('Atomic runtime identity changed')
    for key in ('entities', 'receipts'):
        factory[key] = _map(factory.get(key), key)
    # Capacity is private same-tick planning evidence. Keep it off factory and
    # GameSnapshot.for_jev() so the complete receiver matrix never reaches the model.
    try:
        receiver_capacity = decode_receiver_capacity(raw, result, native.catalog)
    except (AttributeError, KeyError, TypeError, ValueError):
        # Capacity is optional advisory evidence. A missing/over-budget or
        # unqualified sidecar must never invalidate the primary coherent game
        # snapshot; it simply cannot support a capacity-dependent plan.
        receiver_capacity = None
    # A capability wrapper cannot supply or preserve actor headroom. Only the
    # current top-level reading above can publish it after full validation.
    factory.pop('inventory_insertable', None)
    factory.pop('inventory_insertable_evidence', None)
    for entity in factory['entities'].values():
        if isinstance(entity, dict) and 'fuel_insertable' in entity:
            entity['fuel_insertable'] = _capacity(entity['fuel_insertable'])
    researched = factory.get('researched')
    # Factorio 2.0.77 encodes an empty Lua sequence as {}. Normalize only
    # this known list boundary; populated objects remain invalid evidence.
    if type(researched) is dict and not researched:
        researched = []
    if (not isinstance(researched, list) or len(researched) > 4096
            or any(not isinstance(v, str) or not v or len(v) > 128 for v in researched)):
        raise ValueError('Invalid atomic research state')
    # Publish the same validated list at both snapshot boundaries. The local
    # normalization above is otherwise lost when the factory dict is attached.
    factory['researched'] = researched
    launched = _integer(factory.get('rockets_launched'), 'launch counter')
    baseline = _integer(factory.get('rocket_baseline'), 'launch baseline')
    if launched < baseline:
        raise ValueError('Atomic launch counter regressed')
    bounds = result.get('bounds')
    if not isinstance(bounds, dict):
        raise ValueError('Invalid atomic query bounds')
    if (bounds != expected_bounds
            or any(type(value) is not type(expected_bounds[key])
                   for key, value in bounds.items())
            or (expected_bounds is EXPANDED_ANCHOR_BOUNDS
                and any(type(radius) is not int for radius in bounds['oil_query_radii']))):
        raise ValueError('Invalid atomic query bounds')
    anchor_diagnostics = result.get('anchor_diagnostics')
    if expected_bounds is EXPANDED_ANCHOR_BOUNDS:
        if (not isinstance(anchor_diagnostics, dict)
                or set(anchor_diagnostics) != {'oil', 'water', 'selection'}
                or anchor_diagnostics['selection'] != 'bounded_witness_not_global_nearest'
                or any(not isinstance(anchor_diagnostics[item], dict)
                       or set(anchor_diagnostics[item]) != {'radius', 'saturated'}
                       or type(anchor_diagnostics[item]['radius']) is not int
                       or type(anchor_diagnostics[item]['saturated']) is not bool
                       for item in ('oil', 'water'))
                or anchor_diagnostics['oil']['radius'] not in (256, 512, 1024)
                or anchor_diagnostics['water']['radius'] != 256):
            raise ValueError('Invalid atomic anchor diagnostics')
    elif anchor_diagnostics is not None:
        raise ValueError('Unexpected atomic anchor diagnostics')
    bootstrap = result.get('bootstrap')
    if (not isinstance(bootstrap, dict) or type(bootstrap.get('query_limit')) is not int
            or bootstrap['query_limit'] != 129
            or type(bootstrap.get('output_connected')) is not bool):
        raise ValueError('Invalid atomic bootstrap')
    placed = bootstrap.get('placed_entities')
    if type(placed) is dict and not placed:
        placed = []
    if (not isinstance(placed, list) or len(placed) > 128
            or any(name not in {'burner-mining-drill', 'wooden-chest'} for name in placed)):
        raise ValueError('Invalid atomic bootstrap entities')
    collected = _integer(bootstrap.get('iron_ore_collected'), 'bootstrap stock')
    drill_data = bootstrap.get('drill')
    drill = None
    if drill_data is not False:
        if (not isinstance(drill_data, dict) or drill_data.get('name') != 'burner-mining-drill'
                or not isinstance(drill_data.get('status'), str)
                or not drill_data['status'] or len(drill_data['status']) > 64):
            raise ValueError('Invalid atomic bootstrap drill')
        unit = _integer(drill_data.get('unit_number'), 'bootstrap unit', 1)
        if prior_drill is not None and unit != prior_drill:
            raise ValueError('Atomic bootstrap identity changed')
        drill = SimpleNamespace(name='burner-mining-drill', unit_number=unit,
                position=Position(x=_position(drill_data.get('position'))[0],
                                  y=_position(drill_data.get('position'))[1]),
                drop_position=Position(x=_position(drill_data.get('drop_position'))[0],
                                       y=_position(drill_data.get('drop_position'))[1]),
                status=SimpleNamespace(value=drill_data['status']),
                fuel=_inventory(drill_data.get('fuel')))
        if 'burner-mining-drill' not in placed:
            raise ValueError('Invalid atomic bootstrap membership')
    elif prior_drill is not None:
        raise ValueError('Atomic bootstrap drill missing')
    elif bootstrap['output_connected'] or collected:
        raise ValueError('Atomic bootstrap output without producer')
    if bootstrap['output_connected'] and 'wooden-chest' not in placed:
        raise ValueError('Atomic bootstrap chest missing')
    resources, nearby, targets = {}, {}, {}
    for group, allowed in (('targets', RAW_ITEMS), ('anchors', {'water', 'crude-oil'})):
        observed = _map(result.get(group), 'discovery', len(allowed))
        if set(observed) - allowed:
            raise ValueError('Invalid atomic discovery target')
        for item, value in observed.items():
            if (not isinstance(value, dict) or not isinstance(value.get('name'), str)
                    or not value['name'] or len(value['name']) > 128
                    or (item not in {'wood', 'water'} and value['name'] != item)
                    or (item == 'water' and value['name'] not in {'water', 'deepwater'})
                    or type(value.get('surface_index')) is not int
                    or value['surface_index'] != result['surface_index']):
                raise ValueError('Invalid atomic discovery identity')
            x, y = _position(value.get('position'))
            if (group == 'anchors' and item == 'water'
                    and (attachment is None or profile in {
                         WATER_ORIGIN_OBSERVATION_PROFILE, MANUAL_CYCLE_PROFILE,
                         CLOSED_WORLD_PROFILE, BOOTSTRAP_PROFILE, MANUAL_CYCLE_BOOTSTRAP_PROFILE}
                         or profile is False)
                    and (x != math.floor(x) or y != math.floor(y))):
                raise ValueError('Water-origin observer returned a non-tile anchor')
            resources[item] = Position(x=x, y=y)
            nearby[item] = math.hypot(x - position[0], y - position[1])
            anchor_radius = (anchor_diagnostics['water' if item == 'water' else 'oil']['radius']
                             if anchor_diagnostics is not None else bounds['anchor_radius'])
            if group == 'anchors' and nearby[item] > anchor_radius + 2:
                raise ValueError('Atomic anchor outside query bounds')
            if group == 'targets':
                targets[item] = {k: value[k] for k in ('name', 'position', 'surface_index')}
    counts = result.get('cache')
    if (not isinstance(counts, dict) or set(counts) != {'hits', 'misses'}
            or any(type(v) is not int or not 0 <= v <= 5 for v in counts.values())
            or sum(counts.values()) != 5):
        raise ValueError('Invalid atomic discovery diagnostics')
    # Validation has completed. Only now publish this observation and advisory
    # caches; malformed payloads cannot overwrite a previously coherent view.
    if capacity is not None:
        factory['inventory_insertable'] = dict(capacity['items'])
        factory['inventory_insertable_evidence'] = {
            **capacity, 'session_id': session,
            **{key: result[key] for key in ('actor_unit', 'surface_index', 'force_index')},
            'basis': 'native_insertable_count_estimate',
        }
    factory['fair_resource_targets'] = targets
    bootstrap_output = decode_bootstrap(raw, result, attachment)
    if 'bootstrap_output_pending' in result['factory']:
        factory['bootstrap_output_pending'] = result['factory']['bootstrap_output_pending']
    if bootstrap_output is not None:
        factory['bootstrap_output'] = bootstrap_output
    factory['observation_snapshot_schema'] = 2
    factory['observation_query_bounds'] = dict(bounds)
    if anchor_diagnostics is not None:
        factory['observation_anchor_diagnostics'] = anchor_diagnostics
    if drill:
        for role, entity in factory['entities'].items():
            if (isinstance(entity, dict) and entity.get('name') == 'wooden-chest'
                    and isinstance(entity.get('position'), dict)):
                point = _position(entity['position'])
                if (abs(point[0] - drill.drop_position.x) < .5
                        and abs(point[1] - drill.drop_position.y) < .5):
                    factory['drill_output_role'] = role
                    break
    if bootstrap_output is not None:
        from ..bootstrap_output import binding
        from .native_bootstrap_output import read_witness
        witness = (read_witness(attachment)
                   if bootstrap_output['origin'] == 'legacy_authorized_current_asset' else None)
        probe = SimpleNamespace(world_kind='fle', session_id=session, tick=tick,
            factory=factory, iron_ore_collected=collected,
            _bootstrap_output_ownership_witness=witness,
            _coherent_observation_verified=(session, tick),
            _atomic_inventory_verified=(session, tick))
        if binding(probe, allow_pending=True) is None:
            raise ValueError('Bootstrap output identity or ownership changed')
    else:
        witness = None
    snapshot.tick, snapshot.session_id, snapshot.world_kind = tick, session, 'fle'
    snapshot.player_position, snapshot.inventory = position, inventory
    snapshot.placed_entities, snapshot.nearby_resources = list(placed), nearby
    snapshot.drill_status = drill.status.value if drill else ''
    snapshot.drill_fuel = drill.fuel.get('coal', 0) if drill else 0
    snapshot.drill_output_connected = bootstrap['output_connected']
    snapshot.iron_ore_collected = collected
    snapshot.factory, snapshot.game_version = factory, native.catalog.version
    snapshot._bootstrap_output_ownership_witness = witness
    snapshot._receiver_input_capacity = receiver_capacity
    snapshot.researched, snapshot.victory = researched, launched > baseline
    snapshot.victory_source = 'native:base-game-rocket-launch' if snapshot.victory else None
    snapshot._native_controls = controls
    snapshot._coherent_observation_verified = (session, tick)
    backend._resources, backend._drill = resources, drill
    backend._bootstrap_output_pending = factory.get('bootstrap_output_pending', False)
    native._coherent_identity, native._coherent_tick = identity, tick
    native._coherent_drill = drill.unit_number if drill else None
    for name, value in counts.items():
        backend._observation_profile.cache[name] += value
    return snapshot
