"""Current recipe demand for a bounded craft; never execution authority."""
from copy import deepcopy
from dataclasses import asdict
import json
import math
from types import SimpleNamespace

from .bootstrap_chain import dependency_chain
from .catalog import Catalog


def craft_demand(snapshot, catalog, plan):
    """Recompute a selected recipe branch and the paid craft from native facts."""
    try:
        from .decision_support import _craft_start_evidence
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
