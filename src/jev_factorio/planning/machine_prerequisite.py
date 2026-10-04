"""Explain a missing production machine separately from recipe ingredients."""
from copy import deepcopy


def raw_machine_prerequisite(snapshot, catalog, plan):
    """Qualify one missing-machine edge in a current bounded raw-input path.

    A furnace is not an ingredient of iron plate. Validate both kinds of edge
    explicitly; an arbitrary planner path alone is not a dependency proof.
    """
    from .factory import FactoryPlanner

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
    edges, machine_edge = [], None
    try:
        for product, dependency in zip(path, path[1:]):
            recipe = catalog.recipe_for(product)
            if (recipe.get('hidden') or not catalog.enabled(recipe, snapshot.researched or [])
                    or not any(p.get('type') == 'item' and p.get('name') == product
                               and type(p.get('amount')) in (int, float) and p['amount'] > 0
                               and p.get('probability', 1) == 1
                               for p in recipe.get('products', []))):
                return None
            ingredient = next((r for r in recipe.get('ingredients', [])
                               if r.get('type') == 'item' and r.get('name') == dependency
                               and type(r.get('amount')) in (int, float) and r['amount'] > 0), None)
            if ingredient is not None:
                edges.append({'kind': 'recipe_input', 'product': product,
                              'input': dependency, 'recipe': deepcopy(recipe)})
                continue
            role = 'recipe:' + recipe['name']
            # The base planner selects this machine for a new producer. Existing
            # machines, carried machines and handcraftable recipes contradict
            # this particular missing-machine acquisition explanation.
            if (machine_edge is not None or role in snapshot.factory.get('entities', {})
                    or snapshot.inventory.get(dependency, 0) != 0
                    or catalog.hand_categories.get(recipe['category'])
                    or FactoryPlanner(catalog, snapshot, plan.goal)._machine_type(recipe) != dependency):
                return None
            machine_edge = {'kind': 'missing_production_machine', 'product': product,
                            'machine': dependency, 'role': role,
                            'recipe': deepcopy(recipe),
                            'prototype': deepcopy(catalog.machines[dependency])}
            edges.append(machine_edge)
        if (machine_edge is None or edges[-1]['kind'] != 'recipe_input'
                or edges[-1]['recipe']['name'] != raw.get('recipe')):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {'schema': 1, 'session_id': snapshot.session_id, 'observed_tick': snapshot.tick,
            'planner_item_path': list(path), 'local_target': deepcopy(local),
            'gather_resource': step.item, 'gather_quantity': params['quantity'],
            'gather_inventory_now': snapshot.inventory.get(step.item, 0),
            'gather_inventory_target': step.threshold, 'edges': edges,
            'basis': 'current_catalog_input_edges_and_observed_missing_machine',
            'gather_craft_placement_and_production_require_native_verification': True}
