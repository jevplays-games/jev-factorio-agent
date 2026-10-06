"""Current recursive recipe-input accounting, never a completed-output proof."""
from copy import deepcopy
import math
import json


def dependency_chain(snapshot, catalog, local, path):
    if (not isinstance(local, dict) or not isinstance(path, list) or not 2 <= len(path) <= 32
            or path[0] != local.get('item') or len(set(path)) != len(path)
            or type(local.get('inventory_target')) is not int or local['inventory_target'] <= 0):
        raise ValueError('Invalid local dependency root')
    amount = local['inventory_target']
    edges = []
    for product, ingredient in zip(path, path[1:]):
        recipe = catalog.recipe_for(product)
        if (not isinstance(recipe, dict) or type(recipe.get('enabled')) is not bool
                or type(recipe.get('hidden', False)) is not bool or recipe.get('hidden', False)
                or not catalog.enabled(recipe, snapshot.researched or [])
                or not isinstance(recipe.get('category'), str)):
            raise ValueError('Invalid current recipe')
        for key in ('ingredients', 'products'):
            if (not isinstance(recipe.get(key), list) or not recipe[key]
                    or any(not isinstance(row, dict) or not isinstance(row.get('name'), str)
                        or row.get('type') != 'item' or type(row.get('amount')) not in (int, float)
                        or not math.isfinite(row['amount']) or row['amount'] <= 0
                        or type(row.get('probability', 1)) not in (int, float)
                        or not math.isfinite(row.get('probability', 1))
                        or row.get('probability', 1) != 1 for row in recipe[key])):
                raise ValueError('Invalid deterministic recipe arithmetic')
        outputs = [row['amount'] for row in recipe['products'] if row['name'] == product]
        inputs = [row['amount'] for row in recipe['ingredients'] if row['name'] == ingredient]
        if len(outputs) != 1 or len(inputs) != 1:
            raise ValueError('Ambiguous selected recipe edge')
        carried = snapshot.inventory.get(product, 0)
        if type(carried) is not int or not 0 <= carried < amount:
            raise ValueError('No current recursive product deficit')
        batches = min(20, math.ceil((amount - carried) / outputs[0]))
        hand = bool(catalog.hand_categories.get(recipe['category']))
        limits = {}
        buffered = in_flight = 0
        if not hand:
            for row in recipe['ingredients']:
                stack = catalog.stack_sizes.get(row['name'], 200)
                if type(stack) is not int or stack <= 0:
                    raise ValueError('Invalid stack bound')
                limits[row['name']] = stack
                batches = min(batches, max(1, math.floor(stack / row['amount'])))
            machine = snapshot.factory.get('entities', {}).get('recipe:' + recipe['name'])
            if not isinstance(machine, dict):
                raise ValueError('Missing current recipe machine')
            buffered = machine.get('input', {}).get(ingredient, 0)
            in_flight = inputs[0] if machine.get('crafting') else 0
            if type(buffered) not in (int, float) or not math.isfinite(buffered) or buffered < 0:
                raise ValueError('Invalid current machine input')
        child = max(0, math.ceil(inputs[0] * batches - buffered - in_flight))
        if child <= 0:
            raise ValueError('Selected input already supplied')
        edges.append({'product': product, 'ingredient': ingredient,
            'product_inventory_target': amount, 'carried_product': carried,
            'recipe': deepcopy(recipe), 'hand_category': hand, 'stack_limits': limits,
            'batches': batches, 'buffered': buffered, 'in_flight': in_flight,
            'input_inventory_target': child})
        amount = child
    return {'schema': 1, 'local_target': deepcopy(local), 'edges': edges,
        'raw_input_inventory_target': amount,
        'scope': 'selected_branch_not_full_target_bill',
        'later_completion_unverified': True}


def validate_dependency_chain(facts, local, path, witness, raw_required):
    try:
        from types import SimpleNamespace
        from .catalog import Catalog
        if not isinstance(witness, dict) or not isinstance(witness.get('edges'), list):
            return False
        recipes = {}; hand = {}; stacks = {}
        for edge in witness['edges']:
            if not isinstance(edge, dict) or type(edge.get('hand_category')) is not bool:
                return False
            recipe = edge['recipe']
            if not isinstance(recipe, dict) or not isinstance(edge.get('stack_limits'), dict):
                return False
            name, category = recipe['name'], recipe['category']
            if name in recipes and recipes[name] != recipe:
                return False
            if category in hand and hand[category] != edge['hand_category']:
                return False
            recipes[name] = recipe; hand[category] = edge['hand_category']
            for item, size in edge['stack_limits'].items():
                if item in stacks and stacks[item] != size:
                    return False
                stacks[item] = size
        observed = facts['factory'].get('recipe_dependency_catalog')
        if (not isinstance(observed, dict) or type(observed.get('tick')) is not int
                or observed['tick'] != facts.get('tick') or observed.get('session_id') != facts.get('session_id')
                or observed.get('version') != facts.get('game_version') or observed.get('schema') != 1
                or type(observed.get('schema')) is not int):
            return False
        if any(json.dumps(observed.get('recipes', {}).get(name), sort_keys=True, allow_nan=False) != json.dumps(recipe, sort_keys=True, allow_nan=False) for name, recipe in recipes.items()):
            return False
        if any(observed.get('hand_categories', {}).get(category) is not value for category, value in hand.items()):
            return False
        if any(type(observed.get('stack_sizes', {}).get(item)) is not int
                or observed['stack_sizes'][item] != size for item, size in stacks.items()):
            return False
        catalog = Catalog(observed['version'], observed['recipes'], {}, {}, observed['hand_categories'], observed['stack_sizes'])
        snapshot = SimpleNamespace(inventory=facts['inventory'], factory=facts['factory'],
            researched=facts.get('researched', []))
        return (json.dumps(dependency_chain(snapshot, catalog, local, path), sort_keys=True, allow_nan=False) == json.dumps(witness, sort_keys=True, allow_nan=False)
            and witness['raw_input_inventory_target'] == raw_required)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return False


def catalog_projection(snapshot, catalog, plans):
    """Independent current Catalog subset; never copied from candidate witnesses."""
    recipes = {}; hand = {}; stacks = {}; machines = {}; route_state = None
    for plan in plans:
        component = ((plan.materials or {}).get('buffer_component_demand')
                     or (plan.materials or {}).get('buffer_commissioning_demand'))
        if isinstance(component, dict):
            for product in component.get('parent_item_path', [])[:-1]:
                recipe = catalog.recipe_for(product)
                recipes[recipe['name']] = deepcopy(recipe)
                hand[recipe['category']] = bool(catalog.hand_categories.get(recipe['category']))
                if not hand[recipe['category']]:
                    for entry in recipe['ingredients']:
                        stacks[entry['name']] = catalog.stack_sizes.get(entry['name'], 200)
        if (isinstance((plan.materials or {}).get('input_route_kit_prerequisite'), dict)
                and isinstance((plan.materials or {}).get('recipe_input_transfer'), dict)
                and len(plans) == 2
                and any(isinstance((other.materials or {}).get('bootstrap_output_pickup'), dict)
                        for other in plans if other.id != plan.id)):
            reserve_recipe = catalog.recipes.get('logistic-science-pack')
            if reserve_recipe is not None:
                recipes['logistic-science-pack'] = deepcopy(reserve_recipe)
            from ..input_routes import sources
            # Ordinary model facts compact route geometry/receipts. Keep the
            # independently observed route contract needed by this comparison.
            source = plan.materials['input_route_kit_prerequisite'].get('source')
            try:
                observed_routes = sources(snapshot)
            except (KeyError, TypeError, ValueError, AttributeError):
                observed_routes = {}  # Invalid telemetry cannot qualify a comparison.
            if source in observed_routes:
                route_state = {'protocol': snapshot.factory['input_routes']['protocol'],
                    'session_id': snapshot.session_id, 'tick': snapshot.tick,
                    'sources': {source: deepcopy(observed_routes[source])}}
        marker = ((plan.materials or {}).get('bootstrap_output_pickup')
                  or (plan.materials or {}).get('recipe_input_transfer')
                  or (plan.materials or {}).get('output_pickup')
                  or (plan.materials or {}).get('craft_dependency'))
        if not isinstance(marker, dict):continue
        path = marker.get('planner_item_path')
        if not isinstance(path, list):continue
        if 'craft_dependency' in (plan.materials or {}):
            craft_recipe = catalog.recipes.get(marker.get('recipe'))
            if craft_recipe is not None:
                recipes[craft_recipe['name']] = deepcopy(craft_recipe)
                category = craft_recipe['category']
                hand[category] = bool(catalog.hand_categories.get(category))
        if 'output_pickup' in (plan.materials or {}):
            role = marker.get('source_role', '')
            if role.startswith('output-chest:'):
                from .buffer_pickup import identity
                owner = identity(snapshot, role, marker.get('item'))
                if owner is not None:
                    role = owner['source_role']
                    machine = snapshot.factory.get('entities', {}).get(role, {})
                    if machine.get('name') in catalog.machines:
                        machines[machine['name']] = deepcopy(catalog.machines[machine['name']])
            source_recipe = catalog.recipes.get(role.removeprefix('recipe:'))
            if source_recipe is not None:
                recipes[source_recipe['name']] = deepcopy(source_recipe)
        if any(key in (plan.materials or {}) for key in ('recipe_input_transfer', 'output_pickup')):
            machine = snapshot.factory.get('entities', {}).get(marker.get('source_role'), {})
            name = machine.get('name')
            if name in catalog.machines:machines[name] = deepcopy(catalog.machines[name])
        for product in path[:-1]:
            recipe = catalog.recipe_for(product)
            recipes[recipe['name']] = deepcopy(recipe)
            hand[recipe['category']] = bool(catalog.hand_categories.get(recipe['category']))
            if not hand[recipe['category']]:
                for entry in recipe['ingredients']:
                    stacks[entry['name']] = catalog.stack_sizes.get(entry['name'], 200)
    return {'schema': 1, 'tick': snapshot.tick, 'session_id': snapshot.session_id,
        'version': catalog.version, 'recipes': recipes, 'hand_categories': hand, 'stack_sizes': stacks,
        **({'machines': machines} if machines else {}), **({'comparison_input_route': route_state} if route_state else {})}
