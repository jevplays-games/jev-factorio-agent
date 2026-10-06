"""Local decision evidence and transparent ranking; never action authorization.

Durations are policy estimates. Euclidean travel is a lower bound, not a native
path or arrival promise. Missing geometry is unknown, never zero-cost travel.
"""
from __future__ import annotations

import json
import hashlib
import math
from copy import deepcopy
from dataclasses import asdict, replace

from .scheduling import (RAW_TICKS_PER_ITEM, SAFETY_TICKS, SERVICE_TICKS,
                         TRAVEL_TICKS_PER_TILE, research_schedule)
from ..production_sites import sources as production_site_sources
from ..input_routes import sources as input_route_sources
from ..mining_outposts import (PARTS as OUTPOST_PARTS,
                               RESOURCES as OUTPOST_RESOURCES,
                               current as outpost_current,
                               remaining_kit as outpost_remaining_kit,
                               sources as outpost_sources)


def _finite(value):
    return type(value) in {int, float} and math.isfinite(value)


def _position(value):
    if isinstance(value, dict):
        value = [value.get('x'), value.get('y')]
    if isinstance(value, (tuple, list)) and len(value) == 2 and all(_finite(v) for v in value):
        return tuple(value)
    return None


def distinct_candidates(plans):
    """Collapse identical executable options without changing the retained ID.

    Apply after failure-budget filtering. Receipts are part of the signature:
    different verification identities must not be silently aliased.
    """
    result, seen = [], set()
    for plan in plans:
        signature = json.dumps([step.__dict__ for step in plan.steps], sort_keys=True,
                               allow_nan=False, separators=(',', ':'))
        if signature not in seen:
            seen.add(signature)
            result.append(plan)
    return result


def _craft_start_evidence(snapshot, catalog, step):
    """Describe a planned handcraft start; never certify future output."""
    parameters = step.parameters or {}
    recipe_name, batches = parameters.get('recipe'), parameters.get('batches')
    recipe = catalog.recipes.get(recipe_name, {})
    if type(batches) is not int or batches < 1 or not recipe:
        return None
    ingredients, products = recipe.get('ingredients', []), recipe.get('products', [])
    if (not ingredients or not products
            or any(entry.get('type') != 'item' or not _finite(entry.get('amount'))
                   or entry['amount'] <= 0 for entry in ingredients + products)
            or any(product.get('probability', 1) != 1 for product in products)):
        return None
    inputs = {}
    for entry in ingredients:
        inputs[entry['name']] = inputs.get(entry['name'], 0) + entry['amount'] * batches
    expected = {}
    for product in products:
        expected[product['name']] = expected.get(product['name'], 0) + product['amount'] * batches
    if inputs != (step.costs or {}) or step.item not in expected:
        return None
    factory = snapshot.factory
    return {
        'observed_tick': snapshot.tick,
        'native_recipe': recipe_name,
        'input_costs_match_native_recipe': True,
        'inputs_in_inventory_now': all(snapshot.inventory.get(item, 0) >= count
                                       for item, count in inputs.items()),
        'recipe_unlocked_and_handcraftable': (
            bool(catalog.hand_categories.get(recipe.get('category')))
            and catalog.enabled(recipe, snapshot.researched or [])),
        'player_connected_and_bound': (factory.get('player_connected') is True
                                       and factory.get('player_bound') is True),
        'crafting_queue_empty': factory.get('crafting_queue') == 0,
        'craft_job_protocol_ready': (type(factory.get('craft_jobs_protocol')) is int
                                     and factory['craft_jobs_protocol'] == 1
                                     if step.action == 'factory_craft_job' else None),
        'expected_products_after_native_verification': expected,
        'native_receipt_required_for_completion': step.action == 'factory_craft_job',
    }


def _local_target_completion_evidence(snapshot, catalog, plan, craft_start,
                                      craft_dependency):
    """Assess whether a receipt-conditional direct craft covers the local target.

    This is a forecast from the current native recipe and complete inventory
    observation. It never reports the craft as completed; the native receipt
    remains the only completion authority.
    """
    if (snapshot.world_kind != 'fle' or len(plan.steps) != 1
            or not isinstance(craft_start, dict)
            or not isinstance(craft_dependency, dict)):
        return None
    materials = plan.materials or {}
    local = materials.get('local_objective')
    intent = materials.get('work_intent')
    if not isinstance(local, dict) or not isinstance(intent, dict):
        return None
    item, target = local.get('item'), local.get('inventory_target')
    step = plan.steps[0]
    parameters = step.parameters or {}
    if (not isinstance(item, str) or not item or type(target) is not int or target < 1
            or step.action != 'factory_craft_job' or step.effect != 'craft_job_complete'
            or step.item != item or type(parameters.get('batches')) is not int
            or not 1 <= parameters['batches'] <= 200
            or not isinstance(parameters.get('recipe'), str)
            or not parameters['recipe']
            or not isinstance(parameters.get('receipt'), str)
            or not parameters['receipt']
            or intent.get('scope') != 'immediate'
            or intent.get('observed_tick') != snapshot.tick
            or craft_start.get('observed_tick') != snapshot.tick
            or craft_start.get('native_recipe') != parameters['recipe']
            or craft_start.get('native_receipt_required_for_completion') is not True
            or craft_dependency.get('observed_tick') != snapshot.tick
            or craft_dependency.get('current_craft_product') != item
            or craft_dependency.get('planner_item_path') != [item]
            or craft_dependency.get('basis') !=
                'current_recursive_planner_provenance_and_native_recipe'):
        return None
    if any(craft_start.get(key) is not True for key in (
            'input_costs_match_native_recipe', 'inputs_in_inventory_now',
            'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
            'crafting_queue_empty', 'craft_job_protocol_ready')):
        return None
    try:
        if not step.allowed(snapshot):
            return None
    except (KeyError, TypeError, ValueError):
        return None

    session, tick = snapshot.session_id, snapshot.tick
    identity = (session, tick)
    if (not isinstance(session, str) or not session or type(tick) is not int
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or getattr(snapshot, '_atomic_inventory_verified', None) != identity):
        return None
    inventory = snapshot.inventory
    if (not isinstance(inventory, dict) or len(inventory) > 4096
            or any(not isinstance(name, str) or not name or len(name) > 128
                   or type(amount) is not int or amount < 0
                   for name, amount in inventory.items())):
        return None
    current = inventory.get(item, 0)

    # Recompute the target item's output from the version-bound native catalog;
    # do not trust planner annotations or fractional/bool quantities.
    recipe = catalog.recipes.get(parameters['recipe'])
    outputs = craft_start.get('expected_products_after_native_verification')
    stack_size = catalog.stack_sizes.get(item)
    output_value = outputs.get(item) if isinstance(outputs, dict) else None
    if (not isinstance(recipe, dict) or recipe.get('name') != parameters['recipe']
            or recipe.get('hidden') or not catalog.enabled(recipe, snapshot.researched or [])
            or not isinstance(recipe.get('products'), list)
            or len(recipe['products']) != 1
            or not isinstance(outputs, dict) or not outputs
            or any(not isinstance(name, str) or not name
                   or type(amount) not in {int, float} or not _finite(amount)
                   or amount < 1 or not float(amount).is_integer()
                   for name, amount in outputs.items())
            or type(stack_size) is not int or stack_size < 1
            or type(output_value) not in {int, float} or not _finite(output_value)
            or output_value < 1 or not float(output_value).is_integer()):
        return None
    output = int(output_value)
    native_outputs = {}
    for product in recipe.get('products', []):
        probability = product.get('probability', 1) if isinstance(product, dict) else None
        if (not isinstance(product, dict) or product.get('type') != 'item'
                or type(probability) not in {int, float} or probability != 1
                or type(product.get('amount')) not in {int, float}
                or not _finite(product['amount']) or product['amount'] < 1
                or not float(product['amount']).is_integer()):
            return None
        name = product.get('name')
        if not isinstance(name, str) or not name:
            return None
        native_outputs[name] = (native_outputs.get(name, 0)
                                + int(product['amount']) * parameters['batches'])
    normalized_outputs = {name: int(amount) for name, amount in outputs.items()}
    if native_outputs != normalized_outputs or native_outputs.get(item) != output:
        return None

    shortfall = max(0, target - current)
    return {
        'observed_tick': tick,
        'session_id': session,
        'target_item': item,
        'target_inventory': target,
        'inventory_now': current,
        'shortfall_now': shortfall,
        'expected_output_after_native_receipt': output,
        'shortfall_after_expected_output': max(0, shortfall - output),
        'would_close_current_shortfall_if_native_receipt_verifies': (
            shortfall > 0 and output >= shortfall),
        'native_recipe': parameters['recipe'],
        'native_batches': parameters['batches'],
        'target_item_stack_size': stack_size,
        'inventory_basis': 'coherent_snapshot_and_atomic_craft_inventory',
        'native_receipt_required_for_completion': True,
        'forecast_is_not_completed_output': True,
    }


def _local_target_gather_completion_evidence(snapshot, plan, gather_start):
    """Qualify an immediate gather whose verified inventory threshold is the local target.

    This records only a conditional start forecast. It does not predict arrival,
    patch yield, or harvested quantity; the fresh native inventory threshold is
    the sole completion authority.
    """
    if (snapshot.world_kind != 'fle' or len(plan.steps) != 1
            or not isinstance(gather_start, dict)):
        return None
    materials = plan.materials or {}
    local = materials.get('local_objective')
    intent = materials.get('work_intent')
    if not isinstance(local, dict) or not isinstance(intent, dict):
        return None
    item, target = local.get('item'), local.get('inventory_target')
    step = plan.steps[0]
    parameters = step.parameters or {}
    quantity = parameters.get('quantity')
    if (not isinstance(item, str) or not item or type(target) is not int or target < 1
            or step.action != 'factory_gather' or step.effect != 'inventory'
            or step.item != item
            or set(parameters) != {'resource', 'quantity'}
            or parameters.get('resource') != item
            or type(quantity) is not int or not 1 <= quantity <= 200
            or step.costs not in (None, {})
            or type(step.threshold) is not int or step.threshold != target
            or intent.get('scope') != 'immediate'
            or intent.get('observed_tick') != snapshot.tick
            or gather_start.get('observed_tick') != snapshot.tick
            or gather_start.get('session_id') != snapshot.session_id
            or gather_start.get('resource_in_current_observation') is not True
            or gather_start.get('fair_target_identity_observed') is not True
            or gather_start.get('travel_is_lower_bound_not_arrival_proof') is not True):
        return None

    session, tick = snapshot.session_id, snapshot.tick
    identity = (session, tick)
    if (not isinstance(session, str) or not session or type(tick) is not int
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or getattr(snapshot, '_atomic_inventory_verified', None) != identity):
        return None
    native_target = _current_native_fair_resource_target(snapshot, item)
    if native_target is None:
        return None
    inventory = snapshot.inventory
    if (not isinstance(inventory, dict) or len(inventory) > 4096
            or any(not isinstance(name, str) or not name or len(name) > 128
                   or type(amount) is not int or amount < 0
                   for name, amount in inventory.items())):
        return None
    current = inventory.get(item, 0)
    shortfall = target - current
    if (shortfall <= 0 or quantity != shortfall
            or gather_start.get('resource_inventory_now') != current
            or gather_start.get('target_inventory_after_this_step') != step.threshold):
        return None

    # Require the current native actor-inventory insertable reading. The generic
    # command guard permits unknown headroom for compatibility; this stronger
    # forecast does not.
    factory = snapshot.factory
    runtime = factory.get('acceptance_runtime')
    headroom = factory.get('inventory_insertable')
    capacity = factory.get('inventory_insertable_evidence')
    if (not isinstance(runtime, dict) or not isinstance(headroom, dict)
            or type(headroom.get(item)) is not int or headroom[item] < quantity
            or not isinstance(capacity, dict) or capacity.get('schema') != 1
            or type(capacity.get('schema')) is not int
            or capacity.get('tick') != tick or type(capacity.get('tick')) is not int
            or capacity.get('session_id') != session
            or capacity.get('inventory') != 'character_main'
            or capacity.get('quality') != 'normal'
            or capacity.get('method') != 'get_insertable_count'
            or capacity.get('items') != headroom
            or capacity.get('basis') != 'native_insertable_count_estimate'
            or any(type(capacity.get(key)) is not int
                   or capacity[key] != runtime.get(key)
                   for key in ('actor_unit', 'surface_index', 'force_index'))
            or factory.get('player_connected') is not True
            or factory.get('player_bound') is not True):
        return None
    try:
        if not step.allowed(snapshot):
            return None
    except (KeyError, TypeError, ValueError):
        return None

    return {
        'observed_tick': tick,
        'session_id': session,
        'target_item': item,
        'target_inventory': target,
        'inventory_now': current,
        'shortfall_now': shortfall,
        'requested_gather_quantity': quantity,
        'target_inventory_threshold': step.threshold,
        'insertable_headroom_now': headroom[item],
        'fair_target_name': native_target['name'],
        'fair_target_surface_index': native_target['surface_index'],
        'requested_quantity_equals_current_shortfall': True,
        'would_close_current_shortfall_if_native_inventory_verifies': True,
        'inventory_basis': 'coherent_snapshot_and_atomic_native_inventory',
        'fresh_native_inventory_threshold_required': True,
        'travel_is_lower_bound_not_arrival_proof': True,
        'forecast_is_not_harvested_output': True,
    }


def _local_fuel_recipe_dependency(snapshot, catalog, plan, fuel_transfer_start):
    """Prove that a paid furnace-fuel step feeds the current local recipe goal.

    This is a direct item-ingredient edge from the selected local recipe to the
    item produced by an owned, currently observed furnace role. It does not
    infer fuel for a boiler, power generation, or completion of the local goal.
    """
    if (len(plan.steps) != 1 or plan.steps[0].action != 'factory_insert'
            or not isinstance(fuel_transfer_start, dict)):
        return None
    step = plan.steps[0]
    parameters = step.parameters or {}
    tick, session = snapshot.tick, snapshot.session_id
    identity = (session, tick)
    factory = snapshot.factory
    inventory = snapshot.inventory
    runtime = factory.get('acceptance_runtime')
    receipts = factory.get('receipts')
    if (snapshot.world_kind != 'fle' or type(tick) is not int or tick < 0
            or not isinstance(session, str) or not session
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or getattr(snapshot, '_atomic_inventory_verified', None) != identity
            or factory.get('observation_snapshot_schema') != 2
            or type(factory.get('observation_snapshot_schema')) is not int
            or type(factory.get('tick')) is not int or factory.get('tick') != tick
            or not isinstance(runtime, dict) or runtime.get('schema') != 1
            or type(runtime.get('schema')) is not int or runtime.get('session_id') != session
            or type(runtime.get('actor_unit')) is not int or runtime['actor_unit'] < 1
            or type(runtime.get('surface_index')) is not int or runtime['surface_index'] < 1
            or type(runtime.get('force_index')) is not int or runtime['force_index'] < 1
            or type(runtime.get('speed')) not in {int, float} or runtime.get('speed') != 1
            or runtime.get('tick_paused') is not False
            or not isinstance(runtime.get('mods'), dict)
            or set(runtime['mods']) - {'base', 'core'}
            or runtime['mods'].get('base') != catalog.version
            or snapshot.game_version != catalog.version
            or factory.get('player_connected') is not True
            or factory.get('player_bound') is not True
            or not isinstance(inventory, dict)
            or not isinstance(receipts, dict)
            or not isinstance(factory.get('entities'), dict)
            or parameters.get('item') != 'coal'
            or parameters.get('role') != fuel_transfer_start.get('burner_role')
            or parameters.get('quantity') != fuel_transfer_start.get('coal_to_transfer')
            or parameters.get('receipt') != fuel_transfer_start.get('native_receipt')
            or fuel_transfer_start.get('observed_tick') != tick
            or fuel_transfer_start.get('basis') !=
                'current_planner_need_owned_burner_and_paid_inventory'
            or fuel_transfer_start.get('burner_unit') is None
            or type(parameters.get('quantity')) is not int or parameters['quantity'] < 1
            or step.effect != 'transfer'
            or step.costs != {'coal': parameters.get('quantity')}
            or parameters.get('receipt') !=
                f'{tick}:factory_insert:{parameters.get("role")}:coal'
            or parameters['receipt'] in receipts):
        return None

    local = (plan.materials or {}).get('local_objective')
    if not isinstance(local, dict):
        return None
    local_item, local_target = local.get('item'), local.get('inventory_target')
    current_local = inventory.get(local_item, 0) if isinstance(local_item, str) else None
    if (not isinstance(local_item, str) or not local_item
            or type(local_target) is not int or local_target < 1
            or local.get('ultimate_goal') != plan.goal
            or type(current_local) is not int or current_local < 0
            or current_local >= local_target):
        return None

    role = parameters.get('role')
    if not isinstance(role, str) or not role.startswith('recipe:'):
        return None
    producer_item = role.removeprefix('recipe:')
    if not producer_item:
        return None
    entity = snapshot.factory.get('entities', {}).get(role)
    unit = fuel_transfer_start.get('burner_unit')
    if (not isinstance(entity, dict) or entity.get('name') not in {'stone-furnace', 'steel-furnace'}
            or type(entity.get('unit_number')) is not int or entity['unit_number'] != unit
            or type(unit) is not int or unit < 1
            or entity.get('recipe') not in ('', producer_item)):
        return None
    fuel_bag = entity.get('fuel')
    current_fuel = fuel_bag.get('coal', 0) if isinstance(fuel_bag, dict) else None
    current_coal = inventory.get('coal')
    quantity = parameters['quantity']
    try:
        if (type(current_fuel) is not int or current_fuel < 0
                or current_fuel != fuel_transfer_start.get('fuel_now')
                or type(current_coal) is not int or current_coal < quantity
                or fuel_transfer_start.get('coal_in_inventory_now') != current_coal
                or not step.allowed(snapshot) or step.satisfied(snapshot)):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    try:
        sites = snapshot.factory.get('production_sites')
        owned = production_site_sources(snapshot).get(role)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if (not isinstance(sites, dict) or sites.get('protocol') != 1
            or sites.get('session_id') != session or sites.get('tick') != tick
            or not _recipe_source_matches(snapshot, catalog, role, entity)):
        return None
    surveyed_source = isinstance(owned, dict) and owned.get('state') == 'owned'

    researched = snapshot.researched
    if not isinstance(researched, list):
        return None
    local_recipe = catalog.recipes.get(local_item)
    producer_recipe = catalog.recipes.get(producer_item)
    prototype = catalog.machines.get(entity.get('name'))
    if (not isinstance(local_recipe, dict) or local_recipe.get('name') != local_item
            or type(local_recipe.get('hidden')) is not bool or local_recipe['hidden']
            or type(local_recipe.get('enabled')) is not bool
            or not isinstance(producer_recipe, dict)
            or producer_recipe.get('name') != producer_item
            or type(producer_recipe.get('hidden')) is not bool or producer_recipe['hidden']
            or type(producer_recipe.get('enabled')) is not bool
            or not isinstance(prototype, dict) or prototype.get('burner') is not True
            or prototype.get('categories', {}).get(producer_recipe.get('category')) is not True):
        return None
    try:
        if (not catalog.enabled(local_recipe, researched)
                or not catalog.enabled(producer_recipe, researched)):
            return None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

    def has_item_product(recipe, item):
        products = recipe.get('products')
        return (isinstance(products, list) and any(
            isinstance(product, dict) and product.get('type') == 'item'
            and product.get('name') == item and _finite(product.get('amount'))
            and product['amount'] > 0 for product in products))

    ingredients = local_recipe.get('ingredients')
    matching_ingredients = ([entry for entry in ingredients if isinstance(entry, dict)
                             and entry.get('type') == 'item'
                             and entry.get('name') == producer_item
                             and _finite(entry.get('amount')) and entry['amount'] > 0]
                            if isinstance(ingredients, list) else [])
    if (not has_item_product(local_recipe, local_item)
            or not has_item_product(producer_recipe, producer_item)
            or not matching_ingredients):
        return None

    path = [local_item, producer_item]
    return {
        'observed_tick': tick,
        'session_id': session,
        'basis': ('same_tick_enabled_local_recipe_ingredient_and_owned_furnace'
                  if surveyed_source else
                  'same_tick_enabled_local_recipe_ingredient_and_registered_furnace'),
        'planner_item_path': path,
        'local_target_item': local_item,
        'local_target_inventory_now': current_local,
        'local_target_inventory_target': local_target,
        'local_target_shortfall_now': local_target - current_local,
        'local_recipe': local_item,
        'local_recipe_enabled_now': True,
        'ingredient_item': producer_item,
        'ingredient_amount_per_batch': min(entry['amount'] for entry in matching_ingredients),
        'producer_role': role,
        'producer_unit': unit,
        'producer_recipe': producer_item,
        'producer_recipe_enabled_now': True,
        'producer_machine': entity['name'],
        'producer_machine_recipe_now': entity.get('recipe'),
        **({'producer_owned_source_identity_current': True} if surveyed_source else
           {'producer_registered_source_identity_current': True}),
        'does_not_establish_furnace_output_or_goal_completion': True,
    }


def _buffer_build_start_evidence(snapshot, catalog, plan):
    if len(plan.steps) != 1 or plan.steps[0].action != 'factory_buffer_build':
        return None
    from .output_buffers import buffer_build_start
    step = plan.steps[0]
    proof = buffer_build_start(snapshot, catalog, step.parameters)
    if (proof is None or step.effect != 'buffer_component'
            or step.costs != {proof['component_item']: 1}
            or not step.allowed(snapshot) or step.satisfied(snapshot)):
        return None
    return proof


def _buffer_fuel_start_evidence(snapshot, catalog, plan):
    if len(plan.steps) != 1 or plan.steps[0].action != 'factory_insert':
        return None
    from .output_buffers import buffer_fuel_start
    step = plan.steps[0]
    proof = buffer_fuel_start(snapshot, catalog, step.parameters)
    service = (plan.materials or {}).get('fuel_service', {})
    if not isinstance(service, dict):
        return None
    consumers = service.get('consumers')
    if proof is None or not isinstance(consumers, list) or not consumers:
        return None
    primary = consumers[0]
    if (step.effect != 'transfer' or step.costs != {'coal': proof['coal_to_transfer']}
            or service.get('schema') != 2 or service.get('observed_tick') != snapshot.tick
            or type(service.get('consumer_count')) is not int
            or service['consumer_count'] != len(consumers)
            or service.get('acquisition_performed_by_this_plan') is not False
            or primary != {'role': proof['burner_role'], 'fuel': proof['fuel_now'],
                'target': proof['fuel_now']+proof['current_coal_deficit'],
                'deficit': proof['current_coal_deficit'], 'insertable': proof['fuel_insertable_now']}
            or type(service.get('carried_spendable')) is not int
            or proof['coal_to_transfer'] != min(proof['current_coal_deficit'], service['carried_spendable'])
            or not step.allowed(snapshot) or step.satisfied(snapshot)):
        return None
    return proof


def _placement_start_evidence(snapshot, plan):
    if len(plan.steps) != 1 or plan.steps[0].action != 'factory_place':
        return None
    step = plan.steps[0]
    parameters = step.parameters or {}
    if not str(parameters.get('anchor', '')).startswith('cell-site:'):
        return None
    try:
        site = production_site_sources(snapshot).get(parameters.get('role'), {})
    except (ValueError, KeyError, TypeError):
        return None
    if (site.get('state') != 'proposed' or site.get('reason') != 'joint_layout_available'
            or site.get('anchor') != parameters.get('anchor')
            or parameters.get('name') != 'stone-furnace'
            or step.costs != {'stone-furnace': 1}
            or _position(site.get('position')) is None):
        return None
    return {
        'observed_tick': snapshot.tick,
        'source_role': parameters['role'],
        'site_anchor': parameters['anchor'],
        'site_position': deepcopy(site['position']),
        'site_state': 'proposed',
        'native_offer_checked_current_site_clearance': True,
        'paid_furnace_in_inventory_now': snapshot.inventory.get('stone-furnace', 0) >= 1,
        'no_source_owned_at_role_now': parameters['role'] not in snapshot.factory.get('entities', {}),
        'player_connected_and_bound_now': (
            snapshot.factory.get('player_connected') is True
            and snapshot.factory.get('player_bound') is True),
        'crafting_queue_empty_now': snapshot.factory.get('crafting_queue') == 0,
        'travel_is_lower_bound_not_arrival_proof': True,
        'native_preflight_rechecks_offer_and_actor': True,
        'later_transport_and_output_require_native_verification': True,
    }


def _utility_lab_research_dependency(snapshot, catalog, plan):
    """Qualify a paid lab as the current capability-research prerequisite.

    This is deliberately not placement-site evidence. The ordinary native
    placement action searches for a site and rechecks manual build conditions
    at dispatch; until then, collision clearance, travel, and arrival remain
    unknown.
    """
    if (plan.goal != 'rocket_launch' or len(plan.steps) != 1
            or plan.steps[0].action != 'factory_place'):
        return None
    step = plan.steps[0]
    parameters = step.parameters or {}
    materials = plan.materials or {}
    dependency = materials.get('utility_lab_research_dependency')
    local = materials.get('local_objective')
    intent = materials.get('work_intent')
    economics = materials.get('economics')
    technology = dependency.get('technology') if isinstance(dependency, dict) else None
    if (parameters != {'role': 'utility:lab', 'name': 'lab', 'anchor': 'factory'}
            or step.costs != {'lab': 1}
            or not isinstance(dependency, dict) or not isinstance(local, dict)
            or not isinstance(intent, dict) or not isinstance(economics, dict)
            or dependency.get('observed_tick') != snapshot.tick
            or dependency.get('objective') != 'unlock_basic_assembly'
            or dependency.get('required_role') != 'utility:lab'
            or dependency.get('basis') !=
                'current_capability_technology_and_native_research_planner'
            or dependency.get('power_and_research_are_not_established') is not True
            or economics.get('observed_tick') != snapshot.tick
            or economics.get('objective') != 'unlock_basic_assembly'
            or economics.get('technology') != technology
            or local.get('kind') != 'research_prerequisite'
            or local.get('ultimate_goal') != plan.goal
            or local.get('immediate_prerequisite') != 'utility:lab'
            or local.get('observed_tick') != snapshot.tick
            or local.get('basis') != 'current_capability_research_plan'
            or local.get('later_power_and_research_need_native_verification') is not True
            or local.get('primary_target') != {
                'kind': 'native_technology', 'technology': technology}
            or intent.get('observed_tick') != snapshot.tick
            or intent.get('scope') != 'immediate'
            or intent.get('basis') != 'current_selected_capability_research_prerequisite'
            or not isinstance(technology, str) or not technology
            or type(snapshot.factory.get('player_connected')) is not bool
            or snapshot.factory.get('player_connected') is not True
            or snapshot.factory.get('player_bound') is not True
            or snapshot.factory.get('crafting_queue') != 0
            or snapshot.factory.get('research') not in ('', None)
            or 'utility:lab' in snapshot.factory.get('entities', {})
            or type(snapshot.inventory.get('lab')) is not int
            or snapshot.inventory['lab'] < 1):
        return None
    from .economics import capability_technology
    if capability_technology(catalog, snapshot.researched or []) != technology:
        return None
    tech = catalog.technologies.get(technology, {})
    assembler = catalog.recipes.get('assembling-machine-1', {})
    if (assembler.get('name') != 'assembling-machine-1'
            or not tech.get('enabled') or tech.get('trigger')
            or any(parent not in (snapshot.researched or [])
                   for parent in tech.get('prerequisites', []))
            or catalog.enabled(assembler, snapshot.researched or [])
            or technology not in catalog.unlocks('assembling-machine-1')):
        return None
    return {
        'observed_tick': snapshot.tick,
        'technology': technology,
        'technology_not_researched_now': technology not in (snapshot.researched or []),
        'technology_unlocks_basic_assembler': True,
        'current_research_idle': True,
        'current_technology_prerequisites_satisfied': True,
        'lab_required_by_native_research_walk': True,
        'utility_lab_absent_now': True,
        'paid_lab_in_inventory_now': snapshot.inventory['lab'],
        'player_connected_and_bound_now': True,
        'crafting_queue_empty_now': True,
        'native_placement_site_preflight_performed': False,
        'placement_site_clearance_unknown_until_dispatch': True,
        'travel_and_arrival_unverified': True,
        'existing_native_action_performs_bounded_search_and_fresh_build_checks': True,
        'native_build_result_and_fresh_role_postcondition_required': True,
        'lab_power_and_research_require_later_native_verification': True,
        'basis': 'same_tick_capability_research_plan_and_paid_lab_prerequisite',
    }


def _utility_power_prerequisite_start_evidence(
        snapshot, catalog, plan, gather_start, local_target_completion, *,
        craft_start=None, recipe_input_transfer_start=None, output_pickup_start=None,
        raw_prerequisite=None, fuel_prerequisite=None, fuel_transfer_start=None,
        buffer_build_start=None, buffer_fuel_start=None):
    """Bind a power-chain prerequisite to a current consumer and native topology.

    This supports only the next planner-selected prerequisite. It never claims
    that a constructed chain generates electricity, that a transfer succeeded,
    or that downstream research/production completed.
    """
    materials = plan.materials or {}
    annotation = materials.get('utility_power_prerequisite')
    if not isinstance(annotation, dict) or set(annotation) != {
            'observed_tick', 'consumer_role', 'consumer_unit', 'planner_path', 'research'}:
        return None

    tick, session = snapshot.tick, snapshot.session_id
    factory = snapshot.factory
    identity = (session, tick)
    runtime = factory.get('acceptance_runtime')
    if (snapshot.world_kind != 'fle' or type(tick) is not int or tick < 0
            or not isinstance(session, str) or not session
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or factory.get('observation_snapshot_schema') != 2
            or type(factory.get('tick')) is not int or factory.get('tick') != tick
            or not isinstance(runtime, dict) or runtime.get('schema') != 1
            or type(runtime.get('schema')) is not int
            or runtime.get('session_id') != session
            or type(runtime.get('speed')) not in {int, float} or runtime.get('speed') != 1
            or runtime.get('tick_paused') is not False
            or type(runtime.get('actor_unit')) is not int or runtime['actor_unit'] < 1
            or type(runtime.get('surface_index')) is not int or runtime['surface_index'] < 1
            or type(runtime.get('force_index')) is not int or runtime['force_index'] < 1
            or not isinstance(runtime.get('mods'), dict)
            or set(runtime['mods']) - {'base', 'core'}
            or runtime['mods'].get('base') != catalog.version
            or snapshot.game_version != catalog.version
            or factory.get('player_connected') is not True
            or factory.get('player_bound') is not True
            or type(factory.get('crafting_queue')) is not int
            or factory.get('crafting_queue') != 0
            or not isinstance(snapshot.inventory, dict)
            or not isinstance(factory.get('receipts'), dict)):
        return None

    role = annotation.get('consumer_role')
    consumer_unit = annotation.get('consumer_unit')
    path = annotation.get('planner_path')
    research = annotation.get('research')
    if (annotation.get('observed_tick') != tick or type(annotation.get('observed_tick')) is not int
            or not isinstance(role, str) or not role or len(role) > 128
            or type(consumer_unit) is not int or consumer_unit < 1
            or not isinstance(path, list) or len(path) > 32
            or any(not isinstance(entry, str) or not entry or len(entry) > 128 for entry in path)
            or len(path) != len(set(path))):
        return None
    path_research = next((entry.removeprefix('technology:') for entry in reversed(path)
                          if entry.startswith('technology:')), None)
    if (research != path_research
            or any(entry.startswith('technology:') and not entry.removeprefix('technology:')
                   for entry in path)):
        return None

    # The catalog is revalidated against the live runtime's version and mod
    # set before using its boiler burner prototype or recipe/technology facts.
    boiler_prototype = catalog.machines.get('boiler')
    if (not isinstance(boiler_prototype, dict)
            or boiler_prototype.get('burner') is not True
            or not isinstance(catalog.recipes, dict)
            or not isinstance(catalog.technologies, dict)):
        return None

    entities = factory.get('entities')
    if not isinstance(entities, dict):
        return None

    def current_entity(entity_role, expected_name):
        row = entities.get(entity_role)
        if (not isinstance(row, dict) or row.get('name') != expected_name
                or type(row.get('unit_number')) is not int or row['unit_number'] < 1):
            return None
        return row

    consumer = entities.get(role)
    if (not isinstance(consumer, dict) or type(consumer.get('unit_number')) is not int
            or consumer.get('unit_number') != consumer_unit):
        return None

    # Validate a real, current planner consumer. A lab must be the explicit
    # steam-power goal or the selected prerequisite for a supported technology.
    demand = None
    if role == 'utility:lab':
        if consumer.get('name') != 'lab':
            return None
        if research is None:
            if (plan.goal != 'steam_power'
                    or any(entry.startswith('technology:') for entry in path)):
                return None
            demand = {
                'kind': 'explicit_steam_power_goal',
                'current_research': factory.get('research') or None,
            }
        else:
            tech = catalog.technologies.get(research)
            researched = snapshot.researched
            if (plan.goal != 'rocket_launch' or not isinstance(tech, dict)
                    or not isinstance(researched, list)
                    or type(tech.get('enabled')) is not bool
                    or research in researched or not tech.get('enabled')
                    or tech.get('trigger')
                    or not isinstance(tech.get('prerequisites'), list)
                    or any(parent not in researched for parent in tech.get('prerequisites', []))
                    or (factory.get('research') not in ('', None, research))):
                return None
            demand = {
                'kind': 'current_technology_lab_demand',
                'technology': research,
                'technology_not_researched_now': True,
                'technology_prerequisites_satisfied_now': True,
            }
    elif role.startswith('recipe:') and plan.goal == 'rocket_launch':
        recipe_name = role.removeprefix('recipe:')
        recipe = catalog.recipes.get(recipe_name)
        prototype = catalog.machines.get(consumer.get('name'))
        researched = snapshot.researched
        if not isinstance(researched, list):
            return None
        try:
            recipe_ready = (
                isinstance(recipe, dict) and recipe.get('name') == recipe_name
                and type(recipe.get('hidden')) is bool and recipe.get('hidden') is False
                and type(recipe.get('enabled')) is bool
                and catalog.enabled(recipe, researched)
                and isinstance(prototype, dict) and prototype.get('electric') is True
                and isinstance(prototype.get('categories'), dict)
                and prototype['categories'].get(recipe.get('category')) is True
                and consumer.get('recipe') == recipe_name
                and isinstance(recipe.get('products'), list)
                and any(entry.startswith('item:') and any(
                    isinstance(product, dict) and product.get('type') == 'item'
                    and product.get('name') == entry.removeprefix('item:')
                    and _finite(product.get('amount')) and product['amount'] > 0
                    for product in recipe.get('products', [])) for entry in path))
        except (KeyError, TypeError, AttributeError, ValueError):
            return None
        if not recipe_ready:
            return None
        if research is not None:
            tech = catalog.technologies.get(research)
            if (not isinstance(tech, dict) or research in researched
                    or type(tech.get('enabled')) is not bool or not tech.get('enabled')
                    or tech.get('trigger')
                    or not isinstance(tech.get('prerequisites'), list)
                    or any(parent not in researched
                           for parent in tech.get('prerequisites', []))):
                return None
        demand = {
            'kind': 'current_native_recipe_demand',
            'recipe': recipe_name,
            'recipe_enabled_now': True,
            'machine_recipe_matches_now': True,
        }
    else:
        return None

    # Recompile the direct next power prerequisite against this snapshot. Both
    # serial and transport-aware planners are supported; only an exact offered step
    # with the current annotation can qualify.
    rebuilt = None
    try:
        from .factory import FactoryPlanner
        from .ready_work import ReadyWorkPlanner
        from .output_buffers import OutputBufferPlanner
        from .input_routes import InputRoutePlanner
        from .mining_outposts import MiningOutpostPlanner
        from . import capital

        capital_marker = materials.get(capital.MARKER)
        if capital_marker is not None:
            # A capital wrapper is not authority to accept an arbitrary power
            # action. Validate its catalog-bound spec and reproduce the entire
            # current continuation, including its stage, ID and paid step.
            if (not isinstance(capital_marker, dict)
                    or set(capital_marker) != {'spec', 'stage', 'observed_tick'}
                    or capital_marker['stage'] != 'supply'
                    or type(capital_marker['observed_tick']) is not int
                    or capital_marker['observed_tick'] != tick):
                return None
            capital.validate_spec(capital_marker['spec'], catalog, snapshot.researched)
            if capital_marker['spec']['role'] != role:
                return None

        for planner_type in (FactoryPlanner, ReadyWorkPlanner, OutputBufferPlanner,
                             InputRoutePlanner, MiningOutpostPlanner):
            if issubclass(planner_type, OutputBufferPlanner):
                # Atomic observation coherence does not by itself validate the
                # paid owner records that transport-aware planning consults.
                # Reuse the durable ownership and native component predicates
                # before a buffer kit can establish a power-chain purpose.
                from ..output_buffers import (sources as buffer_sources,
                                              validate_commitments,
                                              component_complete)
                buffer_rows = buffer_sources(snapshot)
                owners = {source: {'source_unit': row.get('source_unit'),
                                  'layout': row.get('layout'),
                                  'parts': row.get('parts')}
                          for source, row in buffer_rows.items()}
                validate_commitments(owners, successors='successors' in factory)
                if any(not component_complete({
                        'source': source, 'layout': row['layout'],
                        'part': part, 'receipt': owner['receipt']}, snapshot)
                       for source, row in buffer_rows.items()
                       for part, owner in row['parts'].items()):
                    return None
            current_planner = planner_type(catalog, snapshot, plan.goal)
            current = (capital.continuation(current_planner, capital_marker['spec'])
                       if capital_marker is not None
                       else current_planner._powered(role, tuple(path)))
            if (capital_marker is not None
                    and (current.materials or {}).get(capital.MARKER) != capital_marker):
                continue
            if _same_current_steps(catalog, current, plan):
                current_annotation = (current.materials or {}).get('utility_power_prerequisite')
                if current_annotation == annotation:
                    rebuilt = current
                    break
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if rebuilt is None or len(plan.steps) != 1:
        return None

    pump = current_entity('utility:water', 'offshore-pump')
    boiler = current_entity('utility:boiler', 'boiler')
    engine = current_entity('utility:engine', 'steam-engine')
    units = [row['unit_number'] for row in (pump, boiler, engine, consumer) if row is not None]
    if len(units) != len(set(units)):
        return None

    # Match live segment/network identities, not mere entity existence. An
    # incomplete chain is allowed only for a planner-selected construction or
    # connection prerequisite; boiler fuel is not admitted before all links.
    def fluid_segments(entity, fluid):
        ports = entity.get('fluid_ports', [])
        if ports == {}:
            ports = []
        if not isinstance(ports, list) or len(ports) > 64:
            return None
        result = set()
        for port in ports:
            if not isinstance(port, dict) or not isinstance(port.get('fluid', ''), str):
                return None
            if port.get('fluid', '') not in {'', fluid} or 'id' not in port:
                continue
            segment = port.get('id')
            if type(segment) is not int or segment < 1:
                return None
            result.add(segment)
        return result

    water_connected = steam_connected = electric_connected = False
    if pump is not None and boiler is not None:
        pump_water = fluid_segments(pump, 'water')
        boiler_water = fluid_segments(boiler, 'water')
        if pump_water is None or boiler_water is None:
            return None
        water_connected = bool(pump_water & boiler_water)
    if boiler is not None and engine is not None:
        boiler_steam = fluid_segments(boiler, 'steam')
        engine_steam = fluid_segments(engine, 'steam')
        if boiler_steam is None or engine_steam is None:
            return None
        steam_connected = bool(boiler_steam & engine_steam)
    if engine is not None and consumer is not None:
        network_engine, network_consumer = (engine.get('electric_network_id'),
                                            consumer.get('electric_network_id'))
        if any(value is not None and (type(value) is not int or value < 1)
               for value in (network_engine, network_consumer)):
            return None
        electric_connected = (type(network_engine) is int and network_engine > 0
                              and network_engine == network_consumer)

    step = plan.steps[0]
    parameters = step.parameters or {}
    action_kind = 'utility_chain_prerequisite'
    boiler_coal = None
    fuel_bag = boiler.get('fuel') if boiler is not None else None
    observed_boiler_coal = (fuel_bag.get('coal', 0) if isinstance(fuel_bag, dict) else None)
    coal_carried = snapshot.inventory.get('coal')
    coal_deficit = None
    planned_receipt = None
    gather_evidence = None
    child_evidence = None
    local_recipe_dependency = None

    def child_start(kind, fields):
        witnesses = {name: deepcopy(value) for name, value in fields.items()}
        child_parameters = dict(step.parameters or {})
        role_value = child_parameters.get('role') or (child_parameters.get('source')
            if step.action == 'factory_buffer_build' else None)
        path_value = next((value.get('planner_item_path') for value in witnesses.values()
                           if isinstance(value, dict)
                           and isinstance(value.get('planner_item_path'), list)), None)
        if path_value is None:
            for provenance_key in ('raw_prerequisite', 'craft_dependency',
                                   'recipe_input_transfer', 'output_pickup',
                                   'fuel_prerequisite'):
                provenance = (plan.materials or {}).get(provenance_key)
                candidate_path = (provenance.get('planner_item_path')
                                  if isinstance(provenance, dict) else None)
                if isinstance(candidate_path, list) and candidate_path:
                    path_value = candidate_path
                    break
        if path_value is None:
            path_value = [entry.removeprefix('item:') for entry in path
                          if entry.startswith('item:')]
        return {
            'observed_tick': tick,
            'kind': kind,
            'action': step.action,
            'item': (child_parameters.get('item') if step.action in {'factory_insert', 'factory_extract'}
                     else step.item),
            'role': role_value or child_parameters.get('resource'),
            'quantity': child_parameters.get('quantity'),
            'planner_item_path': list(path_value),
            'step_costs': dict(step.costs or {}),
            'witness_fields': sorted(witnesses),
            'witnesses': witnesses,
            'completion_requires_fresh_native_receipt_or_inventory': True,
        }

    if isinstance(fuel_transfer_start, dict):
        local_recipe_dependency = _local_fuel_recipe_dependency(
            snapshot, catalog, plan, fuel_transfer_start)

    if step.action == 'factory_connect':
        expected = None
        if not water_connected:
            expected = {'source': 'utility:water', 'target': 'utility:boiler',
                        'kind': 'pipe', 'fluid': 'water'}
        elif not steam_connected:
            expected = {'source': 'utility:boiler', 'target': 'utility:engine',
                        'kind': 'pipe', 'fluid': 'steam'}
        elif not electric_connected:
            expected = {'source': 'utility:engine', 'target': role,
                        'kind': 'small-electric-pole', 'fluid': 'electricity'}
        if expected is None or parameters != expected or step.satisfied(snapshot):
            return None
        action_kind = 'utility_connection_start'
    elif step.action == 'factory_place':
        target = parameters.get('role')
        expected_roles = {'utility:water', 'utility:boiler', 'utility:engine'}
        if (target not in expected_roles or target in entities
                or parameters.get('anchor') not in {'water', 'utility:water', 'utility:boiler'}
                or step.satisfied(snapshot)):
            return None
        action_kind = 'utility_entity_construction_start'
    elif step.action == 'factory_insert':
        # Recursive power construction can itself depend on a paid recipe input
        # or a burner-furnace service transfer. These are accepted only from the
        # established same-tick witnesses; boiler fuel remains below and still
        # requires the complete, connected consumer chain.
        if (isinstance(buffer_fuel_start, dict)
                and buffer_fuel_start == _buffer_fuel_start_evidence(snapshot, catalog, plan)):
            action_kind = 'utility_chain_buffer_fuel_transfer_start'
            child_evidence = child_start(action_kind, {
                'buffer_fuel_start_evidence': buffer_fuel_start})
        elif (isinstance(recipe_input_transfer_start, dict)
                and recipe_input_transfer_start.get('observed_tick') == tick
                and recipe_input_transfer_start.get('basis') ==
                    'current_planner_recipe_input_and_owned_native_machine'
                and recipe_input_transfer_start.get('owned_source_role') == parameters.get('role')
                and recipe_input_transfer_start.get('ingredient') == parameters.get('item')
                and recipe_input_transfer_start.get('paid_quantity_to_transfer') == parameters.get('quantity')
                and recipe_input_transfer_start.get('planned_native_receipt_id') == parameters.get('receipt')
                and step.costs == {parameters.get('item'): parameters.get('quantity')}):
            action_kind = 'utility_chain_recipe_input_transfer_start'
            child_evidence = child_start(action_kind, {
                'recipe_input_transfer_start_evidence': recipe_input_transfer_start,
            })
        elif (isinstance(fuel_transfer_start, dict)
                and fuel_transfer_start.get('observed_tick') == tick
                and fuel_transfer_start.get('basis') ==
                    'current_planner_need_owned_burner_and_paid_inventory'
                and fuel_transfer_start.get('burner_role') == parameters.get('role')
                and fuel_transfer_start.get('coal_to_transfer') == parameters.get('quantity')
                and fuel_transfer_start.get('native_receipt') == parameters.get('receipt')
                and isinstance(local_recipe_dependency, dict)
                and fuel_transfer_start.get('local_recipe_dependency') ==
                    local_recipe_dependency
                and fuel_transfer_start.get('planner_item_path') ==
                    local_recipe_dependency.get('planner_item_path')
                and parameters.get('item') == 'coal'
                and step.costs == {'coal': parameters.get('quantity')}):
            action_kind = 'utility_chain_furnace_fuel_transfer_start'
            child_evidence = child_start(action_kind, {
                'fuel_transfer_start_evidence': fuel_transfer_start,
            })
        elif (not water_connected or not steam_connected or not electric_connected
                or boiler is None or type(observed_boiler_coal) is not int):
            return None
        else:
            boiler_coal = observed_boiler_coal
            if not 0 <= boiler_coal < 5:
                return None
            coal_deficit = 5 - boiler_coal
            planned_receipt = f'{tick}:factory_insert:utility:boiler:coal'
            if (step.effect != 'transfer' or parameters != {
                    'role': 'utility:boiler', 'item': 'coal',
                    'quantity': coal_deficit, 'receipt': planned_receipt}
                    or step.costs != {'coal': coal_deficit}
                    or type(coal_carried) is not int or coal_carried < coal_deficit
                    or getattr(snapshot, '_atomic_inventory_verified', None) != identity
                    or planned_receipt in factory.get('receipts', {})
                    or not step.allowed(snapshot) or step.satisfied(snapshot)):
                return None
            action_kind = 'boiler_fuel_transfer_start'
    elif step.action == 'factory_gather':
        boiler_gather = (water_connected and steam_connected and electric_connected
                         and boiler is not None and type(observed_boiler_coal) is int)
        if boiler_gather:
            if not isinstance(local_target_completion, dict):
                return None
            if (observed_boiler_coal < 0 or observed_boiler_coal >= 5
                    or local_target_completion.get('target_item') != 'coal'
                    or local_target_completion.get('observed_tick') != tick
                    or local_target_completion.get('inventory_basis') !=
                        'coherent_snapshot_and_atomic_native_inventory'):
                return None
            boiler_coal = observed_boiler_coal
            if type(coal_carried) is not int or coal_carried < 0:
                return None
            coal_deficit = 5 - boiler_coal - coal_carried
            gather_evidence = local_target_completion
            if (coal_deficit <= 0 or parameters.get('resource') != 'coal'
                    or parameters.get('quantity') != coal_deficit
                    or step.threshold != coal_carried + coal_deficit):
                return None
            action_kind = 'boiler_fuel_gather_start'
        else:
            resource = parameters.get('resource')
            fair_target = _current_native_fair_resource_target(snapshot, resource)
            raw_path = raw_prerequisite.get('planner_item_path') if isinstance(
                raw_prerequisite, dict) else None
            parent_item = (raw_prerequisite.get('direct_product')
                           if isinstance(raw_prerequisite, dict) else None)
            recipe_name = (raw_prerequisite.get('direct_recipe')
                           if isinstance(raw_prerequisite, dict) else None)
            recipe = catalog.recipes.get(recipe_name, {}) if isinstance(recipe_name, str) else {}
            raw_is_current = (
                isinstance(raw_prerequisite, dict)
                and raw_prerequisite.get('observed_tick') == tick
                # The published witness binds recipe/product/path; the exact
                # current ingredient is bound by the action parameters and the
                # catalog ingredient check below.
                and raw_prerequisite.get('ingredient', resource) == resource
                and isinstance(parent_item, str) and bool(parent_item)
                and isinstance(raw_path, list) and 2 <= len(raw_path) <= 32
                and raw_path[-2:] == [parent_item, resource]
                and recipe.get('name') == recipe_name and not recipe.get('hidden')
                and catalog.enabled(recipe, snapshot.researched or [])
                and any(isinstance(product, dict) and product.get('type') == 'item'
                        and product.get('name') == parent_item
                        and _finite(product.get('amount')) and product['amount'] > 0
                        for product in recipe.get('products', []))
                and any(isinstance(ingredient, dict) and ingredient.get('type') == 'item'
                        and ingredient.get('name') == resource
                        and _finite(ingredient.get('amount')) and ingredient['amount'] > 0
                        for ingredient in recipe.get('ingredients', [])))
            gather_is_current = (
                isinstance(gather_start, dict)
                and gather_start.get('observed_tick') == tick
                and gather_start.get('session_id') == session
                and gather_start.get('resource_in_current_observation') is True
                and gather_start.get('fair_target_identity_observed') is True
                and gather_start.get('travel_is_lower_bound_not_arrival_proof') is True
                and fair_target is not None
                and type(gather_start.get('resource_inventory_now')) is int
                and gather_start['resource_inventory_now'] == snapshot.inventory.get(resource, 0)
                and type(parameters.get('quantity')) is int
                and 1 <= parameters['quantity'] <= 50
                and set(parameters) == {'resource', 'quantity'}
                and parameters.get('resource') == resource
                and step.effect == 'inventory' and step.item == resource
                and type(step.threshold) is int
                and step.threshold == gather_start.get('target_inventory_after_this_step')
                and step.threshold == gather_start['resource_inventory_now'] + parameters['quantity']
                and step.costs in (None, {}))
            headroom = factory.get('inventory_insertable')
            capacity = factory.get('inventory_insertable_evidence')
            runtime_identity = factory.get('acceptance_runtime')
            headroom_is_current = (
                isinstance(headroom, dict)
                and type(headroom.get(resource)) is int
                and headroom[resource] >= parameters.get('quantity', 2**53)
                and isinstance(capacity, dict)
                and capacity.get('schema') == 1 and type(capacity.get('schema')) is int
                and capacity.get('tick') == tick and type(capacity.get('tick')) is int
                and capacity.get('session_id') == session
                and capacity.get('inventory') == 'character_main'
                and capacity.get('quality') == 'normal'
                and capacity.get('method') == 'get_insertable_count'
                and capacity.get('items') == headroom
                and capacity.get('basis') == 'native_insertable_count_estimate'
                and isinstance(runtime_identity, dict)
                and all(type(capacity.get(key)) is int
                        and capacity[key] == runtime_identity.get(key)
                        for key in ('actor_unit', 'surface_index', 'force_index')))
            gather_is_current = gather_is_current and headroom_is_current
            if not raw_is_current or not gather_is_current:
                # A burner-furnace coal gather is a separate current consumer
                # prerequisite. It is supported only by the planner's existing
                # same-tick current-fuel-need witness, never by boiler evidence.
                fuel_gather_is_current = (
                    isinstance(fuel_prerequisite, dict)
                    and fuel_prerequisite.get('observed_tick') == tick
                    and fuel_prerequisite.get('basis') ==
                        'current_planner_fuel_need_and_owned_native_burner'
                    and resource == 'coal'
                    and fuel_prerequisite.get('planned_gather_units') == parameters.get('quantity')
                    and type(fuel_prerequisite.get('current_unfunded_units')) is int
                    and fuel_prerequisite['current_unfunded_units'] > 0
                    and gather_is_current)
                if not fuel_gather_is_current:
                    return None
                action_kind = 'utility_chain_furnace_fuel_gather_start'
                child_evidence = child_start(action_kind, {
                    'gather_start_evidence': gather_start,
                    'fuel_prerequisite': fuel_prerequisite,
                })
            else:
                if (getattr(snapshot, '_atomic_inventory_verified', None) != identity
                        or not step.allowed(snapshot) or step.satisfied(snapshot)):
                    return None
                action_kind = 'utility_chain_raw_gather_start'
                child_evidence = child_start(action_kind, {
                    'gather_start_evidence': gather_start,
                    'raw_prerequisite': raw_prerequisite,
                })
    elif step.action in {'factory_craft', 'factory_craft_job'}:
        parameters = step.parameters or {}
        recipe_name, batches = parameters.get('recipe'), parameters.get('batches')
        recipe = catalog.recipes.get(recipe_name, {}) if isinstance(recipe_name, str) else {}
        expected = craft_start.get('expected_products_after_native_verification') if isinstance(
            craft_start, dict) else None
        handcraft_is_current = (
            isinstance(craft_start, dict)
            and craft_start.get('observed_tick') == tick
            and craft_start.get('native_recipe') == recipe_name
            and craft_start.get('input_costs_match_native_recipe') is True
            and craft_start.get('inputs_in_inventory_now') is True
            and craft_start.get('recipe_unlocked_and_handcraftable') is True
            and craft_start.get('player_connected_and_bound') is True
            and craft_start.get('crafting_queue_empty') is True
            and type(batches) is int and batches >= 1
            and isinstance(expected, dict) and type(expected.get(step.item)) is int
            and expected[step.item] > 0
            and recipe.get('name') == recipe_name and not recipe.get('hidden')
            and catalog.enabled(recipe, snapshot.researched or [])
            and step.item in {product.get('name') for product in recipe.get('products', [])
                              if isinstance(product, dict) and product.get('type') == 'item'
                              and product.get('probability', 1) == 1}
            and (step.action != 'factory_craft_job'
                 or (craft_start.get('craft_job_protocol_ready') is True
                     and craft_start.get('native_receipt_required_for_completion') is True)))
        if (not handcraft_is_current or not step.allowed(snapshot)
                or step.satisfied(snapshot)
                or (step.costs and any(count > 0 for count in step.costs.values())
                    and getattr(snapshot, '_atomic_inventory_verified', None) != identity)):
            return None
        action_kind = 'utility_chain_handcraft_start'
        child_evidence = child_start(action_kind, {'craft_start_evidence': craft_start})
    elif step.action == 'factory_buffer_build':
        if (not isinstance(buffer_build_start, dict)
                or buffer_build_start != _buffer_build_start_evidence(snapshot, catalog, plan)):
            return None
        action_kind = 'utility_chain_buffer_build_start'
        child_evidence = child_start(action_kind, {
            'buffer_build_start_evidence': buffer_build_start})
    elif step.action == 'factory_extract':
        if (not isinstance(output_pickup_start, dict)
                or output_pickup_start.get('observed_tick') != tick
                or output_pickup_start.get('basis') !=
                    'current_planner_output_and_owned_native_machine'
                or not step.allowed(snapshot) or step.satisfied(snapshot)):
            return None
        action_kind = 'utility_chain_output_pickup_start'
        child_evidence = child_start(action_kind, {
            'output_pickup_start_evidence': output_pickup_start,
        })
    else:
        # This witness is intentionally limited to a directly payable/harvestable
        # action or a physical utility-chain construction/connection step.
        return None

    try:
        if (step.costs and any(count > 0 for count in step.costs.values())
                and getattr(snapshot, '_atomic_inventory_verified', None) != identity):
            return None
        if not step.allowed(snapshot) or step.satisfied(snapshot):
            return None
    except (KeyError, TypeError, ValueError):
        return None

    planned_receipt_absent = None
    paid_inventory_sufficient = None
    action_coal_now = None
    if step.action == 'factory_insert':
        item = parameters.get('item')
        quantity = parameters.get('quantity')
        receipt = parameters.get('receipt')
        planned_receipt = receipt
        planned_receipt_absent = (isinstance(receipt, str) and bool(receipt)
                                  and receipt not in factory.get('receipts', {}))
        carried_item = snapshot.inventory.get(item) if isinstance(item, str) else None
        paid_inventory_sufficient = (
            isinstance(item, str) and type(quantity) is int and quantity > 0
            and type(carried_item) is int and carried_item >= quantity
            and step.costs == {item: quantity}
            and getattr(snapshot, '_atomic_inventory_verified', None) == identity
            and planned_receipt_absent is True)
        if item == 'coal':
            action_coal_now = carried_item
    elif step.action == 'factory_gather' and parameters.get('resource') == 'coal':
        action_coal_now = coal_carried

    return {
        'observed_tick': tick,
        'session_id': session,
        'consumer_role': role,
        'consumer_unit': consumer_unit,
        'planner_path': list(path),
        'research': research,
        'consumer_demand': demand,
        'boiler_catalog_burner_current': True,
        'utility_units_current': {
            'water_pump': pump['unit_number'] if pump else None,
            'boiler': boiler['unit_number'] if boiler else None,
            'steam_engine': engine['unit_number'] if engine else None,
        },
        'connections_current': {
            'water_to_boiler': water_connected,
            'boiler_to_engine_steam': steam_connected,
            'engine_to_consumer_electricity': electric_connected,
        },
        'local_recipe_dependency': local_recipe_dependency,
        'next_action_kind': action_kind,
        'next_action': step.action,
        'boiler_coal_now': boiler_coal,
        'boiler_coal_deficit_to_five': coal_deficit,
        'actor_coal_now': action_coal_now if type(action_coal_now) is int else None,
        'planned_native_receipt': planned_receipt,
        'planned_receipt_absent_now': planned_receipt_absent,
        'child_start_evidence': child_evidence,
        'transfer_receipt_observed_now': False if step.action == 'factory_insert' else None,
        'paid_inventory_sufficient_now': paid_inventory_sufficient,
        'gather_start_evidence': gather_evidence,
        'placement_or_connection_rechecks_native_preconditions': True,
        'native_transfer_rechecks_reach_capacity_receipt_and_postcondition': (
            step.action == 'factory_insert'),
        'does_not_establish_electricity_or_research_completion': True,
        'basis': 'exact_current_power_planner_step_and_coherent_native_chain',
    }


def _receiver_capacity_start_evidence(snapshot, catalog, role, item, quantity, source_unit):
    """Require an identity- and item-bound native receiver read from this RPC."""
    from ..backends.native_input_capacity import source_sha256

    factory = snapshot.factory
    value = getattr(snapshot, '_receiver_input_capacity', None)
    runtime = factory.get('acceptance_runtime')
    entity = factory.get('entities', {}).get(role)
    entity_name = entity.get('name') if isinstance(entity, dict) else None
    receiver = (value.get('receivers', {}).get(role)
                if isinstance(value, dict) and isinstance(value.get('receivers'), dict)
                else None)
    sample = (receiver.get('items', {}).get(item)
              if isinstance(receiver, dict) and isinstance(receiver.get('items'), dict)
              else None)
    actor_count = snapshot.inventory.get(item)
    if (snapshot.world_kind != 'fle'
            or getattr(snapshot, '_coherent_observation_verified', None)
                != (snapshot.session_id, snapshot.tick)
            or factory.get('observation_snapshot_schema') != 2
            or not isinstance(runtime, dict)
            or not isinstance(value, dict) or value.get('schema') != 1
            or type(value.get('schema')) is not int or value.get('complete') is not True
            or value.get('basis') != 'same_rpc_owned_campaign_receiver_capacity'
            or value.get('method') != 'get_insertable_count'
            or value.get('query_source_sha256') != source_sha256()
            or value.get('tick') != snapshot.tick
            or value.get('session_id') != snapshot.session_id
            or value.get('actor_unit') != runtime.get('actor_unit')
            or value.get('surface_index') != runtime.get('surface_index')
            or value.get('force_index') != runtime.get('force_index')
            or not isinstance(receiver, dict)
            or receiver.get('surface_index') != runtime.get('surface_index')
            or receiver.get('force_index') != runtime.get('force_index')
            or receiver.get('unit_number') != source_unit
            or type(receiver.get('unit_number')) is not int
            or receiver.get('name') != entity_name
            or receiver.get('type') not in {'furnace', 'assembling-machine', 'rocket-silo'}
            or type(receiver.get('burner')) is not bool
            or type(actor_count) is not int
            or type(quantity) is not int or quantity < 1
            or actor_count < quantity
            or type(sample) is not int
            or sample < quantity):
        return None
    machine = catalog.machines.get(receiver['name'], {})
    expected_type = ({'stone-furnace': 'furnace', 'steel-furnace': 'furnace',
                      'electric-furnace': 'furnace',
                      'assembling-machine-1': 'assembling-machine',
                      'assembling-machine-2': 'assembling-machine',
                      'assembling-machine-3': 'assembling-machine',
                      'rocket-silo': 'rocket-silo'}.get(receiver['name']))
    expected_inventory = ('fuel' if item == 'coal' and receiver['burner']
                          else 'furnace_source' if receiver['type'] == 'furnace'
                          else 'assembling_machine_input')
    source = (factory.get('production_sites', {}).get('sources', {}).get(role)
              if isinstance(factory.get('production_sites'), dict) else None)
    try:
        owned_sources = production_site_sources(snapshot)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if (receiver.get('type') != expected_type
            or receiver.get('burner') is not (machine.get('burner') is True)
            or not isinstance(source, dict) or source.get('state') != 'owned'
            or source.get('source_unit') != source_unit
            or owned_sources.get(role) != source):
        return None
    return {
        'observed_tick': snapshot.tick, 'session_id': snapshot.session_id,
        'actor_unit': value['actor_unit'], 'surface_index': value['surface_index'],
        'force_index': value['force_index'], 'source_role': role,
        'source_unit': source_unit, 'receiver_name': receiver['name'],
        'receiver_type': receiver['type'], 'inventory': expected_inventory,
        'item': item, 'actor_count_now': actor_count,
        'insertable_count_now': sample,
        'method': value['method'],
        'query_source_sha256': value['query_source_sha256'],
        'basis': 'same_rpc_owned_campaign_receiver_capacity',
        'native_dispatch_rechecks_insertable_count_before_removal': True,
    }


def _current_shared_bill(snapshot, catalog, plan, local):
    """Recompute the ready-work horizon, independently of the immediate plan bill.

    ``materials.batches`` describes the first recursive local plan. Ready-work
    targets can also cover the existing bounded research horizon. That smaller
    bill cannot authenticate (or disprove) a horizon craft's batch count.
    """
    from .demand import SupplyLedger, horizon_demands

    try:
        item, amount = local['item'], local['inventory_target']
        if (not isinstance(item, str) or not item or type(amount) is not int
                or amount <= 0 or local.get('ultimate_goal') != plan.goal
                or not isinstance((plan.materials or {}).get('batches'), dict)):
            return None
        demands = horizon_demands(snapshot, catalog, plan.goal, item, amount)
        bill = catalog.material_demands(
            demands, SupplyLedger.capture(snapshot, catalog).forecast_stock(),
            snapshot.researched)
        craft_item = plan.steps[0].item
        target = sum(math.ceil(ingredient['amount'] * batches)
                     for name, batches in bill.batches.items()
                     for ingredient in catalog.recipes[name]['ingredients']
                     if ingredient['type'] == 'item' and ingredient['name'] == craft_item)
        return {'demands': demands, 'batches': bill.batches, 'target': target}
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None


def _recipe_source_matches(snapshot, catalog, role, machine):
    """Bind a current recipe role without treating ore-site surveys as universal.

    Native recipe roles are registered after ordinary placement; discovery only
    adds stock containers. Ore furnaces still require their production-site
    identity. Registered burner furnaces choose their recipe from input, so an
    idle furnace may report an empty recipe. Its catalog must independently
    permit the enabled smelting recipe. This is current entity identity, not
    historical payment evidence.
    """
    try:
        unit = machine['unit_number']
        if type(unit) is not int or unit <= 0:
            return False
        sources = snapshot.factory.get('production_sites', {}).get('sources', {})
        if role in sources or role in {'recipe:iron-plate', 'recipe:copper-plate'}:
            source = sources.get(role)
            return (isinstance(source, dict) and source.get('state') == 'owned'
                    and type(source.get('source_unit')) is int and source['source_unit'] == unit)
        recipe_name = role.removeprefix('recipe:')
        recipe = catalog.recipes[recipe_name]
        prototype = catalog.machines[machine['name']]
        return (snapshot.world_kind == 'fle'
                and isinstance(snapshot.session_id, str) and bool(snapshot.session_id)
                and type(snapshot.tick) is int
                and type(snapshot.factory.get('tick')) is int
                and snapshot.factory['tick'] == snapshot.tick
                and role == 'recipe:' + recipe['name']
                and prototype.get('categories', {}).get(recipe['category']) is True
                and ((machine['recipe'] == recipe_name
                      and prototype.get('electric') is True and prototype.get('burner') is False)
                     or (machine['name'] in {'stone-furnace', 'steel-furnace'}
                         and prototype.get('burner') is True and prototype.get('electric') is False
                         and recipe['category'] == 'smelting'
                         and machine['recipe'] in ('', recipe_name)
                         and not recipe.get('hidden')
                         and catalog.enabled(recipe, snapshot.researched or []))))
    except (KeyError, TypeError, AttributeError):
        return False


def _recipe_input_transfer_start_evidence(snapshot, catalog, plan, *, path_root=None,
                                          require_receiver_capacity=False, include_dependency_chain=False):
    """Bind a paid recipe input transfer to current native facts, not future output."""
    if len(plan.steps) != 1:
        return None
    step = plan.steps[0]
    provenance = (plan.materials or {}).get('recipe_input_transfer')
    local = (plan.materials or {}).get('local_objective')
    parameters = step.parameters or {}
    if (step.action != 'factory_insert' or step.effect != 'transfer'
            or not isinstance(provenance, dict) or not isinstance(local, dict)):
        return None
    role, item, recipe_name = (provenance.get('source_role'),
                               provenance.get('ingredient'), provenance.get('recipe'))
    if (not isinstance(role, str) or not role.startswith('recipe:')
            or not isinstance(item, str) or not item
            or not isinstance(recipe_name, str) or role != 'recipe:' + recipe_name):
        return None
    factory = snapshot.factory
    machine = factory.get('entities', {}).get(role, {})
    recipe = catalog.recipes.get(recipe_name, {})
    prototype = catalog.machines.get(machine.get('name'), {})
    ingredients = recipe.get('ingredients', [])
    matches = [row for row in ingredients if row.get('type') == 'item'
               and row.get('name') == item] if isinstance(ingredients, list) else []
    path = provenance.get('planner_item_path')
    batches = provenance.get('planned_batches')
    input_bag = machine.get('input')
    buffered = input_bag.get(item, 0) if isinstance(input_bag, dict) else None
    crafting = machine.get('crafting')
    fuel_bag = machine.get('fuel')
    burner_fuel = fuel_bag.get('coal', 0) if isinstance(fuel_bag, dict) else None
    carried = snapshot.inventory.get(item)
    if (len(matches) != 1 or type(batches) is not int or batches < 1
            or not _finite(matches[0].get('amount')) or matches[0]['amount'] <= 0
            or type(buffered) is not int or buffered < 0 or type(crafting) is not bool):
        return None
    required = max(0, math.ceil(matches[0]['amount'] * batches - buffered
                                 - (matches[0]['amount'] if crafting else 0)))
    expected_path_root = local.get('item') if path_root is None else path_root
    if (not isinstance(expected_path_root, str) or not expected_path_root
            or provenance.get('observed_tick') != snapshot.tick
            or type(machine.get('unit_number')) is not int or machine['unit_number'] <= 0
            or provenance.get('source_unit') != machine['unit_number']
            or not _recipe_source_matches(snapshot, catalog, role, machine)
            or recipe.get('name') != recipe_name or recipe.get('hidden')
            or not catalog.enabled(recipe, snapshot.researched or [])
            or not bool(prototype.get('categories', {}).get(recipe.get('category')))
            or (prototype.get('burner') is True
                and (not _finite(burner_fuel) or burner_fuel <= 0))
            or (prototype.get('burner') is not True
                and machine.get('recipe') != recipe_name)
            or provenance.get('observed_input') != buffered
            or provenance.get('observed_crafting') is not crafting
            or not isinstance(path, list) or not 2 <= len(path) <= 32
            or any(not isinstance(part, str) or not part for part in path)
            or path[0] != expected_path_root or path[-2:] != [recipe_name, item]
            or type(required) is not int or required < 1
            or parameters.get('role') != role or parameters.get('item') != item
            or parameters.get('quantity') != required
            or parameters.get('receipt') != f'{snapshot.tick}:factory_insert:{role}:{item}'
            or step.costs != {item: required}
            or type(carried) is not int or carried < required
            or factory.get('player_connected') is not True
            or factory.get('player_bound') is not True):
        return None
    evidence = {
        'observed_tick': snapshot.tick,
        'planner_item_path': list(path),
        'direct_native_recipe': recipe_name,
        'owned_source_role': role,
        'owned_source_unit': machine['unit_number'],
        'ingredient': item,
        'ingredient_in_machine_now': buffered,
        'ingredient_in_inventory_now': carried,
        'burner_fuel_coal_now': burner_fuel if prototype.get('burner') is True else None,
        'paid_quantity_to_transfer': required,
        'planned_native_receipt_id': parameters['receipt'],
        'basis': 'current_planner_recipe_input_and_owned_native_machine',
        'native_transfer_and_later_output_require_verification': True,
    }
    if include_dependency_chain and path_root is None:
        try:
            from .bootstrap_chain import dependency_chain
            chain = dependency_chain(snapshot, catalog, local, path)
            if chain['raw_input_inventory_target'] == required:
                evidence['recipe_dependency_chain'] = chain
                evidence['session_id'] = snapshot.session_id
                evidence['native_catalog_version'] = catalog.version
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            pass  # Existing start evidence retains its original scope.
    if require_receiver_capacity:
        receiver_capacity = _receiver_capacity_start_evidence(
            snapshot, catalog, role, item, required, machine['unit_number'])
        if receiver_capacity is None:
            return None
        evidence['receiver_capacity'] = receiver_capacity
    return evidence



def _paid_service_start_evidence(snapshot, catalog, plan, *, pickup=False):
    """Qualify only the first transfer of an exactly recompiled paid visit."""
    from types import SimpleNamespace
    from .factory import FactoryPlanner
    from .service_visits import service_visit
    from .demand import SupplyLedger

    try:
        factory, marker = snapshot.factory, (plan.materials or {})['service_visit']
        identity = (snapshot.session_id, snapshot.tick)
        if (snapshot.world_kind != 'fle' or len(plan.steps) != 2 or type(snapshot.tick) is not int
                or type(factory.get('tick')) is not int or factory['tick'] != snapshot.tick
                or catalog.version != snapshot.game_version
                or getattr(snapshot, '_coherent_observation_verified', None) != identity
                or getattr(snapshot, '_atomic_inventory_verified', None) != identity
                or type(factory.get('crafting_queue')) is not int or factory['crafting_queue'] != 0
                or not isinstance(factory.get('receipts'), dict)
                or factory.get('craft_job', {}).get('status') not in {None, 'completed'}
                or getattr(snapshot, '_paid_service_admissions', {}).get(plan.id) != {
                    'identity': identity, 'marker': marker}):
            return None
        first, second = plan.steps
        first_action = 'factory_extract' if pickup else 'factory_insert'
        role = first.parameters['role']
        if (first.action != first_action or second.action != 'factory_insert'
                or second.parameters.get('role') != role
                or first.parameters.get('item') == 'coal'
                or second.parameters.get('item') != 'coal'):
            return None
        stock = marker['paid_stock_now']
        current_supply = SupplyLedger.capture(snapshot, catalog).carried
        costs = {}
        for step in plan.steps:
            p = step.parameters
            item, quantity = p['item'], p['quantity']
            if (step.effect != 'transfer' or type(quantity) is not int or quantity < 1
                    or step.costs != ({} if step.action == 'factory_extract' else {item: quantity})
                    or p['receipt'] != f'{snapshot.tick}:{step.action}:{role}:{item}'
                    or p['receipt'] in factory['receipts']
                    or not step.allowed(snapshot) or step.satisfied(snapshot)):
                return None
            for name, count in step.costs.items():
                costs[name] = costs.get(name, 0) + count
        if (set(stock) != set(costs) or any(type(stock[item]) is not int
                or not costs[item] <= stock[item] <= current_supply.get(item, -1)
                for item in costs)):
            return None
        materials = {k: v for k, v in plan.materials.items() if k != 'service_visit'}
        atomic = replace(plan, id=f'factory:{first_action}:{role}',
                         steps=(first,), materials=materials)
        input_start = (_output_pickup_start_evidence(
            snapshot, catalog, atomic, include_dependency_chain=False) if pickup
                       else _recipe_input_transfer_start_evidence(snapshot, catalog, atomic))
        if input_start is None:
            return None
        planner = FactoryPlanner(catalog, snapshot, plan.goal)
        planner.ledger = SimpleNamespace(carried=stock)
        planner.targets = {}
        compiled = service_visit(planner, atomic)
        if (compiled.id != plan.id or compiled.steps != plan.steps
                or compiled.materials.get('service_visit') != marker):
            return None
        recipe = catalog.recipes[role.removeprefix('recipe:') if pickup
                                 else input_start['direct_native_recipe']]
        path = input_start['planner_item_path']
        native_path = {}
        for product, ingredient in zip(path, path[1:]):
            native = catalog.recipes.get(product)
            if (not isinstance(native, dict) or not catalog.enabled(native, snapshot.researched or [])
                    or not any(row.get('type') == 'item' and row.get('name') == product
                               for row in native.get('products', []))
                    or not any(row.get('type') == 'item' and row.get('name') == ingredient
                               for row in native.get('ingredients', []))):
                return None
            native_path[product] = deepcopy(native)
        return {
            'basis': ('current_recompiled_paid_service_first_output_pickup' if pickup
                      else 'current_recompiled_paid_service_first_recipe_input'),
            'observed_tick': snapshot.tick, 'session_id': snapshot.session_id,
            'native_catalog_version': catalog.version,
            'service_visit': deepcopy(marker), 'combined_paid_costs': costs,
            ('first_output_pickup' if pickup else 'first_recipe_input'): input_start,
            'native_recipe': deepcopy(recipe), 'native_parent_recipes': native_path,
            'native_receipt_queries': [{
                'schema': 1, 'session_id': snapshot.session_id, 'tick': snapshot.tick,
                'receipt_count': len(factory['receipts']), 'receipt': step.parameters['receipt'],
                'present': False, 'map_verified': True} for step in plan.steps],
            'native_receiver_capacity_and_each_step_require_rechecks': True,
            'later_fuel_output_and_target_completion_unverified': True,
        }
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None

def _paid_service_input_start_evidence(snapshot, catalog, plan):
    return _paid_service_start_evidence(snapshot, catalog, plan)


def _paid_service_output_start_evidence(snapshot, catalog, plan):
    return _paid_service_start_evidence(snapshot, catalog, plan, pickup=True)


def _output_pickup_start_evidence(snapshot, catalog, plan, *, path_root=None,
                                  include_dependency_chain=True):
    """Describe ready output at an owned native source, never a completed pickup."""
    if len(plan.steps) != 1:
        return None
    step = plan.steps[0]
    materials = plan.materials or {}
    provenance = materials.get('output_pickup')
    local = materials.get('local_objective')
    intent = materials.get('work_intent')
    p = step.parameters or {}
    if (step.action != 'factory_extract' or step.effect != 'transfer'
            or step.costs != {} or not isinstance(provenance, dict)
            or not isinstance(local, dict) or not isinstance(intent, dict)
            or intent.get('scope') != 'immediate'
            or intent.get('observed_tick') != snapshot.tick
            or provenance.get('observed_tick') != snapshot.tick):
        return None
    role, item, quantity = p.get('role'), p.get('item'), p.get('quantity')
    if (not isinstance(role, str) or not role.startswith(('recipe:', 'output-chest:'))
            or not isinstance(item, str) or not item
            or type(quantity) is not int or not 1 <= quantity <= 200):
        return None
    factory = snapshot.factory
    entities = factory.get('entities')
    if not isinstance(entities, dict):
        return None
    machine = entities.get(role)
    if not isinstance(machine, dict):
        return None
    unit = machine.get('unit_number')
    output = machine.get('output')
    available = output.get(item) if isinstance(output, dict) else None
    buffer_identity = None
    source_role = role
    if role.startswith('output-chest:'):
        from .buffer_pickup import identity
        buffer_identity = identity(snapshot, role, item)
        if buffer_identity is None:
            return None
        source_role = buffer_identity['source_role']
    recipe_name = source_role.removeprefix('recipe:')
    recipe = catalog.recipes.get(recipe_name)
    path = provenance.get('planner_item_path')
    expected_path_root = local.get('item') if path_root is None else path_root
    if (not isinstance(expected_path_root, str) or not expected_path_root
            or type(unit) is not int or unit <= 0
            or not _recipe_source_matches(snapshot, catalog, source_role, entities.get(source_role, {}))
            or not isinstance(recipe, dict) or recipe.get('name') != recipe_name
            or recipe.get('hidden') or not catalog.enabled(recipe, snapshot.researched or [])
            or not any(product.get('type') == 'item' and product.get('name') == item
                       for product in recipe.get('products', []))
            or not isinstance(path, list) or not 1 <= len(path) <= 32
            or any(not isinstance(part, str) or not part for part in path)
            or path[0] != expected_path_root or path[-1] != item
            or provenance.get('source_role') != role
            or provenance.get('source_unit') != unit
            or provenance.get('item') != item
            or type(available) is not int or available < quantity
            or provenance.get('observed_output') != available
            or p.get('receipt') != f'{snapshot.tick}:factory_extract:{role}:{item}'
            or factory.get('player_connected') is not True
            or factory.get('player_bound') is not True):
        return None
    evidence = {
        'observed_tick': snapshot.tick,
        'planner_item_path': list(path),
        'owned_source_role': role,
        'owned_source_unit': unit,
        'ready_output_item': item,
        'ready_output_quantity_now': available,
        'planned_pickup_quantity': quantity,
        'planned_native_receipt_id': p['receipt'],
        'player_connected_and_bound_now': True,
        'basis': 'current_planner_output_and_owned_native_machine',
        'native_pickup_and_inventory_delta_require_verification': True,
    }
    if buffer_identity is not None:
        evidence['basis'] = 'current_planner_output_and_paid_native_buffer'
        evidence['paid_buffer_identity'] = buffer_identity
    if include_dependency_chain and path_root is None and len(path) > 1:
        try:
            from .bootstrap_chain import dependency_chain
            chain = dependency_chain(snapshot, catalog, local, path)
            carried = snapshot.inventory.get(item, 0)
            required = chain['raw_input_inventory_target']
            if (type(carried) is int and 0 <= carried < required
                    and quantity <= required - carried):
                evidence['recipe_dependency_chain'] = chain
                evidence['current_input_demand'] = {
                    'required_carried_quantity': required,
                    'carried_quantity': carried,
                    'remaining_deficit': required - carried,
                }
                evidence['session_id'] = snapshot.session_id
                evidence['native_catalog_version'] = catalog.version
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            pass  # Preserve the original ready-stock witness without a demand claim.
    return evidence


def _bootstrap_output_ownership_digest(owned):
    """Keep immutable native ownership bound without duplicating its row."""
    keys = ('session_id', 'actor_unit', 'surface_index', 'force_index', 'drill_unit',
            'chest_unit', 'drill_position', 'drop_position', 'chest_position', 'origin',
            'binding_id', 'authorization_sha256', 'bound_at_tick',
            'historical_paid_placement_proven', 'paid_drill_unit', 'paid_chest_unit')
    try:
        identity = {key: owned[key] for key in keys}
        return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (KeyError, TypeError, ValueError):
        return None


def _bootstrap_output_pickup_start_evidence(snapshot, catalog, plan):
    """Recompile a current raw pickup from explicitly owned native stock.

    A legacy current-asset authorization is not historical paid placement proof.
    This witness forecasts only a bounded receipt-verified inventory transfer.
    """
    from ..bootstrap_output import ROLE, binding, allowed
    owned = binding(snapshot)
    if (owned is None or snapshot.game_version != catalog.version or len(plan.steps) != 1
            or not isinstance(snapshot.session_id, str) or not snapshot.session_id
            or type(snapshot.inventory.get('iron-ore', 0)) is not int
            or snapshot.inventory.get('iron-ore', 0) < 0):
        return None
    step = plan.steps[0]
    materials = plan.materials or {}
    provenance = materials.get('bootstrap_output_pickup')
    local = materials.get('local_objective')
    intent = materials.get('work_intent')
    p = step.parameters or {}
    if (step.action != 'factory_extract' or step.effect != 'transfer' or step.costs != {}
            or not isinstance(provenance, dict) or not isinstance(local, dict)
            or not isinstance(local.get('item'), str) or not local['item']
            or not isinstance(intent, dict) or intent.get('scope') != 'immediate'
            or type(intent.get('observed_tick')) is not int or intent['observed_tick'] != snapshot.tick
            or local.get('ultimate_goal') != plan.goal
            or type(local.get('inventory_target')) is not int or local['inventory_target'] <= 0
            or type(snapshot.inventory.get(local.get('item'), 0)) is not int
            or snapshot.inventory.get(local.get('item'), 0) >= local['inventory_target']
            or set(p) != {'role', 'item', 'quantity', 'receipt'}
            or not allowed(p, snapshot)
            or p['receipt'] != f'{snapshot.tick}:factory_extract:{ROLE}:iron-ore'
            or provenance.get('observed_tick') != snapshot.tick
            or provenance.get('source_role') != ROLE
            or type(provenance.get('source_unit')) is not int
            or provenance['source_unit'] != owned['chest_unit']
            or provenance.get('item') != 'iron-ore'
            or not isinstance(provenance.get('current_raw_demand'), dict)
            or type(provenance.get('observed_output')) is not int
            or provenance['observed_output'] != owned['output'].get('iron-ore', 0)
            or not _current_item_dependency_path(snapshot, catalog,
                provenance.get('planner_item_path'), local.get('item'), 'iron-ore')):
        return None
    try:
        from .input_routes import InputRoutePlanner
        matches = [candidate for candidate in InputRoutePlanner(
            catalog, snapshot, plan.goal).candidates()
            if candidate.id == plan.id and candidate.steps == plan.steps
            and all((candidate.materials or {}).get(key) == materials.get(key)
                for key in ('bootstrap_output_pickup', 'local_objective', 'work_intent',
                            'shortages', 'batches', 'input_route_kit_prerequisite'))]
        if len(matches) != 1:
            return None
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None
    try:
        from .bootstrap_chain import dependency_chain
        chain = dependency_chain(snapshot, catalog, local, provenance['planner_item_path'])
        if chain['raw_input_inventory_target'] != provenance['current_raw_demand']['required_carried_quantity']:
            return None
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None
    return {
        'schema': 1, 'observed_tick': snapshot.tick, 'session_id': snapshot.session_id,
        'catalog_version': catalog.version, 'source_role': ROLE,
        'recipe_dependency_chain': chain,
        'source_unit': owned['chest_unit'], 'binding_id': owned['binding_id'],
        'ownership_sha256': _bootstrap_output_ownership_digest(owned),
        'planner_item_path': list(provenance['planner_item_path']),
        'inventory_now': snapshot.inventory.get('iron-ore', 0),
        'current_raw_demand': deepcopy(provenance['current_raw_demand']),
        'planned_pickup_quantity': p['quantity'], 'planned_native_receipt_id': p['receipt'],
        'basis': 'recompiled_current_local_demand_and_owned_bootstrap_output',
        'native_pickup_and_inventory_delta_require_verification': True,
        'later_recipe_output_and_target_completion_unverified': True,
    }


def _production_machine_edges(snapshot, catalog, path, goal, target, *, carried_machine=False):
    from .factory import FactoryPlanner

    edges, machine_edge = [], None
    requested = target
    try:
        for product, dependency in zip(path, path[1:]):
            recipe = catalog.recipe_for(product)
            if (recipe.get('hidden') or not catalog.enabled(recipe, snapshot.researched or [])
                    or not any(p.get('type') == 'item' and p.get('name') == product
                               and type(p.get('amount')) in (int, float) and p['amount'] > 0
                               and p.get('probability', 1) == 1
                               for p in recipe.get('products', []))):
                return None
            carried = snapshot.inventory.get(product, 0)
            output = next(p['amount'] for p in recipe['products']
                          if p.get('type') == 'item' and p.get('name') == product)
            if (type(carried) is not int or carried < 0 or not _finite(output) or output <= 0
                    or not _finite(requested) or requested <= carried):
                return None
            demand = {'required_product_units': requested, 'carried_product_units': carried,
                      'missing_product_units': requested - carried,
                      'scope': 'selected_dependency_branch_not_full_material_bill'}
            ingredient = next((r for r in recipe.get('ingredients', [])
                               if r.get('type') == 'item' and r.get('name') == dependency
                               and _finite(r.get('amount')) and r['amount'] > 0), None)
            if ingredient is not None:
                requested = math.ceil((requested - carried) / output) * ingredient['amount']
                edges.append({'kind': 'recipe_input', 'product': product,
                              'input': dependency, 'recipe': deepcopy(recipe),
                              'demand': demand, 'required_input_units': requested})
                continue
            role = 'recipe:' + recipe['name']
            # The base planner selects this machine for a new producer. Existing
            # machines, carried machines and handcraftable recipes contradict
            # this particular missing-machine acquisition explanation.
            if (machine_edge is not None or role in snapshot.factory.get('entities', {})
                    or type(snapshot.inventory.get(dependency, 0)) is not int
                    or (snapshot.inventory.get(dependency, 0) < 1 if carried_machine
                        else snapshot.inventory.get(dependency, 0) != 0)
                    or catalog.hand_categories.get(recipe['category'])
                    or FactoryPlanner(catalog, snapshot, goal)._machine_type(recipe) != dependency):
                return None
            machine_edge = {'kind': 'missing_production_machine', 'product': product,
                            'machine': dependency, 'role': role,
                            'demand': demand,
                            'recipe': deepcopy(recipe),
                            'prototype': deepcopy(catalog.machines[dependency])}
            edges.append(machine_edge)
            requested = 1
        if machine_edge is None:
            return None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return edges


def raw_machine_prerequisite(snapshot, catalog, plan):
    """Qualify one missing-machine edge in a current bounded raw-input path.

    A furnace is not an ingredient of iron plate. Validate both kinds of edge
    explicitly; an arbitrary planner path alone is not a dependency proof.
    """
    materials = plan.materials or {}
    raw = materials.get('raw_prerequisite')
    local = materials.get('local_objective')
    intent = materials.get('work_intent')
    if (len(plan.steps) != 1 or not isinstance(raw, dict)
            or not isinstance(local, dict) or not isinstance(intent, dict)
            or intent != {'scope': 'immediate', 'observed_tick': snapshot.tick}
            or raw.get('observed_tick') != snapshot.tick):
        return None
    step = plan.steps[0]
    params = step.parameters or {}
    path = raw.get('planner_item_path')
    target = local.get('inventory_target')
    if (step.action != 'factory_gather' or step.effect != 'inventory'
            or step.costs not in (None, {})
            or params.get('resource') != step.item
            or type(params.get('quantity')) is not int or not 1 <= params['quantity'] <= 50
            or type(snapshot.inventory.get(step.item, 0)) is not int
            or type(step.threshold) is not int
            or step.threshold != snapshot.inventory.get(step.item, 0) + params['quantity']
            or not isinstance(path, list) or not 3 <= len(path) <= 32
            or any(not isinstance(item, str) or not item for item in path)
            or len(set(path)) != len(path)
            or path[0] != local.get('item') or path[-1] != step.item
            or path[-2] != raw.get('direct_product')
            or raw.get('ingredient') != step.item
            or type(target) is not int or target <= 0
            or type(snapshot.inventory.get(path[0], 0)) is not int
            or snapshot.inventory.get(path[0], 0) >= target):
        return None
    edges = _production_machine_edges(snapshot, catalog, path, plan.goal, target)
    if (edges is None or edges[-1]['kind'] != 'recipe_input'
            or edges[-1]['recipe']['name'] != raw.get('recipe')):
        return None
    return {'schema': 1, 'session_id': snapshot.session_id, 'observed_tick': snapshot.tick,
            'planner_item_path': list(path), 'local_target': deepcopy(local),
            'gather_resource': step.item, 'gather_quantity': params['quantity'],
            'gather_inventory_now': snapshot.inventory.get(step.item, 0),
            'gather_inventory_target': step.threshold, 'edges': edges,
            'basis': 'current_catalog_input_edges_and_observed_missing_machine',
            'gather_craft_placement_and_production_require_native_verification': True}


def machine_construction_prerequisite(snapshot, catalog, plan, craft_start, placement_start):
    """Carry the missing-machine purpose through paid crafting and placement."""
    materials = plan.materials or {}
    local, intent = materials.get('local_objective'), materials.get('work_intent')
    if (len(plan.steps) != 1 or not isinstance(local, dict)
            or intent != {'scope': 'immediate', 'observed_tick': snapshot.tick}
            or local.get('ultimate_goal') != plan.goal
            or type(local.get('inventory_target')) is not int
            or type(snapshot.inventory.get(local.get('item'), 0)) is not int
            or not 0 <= snapshot.inventory.get(local.get('item'), 0) < local['inventory_target']):
        return None
    step = plan.steps[0]
    params = step.parameters or {}
    placing = step.action == 'factory_place'
    start = placement_start if placing else craft_start
    annotation = materials.get('placement_dependency' if placing else 'craft_dependency')
    if (not isinstance(annotation, dict) or annotation.get('observed_tick') != snapshot.tick
            or not isinstance(start, dict) or start.get('observed_tick') != snapshot.tick):
        return None
    path = annotation.get('planner_item_path')
    if (not isinstance(path, list) or not 1 <= len(path) <= 31
            or any(not isinstance(item, str) or not item for item in path)
            or path[0] != local.get('item')):
        return None
    if placing:
        machine = params.get('name')
        if (start != _placement_start_evidence(snapshot, plan)
                or any(start.get(k) is not True for k in (
                    'paid_furnace_in_inventory_now', 'no_source_owned_at_role_now',
                    'player_connected_and_bound_now', 'crafting_queue_empty_now'))
                or annotation.get('machine') != machine
                or annotation.get('source_role') != params.get('role')
                or annotation.get('site_anchor') != params.get('anchor')
                or params.get('role') != 'recipe:' + path[-1]):
            return None
        path = [*path, machine]
    else:
        if (step.action != 'factory_craft_job' or step.effect != 'craft_job_complete'
                or not isinstance(params.get('receipt'), str) or not params['receipt']
                or start != _craft_start_evidence(snapshot, catalog, step)
                or any(start.get(k) is not True for k in (
                    'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                    'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                    'crafting_queue_empty', 'craft_job_protocol_ready',
                    'native_receipt_required_for_completion'))
                or annotation.get('recipe') != params.get('recipe')
                or annotation.get('product') != step.item or path[-1] != step.item):
            return None
    if len(set(path)) != len(path):
        return None
    edges = _production_machine_edges(snapshot, catalog, path, plan.goal,
                                      local['inventory_target'], carried_machine=placing)
    if edges is None:
        return None
    missing = next(edge for edge in edges if edge['kind'] == 'missing_production_machine')
    if placing and (edges[-1] != missing or missing['role'] != params['role']):
        return None
    return {'schema': 1, 'session_id': snapshot.session_id, 'observed_tick': snapshot.tick,
            'local_target': deepcopy(local), 'planner_item_path': path, 'edges': edges,
            'step': plan.to_dict()['steps'][0], 'start_evidence': deepcopy(start),
            'current_native_craft_recipe': (None if placing else deepcopy(
                catalog.recipes[params['recipe']])),
            'carried_costs': {item: snapshot.inventory.get(item, 0) for item in step.costs or {}},
            'machine_role': missing['role'], 'machine_item': missing['machine'],
            'machine_inventory_now': snapshot.inventory.get(missing['machine'], 0),
            'basis': 'current_catalog_input_edges_and_observed_missing_machine',
            'craft_placement_fuel_and_production_require_native_verification': True}


def _current_item_dependency_path(snapshot, catalog, path, root, tail):
    """Validate a same-tick catalog path without conflating parent and child roots."""
    if (not isinstance(path, list) or not 1 <= len(path) <= 32
            or any(not isinstance(item, str) or not item for item in path)
            or path[0] != root or path[-1] != tail or len(set(path)) != len(path)):
        return False
    for product, ingredient in zip(path, path[1:]):
        try:
            recipe = catalog.recipe_for(product)
        except (KeyError, ValueError, TypeError):
            return False
        if (not isinstance(recipe, dict) or recipe.get('hidden')
                or not catalog.enabled(recipe, snapshot.researched or [])
                or not any(isinstance(row, dict) and row.get('type') == 'item'
                           and row.get('name') == product
                           and _finite(row.get('amount')) and row['amount'] > 0
                           and row.get('probability', 1) == 1
                           for row in recipe.get('products', []))
                or not any(isinstance(row, dict) and row.get('type') == 'item'
                           and row.get('name') == ingredient
                           and _finite(row.get('amount')) and row['amount'] > 0
                           for row in recipe.get('ingredients', []))):
            return False
    return True


def _buffer_component_start_evidence(snapshot, catalog, plan, craft_start, pickup_start):
    """Verify the paid construction bridge separately from recipe ancestry."""
    annotation = (plan.materials or {}).get('buffer_component_prerequisite')
    if not isinstance(annotation, dict) or len(plan.steps) != 1:
        return None
    from .output_buffers import construction_pickup_bill
    from ..output_buffers import sources as buffer_sources
    try:
        step = plan.steps[0]
        item = (step.parameters or {}).get('item') if step.action == 'factory_extract' else step.item
        expected = construction_pickup_bill(snapshot, catalog,
            buffer_sources(snapshot).get(annotation.get('source_role')),
            annotation.get('next_part'), item)
        if annotation != expected or expected is None:
            return None
        if step.action == 'factory_extract':
            if not isinstance(pickup_start, dict):
                return None
            path = pickup_start['planner_item_path']
            component = expected['component_item']
            index = path.index(component)
            if (not 0 < pickup_start['planned_pickup_quantity'] <= min(50, expected['component_input_deficit'])
                    or not _current_item_dependency_path(snapshot, catalog,
                        path[index:], component, item)):
                return None
        elif step.action == 'factory_craft':
            if (not isinstance(craft_start, dict)
                    or craft_start != _craft_start_evidence(snapshot, catalog, step)
                    or any(craft_start.get(key) is not True for key in (
                        'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                        'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                        'crafting_queue_empty'))
                    or not 0 < craft_start['expected_products_after_native_verification'].get(item, 0)
                        <= expected['component_input_deficit']
                    or not step.allowed(snapshot) or step.satisfied(snapshot)
                    or getattr(snapshot, '_atomic_inventory_verified', None) !=
                        (snapshot.session_id, snapshot.tick)):
                return None
        else:
            return None
        return expected
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _ready_work_raw_target(snapshot, catalog, goal, local_item, local_target, resource):
    """Recompute the current raw-material horizon for a local ready-work target."""
    from .demand import SupplyLedger, horizon_demands
    from .factory import RAW_ITEMS

    if (resource not in RAW_ITEMS - {'wood'} or not isinstance(local_item, str)
            or not local_item or type(local_target) is not int or local_target < 1):
        return False, None
    try:
        demands = horizon_demands(snapshot, catalog, goal, local_item, local_target)
        ledger = SupplyLedger.capture(snapshot, catalog)
        bill = catalog.material_demands(
            demands, ledger.forecast_stock(), snapshot.researched or [])
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False, None
    shortage = bill.shortages.get(resource, 0)
    if not _finite(shortage) or shortage < 0:
        return False, None
    if shortage <= 0:
        return True, None
    current = snapshot.inventory.get(resource, 0)
    if type(current) is not int or current < 0:
        return False, None
    return True, current + math.ceil(shortage)


def _current_parent_utility_demand(snapshot, catalog, annotation, goal):
    """Revalidate inherited utility-demand context without claiming a power step."""
    if not isinstance(annotation, dict) or set(annotation) != {
            'observed_tick', 'consumer_role', 'consumer_unit', 'planner_path', 'research'}:
        return None
    tick, session = snapshot.tick, snapshot.session_id
    factory = snapshot.factory
    identity = (session, tick)
    runtime = factory.get('acceptance_runtime')
    if (snapshot.world_kind != 'fle' or type(tick) is not int or tick < 0
            or not isinstance(session, str) or not session
            or annotation.get('observed_tick') != tick
            or type(annotation.get('observed_tick')) is not int
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or factory.get('observation_snapshot_schema') != 2
            or factory.get('tick') != tick
            or not isinstance(runtime, dict) or runtime.get('schema') != 1
            or runtime.get('session_id') != session
            or runtime.get('speed') != 1 or runtime.get('tick_paused') is not False
            or not isinstance(runtime.get('mods'), dict)
            or runtime['mods'].get('base') != catalog.version
            or snapshot.game_version != catalog.version
            or factory.get('player_connected') is not True
            or factory.get('player_bound') is not True
            or factory.get('crafting_queue') != 0):
        return None

    role = annotation.get('consumer_role')
    unit = annotation.get('consumer_unit')
    path = annotation.get('planner_path')
    research = annotation.get('research')
    if (not isinstance(role, str) or not role or len(role) > 128
            or type(unit) is not int or unit < 1
            or not isinstance(path, list) or not 1 <= len(path) <= 32
            or any(not isinstance(entry, str) or not entry or len(entry) > 128
                   for entry in path)
            or len(path) != len(set(path))):
        return None
    path_research = next((entry.removeprefix('technology:') for entry in reversed(path)
                          if entry.startswith('technology:')), None)
    if research != path_research:
        return None

    entities = factory.get('entities')
    consumer = entities.get(role) if isinstance(entities, dict) else None
    if (not isinstance(consumer, dict) or consumer.get('unit_number') != unit
            or type(consumer.get('unit_number')) is not int):
        return None
    researched = snapshot.researched
    if not isinstance(researched, list):
        return None
    if role == 'utility:lab':
        if consumer.get('name') != 'lab' or not isinstance(research, str) or not research:
            return None
        tech = catalog.technologies.get(research)
        if (not isinstance(tech, dict) or type(tech.get('enabled')) is not bool
                or not tech['enabled'] or research in researched or tech.get('trigger')
                or not isinstance(tech.get('prerequisites'), list)
                or any(parent not in researched for parent in tech['prerequisites'])
                or factory.get('research') not in ('', None, research)):
            return None
        current_demand = {
            'kind': 'current_technology_lab_demand',
            'technology': research,
            'technology_not_researched_now': True,
            'technology_prerequisites_satisfied_now': True,
        }
    elif role.startswith('recipe:') and goal == 'rocket_launch':
        recipe_name = role.removeprefix('recipe:')
        recipe = catalog.recipes.get(recipe_name)
        prototype = catalog.machines.get(consumer.get('name'))
        try:
            recipe_output_in_path = any(
                entry.startswith('item:') and any(
                    isinstance(product, dict) and product.get('type') == 'item'
                    and product.get('name') == entry.removeprefix('item:')
                    and _finite(product.get('amount')) and product['amount'] > 0
                    for product in recipe.get('products', []))
                for entry in path)
            recipe_ready = (
                isinstance(recipe, dict) and recipe.get('name') == recipe_name
                and recipe.get('hidden') is False
                and type(recipe.get('enabled')) is bool
                and catalog.enabled(recipe, researched)
                and isinstance(prototype, dict) and prototype.get('electric') is True
                and isinstance(prototype.get('categories'), dict)
                and prototype['categories'].get(recipe.get('category')) is True
                and consumer.get('recipe') == recipe_name
                and recipe_output_in_path)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        if not recipe_ready:
            return None
        if research is not None:
            tech = catalog.technologies.get(research)
            if (not isinstance(research, str) or not research
                    or not isinstance(tech, dict) or research in researched
                    or type(tech.get('enabled')) is not bool or not tech['enabled']
                    or tech.get('trigger') or not isinstance(tech.get('prerequisites'), list)
                    or any(parent not in researched for parent in tech['prerequisites'])):
                return None
        current_demand = {
            'kind': 'current_native_recipe_demand',
            'recipe': recipe_name,
            'recipe_enabled_now': True,
            'machine_recipe_matches_now': True,
        }
    else:
        return None

    # The annotation alone is planner metadata. Re-derive it from this same
    # coherent snapshot and current consumer path before carrying its purpose.
    try:
        from .factory import FactoryPlanner
        from .ready_work import ReadyWorkPlanner

        rederived = None
        for planner_type in (FactoryPlanner, ReadyWorkPlanner):
            current = planner_type(catalog, snapshot, goal)._powered(role, tuple(path))
            if (current is not None
                    and (current.materials or {}).get('utility_power_prerequisite') == annotation):
                rederived = current
                break
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if rederived is None:
        return None
    return {
        'observed_tick': tick,
        'session_id': session,
        'consumer_role': role,
        'consumer_unit': unit,
        'planner_path': list(path),
        'current_demand': current_demand,
        'annotation_matches_current_planner_demand': True,
        'does_not_establish_power_start_or_connection': True,
    }


def _direct_alternative_parent_demand_start_evidence(
        snapshot, catalog, plans, plan, raw_prerequisite, gather_start):
    """Bind a direct gather alternative to a current immediate parent target.

    This is a conditional level-1 input forecast. It neither promotes a
    speculative policy investment into a native payoff claim nor predicts the
    gather, downstream recipe output, power, or local-target completion.
    """
    if (snapshot.world_kind != 'fle' or plan.goal != 'rocket_launch'
            or len(plan.steps) != 1 or plan.steps[0].action != 'factory_gather'):
        return None
    materials = plan.materials or {}
    marker = materials.get('direct_alternative_to_proposed_outpost')
    if (not isinstance(marker, dict) or marker.get('schema') != 2
            or type(marker.get('schema')) is not int
            or marker.get('observed_tick') != snapshot.tick
            or type(marker.get('observed_tick')) is not int
            or marker.get('basis') != 'planner_policy_investment_has_no_native_payback_evidence'):
        return None

    parent_id = marker.get('investment_plan_id')
    if not isinstance(parent_id, str) or not parent_id or parent_id == plan.id:
        return None
    parents = [candidate for candidate in plans if candidate.id == parent_id]
    if len(parents) != 1:
        return None
    parent = parents[0]
    if (parent.goal != plan.goal or len(parent.steps) != 1
            or parent.steps[0].action != 'factory_outpost_build'):
        return None

    parent_materials = parent.materials or {}
    parent_request = parent_materials.get('proposed_outpost_request')
    marked_request = marker.get('proposed_outpost_request')
    purpose = marker.get('parent_purpose')
    local = parent_materials.get('local_objective')
    intent = parent_materials.get('work_intent')
    direct_local = materials.get('local_objective')
    if (not isinstance(parent_request, dict) or parent_request != marked_request
            or not isinstance(purpose, dict) or not isinstance(local, dict)
            or direct_local != local
            or not isinstance(intent, dict)
            or purpose.get('schema') != 1 or type(purpose.get('schema')) is not int
            or purpose.get('observed_tick') != snapshot.tick
            or purpose.get('local_objective') != local
            or purpose.get('work_intent') != intent
            or purpose.get('proposed_outpost_request') != parent_request
            or intent.get('scope') != 'immediate'
            or intent.get('observed_tick') != snapshot.tick
            or local.get('ultimate_goal') != parent.goal):
        return None

    for key in ('utility_power_prerequisite', 'economics'):
        parent_value = parent_materials.get(key)
        if isinstance(parent_value, dict):
            if purpose.get(key) != parent_value:
                return None
        elif key in purpose:
            return None

    local_item, local_target = local.get('item'), local.get('inventory_target')
    request_resource = marker.get('resource')
    requested_amount = marker.get('requested_amount')
    step = plan.steps[0]
    parameters = step.parameters or {}
    resource = parameters.get('resource')
    request_path = parent_request.get('planner_item_path')
    raw_path = raw_prerequisite.get('planner_item_path') if isinstance(
        raw_prerequisite, dict) else None
    current_inventory = snapshot.inventory.get(resource, 0) if isinstance(resource, str) else None
    quantity = parameters.get('quantity')
    compiled = marker.get('compiled_gather_target')
    parent_parameters = parent.steps[0].parameters or {}
    if (not isinstance(local_item, str) or not local_item
            or type(local_target) is not int or local_target < 1
            or type(snapshot.inventory.get(local_item, 0)) is not int
            or snapshot.inventory.get(local_item, 0) >= local_target
            or parent_request.get('schema') != 1
            or type(parent_request.get('schema')) is not int
            or parent_request.get('observed_tick') != snapshot.tick
            or parent_request.get('resource') != request_resource
            or type(requested_amount) is not int or requested_amount < 1
            or parent_request.get('requested_amount') != requested_amount
            or not isinstance(request_resource, str)
            or request_resource not in OUTPOST_RESOURCES
            or parent_parameters.get('resource') != request_resource
            or parent_parameters.get('layout') != parent_request.get('layout')
            or not isinstance(parent_request.get('layout'), str)
            or not isinstance(request_path, list) or not 2 <= len(request_path) <= 32
            or any(not isinstance(item, str) or not item for item in request_path)
            or request_path[0] != local_item or request_path[-1] != request_resource
            or resource != request_resource or step.item != resource
            or step.effect != 'inventory' or set(parameters) != {'resource', 'quantity'}
            or type(current_inventory) is not int or current_inventory < 0
            or type(quantity) is not int or not 1 <= quantity <= 50
            or type(step.threshold) is not int
            or step.threshold != current_inventory + quantity
            or not isinstance(compiled, dict)
            or compiled.get('resource') != resource
            or compiled.get('quantity') != quantity
            or compiled.get('inventory_target') != step.threshold
            or compiled.get('basis') != 'speculative_worker_current_raw_target'
            or not isinstance(raw_prerequisite, dict)
            or raw_prerequisite.get('observed_tick') != snapshot.tick
            or raw_prerequisite.get('direct_product') != (raw_path[-2] if isinstance(raw_path, list) and len(raw_path) >= 2 else None)
            or raw_path != request_path
            or not isinstance(gather_start, dict)
            or gather_start.get('observed_tick') != snapshot.tick
            or gather_start.get('session_id') != snapshot.session_id
            or gather_start.get('resource_in_current_observation') is not True
            or gather_start.get('fair_target_identity_observed') is not True
            or gather_start.get('resource_inventory_now') != current_inventory
            or gather_start.get('target_inventory_after_this_step') != step.threshold
            or gather_start.get('travel_is_lower_bound_not_arrival_proof') is not True):
        return None

    request_recipe = raw_prerequisite.get('direct_recipe')
    direct_product = raw_prerequisite.get('direct_product')
    try:
        direct_recipe = catalog.recipe_for(direct_product)
    except (KeyError, TypeError, ValueError):
        return None
    if (direct_recipe.get('name') != request_recipe
            or not _current_item_dependency_path(
                snapshot, catalog, raw_path, local_item, resource)):
        return None

    utility_annotation = parent_materials.get('utility_power_prerequisite')
    current_utility_demand = None
    if utility_annotation is not None:
        current_utility_demand = _current_parent_utility_demand(
            snapshot, catalog, utility_annotation, parent.goal)
        if current_utility_demand is None:
            return None

    # The parent must still be the exact current proposed outpost command. Its
    # existing native guard remains authoritative for selection and dispatch.
    try:
        source = outpost_sources(snapshot).get(request_resource)
        if (not isinstance(source, dict) or source.get('state') != 'proposed'
                or source.get('layout') != parent_parameters.get('layout')
                or not outpost_current(source, snapshot)
                or not parent.steps[0].allowed(snapshot)
                or parent.steps[0].satisfied(snapshot)
                or not step.allowed(snapshot) or step.satisfied(snapshot)):
            return None
    except (KeyError, TypeError, ValueError):
        return None

    # Recompute the optional ReadyWork raw horizon from the same current
    # snapshot. This preserves the original outpost request (for example 20)
    # separately from a current compiled raw target (for example 41).
    valid_horizon, raw_target = _ready_work_raw_target(
        snapshot, catalog, plan.goal, local_item, local_target, resource)
    if (not valid_horizon or compiled.get('ready_work_raw_target') != raw_target):
        return None
    compiled_target = max(requested_amount, raw_target or 0)
    expected_threshold = min(compiled_target, current_inventory + 50)
    if step.threshold != expected_threshold:
        return None

    identity = (snapshot.session_id, snapshot.tick)
    runtime = snapshot.factory.get('acceptance_runtime')
    if (not isinstance(snapshot.session_id, str) or not snapshot.session_id
            or type(snapshot.tick) is not int or snapshot.tick < 0
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or getattr(snapshot, '_atomic_inventory_verified', None) != identity
            or snapshot.factory.get('observation_snapshot_schema') != 2
            or snapshot.factory.get('tick') != snapshot.tick
            or snapshot.game_version != catalog.version
            or not isinstance(runtime, dict) or runtime.get('schema') != 1
            or runtime.get('session_id') != snapshot.session_id
            or runtime.get('speed') != 1 or runtime.get('tick_paused') is not False
            or not isinstance(runtime.get('mods'), dict)
            or runtime['mods'].get('base') != catalog.version
            or snapshot.factory.get('player_connected') is not True
            or snapshot.factory.get('player_bound') is not True
            or snapshot.factory.get('crafting_queue') != 0):
        return None
    fair_target = _current_native_fair_resource_target(snapshot, resource)
    if fair_target is None:
        return None

    return {
        'schema': 1,
        'observed_tick': snapshot.tick,
        'session_id': snapshot.session_id,
        'parent_plan_id': parent.id,
        'parent_action': parent.steps[0].action,
        'parent_outpost_resource': request_resource,
        'parent_outpost_layout': source['layout'],
        'parent_outpost_still_proposed_and_allowed': True,
        'parent_local_target_item': local_item,
        'parent_local_target_inventory': local_target,
        'parent_work_intent_scope': 'immediate',
        'parent_utility_power_annotation_present': utility_annotation is not None,
        'parent_current_utility_demand': current_utility_demand,
        'parent_utility_annotation_is_not_power_start_evidence': True,
        'parent_proposed_request_amount': requested_amount,
        'current_direct_recipe_path': list(raw_path),
        'gather_resource': resource,
        'gather_inventory_now': current_inventory,
        'gather_quantity': quantity,
        'gather_inventory_target': step.threshold,
        'ready_work_raw_target': raw_target,
        'compiled_target_basis': ('current_ready_work_raw_target'
                                  if raw_target is not None
                                  and raw_target > requested_amount
                                  else 'immediate_planner_request'),
        'fair_target_name': fair_target['name'],
        'fair_target_surface_index': fair_target['surface_index'],
        'native_actor_bound_and_inventory_fresh': True,
        'useful_partial_benefit_level': 1,
        'gather_and_later_recipe_output_require_fresh_native_verification': True,
        'does_not_establish_gathered_output_or_local_target_completion': True,
        'does_not_establish_outpost_payback_or_completion': True,
        'basis': 'same_tick_current_parent_demand_and_catalog_recipe_input_path',
    }


def _current_native_fair_resource_target(snapshot, item):
    """Reuse the atomic observer's decoded, same-session resource identity."""
    if (snapshot.world_kind != 'fle' or not isinstance(item, str)
            or item not in {'wood', 'coal', 'iron-ore', 'copper-ore', 'stone'}):
        return None
    session, tick = snapshot.session_id, snapshot.tick
    identity = (session, tick)
    factory = snapshot.factory
    runtime = factory.get('acceptance_runtime')
    if (not isinstance(session, str) or not session or type(tick) is not int
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or factory.get('observation_snapshot_schema') != 2
            or factory.get('tick') != tick
            or factory.get('player_connected') is not True
            or factory.get('player_bound') is not True
            or not isinstance(runtime, dict) or runtime.get('schema') != 1
            or runtime.get('session_id') != session
            or runtime.get('speed') != 1 or runtime.get('tick_paused') is not False
            or any(type(runtime.get(key)) is not int or runtime[key] <= 0
                   for key in ('actor_unit', 'player_index', 'surface_index', 'force_index'))):
        return None
    targets = factory.get('fair_resource_targets')
    target = targets.get(item) if isinstance(targets, dict) else None
    if (not isinstance(target, dict) or not isinstance(target.get('name'), str)
            or not target['name'].strip()
            or (item != 'wood' and target['name'] != item)
            or type(target.get('surface_index')) is not int
            or target['surface_index'] != runtime['surface_index']
            or _position(target.get('position')) is None
            or item not in snapshot.nearby_resources):
        return None
    return target


def _outpost_kit_prerequisite_start_evidence(snapshot, catalog, plan):
    """Qualify one current child-kit step while keeping its outer demand distinct.

    The current outpost admission rule is represented as a planner policy
    heuristic. This evidence does not forecast native payback or claim that the
    outpost arrived, flowed, produced output, or completed the outer target.
    """
    materials = plan.materials or {}
    provenance = materials.get('outpost_kit_prerequisite')
    local = materials.get('local_objective')
    intent = materials.get('work_intent')
    if (plan.goal != 'rocket_launch' or len(plan.steps) != 1
            or not isinstance(provenance, dict) or not isinstance(local, dict)
            or not isinstance(intent, dict) or provenance.get('schema') != 1
            or provenance.get('observed_tick') != snapshot.tick
            or intent.get('observed_tick') != snapshot.tick
            or intent.get('scope') != 'immediate'):
        return None

    resource, layout = provenance.get('outpost_resource'), provenance.get('outpost_layout')
    parent, child, admission = (provenance.get('parent_request'),
                                provenance.get('child_request'),
                                provenance.get('admission'))
    local_item = local.get('item')
    if (resource not in OUTPOST_RESOURCES or not isinstance(layout, str) or not layout
            or not isinstance(parent, dict) or not isinstance(child, dict)
            or not isinstance(admission, dict)
            or parent.get('item') != resource
            or parent.get('local_target_item') != local_item
            or not isinstance(local_item, str) or not local_item
            or type(parent.get('amount')) is not int or parent['amount'] <= 0
            or type(parent.get('inventory_now')) is not int
            or parent['inventory_now'] < 0
            or parent['inventory_now'] != snapshot.inventory.get(resource, 0)
            or type(child.get('quantity')) is not int or child['quantity'] < 1
            or child.get('kind') not in {'outpost_component', 'outpost_construction_fuel'}):
        return None
    kit_item = child.get('item')
    if (not isinstance(kit_item, str) or not kit_item
            or (child['kind'] == 'outpost_component' and kit_item not in OUTPOST_PARTS.values())
            or (child['kind'] == 'outpost_construction_fuel'
                and (kit_item != 'coal' or child['quantity'] != 5
                     or snapshot.inventory.get('coal', 0) >= 5))):
        return None

    try:
        row = outpost_sources(snapshot).get(resource)
        paid_sites = production_site_sources(snapshot)
        routes = input_route_sources(snapshot)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if (not isinstance(row, dict) or not outpost_current(row, snapshot)
            or row.get('layout') != layout or row.get('state') not in {'proposed', 'building'}
            or type(row.get('remaining')) is not int or row['remaining'] < 100
            or provenance.get('outpost_remaining') != row['remaining']
            or (child['kind'] == 'outpost_component'
                and outpost_remaining_kit(row).get(kit_item) != child['quantity'])):
        return None
    if (snapshot.factory.get('player_connected') is not True
            or snapshot.factory.get('player_bound') is not True):
        return None

    parent_path = parent.get('planner_item_path')
    if not _current_item_dependency_path(snapshot, catalog, parent_path,
                                         local_item, resource):
        return None

    # Recompute the source identity and direct-route condition from current
    # protocol/session/tick-bound witnesses; planner annotations alone do not
    # establish that the kit is attached to this producer.
    source_role = OUTPOST_RESOURCES[resource]
    producer = snapshot.factory.get('entities', {}).get(source_role)
    producer_site = paid_sites.get(source_role)
    if (not isinstance(producer, dict) or not isinstance(producer_site, dict)
            or producer_site.get('state') != 'owned'
            or type(producer.get('unit_number')) is not int or producer['unit_number'] <= 0
            or producer_site.get('source_unit') != producer['unit_number']
            or source_role in routes):
        return None

    outputs = []
    for entity in snapshot.factory.get('entities', {}).values():
        if not isinstance(entity, dict):
            return None
        output = entity.get('output', {})
        if not isinstance(output, dict):
            return None
        count = output.get(resource, 0)
        if type(count) is not int or count < 0:
            return None
        outputs.append(count)
    paid_output_absent = not any(outputs)

    policy = admission.get('classification')
    if row['state'] == 'proposed':
        shortage = parent['amount'] - parent['inventory_now']
        finished = producer.get('products_finished')
        if (policy != 'existing_proposed_outpost_policy_heuristic'
                or admission.get('state_at_admission') != 'proposed'
                or admission.get('source_role') != source_role
                or admission.get('source_unit') != producer['unit_number']
                or type(finished) is not int or finished < 20
                or admission.get('products_finished') != finished
                or admission.get('minimum_products_finished') != 20
                or admission.get('direct_input_route_absent') is not True
                or type(shortage) not in {int, float} or shortage < 10
                or admission.get('shortage_now') != shortage
                or admission.get('minimum_shortage') != 10
                or not paid_output_absent
                or admission.get('paid_output_absent') is not True
                or admission.get('native_outpost_payback_observed') is not False
                or admission.get('basis') !=
                    'current_planner_direct_route_and_minimum_runway_policy'):
            return None
        admission_basis = 'current_proposed_outpost_policy_heuristic'
    else:
        paid_parts = sorted(row['parts'])
        if (policy != 'current_paid_outpost_prefix_continuation'
                or not paid_parts
                or admission.get('state_at_admission') != 'building'
                or admission.get('paid_parts') != paid_parts
                or admission.get('current_paid_prefix') is not True
                or admission.get('native_outpost_payback_observed') is not False
                or admission.get('basis') != 'current_validated_outpost_component_receipts'):
            return None
        admission_basis = 'current_paid_outpost_prefix_continuation'

    step = plan.steps[0]
    try:
        if not step.allowed(snapshot) or step.satisfied(snapshot):
            return None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

    child_path = None
    action_start = None
    if step.action == 'factory_gather' and step.effect == 'inventory':
        gather = (step.parameters or {}).get('resource')
        parameters = step.parameters or {}
        quantity = parameters.get('quantity')
        inventory_now = snapshot.inventory.get(gather, 0) if isinstance(gather, str) else None
        site = _current_native_fair_resource_target(snapshot, gather)
        if child['kind'] == 'outpost_component':
            raw = materials.get('raw_prerequisite')
            child_path = raw.get('planner_item_path') if isinstance(raw, dict) else None
            direct_product = (child_path[-2]
                              if isinstance(child_path, list) and len(child_path) >= 2 else None)
            try:
                recipe = catalog.recipe_for(direct_product) if direct_product else None
            except (KeyError, ValueError, TypeError):
                recipe = None
            qualified_dependency = (
                isinstance(raw, dict)
                and raw.get('observed_tick') == snapshot.tick
                and raw.get('ingredient') == gather
                and raw.get('direct_product') == direct_product
                and raw.get('recipe') == (recipe.get('name') if isinstance(recipe, dict) else None)
                and _current_item_dependency_path(snapshot, catalog, child_path, kit_item, gather))
            fuel_request_matches = True
        else:
            raw = None
            child_path = [kit_item]
            direct_product = None
            qualified_dependency = gather == 'coal' and gather == kit_item
            fuel_request_matches = (
                qualified_dependency and type(inventory_now) is int
                and inventory_now < child['quantity']
                and type(quantity) is int
                and quantity == min(50, child['quantity'] - inventory_now))
        quantity = (step.parameters or {}).get('quantity')
        inventory_now = snapshot.inventory.get(gather, 0) if isinstance(gather, str) else None
        if (child['kind'] == 'outpost_component' and not qualified_dependency
                or child['kind'] == 'outpost_construction_fuel' and not fuel_request_matches
                or gather != step.item
                or type(quantity) is not int or type(inventory_now) is not int
                or type(step.threshold) is not int
                or step.threshold != inventory_now + quantity
                or not isinstance(site, dict)):
            return None
        action_start = {
            'kind': 'observed_raw_gather_start', 'resource': gather,
            'quantity': quantity, 'inventory_now': inventory_now,
            'fair_target_identity_observed': True,
            'native_target_session_bound': True,
            'fair_target_surface_index': site['surface_index'],
            'travel_is_lower_bound_not_arrival_proof': True,
            'native_harvest_requires_fresh_verification': True,
        }
    elif step.action == 'factory_insert' and step.effect == 'transfer':
        if child['kind'] != 'outpost_component':
            return None
        transfer = _recipe_input_transfer_start_evidence(
            snapshot, catalog, plan, path_root=kit_item, require_receiver_capacity=True)
        if (transfer is None or not _current_item_dependency_path(
                snapshot, catalog, transfer['planner_item_path'], kit_item,
                transfer['ingredient'])):
            return None
        child_path = transfer['planner_item_path']
        action_start = {
            'kind': 'owned_native_recipe_input_transfer_start',
            'transfer': transfer,
            'receiver_capacity_observed': True,
            'fresh_native_dispatch_capacity_check_required': True,
            'native_dispatch_checks_receiver_insertable_count': True,
        }
    elif step.action == 'factory_extract' and step.effect == 'transfer':
        pickup = _output_pickup_start_evidence(snapshot, catalog, plan, path_root=kit_item)
        if (pickup is None or not _current_item_dependency_path(
                snapshot, catalog, pickup['planner_item_path'], kit_item,
                pickup['ready_output_item'])):
            return None
        if (child['kind'] == 'outpost_construction_fuel'
                and (pickup['ready_output_item'] != kit_item
                     or pickup['planned_pickup_quantity'] >
                        child['quantity'] - snapshot.inventory.get(kit_item, 0))):
            return None
        child_path = pickup['planner_item_path']
        action_start = {'kind': 'owned_native_output_pickup_start', 'pickup': pickup}
    elif step.action in {'factory_craft', 'factory_craft_job'}:
        if child['kind'] != 'outpost_component':
            return None
        craft = _craft_start_evidence(snapshot, catalog, step)
        dependency = materials.get('craft_dependency')
        child_path = dependency.get('planner_item_path') if isinstance(dependency, dict) else None
        parameters = step.parameters or {}
        if (step.effect not in {'inventory', 'craft_job_complete'}
                or not isinstance(craft, dict) or craft.get('observed_tick') != snapshot.tick
                or craft.get('native_recipe') != parameters.get('recipe')
                or not isinstance(dependency, dict)
                or dependency.get('observed_tick') != snapshot.tick
                or dependency.get('recipe') != parameters.get('recipe')
                or dependency.get('product') != step.item
                or not _current_item_dependency_path(snapshot, catalog, child_path,
                                                     kit_item, step.item)
                or not all(craft.get(key) is True for key in (
                    'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                    'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                    'crafting_queue_empty'))):
            return None
        if step.action == 'factory_craft_job' and craft.get('craft_job_protocol_ready') is not True:
            return None
        action_start = {
            'kind': 'paid_native_handcraft_start', 'craft': craft,
            'native_output_and_child_completion_require_verification': True,
        }
    else:
        return None

    return {
        'schema': 1,
        'observed_tick': snapshot.tick,
        'outpost_resource': resource,
        'outpost_layout': layout,
        'outpost_state': row['state'],
        'parent_target_item': local_item,
        'parent_request_item': resource,
        'parent_request_amount': parent['amount'],
        'parent_inventory_now': parent['inventory_now'],
        'parent_shortage_now': parent['amount'] - parent['inventory_now'],
        'parent_planner_item_path': list(parent_path),
        'parent_and_child_paths_are_separate': True,
        'child_kit_item': kit_item,
        'child_kit_quantity': child['quantity'],
        'child_request_kind': child['kind'],
        'child_planner_item_path': list(child_path),
        'current_action': step.action,
        'current_action_item': step.item,
        'useful_partial_benefit_level': 1,
        'does_not_establish_level_two_blocker_removal': True,
        'admission_basis': admission_basis,
        'admission_is_not_native_payback_evidence': True,
        'outpost_placement_arrival_flow_output_and_parent_completion_unverified': True,
        'action_start_facts': action_start,
        'native_step_allowed_now': True,
        'native_action_outcome_requires_verification': True,
    }


def _research_science_transfer_start_evidence(snapshot, catalog, plan):
    """Paid packs for the current technology, not research progress or completion."""
    annotation = (plan.materials or {}).get('research_science_transfer')
    if not isinstance(annotation, dict) or len(plan.steps) != 1:
        return None
    step = plan.steps[0]
    parameters = step.parameters or {}
    name, item = annotation.get('technology'), annotation.get('ingredient')
    factory = snapshot.factory
    tech = catalog.technologies.get(name)
    lab = factory.get('entities', {}).get('utility:lab', {})
    identity = (snapshot.session_id, snapshot.tick)
    receipts = factory.get('receipts')
    quantity, receipt = parameters.get('quantity'), parameters.get('receipt')
    if (snapshot.world_kind != 'fle' or type(snapshot.tick) is not int
            or not isinstance(snapshot.session_id, str) or not snapshot.session_id
            or type(factory.get('tick')) is not int or factory.get('tick') != snapshot.tick
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or getattr(snapshot, '_atomic_inventory_verified', None) != identity
            or catalog.version != snapshot.game_version
            or type(annotation.get('observed_tick')) is not int
            or annotation.get('observed_tick') != snapshot.tick
            or not isinstance(tech, dict) or tech.get('enabled') is not True
            or tech.get('trigger') or name in (snapshot.researched or [])
            or any(parent not in (snapshot.researched or []) for parent in tech.get('prerequisites', []))
            or factory.get('research') not in ('', name)
            or factory.get('player_connected') is not True or factory.get('player_bound') is not True
            or type(factory.get('crafting_queue')) is not int or factory['crafting_queue'] != 0
            or not isinstance(lab, dict) or lab.get('name') != 'lab'
            or type(lab.get('unit_number')) is not int or lab['unit_number'] <= 0
            or not isinstance(lab.get('input'), dict) or not isinstance(receipts, dict)
            or not isinstance(receipt, str) or not receipt or receipt in receipts
            or step.action != 'factory_insert' or step.effect != 'transfer'
            or parameters.get('role') != 'utility:lab' or parameters.get('item') != item
            or type(quantity) is not int or quantity < 1 or step.costs != {item: quantity}):
        return None
    bill = {}
    for ingredient in tech.get('ingredients', []):
        if (not isinstance(ingredient, dict) or ingredient.get('type', 'item') != 'item'
                or not isinstance(ingredient.get('name'), str)
                or not _finite(ingredient.get('amount')) or ingredient['amount'] <= 0):
            return None
        bill[ingredient['name']] = bill.get(ingredient['name'], 0) + ingredient['amount']
    amount, carried = bill.get(item), snapshot.inventory.get(item)
    supplied, progress, count = lab['input'].get(item, 0), factory.get('research_progress', 0), tech.get('count')
    if (not _finite(amount) or amount <= 0 or not _finite(supplied) or not 0 <= supplied < amount
            or type(carried) is not int or carried < quantity
            or not _finite(progress) or not 0 <= progress <= 1
            or not _finite(count) or count <= 0
            or quantity != max(1, min(20, math.ceil(count * (1 - progress) * amount)))):
        return None
    return {'basis': 'current_native_technology_paid_science_input',
            'session_id': snapshot.session_id, 'observed_tick': snapshot.tick,
            'native_catalog_version': catalog.version, 'technology': name,
            'technology_enabled_and_unresearched': True,
            'prerequisites': list(tech.get('prerequisites', [])),
            'ingredients_per_unit': bill, 'research_count': count, 'research_progress_now': progress,
            'lab_role': 'utility:lab', 'lab_unit': lab['unit_number'],
            'lab_input_now': dict(lab['input']), 'ingredient': item,
            'actor_science_now': carried, 'paid_quantity_to_transfer': quantity,
            'planned_native_receipt': receipt,
            'native_receipt_query': {'schema': 1, 'session_id': snapshot.session_id,
                'tick': snapshot.tick, 'receipt_count': len(receipts), 'receipt': receipt,
                'present': False, 'map_verified': True},
            'transfer_selection_and_research_progress_require_native_verification': True,
            'research_selection_or_completion_not_established': True}


def _supplied_research_start_evidence(snapshot, catalog, plan):
    """Current lab readiness for selection, never projected research completion."""
    if len(plan.steps) != 1 or plan.steps[0].action != 'factory_research':
        return None
    step = plan.steps[0]
    name = (step.parameters or {}).get('technology')
    factory = snapshot.factory
    tech = catalog.technologies.get(name)
    lab = factory.get('entities', {}).get('utility:lab', {})
    if (snapshot.world_kind != 'fle' or factory.get('tick') != snapshot.tick
            or getattr(snapshot, '_coherent_observation_verified', None) != (snapshot.session_id, snapshot.tick)
            or catalog.version != snapshot.game_version
            or not isinstance(tech, dict) or tech.get('enabled') is not True
            or tech.get('trigger') or name in (snapshot.researched or [])
            or any(parent not in (snapshot.researched or []) for parent in tech.get('prerequisites', []))
            or factory.get('research') != ''
            or factory.get('player_connected') is not True or factory.get('player_bound') is not True
            or type(factory.get('crafting_queue')) is not int or factory['crafting_queue'] != 0
            or lab.get('name') != 'lab' or type(lab.get('unit_number')) is not int
            or lab['unit_number'] <= 0 or type(lab.get('electric_network_id')) is not int
            or lab['electric_network_id'] <= 0 or not _finite(lab.get('energy')) or lab['energy'] <= 0
            or not isinstance(lab.get('input'), dict)):
        return None
    bill = {}
    for ingredient in tech.get('ingredients', []):
        if (not isinstance(ingredient, dict) or ingredient.get('type', 'item') != 'item'
                or not isinstance(ingredient.get('name'), str)
                or not _finite(ingredient.get('amount')) or ingredient['amount'] <= 0):
            return None
        item, amount = ingredient['name'], ingredient['amount']
        count = lab['input'].get(item, 0)
        if not _finite(count) or count < amount:
            return None
        bill[item] = bill.get(item, 0) + amount
    if not bill or any(lab['input'].get(item, 0) < amount for item, amount in bill.items()):
        return None
    return {'basis': 'current_powered_lab_supplied_for_native_research_selection',
            'session_id': snapshot.session_id, 'observed_tick': snapshot.tick,
            'native_catalog_version': catalog.version, 'technology': name,
            'technology_enabled_and_unresearched': True, 'prerequisites_researched': True,
            'lab_role': 'utility:lab', 'lab_unit': lab['unit_number'],
            'electric_network_id': lab['electric_network_id'], 'energy_now': lab['energy'],
            'ingredients_per_unit': bill, 'lab_input_now': dict(lab['input']),
            'research_selection_and_later_progress_require_native_verification': True,
            'technology_completion_not_established': True}


def _native_research_trigger_start_evidence(snapshot, catalog, plan, gather_start):
    """Qualify immediate input to a native trigger without inventing a recipe edge."""
    from .research_trigger import current_machine_input_requirement, current_trigger

    materials = plan.materials or {}
    provenance = materials.get('native_research_trigger')
    intent = materials.get('work_intent')
    if (not isinstance(provenance, dict) or not isinstance(intent, dict)
            or intent.get('scope') != 'immediate'
            or intent.get('observed_tick') != snapshot.tick
            or provenance.get('observed_tick') != snapshot.tick):
        return None
    current = current_trigger(snapshot, catalog, provenance.get('technology'),
                              provenance.get('outer_recipe'))
    if current != provenance:
        return None
    step = plan.steps[0] if len(plan.steps) == 1 else None
    if step is None:
        return None
    if step.action == 'factory_gather' and step.effect == 'inventory':
        parameters = step.parameters or {}
        resource = parameters.get('resource')
        raw = materials.get('raw_prerequisite')
        raw_path = raw.get('planner_item_path') if isinstance(raw, dict) else None
        requirement = current_machine_input_requirement(
            snapshot, catalog, current, resource,
            [current['outer_recipe'], current['trigger_item'], resource]
            if isinstance(resource, str) else None)
        carried = snapshot.inventory.get(resource) if isinstance(resource, str) else None
        quantity = parameters.get('quantity')
        required_now = (requirement.get('machine_input_units_required_now')
                        if isinstance(requirement, dict) else None)
        expected_gather = (min(50, max(0, required_now - carried))
                           if isinstance(requirement, dict) and type(carried) is int else None)
        if (not isinstance(requirement, dict)
                or not isinstance(raw, dict) or raw.get('observed_tick') != snapshot.tick
                or raw.get('direct_product') != current['trigger_item']
                or raw.get('recipe') != current['trigger_recipe']
                or raw.get('ingredient') != resource
                or raw_path != [current['trigger_item'], resource]
                or not _current_item_dependency_path(
                    snapshot, catalog, raw_path, current['trigger_item'], resource)
                or gather_start is None
                or gather_start.get('resource_in_current_observation') is not True
                or gather_start.get('fair_target_identity_observed') is not True
                or type(quantity) is not int or quantity <= 0
                or quantity != expected_gather
                or step.threshold != carried + quantity):
            return None
        action = {
            'kind': 'direct_enabled_trigger_recipe_input_gather',
            'resource': resource, 'quantity': quantity,
            'trigger_recipe_input_units_required': requirement['planned_recipe_input_units'],
            'current_machine_input_units_required': required_now,
            'current_machine_input_now': requirement['machine_input_now'],
            'current_machine_input_in_flight': requirement['machine_input_in_flight'],
            'source_role': requirement['source_role'],
            'source_unit': requirement['source_unit'],
            'machine_recipe_observed': requirement['machine_recipe_observed'],
            'machine_recipe_identity_basis': requirement['machine_recipe_identity_basis'],
            'receiver_capacity': requirement['receiver_capacity'],
            'carried_resource_now': carried,
            'fair_target_identity_observed': True,
            'native_gather_outcome_requires_verification': True,
        }
    elif step.action == 'factory_insert' and step.effect == 'transfer':
        transfer = _recipe_input_transfer_start_evidence(
            snapshot, catalog, plan, path_root=current['trigger_item'],
            require_receiver_capacity=True)
        if not isinstance(transfer, dict):
            return None
        resource = transfer.get('ingredient')
        requirement = current_machine_input_requirement(
            snapshot, catalog, current, resource,
            [current['outer_recipe'], current['trigger_item'], resource])
        if (not isinstance(requirement, dict)
                or transfer.get('direct_native_recipe') != current['trigger_recipe']
                or transfer.get('planner_item_path') != [current['trigger_item'], resource]
                or transfer.get('owned_source_role') != requirement['source_role']
                or transfer.get('owned_source_unit') != requirement['source_unit']
                or transfer.get('paid_quantity_to_transfer') !=
                    requirement['machine_input_units_required_now']
                or transfer.get('receiver_capacity', {}).get('insertable_count_now', 0)
                    < transfer.get('paid_quantity_to_transfer', 1)
                or transfer.get('receiver_capacity', {}).get('observed_tick') != snapshot.tick
                or transfer.get('receiver_capacity', {}).get('session_id') != snapshot.session_id
                or transfer.get('receiver_capacity', {}).get('source_role') !=
                    requirement['source_role']
                or transfer.get('receiver_capacity', {}).get('source_unit') !=
                    requirement['source_unit']
                or transfer.get('receiver_capacity', {}).get('insertable_count_now') !=
                    requirement['receiver_capacity'].get('insertable_count_now')):
            return None
        action = {
            'kind': 'current_owned_trigger_recipe_input_transfer',
            'transfer': transfer,
            'trigger_recipe_input_units_required': requirement['planned_recipe_input_units'],
            'current_machine_input_units_required': requirement['machine_input_units_required_now'],
            'current_machine_input_now': requirement['machine_input_now'],
            'current_machine_input_in_flight': requirement['machine_input_in_flight'],
            'native_dispatch_rechecks_insertable_count_before_removal': True,
        }
    else:
        return None
    return {
        **current,
        'typed_dependency_path': [current['outer_recipe'], current['technology'],
                                  current['trigger_item'],
                                  action.get('resource') or (action.get('transfer') or {}).get(
                                      'ingredient')],
        'outer_recipe_is_not_a_direct_recipe_edge': True,
        'action_start_facts': action,
        'useful_partial_benefit_level': 1,
        'does_not_establish_trigger_item_output_or_unlock': True,
        'basis': 'typed_native_research_trigger_plus_direct_current_recipe_input',
    }

def _same_current_steps(catalog, current, plan):
    if current is None or current.id != plan.id:
        return False
    if current.steps == plan.steps:
        return True
    # BackgroundWorkLoop receipt-tracks a narrow class of one-step
    # handcrafts after ordinary planning. Reproduce only that exact
    # structural conversion without minting a receipt here.
    if (len(current.steps) != 1 or len(plan.steps) != 1
            or current.steps[0].action != 'factory_craft'
            or plan.steps[0].action != 'factory_craft_job'):
        return False
    source_step, tracked_step = current.steps[0], plan.steps[0]
    source_parameters = dict(source_step.parameters or {})
    tracked_parameters = dict(tracked_step.parameters or {})
    receipt = tracked_parameters.pop('receipt', None)
    if (not isinstance(receipt, str) or len(receipt) != 32
            or any(char not in '0123456789abcdef' for char in receipt)
            or tracked_parameters != source_parameters
            or tracked_step.effect != 'craft_job_complete'):
        return False
    recipe = catalog.recipes.get(source_parameters.get('recipe'), {})
    products, ingredients = recipe.get('products', []), recipe.get('ingredients', [])
    if (len(products) != 1 or not isinstance(products[0], dict)
            or products[0].get('type') != 'item'
            or products[0].get('probability', 1) != 1
            or not isinstance(ingredients, list) or not ingredients
            or any(not isinstance(entry, dict) or entry.get('type') != 'item'
                   for entry in ingredients)
            or any(entry.get('name') == products[0].get('name')
                   for entry in ingredients)):
        return False
    try:
        from dataclasses import replace
        return replace(tracked_step, action='factory_craft',
                       effect=source_step.effect,
                       parameters=source_step.parameters) == source_step
    except (TypeError, ValueError):
        return False


def _input_route_kit_parent_purpose(snapshot, catalog, plan):
    """Recompile a bounded kit need from the current owned route and parent path."""
    marker = (plan.materials or {}).get('input_route_kit_prerequisite')
    if not isinstance(marker, dict):
        return None
    local = (plan.materials or {}).get('local_objective')
    parent = marker.get('parent_local_objective')
    path = marker.get('parent_planner_item_path')
    identity = (snapshot.session_id, snapshot.tick)
    if (snapshot.world_kind != 'fle' or plan.goal != 'rocket_launch'
            or snapshot.game_version != catalog.version
            or type(marker.get('schema')) is not int or marker['schema'] != 1
            or type(marker.get('observed_tick')) is not int
            or marker['observed_tick'] != snapshot.tick
            or marker.get('session_id') != snapshot.session_id
            or getattr(snapshot, '_coherent_observation_verified', None) != identity
            or getattr(snapshot, '_atomic_inventory_verified', None) != identity
            or not isinstance(local, dict) or not isinstance(parent, dict)
            or local != {'item': marker.get('kit_item'),
                         'inventory_target': marker.get('kit_inventory_target'),
                         'ultimate_goal': plan.goal}
            or parent.get('ultimate_goal') != plan.goal
            or type(parent.get('inventory_target')) is not int
            or parent['inventory_target'] < 1
            or type(marker.get('kit_inventory_target')) is not int
            or marker['kit_inventory_target'] < 1
            or not isinstance(marker.get('source'), str)
            or not _current_item_dependency_path(snapshot, catalog, path,
                parent.get('item'), marker['source'].removeprefix('recipe:'))):
        return None
    try:
        from ..input_routes import current
        from .input_routes import InputRoutePlanner
        row = input_route_sources(snapshot).get(marker['source'])
        if (not isinstance(row, dict) or not current(row, snapshot)
                or row.get('state') not in {'proposed', 'building'}
                or type(marker.get('source_unit')) is not int
                or marker['source_unit'] != row['source_unit']):
            return None
        # The controller can expose ordinary work after rejecting an optional
        # capital proposal. Reproduce both bounded planner modes; neither mode
        # authorizes capital, clears failures, or changes dispatch eligibility.
        derived = None
        for ordinary in (False, True):
            planner = InputRoutePlanner(catalog, snapshot, plan.goal)
            planner._economic_acquiring = ordinary
            matches = [candidate for candidate in planner.candidates()
                       if _same_current_steps(catalog, candidate, plan)
                       and all((candidate.materials or {}).get(key) ==
                               (plan.materials or {}).get(key)
                               for key in ('input_route_kit_prerequisite',
                                           'local_objective', 'raw_prerequisite',
                                           'craft_dependency'))]
            if len(matches) == 1:
                derived = matches[0]
                break
        if derived is None:
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {**deepcopy(marker), 'basis': 'same_tick_recompiled_owned_input_route_kit_need',
            'route_flow_and_parent_output_are_not_established': True,
            'later_steps_require_fresh_native_preconditions': True}


def _candidate_local_raw_demand(snapshot, catalog, plans, plan, rows):
    """Bind a manual input to the current parent demand beside a route-kit offer."""
    if not plans or plan.id == plans[0].id or len(plan.steps) != 1:
        return None
    row = rows.get(plan.id, {})
    parent = rows.get(plans[0].id, {}).get("input_route_kit_parent_purpose")
    local, raw = row.get("local_target"), row.get("raw_prerequisite")
    identity = (snapshot.session_id, snapshot.tick)
    if (snapshot.world_kind != "fle" or snapshot.game_version != catalog.version
            or getattr(snapshot, "_coherent_observation_verified", None) != identity
            or getattr(snapshot, "_atomic_inventory_verified", None) != identity
            or not isinstance(parent, dict) or not isinstance(local, dict)
            or parent.get("parent_local_objective") != local
            or not isinstance(local.get("item"), str) or not local["item"]
            or local.get("ultimate_goal") != plan.goal
            or type(local.get("inventory_target")) is not int
            or local["inventory_target"] <= 0
            or type(snapshot.inventory.get(local.get("item"), 0)) is not int
            or snapshot.inventory.get(local.get("item"), 0) >= local["inventory_target"]
            or row.get("work_scope") != "immediate"
            or row.get("reasons") != [] or row.get("unknowns") != []
            or not isinstance(raw, dict) or raw.get("observed_tick") != snapshot.tick
            or not _current_item_dependency_path(snapshot, catalog,
                raw.get("planner_item_path"), local.get("item"), plan.steps[0].item)):
        return None
    try:
        from .input_routes import InputRoutePlanner
        matches = [candidate for candidate in InputRoutePlanner(
            catalog, snapshot, plan.goal).candidates()
            if candidate.id == plan.id and candidate.steps == plan.steps
            and all((candidate.materials or {}).get(key) == (plan.materials or {}).get(key)
                    for key in ("local_objective", "raw_prerequisite", "work_intent", "shortages", "batches"))]
        if len(matches) != 1:
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {"schema": 1, "tick": snapshot.tick, "session_id": snapshot.session_id,
            "catalog_version": catalog.version, "parent_source_unit": parent["source_unit"],
            "basis": "recompiled_current_parent_raw_demand"}


def craft_demand(snapshot, catalog, plan):
    """Recompute a selected recipe branch and the paid craft from native facts."""
    try:
        from .bootstrap_chain import dependency_chain
        factory, tick = snapshot.factory, snapshot.tick
        local = plan.materials['local_objective']
        marker = plan.materials['craft_dependency']
        intent = plan.materials['work_intent']
        if (snapshot.world_kind != 'fle' or type(tick) is not int
                or type(factory.get('tick')) is not int or factory['tick'] != tick
                or not isinstance(snapshot.session_id, str) or not snapshot.session_id
                or snapshot.game_version != catalog.version or len(plan.steps) != 1
                or intent != {'scope': 'immediate', 'observed_tick': tick}
                or local.get('ultimate_goal') != plan.goal
                or type(local.get('inventory_target')) is not int
                or local['inventory_target'] <= 0):
            return None
        step = plan.steps[0]; params = step.parameters
        path = marker['planner_item_path']
        if (step.action != 'factory_craft_job' or step.effect != 'craft_job_complete'
                or not isinstance(path, list) or not 1 <= len(path) <= 32
                or len(set(path)) != len(path)
                or any(not isinstance(item, str) or not item for item in path)
                or path[0] != local['item'] or path[-1] != step.item
                or marker['observed_tick'] != tick or type(marker['observed_tick']) is not int
                or marker['product'] != step.item or marker['recipe'] != params['recipe']
                or set(params) != {'recipe', 'batches', 'receipt'}
                or type(params['batches']) is not int or not 1 <= params['batches'] <= 200
                or not isinstance(params['receipt'], str) or not params['receipt']
                or type(factory.get('crafting_queue')) is not int
                or factory['crafting_queue'] != 0):
            return None
        start = _craft_start_evidence(snapshot, catalog, step)
        if not start or any(start.get(key) is not True for key in (
                'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                'crafting_queue_empty', 'craft_job_protocol_ready',
                'native_receipt_required_for_completion')):
            return None
        recipe = catalog.recipes[params['recipe']]
        if (recipe.get('name') != params['recipe'] or recipe.get('hidden', False)
                or type(recipe.get('enabled')) is not bool
                or type(recipe.get('hidden', False)) is not bool
                or catalog.hand_categories.get(recipe.get('category')) is not True
                or len(recipe['products']) != 1):
            return None
        for entry in recipe['ingredients'] + recipe['products']:
            if (entry.get('type') != 'item' or not isinstance(entry.get('name'), str)
                    or not entry['name'] or type(entry.get('amount')) not in (int, float)
                    or not math.isfinite(entry['amount']) or entry['amount'] <= 0
                    or type(entry.get('probability', 1)) not in (int, float)
                    or entry.get('probability', 1) != 1):
                return None
        chain = dependency_chain(snapshot, catalog, local, path) if len(path) > 1 else None
        required = chain['raw_input_inventory_target'] if chain else local['inventory_target']
        carried = snapshot.inventory.get(step.item, 0)
        if type(carried) is not int or not 0 <= carried < required:
            return None
        output = start['expected_products_after_native_verification'][step.item]
        per_batch = recipe['products'][0]['amount']
        if (recipe['products'][0]['name'] != step.item
                or params['batches'] > math.ceil((required - carried) / per_batch)
                or type(step.threshold) not in (int, float)
                or isinstance(step.threshold, bool) or step.threshold != carried + output
                or any(type(snapshot.inventory.get(item, 0)) is not int
                       or snapshot.inventory.get(item, 0) < cost
                       for item, cost in step.costs.items())):
            return None
        return {
            'schema': 1, 'observed_tick': tick, 'session_id': snapshot.session_id,
            'native_catalog_version': catalog.version, 'local_target': deepcopy(local),
            'planner_item_path': list(path), 'recipe_dependency_chain': chain,
            'native_craft_recipe': deepcopy(recipe), 'native_step': asdict(step),
            'start_evidence': start,
            'carried_inputs': {item: snapshot.inventory.get(item, 0) for item in step.costs},
            'required_product_inventory': required, 'carried_product': carried,
            'remaining_product_deficit': required - carried,
            'expected_product_after_receipt': output,
            'scope': 'selected_branch_not_full_target_bill',
            'craft_and_later_completion_require_native_verification': True,
        }
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None


def qualified_craft_demand(plan, facts, row):
    """Rebuild the witness using the independently projected current catalog."""
    from types import SimpleNamespace
    from .catalog import Catalog
    try:
        observed = facts['factory']['recipe_dependency_catalog']
        if (type(observed.get('schema')) is not int or observed['schema'] != 1
                or type(observed.get('tick')) is not int or observed['tick'] != facts['tick']
                or observed['session_id'] != facts['session_id']
                or observed['version'] != facts['game_version']
                or row['local_target'] != plan.materials['local_objective']
                or row['work_scope'] != 'immediate' or row['unknowns'] != []):
            return False
        snapshot = SimpleNamespace(**{key: facts[key] for key in (
            'world_kind', 'tick', 'session_id', 'game_version', 'inventory', 'factory')},
            researched=facts.get('researched', []))
        catalog = Catalog(observed['version'], observed['recipes'], {}, {},
                          observed['hand_categories'], observed['stack_sizes'])
        proof = craft_demand(snapshot, catalog, plan)
        return (proof is not None and json.dumps(proof, sort_keys=True, allow_nan=False)
                == json.dumps(row['craft_recipe_demand'], sort_keys=True, allow_nan=False)
                and proof['start_evidence'] == row['craft_start_evidence'])
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return False


def candidate_evidence(snapshot, catalog, plans) -> dict:
    """Describe the admitted frontier without inventing downstream output."""
    entities = snapshot.factory.get('entities', {})
    try:
        schedules = research_schedule(snapshot, catalog)
    except (ValueError, KeyError, TypeError):
        schedules = []  # Unsupported forecast is explicitly unknown below.
    due_packs = {row['item'] for row in schedules if row['due']}
    result = {}
    from .solid_investment import ranking_marker as solid_marker
    from .coal_funding import ranking_marker as coal_marker
    has_solid_offer = any(solid_marker(plan, snapshot) for plan in plans)
    has_coal_offer = any(coal_marker(plan, snapshot) for plan in plans)
    for index, plan in enumerate(plans):
        kit_parent_purpose = _input_route_kit_parent_purpose(snapshot, catalog, plan)
        bootstrap_pickup_start = _bootstrap_output_pickup_start_evidence(snapshot, catalog, plan)
        if ('input_route_kit_prerequisite' in (plan.materials or {})
                and kit_parent_purpose is None):
            # A stale or forged kit annotation cannot acquire a local-target
            # recipe proof. Keep the executable step and its native guards.
            plan = replace(plan, materials={**(plan.materials or {}),
                'local_objective': None, 'raw_prerequisite': None,
                'craft_dependency': None, 'placement_dependency': None})
        placement_start = _placement_start_evidence(snapshot, plan)
        utility_lab_dependency = _utility_lab_research_dependency(snapshot, catalog, plan)
        power_annotation = (plan.materials or {}).get('utility_power_prerequisite')
        power_path = (power_annotation.get('planner_path')
                      if isinstance(power_annotation, dict) else None)
        power_path_root = next((entry.removeprefix('item:') for entry in power_path
                                if isinstance(entry, str) and entry.startswith('item:')
                                and entry.removeprefix('item:')), None) \
            if isinstance(power_path, list) else None
        if power_path_root is None:
            recipe_input_transfer_start = _recipe_input_transfer_start_evidence(
                snapshot, catalog, plan, include_dependency_chain=True)
            output_pickup_start = _output_pickup_start_evidence(snapshot, catalog, plan)
        else:
            recipe_input_transfer_start = _recipe_input_transfer_start_evidence(
                snapshot, catalog, plan, path_root=power_path_root)
            output_pickup_start = _output_pickup_start_evidence(
                snapshot, catalog, plan, path_root=power_path_root)
        origin = _position(snapshot.player_position)
        travel, actor, unknown, reasons = 0.0, 0.0, [], []
        harvest_thresholds = {}
        urgency, outputs, quantities, costs = 0, set(), 0.0, {}
        craft_handling_forecasts = []
        for step in plan.steps:
            parameters = step.parameters or {}
            role = parameters.get('role', '')
            entity = entities.get(role, {})
            item = parameters.get('item', parameters.get('resource', step.item))
            amount = parameters.get('quantity', 0)
            amount = amount if _finite(amount) and amount > 0 else 0
            for name, count in (step.costs or {}).items():
                costs[name] = costs.get(name, 0) + count
            passive = step.action in {'factory_wait', 'idle'}
            target = None
            if step.action == 'factory_gather':
                evidence = snapshot.factory.get('fair_resource_targets', {}).get(item, {})
                target = _position(evidence.get('position'))
            elif step.action == 'factory_place' and placement_start is not None:
                target = _position(placement_start['site_position'])
            elif step.action in {'walk_to_coal', 'mine_coal', 'walk_to_iron', 'mine_iron'}:
                resource = 'coal' if step.action.endswith('coal') else 'iron-ore'
                evidence = snapshot.factory.get('fair_resource_targets', {}).get(resource, {})
                target = _position(evidence.get('position'))
            elif role:
                target = _position(entity.get('position'))
            if passive or step.action in {'factory_craft', 'factory_craft_job',
                                          'factory_research', 'factory_bind'}:
                distance = 0.0
            elif origin is not None and target is not None:
                distance = math.dist(origin, target)
                origin = target
            else:
                distance = None
                origin = None
                unknown.append('travel:' + step.action)
            if distance is not None:
                travel += distance
                actor += distance * TRAVEL_TICKS_PER_TILE
            if not passive:
                actor += SERVICE_TICKS
            if step.action == 'factory_gather':
                actor += amount * RAW_TICKS_PER_ITEM
                quantities += amount
            elif step.action in {'mine_coal', 'mine_iron'}:
                # Legacy harvest steps verify an inventory threshold, not an
                # additional amount. Count only the unmet observed threshold.
                prior = max(snapshot.inventory.get(step.item, 0),
                            harvest_thresholds.get(step.item, 0))
                remaining = max(0, step.threshold - prior)
                actor += remaining * RAW_TICKS_PER_ITEM
                harvest_thresholds[step.item] = max(prior, step.threshold)
                quantities += remaining
                outputs.add(step.item)
            elif step.action in {'factory_craft', 'factory_craft_job'}:
                recipe = catalog.recipes.get(parameters.get('recipe', ''), {})
                energy, batches = recipe.get('energy'), parameters.get('batches')
                if _finite(energy) and energy > 0 and type(batches) is int and batches > 0:
                    start = _craft_start_evidence(snapshot, catalog, step)
                    if (start is not None
                            and all(start.get(key) is True for key in (
                                'inputs_in_inventory_now', 'recipe_unlocked_and_handcraftable',
                                'player_connected_and_bound', 'crafting_queue_empty'))
                            and (step.action != 'factory_craft_job'
                                 or start.get('craft_job_protocol_ready') is True)):
                        # Compare the forecast handling volume of ready crafts
                        # with gathers/transfers. Output remains unverified and
                        # is never added to the carried supply or async outputs.
                        products = start['expected_products_after_native_verification']
                        quantities += sum(products.values())
                        craft_handling_forecasts.append({
                            'native_recipe': parameters['recipe'], 'batches': batches,
                            'expected_products_after_native_verification': products,
                            'forecast_not_completed_output': True})
                    if step.action == 'factory_craft':
                        actor += energy * batches * 60
                    if step.action == 'factory_craft':
                        outputs.update(p['name'] for p in recipe.get('products', [])
                                       if p.get('type') == 'item')
                else:
                    unknown.append('craft_duration')
            elif step.action in {'factory_insert', 'factory_extract'}:
                quantities += amount
                outputs.add(item)
            if step.action == 'factory_insert':
                fuel = entity.get('fuel', {}).get('coal')
                if item == 'coal' and _finite(fuel) and fuel < 2:
                    urgency = max(urgency, 3)
                    reasons.append('observed_low_fuel:' + role)
                if role == 'utility:lab' and item in due_packs:
                    urgency = max(urgency, 3)
                    reasons.append('due_research_delivery:' + item)
                # A current, coverage-due refill outranks optional stockpiling.
                # This affects ranking only; native execution guards still apply.
                schedule = (plan.materials or {}).get('scheduling', {})
                coverage, lead = schedule.get('coverage_ticks'), schedule.get('lead_ticks')
                if (schedule.get('kind') == 'producer_resupply'
                        and schedule.get('observed_tick') == snapshot.tick
                        and schedule.get('role') == role and schedule.get('item') == item
                        and _finite(coverage) and coverage >= 0 and _finite(lead) and lead >= 0
                        and coverage <= lead + SAFETY_TICKS):
                    urgency = max(urgency, 2)
                    reasons.append('due_producer_refill:' + role)
                recipe = catalog.recipes.get(entity.get('recipe', ''), {})
                ingredients = recipe.get('ingredients', [])
                requirements = {i['name']: i['amount'] for i in ingredients if i.get('type') == 'item'}
                supplied = entity.get('input', {})
                powered = entity.get('energy', 0) > 0 or entity.get('fuel', {}).get('coal', 0) > 0
                if (item in requirements and powered and not entity.get('crafting')
                        and supplied.get(item, 0) < requirements[item]
                        and all(supplied.get(name, 0) + (amount if name == item else 0) >= count
                                for name, count in requirements.items())
                        and len(requirements) == len(ingredients)):
                    urgency = max(urgency, 2)
                    reasons.append('unblocks_supplied_recipe:' + role)
        capital = (plan.materials or {}).get('capital_investment')
        if isinstance(capital, dict) and capital.get('observed_tick') == snapshot.tick:
            from .capital import validate_spec, STAGES
            try:
                validate_spec(capital.get('spec'), catalog, snapshot.researched or [])
                if capital.get('stage') in STAGES and plan.id.startswith(capital['spec']['key'] + ':'):
                    urgency = max(urgency, 1)  # Below due supply and emergency maintenance.
                    reasons.append('justified_capital_stage:' + capital['stage'])
            except (ValueError, KeyError, TypeError):
                pass  # Invalid or stale annotations cannot buy priority.
        if has_solid_offer:
            if outputs & due_packs:
                urgency = max(urgency, 2)
                reasons.append('ready_science_before_solid_investment')
            if solid_marker(plan, snapshot):
                urgency = max(urgency, 1)
                reasons.append('justified_downstream_investment')
        if has_coal_offer:
            if outputs & due_packs:
                urgency = max(urgency, 2)
                reasons.append('ready_science_before_coal_kit')
            if coal_marker(plan, snapshot):
                urgency = max(urgency, 1)
                reasons.append('explicit_coal_kit_investment')
        local_target = (plan.materials or {}).get('local_objective')
        if local_target is not None:
            local_target = deepcopy(local_target)
        intent = (plan.materials or {}).get('work_intent', {})
        scope = (intent.get('scope') if isinstance(intent, dict)
                 and intent.get('observed_tick') == snapshot.tick else None)
        scope = scope if scope in {'immediate', 'lookahead'} else 'unclassified'
        compiled_work_scope = scope
        if utility_lab_dependency is not None:
            unknown.append('placement_site:factory_place')
        prerequisite = (plan.materials or {}).get('raw_prerequisite')
        prerequisite_evidence = None
        gather_start = None
        if len(plan.steps) == 1 and plan.steps[0].action == 'factory_gather':
            step = plan.steps[0]
            resource = (step.parameters or {}).get('resource')
            site = snapshot.factory.get('fair_resource_targets', {}).get(resource, {})
            gather_start = {
                'observed_tick': snapshot.tick,
                'session_id': snapshot.session_id,
                'resource_in_current_observation': resource in snapshot.nearby_resources,
                'fair_target_identity_observed': (
                    isinstance(site, dict) and isinstance(site.get('name'), str)
                    and bool(site['name'].strip())
                    and (resource == 'wood' or site['name'] == resource)
                    and type(site.get('surface_index')) is int and site['surface_index'] > 0
                    and _position(site.get('position')) is not None),
                'resource_inventory_now': snapshot.inventory.get(resource, 0),
                'target_inventory_after_this_step': step.threshold,
                'travel_is_lower_bound_not_arrival_proof': True,
            }
        intent = (plan.materials or {}).get('work_intent')
        craft_start = (_craft_start_evidence(snapshot, catalog, plan.steps[0])
                       if len(plan.steps) == 1 and plan.steps[0].action in {
                           'factory_craft', 'factory_craft_job'}
                       and (intent is None or (isinstance(intent, dict)
                            and intent.get('observed_tick') == snapshot.tick))
                       else None)
        craft_dependency = None
        provenance = (plan.materials or {}).get('craft_dependency')
        if (craft_start is not None
                and craft_start['recipe_unlocked_and_handcraftable'] is True
                and isinstance(provenance, dict)):
            path = provenance.get('planner_item_path')
            local = (plan.materials or {}).get('local_objective')
            craft_target_item = local.get('item') if isinstance(local, dict) else None
            step = plan.steps[0]
            if (provenance.get('observed_tick') == snapshot.tick
                    and provenance.get('recipe') == craft_start['native_recipe']
                    and provenance.get('product') == step.item
                    and isinstance(path, list) and 1 <= len(path) <= 32
                    and all(isinstance(item, str) and item for item in path)
                    and path[-1] == step.item
                    and isinstance(craft_target_item, str) and bool(craft_target_item)
                    and path[0] == craft_target_item):
                craft_dependency = {
                    'observed_tick': snapshot.tick,
                    'planner_item_path': list(path),
                    'current_craft_product': step.item,
                    'basis': 'current_recursive_planner_provenance_and_native_recipe',
                    'later_steps_require_fresh_native_preconditions': True,
                }
        local_target_completion = _local_target_completion_evidence(
            snapshot, catalog, plan, craft_start, craft_dependency)
        if local_target_completion is None:
            local_target_completion = _local_target_gather_completion_evidence(
                snapshot, plan, gather_start)
        shared_bill_craft = None
        bill = (plan.materials or {}).get('shared_bill_craft')
        local = (plan.materials or {}).get('local_objective')
        if (scope == 'lookahead' and craft_start is not None
                and craft_start['recipe_unlocked_and_handcraftable'] is True
                and isinstance(bill, dict) and isinstance(local, dict)
                and len(plan.steps) == 1):
            step = plan.steps[0]
            parameters = step.parameters or {}
            bill_inventory_target = bill.get('bill_inventory_target')
            carried = bill.get('inventory_now')
            produced = craft_start['expected_products_after_native_verification'].get(step.item)
            current_bill = _current_shared_bill(snapshot, catalog, plan, local)
            if (step.action == 'factory_craft_job'
                    and isinstance(parameters.get('receipt'), str)
                    and bool(parameters['receipt'])
                    and craft_start.get('native_recipe') == parameters.get('recipe')
                    and all(craft_start.get(key) is True for key in (
                        'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                        'player_connected_and_bound', 'crafting_queue_empty',
                        'craft_job_protocol_ready', 'native_receipt_required_for_completion'))
                    and bill.get('basis') == 'current_catalog_shared_material_bill'
                    and bill.get('observed_tick') == snapshot.tick
                    and bill.get('local_target_item') == local.get('item')
                    and bill.get('local_target_amount') == local.get('inventory_target')
                    and bill.get('craft_item') == step.item
                    and type(bill.get('local_target_amount')) is int
                    and bill['local_target_amount'] > 0
                    and current_bill is not None
                    and current_bill['target'] == bill_inventory_target
                    and current_bill['batches'].get(parameters.get('recipe')) == parameters.get('batches')
                    and type(bill_inventory_target) is int and bill_inventory_target > 0
                    and type(carried) is int and 0 <= carried < bill_inventory_target
                    and snapshot.inventory.get(step.item, 0) == carried
                    and type(produced) is int and produced > 0
                    and bill.get('planned_product_units') == produced
                    and produced >= bill_inventory_target - carried):
                shared_bill_craft = {
                    'observed_tick': snapshot.tick,
                    'local_target_item': local['item'],
                    'craft_item': step.item,
                    'bounded_bill_inventory_target': bill_inventory_target,
                    'inventory_now': carried,
                    'unfilled_bill_units': bill_inventory_target - carried,
                    'expected_products_after_native_verification': produced,
                    'basis': 'current_catalog_shared_material_bill_and_native_recipe',
                    'bounded_workload_demands': dict(current_bill['demands']),
                    'forecast_is_not_paid_stock_or_completed_output': True,
                    'background_overlap_requires_native_admission': True,
                }
        placement_dependency = None
        provenance = (plan.materials or {}).get('placement_dependency')
        local = (plan.materials or {}).get('local_objective')
        target_item = local.get('item') if isinstance(local, dict) else None
        if (placement_start is not None
                and placement_start['paid_furnace_in_inventory_now'] is True
                and placement_start['no_source_owned_at_role_now'] is True
                and placement_start['player_connected_and_bound_now'] is True
                and placement_start['crafting_queue_empty_now'] is True
                and isinstance(provenance, dict)):
            path = provenance.get('planner_item_path')
            role = placement_start['source_role']
            product = role.removeprefix('recipe:')
            recipe = catalog.recipes.get(product, {})
            furnace = catalog.machines.get('stone-furnace', {})
            if (provenance.get('observed_tick') == snapshot.tick
                    and provenance.get('machine') == 'stone-furnace'
                    and provenance.get('source_role') == role
                    and provenance.get('site_anchor') == placement_start['site_anchor']
                    and isinstance(target_item, str) and bool(target_item)
                    and isinstance(path, list) and 1 <= len(path) <= 32
                    and all(isinstance(item, str) and item for item in path)
                    and path[0] == target_item and path[-1] == product
                    and recipe.get('name') == product and not recipe.get('hidden')
                    and catalog.enabled(recipe, snapshot.researched or [])
                    and bool(furnace.get('categories', {}).get(recipe.get('category')))
                    and any(row.get('type') == 'item' and row.get('name') == product
                            and row.get('amount', 0) > 0
                            for row in recipe.get('products', []))):
                placement_dependency = {
                    'observed_tick': snapshot.tick,
                    'planner_item_path': list(path),
                    'machine_for_recipe': role,
                    'basis': 'current_recursive_planner_and_validated_native_site',
                    'later_flow_and_output_require_fresh_native_preconditions': True,
                }
        if (isinstance(prerequisite, dict) and prerequisite.get('observed_tick') == snapshot.tick
                and len(plan.steps) == 1 and plan.steps[0].action == 'factory_gather'):
            step = plan.steps[0]
            ingredient = (step.parameters or {}).get('resource')
            parent = prerequisite.get('direct_product')
            recipe_name = prerequisite.get('recipe')
            path = prerequisite.get('planner_item_path')
            recipe = catalog.recipes.get(recipe_name, {})
            if (ingredient == prerequisite.get('ingredient')
                    and isinstance(parent, str) and parent
                    and isinstance(path, list) and 2 <= len(path) <= 32
                    and path[-2:] == [parent, ingredient]
                    and any(product.get('type') == 'item' and product.get('name') == parent
                            and product.get('amount', 0) > 0 for product in recipe.get('products', []))
                    and any(entry.get('type') == 'item' and entry.get('name') == ingredient
                            and entry.get('amount', 0) > 0 for entry in recipe.get('ingredients', []))):
                prerequisite_evidence = {
                    'observed_tick': snapshot.tick,
                    'direct_recipe': recipe_name,
                    'direct_product': parent,
                    'planner_item_path': list(path),
                    'basis': 'current_planner_dependency_and_native_catalog_recipe',
                    'later_steps_require_fresh_native_preconditions': True,
                }
        direct_alternative_start = _direct_alternative_parent_demand_start_evidence(
            snapshot, catalog, plans, plan, prerequisite_evidence, gather_start)
        if direct_alternative_start is not None:
            # The current recipe path supplies the already selected local
            # target; this does not qualify the policy investment's payoff.
            scope = 'immediate'
            reasons.append('current_parent_local_target_raw_prerequisite')
        fuel_prerequisite = None
        fuel_transfer_start = None
        fuel = (plan.materials or {}).get('fuel_prerequisite')
        local = (plan.materials or {}).get('local_objective')
        local_item = local.get('item') if isinstance(local, dict) else None
        step = plan.steps[0] if len(plan.steps) == 1 else None
        if isinstance(fuel, dict) and len(plan.steps) == 1:
            parameters = step.parameters or {}
            role = fuel.get('source_role')
            machine = entities.get(role, {}) if isinstance(role, str) else {}
            fuel_bag = machine.get('fuel')
            current = fuel_bag.get('coal', 0) if isinstance(fuel_bag, dict) else None
            recipe_name = role.removeprefix('recipe:') if isinstance(role, str) else ''
            recipe = catalog.recipes.get(recipe_name, {})
            prototype = catalog.machines.get(machine.get('name'), {})
            path = fuel.get('planner_item_path')
            carried = snapshot.inventory.get('coal')
            startup = current == 0 and machine.get('products_finished', 0) == 0
            expected_target = min(5 if startup else 50, catalog.stack_sizes.get('coal', 50))
            required = expected_target - current if _finite(current) else None
            if (step.action == 'factory_insert' and step.effect == 'transfer'
                    and parameters.get('item') == 'coal' and parameters.get('role') == role
                    and type(required) is int and required > 0
                    and parameters.get('quantity') == required
                    and parameters.get('receipt') == f'{snapshot.tick}:factory_insert:{role}:coal'
                    and step.costs == {'coal': required}
                    and type(carried) is int and carried >= required
                    and fuel.get('observed_tick') == snapshot.tick
                    and isinstance(role, str) and role.startswith('recipe:')
                    and type(machine.get('unit_number')) is int
                    and machine['unit_number'] == fuel.get('source_unit')
                    and current == fuel.get('observed_fuel')
                    and fuel.get('target_fuel') == expected_target
                    and fuel.get('startup') is startup
                    and isinstance(fuel_bag, dict)
                    and prototype.get('burner') is True
                    and recipe.get('name') == recipe_name and not recipe.get('hidden')
                    and catalog.enabled(recipe, snapshot.researched or [])
                    and bool(prototype.get('categories', {}).get(recipe.get('category')))
                    and isinstance(local_item, str) and bool(local_item)
                    and isinstance(path, list) and 1 <= len(path) <= 32
                    and all(isinstance(item, str) and item for item in path)
                    and path[0] == local_item and path[-1] == recipe_name
                    and snapshot.factory.get('player_connected') is True
                    and snapshot.factory.get('player_bound') is True):
                fuel_transfer_start = {
                    'observed_tick': snapshot.tick,
                    'planner_item_path': list(path),
                    'burner_role': role,
                    'burner_unit': machine['unit_number'],
                    'fuel_now': current,
                    'coal_in_inventory_now': carried,
                    'coal_to_transfer': required,
                    'native_receipt': parameters['receipt'],
                    'basis': 'current_planner_need_owned_burner_and_paid_inventory',
                    'native_transfer_and_later_output_require_verification': True,
                }
        # ReadyWorkPlanner uses a current grouped service decision for owned
        # burner furnaces. Bind a transfer to the primary current consumer row;
        # optional group members or reserve estimates cannot authorize it.
        service = (plan.materials or {}).get('fuel_service')
        if fuel_transfer_start is None and step is not None and step.action == 'factory_insert':
            consumers = service.get('consumers') if isinstance(service, dict) else None
            primary = (consumers[0] if isinstance(consumers, list) and consumers
                       and isinstance(consumers[0], dict) else None)
            parameters = step.parameters or {}
            role = parameters.get('role')
            machine = entities.get(role, {}) if isinstance(role, str) else {}
            fuel_bag = machine.get('fuel')
            current = fuel_bag.get('coal', 0) if isinstance(fuel_bag, dict) else None
            recipe_name = role.removeprefix('recipe:') if isinstance(role, str) else ''
            recipe = catalog.recipes.get(recipe_name, {})
            prototype = catalog.machines.get(machine.get('name'), {})
            required = (primary.get('deficit') if isinstance(primary, dict) else None)
            receipt = parameters.get('receipt')
            if (isinstance(service, dict) and service.get('schema') == 2
                    and type(service.get('schema')) is int
                    and service.get('observed_tick') == snapshot.tick
                    and service.get('acquisition_performed_by_this_plan') is False
                    and isinstance(consumers, list) and bool(consumers)
                    and service.get('consumer_count') == len(consumers)
                    and primary is not None and primary.get('role') == role
                    and type(required) is int and required > 0
                    and type(current) is int and current >= 0
                    and primary.get('fuel') == current
                    and primary.get('target') == current + required
                    and parameters.get('item') == 'coal'
                    and parameters.get('quantity') == required
                    and receipt == f'{snapshot.tick}:factory_insert:{role}:coal'
                    and step.costs == {'coal': required}
                    and type(snapshot.inventory.get('coal')) is int
                    and snapshot.inventory['coal'] >= required
                    and type(service.get('carried_spendable')) is int
                    and service['carried_spendable'] >= required
                    and isinstance(fuel_bag, dict)
                    and isinstance(role, str) and role.startswith('recipe:')
                    and type(machine.get('unit_number')) is int and machine['unit_number'] > 0
                    and _recipe_source_matches(snapshot, catalog, role, machine)
                    and snapshot.factory.get('production_sites', {}).get('protocol') == 1
                    and snapshot.factory.get('production_sites', {}).get('session_id') == snapshot.session_id
                    and snapshot.factory.get('production_sites', {}).get('tick') == snapshot.tick
                    and recipe.get('name') == recipe_name and not recipe.get('hidden')
                    and catalog.enabled(recipe, snapshot.researched or [])
                    and prototype.get('burner') is True
                    and bool(prototype.get('categories', {}).get(recipe.get('category')))
                    and snapshot.factory.get('player_connected') is True
                    and snapshot.factory.get('player_bound') is True
                    and receipt not in snapshot.factory.get('receipts', {})):
                fuel_transfer_start = {
                    'observed_tick': snapshot.tick,
                    'burner_role': role,
                    'burner_unit': machine['unit_number'],
                    'fuel_now': current,
                    'coal_in_inventory_now': snapshot.inventory['coal'],
                    'coal_to_transfer': required,
                    'native_receipt': receipt,
                    'basis': 'current_planner_need_owned_burner_and_paid_inventory',
                    'service_basis': 'current_primary_fuel_service_consumer',
                    'native_transfer_and_later_output_require_verification': True,
                }
        if (isinstance(fuel, dict) and len(plan.steps) == 1
                and plan.steps[0].action == 'factory_gather'
                and (plan.steps[0].parameters or {}).get('resource') == 'coal'
                and gather_start is not None
                and gather_start['resource_in_current_observation'] is True
                and gather_start['fair_target_identity_observed'] is True):
            role = fuel.get('source_role')
            machine = entities.get(role, {})
            recipe_name = role.removeprefix('recipe:') if isinstance(role, str) else ''
            recipe = catalog.recipes.get(recipe_name, {})
            prototype = catalog.machines.get(machine.get('name'), {})
            path = fuel.get('planner_item_path')
            fuel_bag = machine.get('fuel')
            current = fuel_bag.get('coal', 0) if isinstance(fuel_bag, dict) else None
            startup = current == 0 and machine.get('products_finished', 0) == 0
            sites = snapshot.factory.get('production_sites')
            sources = sites.get('sources') if isinstance(sites, dict) else None
            owned = sources.get(role) if isinstance(sources, dict) else None
            expected_target = min(5 if startup else 50, catalog.stack_sizes.get('coal', 50))
            carried = snapshot.inventory.get('coal', 0)
            gather_quantity = (min(50, max(0, math.ceil(expected_target - current - carried)))
                               if _finite(current) and type(carried) is int else 0)
            if (fuel.get('observed_tick') == snapshot.tick
                    and isinstance(role, str) and role.startswith('recipe:')
                    and type(machine.get('unit_number')) is int
                    and machine['unit_number'] == fuel.get('source_unit')
                    and _finite(current) and current == fuel.get('observed_fuel')
                    and type(expected_target) is int and expected_target >= 1
                    and fuel.get('target_fuel') == expected_target
                    and fuel.get('startup') is startup
                    and current < min(5, expected_target)
                    and type(carried) is int and carried >= 0 and gather_quantity > 0
                    and (plan.steps[0].parameters or {}).get('quantity') == gather_quantity
                    and plan.steps[0].threshold == carried + gather_quantity
                    and prototype.get('burner') is True
                    and (startup or (isinstance(owned, dict)
                         and owned.get('state') == 'owned'
                         and owned.get('source_unit') == machine['unit_number']))
                    and recipe.get('name') == recipe_name and not recipe.get('hidden')
                    and catalog.enabled(recipe, snapshot.researched or [])
                    and bool(prototype.get('categories', {}).get(recipe.get('category')))
                    and isinstance(local_item, str) and bool(local_item)
                    and isinstance(path, list) and 1 <= len(path) <= 32
                    and all(isinstance(item, str) and item for item in path)
                    and path[0] == local_item and path[-1] == recipe_name):
                fuel_prerequisite = {
                    'observed_tick': snapshot.tick,
                    'planner_item_path': list(path),
                    'burner_role': role,
                    'burner_unit': machine['unit_number'],
                    'fuel_now': current,
                    'coal_in_inventory_now': carried,
                    'planned_gather_units': gather_quantity,
                    'established_service_target': expected_target if not startup else None,
                    'startup_target': expected_target if startup else None,
                    'current_required_units': min(5, expected_target) - current,
                    'current_unfunded_units': max(0, min(5, expected_target) - current - carried),
                    'gather_units_beyond_current_need': max(
                        0, gather_quantity - max(0, min(5, expected_target) - current - carried)),
                    'basis': 'current_planner_fuel_need_and_owned_native_burner',
                    'later_fuel_transfer_and_output_require_fresh_native_preconditions': True,
                }
        if isinstance(fuel_transfer_start, dict):
            local_dependency = _local_fuel_recipe_dependency(
                snapshot, catalog, plan, fuel_transfer_start)
            existing_path = fuel_transfer_start.get('planner_item_path')
            if (isinstance(local_dependency, dict)
                    and existing_path in (None, local_dependency['planner_item_path'])):
                fuel_transfer_start['planner_item_path'] = list(
                    local_dependency['planner_item_path'])
                fuel_transfer_start['local_recipe_dependency'] = local_dependency
        buffer_build_start = _buffer_build_start_evidence(snapshot, catalog, plan)
        buffer_fuel_start = _buffer_fuel_start_evidence(snapshot, catalog, plan)
        utility_power_start = _utility_power_prerequisite_start_evidence(
            snapshot, catalog, plan, gather_start, local_target_completion,
            craft_start=craft_start,
            recipe_input_transfer_start=recipe_input_transfer_start,
            output_pickup_start=output_pickup_start,
            raw_prerequisite=prerequisite_evidence,
            fuel_prerequisite=fuel_prerequisite,
            fuel_transfer_start=fuel_transfer_start,
            buffer_build_start=buffer_build_start, buffer_fuel_start=buffer_fuel_start)
        if utility_power_start is not None:
            # This exact recompiled child is on the current power-consumer path.
            # It is an immediate prerequisite action, not predicted generation
            # or completed research, and it does not alter urgency scoring.
            scope = 'immediate'
            reasons.append('current_power_consumer_prerequisite')
        if plan.goal == 'stockpile_fuel' and all(
                step.action in {'walk_to_coal', 'mine_coal'} for step in plan.steps):
            highest = max((step.threshold for step in plan.steps
                           if step.action == 'mine_coal'), default=0)
            scope = 'immediate' if highest <= 5 else 'lookahead'
        outpost_kit_start = _outpost_kit_prerequisite_start_evidence(
            snapshot, catalog, plan)
        native_research_trigger_start = _native_research_trigger_start_evidence(
            snapshot, catalog, plan, gather_start)
        supplied_research_start = _supplied_research_start_evidence(snapshot, catalog, plan)
        science_transfer_start = _research_science_transfer_start_evidence(snapshot, catalog, plan)
        if science_transfer_start is not None:
            scope = 'immediate'
            reasons.append('current_native_technology_science_input')
        if supplied_research_start is not None:
            scope = 'immediate'
            reasons.append('current_powered_science_supplied_research_selection')
        passive = all(s.action in {'factory_wait', 'idle'} for s in plan.steps)
        result[plan.id] = {
            'work_scope': scope,
            'processed_units_basis': 'handling_volume_not_useful_production',
            'compiler_order': index, 'passive': passive, 'urgency': urgency,
            'reasons': sorted(set(reasons)), 'local_target': local_target,
            'travel_tiles_lower_bound': None if any(x.startswith('travel:') for x in unknown) else round(travel, 3),
            'actor_ticks_estimate': None if unknown else math.ceil(actor),
            'processed_units': quantities, 'material_costs': costs,
            **({'craft_handling_forecasts': craft_handling_forecasts}
               if craft_handling_forecasts else {}),
            'delivers_or_crafts': sorted(outputs), 'unknowns': sorted(set(unknown)),
            'raw_prerequisite': prerequisite_evidence,
            'input_route_kit_parent_purpose': kit_parent_purpose,
            'gather_start_evidence': gather_start,
            'fuel_prerequisite': fuel_prerequisite,
            'fuel_transfer_start_evidence': fuel_transfer_start,
            'craft_start_evidence': craft_start,
            'buffer_component_prerequisite_start_evidence': _buffer_component_start_evidence(
                snapshot, catalog, plan, craft_start, output_pickup_start),
            'craft_dependency': craft_dependency,
            'local_target_completion_evidence': local_target_completion,
            'shared_bill_craft': shared_bill_craft,
            'placement_start_evidence': placement_start,
            'buffer_build_start_evidence': buffer_build_start,
            'buffer_fuel_start_evidence': buffer_fuel_start,
            'placement_dependency': placement_dependency,
            'utility_lab_research_dependency': utility_lab_dependency,
            'utility_power_prerequisite_start_evidence': utility_power_start,
            'recipe_input_transfer_start_evidence': recipe_input_transfer_start,
            'paid_service_input_start_evidence': _paid_service_input_start_evidence(snapshot, catalog, plan),
            'native_research_trigger_start_evidence': native_research_trigger_start,
            'supplied_research_start_evidence': supplied_research_start,
            'research_science_transfer_start_evidence': science_transfer_start,
            'outpost_kit_prerequisite_start_evidence': outpost_kit_start,
            'direct_alternative_parent_demand_start_evidence': direct_alternative_start,
            'output_pickup_start_evidence': output_pickup_start,
            'research_deadline_tick': min((row['deadline_tick'] for row in schedules
                if row['item'] in outputs and row['deadline_tick'] is not None), default=None),
            'requires_investment': any(s.action in {'factory_place', 'factory_connect',
                                      'factory_buffer_build', 'factory_input_build', 'factory_solid_build'} for s in plan.steps),
            'estimate_basis': 'native_observation_and_catalog_with_declared_policy_heuristics',
        }
        service_output_start = _paid_service_output_start_evidence(snapshot, catalog, plan)
        if service_output_start is not None:
            result[plan.id]['paid_service_output_start_evidence'] = service_output_start
        current_craft_demand = craft_demand(snapshot, catalog, plan)
        if current_craft_demand is not None:
            result[plan.id]['craft_recipe_demand'] = current_craft_demand
        from .buffer_demand import purpose as buffer_component_purpose
        component_purpose = buffer_component_purpose(snapshot, catalog, plan)
        if component_purpose is not None:
            result[plan.id]['buffer_component_parent_purpose'] = component_purpose
        from .buffer_demand import commissioning_purpose
        commissioning = commissioning_purpose(snapshot, catalog, plan)
        if commissioning is not None:
            result[plan.id]['buffer_commissioning_parent_purpose'] = commissioning
        if bootstrap_pickup_start is not None:
            result[plan.id]['bootstrap_output_pickup_start_evidence'] = bootstrap_pickup_start
        construction = machine_construction_prerequisite(
            snapshot, catalog, plan, craft_start, placement_start)
        if construction is not None:
            result[plan.id]['machine_construction_prerequisite'] = construction
        if prerequisite_evidence is not None:
            machine_prerequisite = raw_machine_prerequisite(snapshot, catalog, plan)
            if machine_prerequisite is not None:
                result[plan.id]['raw_machine_prerequisite'] = machine_prerequisite
        if isinstance((plan.materials or {}).get('direct_alternative_to_proposed_outpost'), dict):
            result[plan.id]['work_scope_provenance'] = {
                'compiled_scope': compiled_work_scope,
                'qualified_current_scope': 'immediate' if direct_alternative_start is not None else None,
                'basis': (direct_alternative_start['basis'] if direct_alternative_start is not None
                          else 'current_parent_demand_not_verified'),
            }
        if scope == 'lookahead' and plan.goal == 'stockpile_fuel':
            result[plan.id]['current_prerequisite_units'] = max(
                0, min(quantities, 5 - snapshot.inventory.get('coal', 0)))
    # A nearer bulk pickup of the same currently needed material is not
    # discretionary stockpiling. Compare its cost using only the current need,
    # not all extra handled units. This is evidence/ranking, never permission.
    def acquired_item(plan):
        if len(plan.steps) != 1 or plan.steps[0].action not in {'factory_extract', 'factory_gather'}:
            return None
        step = plan.steps[0]
        parameters = step.parameters or {}
        item = parameters.get('item', parameters.get('resource', step.item))
        return item if isinstance(item, str) and item else None

    required = {}
    for plan in plans:
        item, row = acquired_item(plan), result[plan.id]
        if item and row['work_scope'] == 'immediate' and row['processed_units'] > 0:
            required[item] = max(required.get(item, 0), row['processed_units'])
    for plan in plans:
        item, row = acquired_item(plan), result[plan.id]
        if item in required and row['work_scope'] == 'lookahead' and row['processed_units'] > 0:
            row['work_scope'] = 'shared_prerequisite'
            row['current_prerequisite_units'] = min(required[item], row['processed_units'])
            row['reasons'].append('same_item_current_prerequisite')
    for plan in plans:
        purpose = _candidate_local_raw_demand(snapshot, catalog, plans, plan, result)
        if purpose is not None:
            result[plan.id]["candidate_local_raw_demand"] = purpose
    return result


def ranking_key(row: dict) -> tuple:
    """Urgency and productive work precede known actor cost; ties stay stable.

    Unknown cost is never zero-cost work. Among equally urgent options, a
    current prerequisite precedes discretionary lookahead. Handling volume is
    only a tie-breaker within a demand class, not proof of useful production.
    This is a scheduling heuristic, not a success probability or calibrated value.
    """
    duration = row['actor_ticks_estimate']
    amount = max(1, row.get('current_prerequisite_units', row['processed_units']))
    return (row['passive'], -row['urgency'], row.get('work_scope') == 'lookahead', duration is None,
            (duration / amount) if duration is not None else 0,
            row['compiler_order'])


def defer_gather_until_bill_craft(plans, support: dict, snapshot, memory):
    """Defer one independent raw gather until a complete paid craft can start.

    This changes only the JEV choice frontier. Native craft admission, its
    output lock, and a fresh observation still own any later gathering.
    """
    if (len(plans) != 2 or memory.status != 'running'
            or any(getattr(memory, name, None) is not None for name in (
                'pending', 'attempt', 'active_plan', 'transfer_recovery',
                'background_job', 'background_attempt', 'capital_investment',
                'solid_funding', 'coal_funding'))
            or any(getattr(memory, name, None) for name in (
                'reservations', 'solid_commitments', 'coal_commitments',
                'output_commitments', 'input_commitments', 'outpost_commitments',
                'successor_projects'))):
        return plans, None
    connector = getattr(memory, 'connector_ownership', None)
    if connector is not None and (not isinstance(connector, dict)
                                  or connector.get('routes') != {}):
        return plans, None
    from ..craft_jobs import permits_locked_outputs

    rows = support.get('candidate_evidence', {})
    crafts = [p for p in plans if len(p.steps) == 1
              and p.steps[0].action == 'factory_craft_job']
    gathers = [p for p in plans if len(p.steps) == 1
               and p.steps[0].action == 'factory_gather']
    if len(crafts) != 1 or len(gathers) != 1:
        return plans, None
    craft, gather = crafts[0], gathers[0]
    craft_row, gather_row = rows.get(craft.id), rows.get(gather.id)
    if not isinstance(craft_row, dict) or not isinstance(gather_row, dict):
        return plans, None
    bill, start = craft_row.get('shared_bill_craft'), craft_row.get('craft_start_evidence')
    raw, gather_start = gather_row.get('raw_prerequisite'), gather_row.get('gather_start_evidence')
    if not all(isinstance(value, dict) for value in (bill, start, raw, gather_start)):
        return plans, None
    craft_step, gather_step = craft.steps[0], gather.steps[0]
    path = raw.get('planner_item_path')
    needed, produced = bill.get('unfilled_bill_units'), bill.get('expected_products_after_native_verification')
    outputs = start.get('expected_products_after_native_verification')
    if (craft_row.get('work_scope') != 'lookahead'
            or gather_row.get('work_scope') != 'immediate'
            or any(row.get('unknowns') != [] or type(row.get('urgency')) is not int
                   or row['urgency'] != 0 or row.get('research_deadline_tick') is not None
                   for row in (craft_row, gather_row))
            or bill.get('observed_tick') != snapshot.tick
            or bill.get('forecast_is_not_paid_stock_or_completed_output') is not True
            or bill.get('background_overlap_requires_native_admission') is not True
            or not isinstance(bill.get('local_target_item'), str)
            or not bill['local_target_item']
            or bill.get('craft_item') != craft_step.item
            or type(needed) is not int or needed <= 0
            or type(produced) is not int or produced < needed
            or not isinstance(outputs, dict) or not 1 <= len(outputs) <= 32
            or outputs.get(craft_step.item) != produced
            or any(not isinstance(item, str) or not item or type(count) is not int
                   or count <= 0 for item, count in outputs.items())
            or start.get('observed_tick') != snapshot.tick
            or start.get('native_recipe') != (craft_step.parameters or {}).get('recipe')
            or not all(start.get(key) is True for key in (
                'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                'crafting_queue_empty', 'craft_job_protocol_ready',
                'native_receipt_required_for_completion'))
            or not isinstance((craft_step.parameters or {}).get('receipt'), str)
            or not craft_step.parameters['receipt']
            or raw.get('observed_tick') != snapshot.tick
            or not isinstance(path, list) or not 2 <= len(path) <= 32
            or path[0] != bill['local_target_item']
            or path[-1] != (gather_step.parameters or {}).get('resource')
            or gather_start.get('resource_in_current_observation') is not True
            or gather_start.get('fair_target_identity_observed') is not True
            or gather_start.get('target_inventory_after_this_step') != gather_step.threshold
            or not craft_step.allowed(snapshot) or craft_step.satisfied(snapshot)
            or not gather_step.allowed(snapshot) or gather_step.satisfied(snapshot)
            or not permits_locked_outputs(gather_step, set(outputs))):
        return plans, None
    return [craft], 'complete_current_bill_craft_before_independent_raw_gather'


def add_craft_overlap_evidence(snapshot, plans, rows):
    """Explain observed craft/gather pairs without changing their choice frontier.

    The craft's inputs are present now; they are not paid until native admission.
    Gathering may overlap only after that admission and a fresh observation.
    """
    if not 2 <= len(plans) <= 32 or len({plan.id for plan in plans}) != len(plans):
        return
    crafts = [p for p in plans if len(p.steps) == 1
              and p.steps[0].action == 'factory_craft_job']
    gathers = [p for p in plans if len(p.steps) == 1
               and p.steps[0].action == 'factory_gather']
    if not crafts or len(gathers) != 1:
        return
    for craft in crafts:
        _add_craft_gather_pair(snapshot, craft, gathers[0], rows)


def _add_craft_gather_pair(snapshot, craft, gather, rows):
    from ..craft_jobs import permits_locked_outputs

    cr, gr = rows.get(craft.id), rows.get(gather.id)
    if not isinstance(cr, dict) or not isinstance(gr, dict):
        return
    start = cr.get('craft_start_evidence')
    raw, gather_start = gr.get('raw_prerequisite'), gr.get('gather_start_evidence')
    if not all(isinstance(value, dict) for value in (start, raw, gather_start)):
        return
    cs, gs = craft.steps[0], gather.steps[0]
    outputs = start.get('expected_products_after_native_verification')
    gp = raw.get('planner_item_path')
    target = cr.get('local_target')
    if (not _current_overlap_craft(snapshot, craft, cr)
            or gr.get('work_scope') != 'immediate'
            or any(row.get('unknowns') != []
            or type(row.get('urgency')) is not int or row['urgency'] != 0
            or row.get('research_deadline_tick') is not None for row in (cr, gr))
            or not isinstance(target, dict) or target != gr.get('local_target')
            or not isinstance(target.get('item'), str) or not target['item']
            or any(value.get('observed_tick') != snapshot.tick
                   for value in (start, raw, gather_start))
            or not isinstance(gp, list) or not 2 <= len(gp) <= 32
            or gp[0] != target['item']
            or gp[-1] != (gs.parameters or {}).get('resource')
            or cs.effect != 'craft_job_complete' or gs.effect != 'inventory'
            or not isinstance((cs.parameters or {}).get('receipt'), str)
            or not cs.parameters['receipt']
            or start.get('native_recipe') != (cs.parameters or {}).get('recipe')
            or not all(start.get(key) is True for key in (
                'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                'crafting_queue_empty', 'craft_job_protocol_ready',
                'native_receipt_required_for_completion'))
            or not isinstance(outputs, dict) or len(outputs) != 1 or cs.item not in outputs
            or any(type(count) is not int or count <= 0 for count in outputs.values())
            or not cs.costs or gs.costs
            or set(cs.costs) & set(outputs)
            or not all(type(count) is int and count > 0
                       and snapshot.inventory.get(item, 0) >= count
                       for item, count in cs.costs.items())
            or gather_start.get('resource_in_current_observation') is not True
            or gather_start.get('fair_target_identity_observed') is not True
            or gather_start.get('session_id') != snapshot.session_id
            or gather_start.get('target_inventory_after_this_step') != gs.threshold
            or not permits_locked_outputs(gs, set(outputs))):
        return
    cr['independent_gather_overlap'] = {
        'observed_tick': snapshot.tick,
        'session_id': snapshot.session_id,
        'basis': 'current_craft_start_and_independent_raw_gather_output_lock',
        'craft_plan_id': craft.id,
        'gather_plan_id': gather.id,
        'local_target': dict(target),
        'inputs_available_now_not_yet_paid': dict(cs.costs),
        'expected_outputs_locked_until_native_receipt': dict(outputs),
        'gather_consumes_no_inventory': True,
        'gather_does_not_touch_locked_outputs': True,
        'ordering_effect': (
            'Starting this craft first can let its native crafting queue run during '
            'later independent gathering. Gathering first leaves this craft unstarted '
            'for the duration of that gather. Both advance the same current target; '
            'this is an overlap opportunity, not a promise of elapsed time saved.'),
        'requires_native_admission_then_fresh_gather_observation': True,
        'future_gather_selection_and_all_judgment_gates_remain_required': True,
        'does_not_authorize_either_action_or_prove_completion': True,
    }


def _current_overlap_craft(snapshot, craft, row):
    """Accept a current recursive dependency or complete current material bill.

    Shared-bill alternatives keep their compiler scope and rank. They are useful
    current ingredients rather than discretionary stock for a future target.
    """
    dependency, bill = row.get('craft_dependency'), row.get('shared_bill_craft')
    local, start = row.get('local_target'), row.get('craft_start_evidence')
    if not isinstance(local, dict) or not isinstance(start, dict):
        return False
    step = craft.steps[0]
    if row.get('work_scope') == 'immediate' and isinstance(dependency, dict):
        path = dependency.get('planner_item_path')
        return (dependency.get('observed_tick') == snapshot.tick
                and isinstance(path, list) and 2 <= len(path) <= 32
                and path[0] == local.get('item') and path[-1] == step.item)
    if row.get('work_scope') != 'lookahead' or not isinstance(bill, dict):
        return False
    # horizon_demands can mix the local target with active-research science.
    # Without a separate current-only bill proof, do not attribute those extra
    # ingredients to this gather's local target.
    if snapshot.factory.get('research') not in (None, ''):
        return False
    carried, target = bill.get('inventory_now'), bill.get('bounded_bill_inventory_target')
    needed, produced = bill.get('unfilled_bill_units'), bill.get('expected_products_after_native_verification')
    outputs = start.get('expected_products_after_native_verification')
    return (bill.get('observed_tick') == snapshot.tick
        and bill.get('basis') == 'current_catalog_shared_material_bill_and_native_recipe'
        and bill.get('local_target_item') == local.get('item')
        and bill.get('craft_item') == step.item
        and bill.get('forecast_is_not_paid_stock_or_completed_output') is True
        and bill.get('background_overlap_requires_native_admission') is True
        and type(carried) is int and type(target) is int and 0 <= carried < target
        and snapshot.inventory.get(step.item, 0) == carried
        and type(needed) is int and needed == target - carried
        and type(produced) is int and produced >= needed
        and isinstance(outputs, dict) and outputs.get(step.item) == produced)


def add_current_raw_bill_evidence(snapshot, catalog, plans, rows):
    """Explain a current raw shortfall without treating forecast stock as paid.

    Recompute only the named local material target, not research horizons, fuel,
    machine capacity, or a completion forecast. A collectible output can reduce
    this bill but still needs its own native collection and later judgments.
    """
    from .demand import SupplyLedger
    from .factory import RAW_ITEMS

    job = snapshot.factory.get('craft_job')
    if (snapshot.world_kind != 'fle' or snapshot.game_version != catalog.version
            or (job is not None and (not isinstance(job, dict) or job.get('status') != 'completed'))
            or type(snapshot.tick) is not int or snapshot.tick < 0
            or not snapshot.session_id or snapshot.factory.get('crafting_queue') != 0):
        return
    for plan in plans:
        row = rows.get(plan.id, {})
        local, start, raw = (row.get(key) for key in
                            ('local_target', 'gather_start_evidence', 'raw_prerequisite'))
        if (len(plan.steps) != 1 or row.get('work_scope') != 'immediate'
                or row.get('unknowns') != [] or not isinstance(local, dict)
                or not isinstance(start, dict) or not isinstance(raw, dict)):
            continue
        step = plan.steps[0]
        item, target = local.get('item'), local.get('inventory_target')
        current = snapshot.inventory.get(step.item, 0)
        if (step.action != 'factory_gather' or step.effect != 'inventory'
                or step.item not in RAW_ITEMS - {'wood'} or step.costs not in (None, {})
                or not isinstance(item, str) or not item or item == step.item
                or type(target) is not int or not 1 <= target <= 200
                or local.get('ultimate_goal') != plan.goal
                or type(current) is not int or current < 0
                or type(step.threshold) is not int or step.threshold <= current
                or (step.parameters or {}) != {'resource': step.item, 'quantity': step.threshold-current}
                or start.get('observed_tick') != snapshot.tick
                or start.get('session_id') != snapshot.session_id
                or start.get('resource_inventory_now') != current
                or start.get('target_inventory_after_this_step') != step.threshold
                or start.get('resource_in_current_observation') is not True
                or start.get('fair_target_identity_observed') is not True
                or raw.get('observed_tick') != snapshot.tick
                or not _current_item_dependency_path(snapshot, catalog,
                    raw.get('planner_item_path'), item, step.item)):
            continue
        try:
            ledger = SupplyLedger.capture(snapshot, catalog)
            # Keep this explanation limited to already observed item supply.
            # Pending craft ownership and production forecasts need other proofs.
            if (ledger.reserved or any(ledger.queued_output.values())
                    or any(ledger.in_flight_output.values())):
                continue
            carried_bill = catalog.material_plan(item, target, ledger.carried, snapshot.researched or [])
            stock = dict(ledger.carried)
            for name, count in ledger.collectible.items():
                stock[name] = stock.get(name, 0) + count
            collected_bill = catalog.material_plan(item, target, stock, snapshot.researched or [])
            batches = collected_bill.batches
            if len(batches) > 32 or len(carried_bill.shortages) > 16:
                continue
            # This is a solid deterministic material bill, not a fluid/coproduct solver.
            for name in set(batches) | set(carried_bill.batches):
                recipe = catalog.recipes[name]
                if (len(recipe['products']) != 1 or any(
                        entry.get('type') != 'item' or not _finite(entry.get('amount'))
                        or entry['amount'] <= 0 or not float(entry['amount']).is_integer()
                        for entry in recipe['ingredients'] + recipe['products'])
                        or recipe['products'][0].get('probability', 1) != 1):
                    raise ValueError('Unsupported material bill')
            shortage = collected_bill.shortages.get(step.item, 0)
            if not _finite(shortage) or shortage <= 0:
                continue
        except (ArithmeticError, KeyError, TypeError, ValueError):
            continue
        row['current_raw_material_bill'] = {
            'observed_tick': snapshot.tick,
            'basis': 'recomputed_native_catalog_local_target_only',
            'local_target': dict(local),
            'carried_only_shortages': dict(carried_bill.shortages),
            'collectible_outputs_not_carried_or_paid': dict(ledger.collectible),
            'shortages_if_observed_outputs_are_collected': dict(collected_bill.shortages),
            'gather_resource': step.item,
            'planned_inventory_increase': step.threshold - current,
            'remaining_resource_shortfall_if_gather_and_collection_verify': max(
                0, shortage - (step.threshold-current)),
            'remaining_recipe_batches_after_supply_credit': dict(batches),
            'limits': ('Material arithmetic only. Collection, gathering, conversion, fuel, '
                       'machines and completion still require native checks and later JEV '
                       'decisions. This does not choose or authorize an action.'),
        }


def candidate_target_objective(plan, row, tick, goal, *, allow_power_promotion=False):
    """Return a candidate's exact, current local target when its producer binds it."""
    materials = plan.materials if isinstance(plan.materials, dict) else {}
    target = materials.get('local_objective')
    intent = materials.get('work_intent')
    if (not isinstance(target, dict) or not isinstance(target.get('ultimate_goal'), str)
            or target['ultimate_goal'] != goal or plan.goal != goal
            or not isinstance(intent, dict)
            or type(intent.get('observed_tick')) is not int
            or intent.get('observed_tick') != tick
            or intent.get('scope') not in {'immediate', 'lookahead'}
            or not isinstance(row, dict)
            or row.get('local_target') != target):
        return None
    if set(target) == {'item', 'inventory_target', 'ultimate_goal'}:
        promoted_power = False
        if (allow_power_promotion and intent['scope'] == 'lookahead'
                and row.get('work_scope') == 'immediate'
                and isinstance(row.get('reasons'), list)
                and 'current_power_consumer_prerequisite' in row.get('reasons', [])):
            from ..judgments import _qualified_utility_power_dependency
            promoted_power = _qualified_utility_power_dependency(plan, row, tick)
        if (not isinstance(target.get('item'), str)
                or not target['item'] or len(target['item']) > 200
                or type(target.get('inventory_target')) is not int
                or target['inventory_target'] <= 0
                or (intent['scope'] == 'immediate'
                    and row.get('work_scope') != 'immediate')
                or (intent['scope'] == 'lookahead'
                    and row.get('work_scope') not in {'lookahead', 'shared_prerequisite'}
                    and not promoted_power)):
            return None
        return deepcopy(target)
    if set(target) != {
            'kind', 'ultimate_goal', 'primary_target', 'immediate_prerequisite',
            'observed_tick', 'basis', 'later_power_and_research_need_native_verification'}:
        return None
    technology_target = target.get('primary_target')
    dependency = row.get('utility_lab_research_dependency')
    technology = (technology_target.get('technology')
                  if isinstance(technology_target, dict) else None)
    if (target.get('kind') != 'research_prerequisite'
            or type(target.get('observed_tick')) is not int
            or target.get('observed_tick') != tick
            or target.get('immediate_prerequisite') != 'utility:lab'
            or target.get('basis') != 'current_capability_research_plan'
            or target.get('later_power_and_research_need_native_verification') is not True
            or not isinstance(technology_target, dict)
            or set(technology_target) != {'kind', 'technology'}
            or technology_target.get('kind') != 'native_technology'
            or not isinstance(technology, str) or not technology or len(technology) > 200
            or intent['scope'] != 'immediate' or row.get('work_scope') != 'immediate'
            or not isinstance(dependency, dict)
            or dependency.get('observed_tick') != tick
            or dependency.get('technology') != technology
            or dependency.get('basis') !=
                'same_tick_capability_research_plan_and_paid_lab_prerequisite'
            or any(dependency.get(key) is not True for key in (
                'technology_not_researched_now', 'technology_unlocks_basic_assembler',
                'current_research_idle', 'current_technology_prerequisites_satisfied',
                'lab_required_by_native_research_walk', 'utility_lab_absent_now',
                'player_connected_and_bound_now', 'crafting_queue_empty_now',
                'placement_site_clearance_unknown_until_dispatch',
                'travel_and_arrival_unverified',
                'existing_native_action_performs_bounded_search_and_fresh_build_checks',
                'native_build_result_and_fresh_role_postcondition_required',
                'lab_power_and_research_require_later_native_verification'))
            or dependency.get('native_placement_site_preflight_performed') is not False
            or type(dependency.get('paid_lab_in_inventory_now')) is not int
            or dependency['paid_lab_in_inventory_now'] < 1):
        return None
    return deepcopy(target)


def scheduling_context(snapshot, catalog, plans, goal: str) -> dict:
    evidence = candidate_evidence(snapshot, catalog, plans)
    add_craft_overlap_evidence(snapshot, plans, evidence)
    add_current_raw_bill_evidence(snapshot, catalog, plans, evidence)
    primary = (plans[0].materials or {}).get('local_objective') if plans else None
    if primary is None and goal == 'stockpile_fuel':
        primary = {'item': 'coal', 'inventory_target': 5, 'ultimate_goal': goal}
    instruction = ('Gather the observed five-coal construction buffer; a bounded '
                   'coal harvest advances this prerequisite.' if goal == 'stockpile_fuel' else
                   'Prevent observed starvation, remove the next production blocker, '
                   'or do useful independent work while production runs. '
                   'A single useful action need not complete the ultimate goal. '
                   'Immediate prerequisites precede discretionary lookahead at equal urgency; '
                   'moving more items is not evidence of more useful production.')
    first_evidence = evidence.get(plans[0].id, {}) if plans else {}
    if isinstance(first_evidence.get('buffer_component_parent_purpose'), dict):
        instruction = (
            'Acquire the next missing component for the currently paid partial output buffer. '
            'Its separate parent recipe demand remains recorded in buffer_component_parent_purpose. '
            'A bounded input or intermediate can advance this component goal; component placement, '
            'buffer flow and the parent production output still require native verification.')
    if plans and 'input_route_kit_prerequisite' in (plans[0].materials or {}):
        primary = first_evidence.get('local_target')
        kit_purpose = first_evidence.get('input_route_kit_parent_purpose')
        if isinstance(kit_purpose, dict):
            instruction = (
                'Acquire the bounded input-route kit target shown here. Its '
                'same-tick parent demand remains a separate conditional purpose; '
                'a raw ingredient is recipe input progress, not a completed kit, '
                'route flow, science output, or ultimate-goal completion.')
    capital = (plans[0].materials or {}).get('capital_investment') if plans else None
    if isinstance(capital, dict) and capital.get('stage') == 'kit':
        from .capital import validate_spec
        try:
            validate_spec(capital['spec'], catalog, snapshot.researched or [])
            if (capital.get('observed_tick') == snapshot.tick
                    and primary == {'item': capital['spec']['machine'],
                                    'inventory_target': 1, 'ultimate_goal': goal}):
                instruction = (
                    'Evaluate the bounded construction-kit action for the proposed machine. '
                    'Its eventual production purpose and payback are separate planner '
                    'estimates, not native evidence of completed production. A recipe '
                    'input or pickup can advance the kit without completing the machine; '
                    'judge independently whether that contribution is useful. Placement, '
                    'power, configuration and output still need native verification.')
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    lab_dependency = (first_evidence.get('utility_lab_research_dependency')
                      if isinstance(first_evidence, dict) else None)
    if isinstance(lab_dependency, dict):
        instruction = (
            f"Place the paid utility lab as the current immediate prerequisite for "
            f"starting {lab_dependency['technology']} research. Placement-site clearance, "
            "travel, lab power, and research completion remain unverified and require "
            "the existing native action and later observations.")
    candidate_targets = {}
    for plan in plans:
        target = candidate_target_objective(
            plan, evidence.get(plan.id), snapshot.tick, goal, allow_power_promotion=True)
        if target is not None:
            candidate_targets[plan.id] = target
    target_identities = {
        json.dumps(target, sort_keys=True, ensure_ascii=False, allow_nan=False)
        for target in candidate_targets.values()
    }
    scoped_frontier = any('local_objective' in (plan.materials or {}) for plan in plans)
    candidate_target_mode = (scoped_frontier and (
        len(candidate_targets) != len(plans) or len(target_identities) != 1))
    if candidate_target_mode:
        primary = None
        instruction = (
            'The candidates may have different current local targets. Judge each plan only '
            'against its matching `candidate_targets` entry when that entry exactly matches '
            'the current plan and candidate evidence. No single `primary_target` applies to '
            'all candidates; a missing or mismatched entry does not establish a target. '
            'Native preconditions, receipts and fresh postconditions remain authoritative.')
    return {
        'local_objective': {
            'kind': 'stockpile_fuel' if goal == 'stockpile_fuel' else 'ready_production',
            'ultimate_goal': goal,
            'primary_target': deepcopy(primary),
            'instruction': instruction,
            'success_authority': 'unchanged native step and goal predicates, never model scores',
            **({'candidate_targets': candidate_targets} if candidate_target_mode else {}),
        },
        'candidate_evidence': evidence,
        'deterministic_ranking': sorted(evidence, key=lambda key: ranking_key(evidence[key])),
        'selection_contract': {'schema': 1, 'observed_tick': snapshot.tick,
                               **({'candidate_objective_binding': 3} if scoped_frontier else {}),
                               'heuristics_are_not_native_timing_measurements': True},
    }
