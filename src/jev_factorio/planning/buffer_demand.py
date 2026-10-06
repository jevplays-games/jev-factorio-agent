"""A paid buffer component is a separate bounded goal, not a recipe edge."""
from copy import deepcopy
from dataclasses import replace

from ..output_buffers import PARTS, current, sources, validate_commitments
from .bootstrap_chain import dependency_chain

MARKER = 'buffer_component_demand'
COMMISSIONING = 'buffer_commissioning_demand'


def commissioning_purpose(snapshot, catalog, plan):
    """Current parent demand for a paid buffer; not proof of transport benefit."""
    try:
        marker = plan.materials[COMMISSIONING]
        local = plan.materials['local_objective']
        row = sources(snapshot)[marker['source_role']]
        step, = plan.steps
        if (snapshot.world_kind != 'fle' or snapshot.game_version != catalog.version
                or type(marker.get('schema')) is not int or marker['schema'] != 1
                or type(snapshot.tick) is not int or type(marker['observed_tick']) is not int
                or marker['observed_tick'] != snapshot.tick
                or marker['session_id'] != snapshot.session_id
                or snapshot.factory.get('tick') != snapshot.tick
                or not current(row, snapshot) or not row['parts']
                or row['source'] != marker['source_role']
                or row['source_unit'] != marker['source_unit']
                or row['layout'] != marker['layout'] or row['parts'] != marker['paid_parts']
                or local != marker['parent_local_objective']
                or local.get('ultimate_goal') != plan.goal
                or plan.materials.get('work_intent') != {'scope': 'immediate', 'observed_tick': snapshot.tick}
                or marker['action'] != step.action or marker['parameters'] != step.parameters
                or marker['costs'] != step.costs):
            return None
        if step.action == 'factory_buffer_build':
            part = next((p for p in PARTS if p not in row['parts']), None)
            if (row['state'] != 'building' or part is None
                    or step.parameters.get('source') != row['source']
                    or step.parameters.get('layout') != row['layout']
                    or step.parameters.get('part') != part or step.costs != {PARTS[part]: 1}):
                return None
        elif step.action == 'factory_insert':
            if (row['state'] != 'ready' or row.get('topology') is not True
                    or set(row['parts']) != set(PARTS)
                    or step.parameters.get('role') != row['parts']['inserter']['role']
                    or step.parameters.get('item') != 'coal'):
                return None
        else:
            return None
        owners = sources(snapshot)
        validate_commitments({role: {'source_unit': owner['source_unit'],
            'layout': owner['layout'], 'parts': owner['parts']}
            for role, owner in owners.items()}, successors='successors' in snapshot.factory)
        if snapshot.factory['entities'][row['source']]['unit_number'] != row['source_unit']:
            return None
        for name, paid in row['parts'].items():
            entity = snapshot.factory['entities'][paid['role']]
            if entity['unit_number'] != paid['unit_number'] or entity['name'] != PARTS[name]:
                return None
        path = marker['parent_item_path']
        if path[-1] != row['item'] or row['item'] != row['source'].removeprefix('recipe:'):
            return None
        chain = dependency_chain(snapshot, catalog, local, path)
        carried = snapshot.inventory.get(row['item'], 0)
        required = chain['raw_input_inventory_target']
        if type(carried) is not int or not 0 <= carried < required:
            return None
        return {**deepcopy(marker), 'parent_recipe_demand': chain,
            'source_item_carried': carried, 'source_item_shortfall': required - carried,
            'basis': 'current_paid_buffer_commissioning_and_parent_recipe_demand',
            'paid_prefix_is_not_measured_payback': True,
            'placement_transfer_flow_and_parent_output_require_native_verification': True}
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None


def scope_commissioning(snapshot, catalog, plan, row, parent_path):
    """Carry recipe purpose across component acquisition into build/service."""
    materials = deepcopy(plan.materials or {})
    local = materials.get('local_objective')
    prefix = [x.removeprefix('item:') for x in parent_path if x.startswith('item:')]
    if (not isinstance(local, dict) or not prefix or prefix[0] != local.get('item')
            or 'utility_power_prerequisite' in materials
            or any(x.startswith('infrastructure:') for x in parent_path)
            or len(plan.steps) != 1):
        return plan
    step = plan.steps[0]
    materials[COMMISSIONING] = {'schema': 1, 'observed_tick': snapshot.tick,
        'session_id': snapshot.session_id, 'source_role': row['source'],
        'source_unit': row['source_unit'], 'layout': row['layout'],
        'paid_parts': deepcopy(row['parts']), 'parent_local_objective': deepcopy(local),
        'parent_item_path': [*prefix, row['item']], 'action': step.action,
        'parameters': deepcopy(step.parameters), 'costs': deepcopy(step.costs)}
    scoped = replace(plan, materials=materials)
    return scoped if commissioning_purpose(snapshot, catalog, scoped) is not None else plan


def qualified_commissioning(plan, facts, row):
    """Recompute parent demand independently; the native start proof is separate."""
    from types import SimpleNamespace
    from .catalog import Catalog
    try:
        observed = facts['factory']['recipe_dependency_catalog']
        if (type(observed['schema']) is not int or observed['schema'] != 1
                or observed['tick'] != facts['tick'] or observed['session_id'] != facts['session_id']
                or observed['version'] != facts['game_version']
                or row['local_target'] != plan.materials['local_objective']):
            return False
        catalog = Catalog(observed['version'], observed['recipes'], {}, observed.get('machines', {}),
                          observed['hand_categories'], observed['stack_sizes'])
        snapshot = SimpleNamespace(**{key: facts[key] for key in
            ('tick', 'session_id', 'world_kind', 'game_version', 'inventory', 'factory', 'researched')})
        proof = row['buffer_commissioning_parent_purpose']
        return proof is not None and commissioning_purpose(snapshot, catalog, plan) == proof
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return False


def purpose(snapshot, catalog, plan):
    """Rebuild current ownership and parent demand without forecasting output."""
    try:
        marker = plan.materials[MARKER]
        local = plan.materials['local_objective']
        parent = marker['parent_local_objective']
        row = sources(snapshot)[marker['source_role']]
        part = marker['part']
        if (snapshot.world_kind != 'fle' or snapshot.game_version != catalog.version
                or type(marker.get('schema')) is not int or marker['schema'] != 1
                or type(snapshot.tick) is not int or marker['observed_tick'] != snapshot.tick
                or type(marker['observed_tick']) is not int
                or marker['session_id'] != snapshot.session_id
                or snapshot.factory.get('tick') != snapshot.tick
                or not current(row, snapshot) or row['state'] != 'building'
                or row['source'] != marker['source_role']
                or row['source_unit'] != marker['source_unit']
                or row['layout'] != marker['layout'] or row['parts'] != marker['paid_parts']
                or not row['parts'] or part not in PARTS
                or next((p for p in PARTS if p not in row['parts']), None) != part
                or local != {'item': PARTS[part], 'inventory_target': 1, 'ultimate_goal': plan.goal}
                or parent.get('ultimate_goal') != plan.goal
                or plan.materials.get('work_intent') != {'scope': 'immediate', 'observed_tick': snapshot.tick}
                or snapshot.inventory.get(PARTS[part], 0) != 0):
            return None
        owners = sources(snapshot)
        validate_commitments({role: {'source_unit': owner['source_unit'],
            'layout': owner['layout'], 'parts': owner['parts']}
            for role, owner in owners.items()}, successors='successors' in snapshot.factory)
        if snapshot.factory['entities'][row['source']]['unit_number'] != row['source_unit']:
            return None
        for name, paid in row['parts'].items():
            entity = snapshot.factory['entities'][paid['role']]
            if entity['unit_number'] != paid['unit_number'] or entity['name'] != PARTS[name]:
                return None
        path = marker['parent_item_path']
        if path[-1] != row['item'] or row['item'] != row['source'].removeprefix('recipe:'):
            return None
        chain = dependency_chain(snapshot, catalog, parent, path)
        return {**deepcopy(marker), 'parent_recipe_demand': chain,
            'basis': 'current_paid_buffer_component_and_separate_parent_recipe_demand',
            'component_and_parent_paths_are_separate': True,
            'component_build_flow_and_parent_output_require_native_verification': True}
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, StopIteration):
        return None


def scope_prerequisite(snapshot, catalog, plan, row, part, parent_path):
    """Preserve the parent purpose while making the current kit target explicit.

    Utility and other non-recipe agendas keep their existing dedicated proofs.
    This changes explanatory demand only; actions, costs and receipts are intact.
    """
    materials = deepcopy(plan.materials or {})
    parent = materials.get('local_objective')
    prefix = [x.removeprefix('item:') for x in parent_path if x.startswith('item:')]
    if (not isinstance(parent, dict) or not prefix or prefix[0] != parent.get('item')
            or row.get('state') != 'building' or not row.get('parts')
            or 'utility_power_prerequisite' in materials
            or any(x.startswith('infrastructure:') for x in parent_path)
            or materials.get('work_intent') != {'scope': 'immediate', 'observed_tick': snapshot.tick}):
        return plan
    markers = ('recipe_input_transfer', 'output_pickup', 'craft_dependency', 'raw_prerequisite')
    changed = False
    for name in markers:
        value = materials.get(name)
        if not isinstance(value, dict):
            continue
        path = value.get('planner_item_path')
        if (not isinstance(path, list) or path[:len(prefix)] != prefix
                or path[len(prefix):len(prefix)+1] != [PARTS[part]]):
            return plan
        value['planner_item_path'] = path[len(prefix):]
        changed = True
    if not changed:
        return plan
    materials['local_objective'] = {'item': PARTS[part], 'inventory_target': 1, 'ultimate_goal': plan.goal}
    materials[MARKER] = {'schema': 1, 'observed_tick': snapshot.tick,
        'session_id': snapshot.session_id, 'source_role': row['source'],
        'source_unit': row['source_unit'], 'layout': row['layout'],
        'paid_parts': deepcopy(row['parts']), 'part': part,
        'parent_local_objective': parent, 'parent_item_path': [*prefix, row['item']]}
    scoped = replace(plan, materials=materials)
    return scoped if purpose(snapshot, catalog, scoped) is not None else plan


def qualified(plan, facts, row):
    """Recompute against independently projected native recipes and owners."""
    from types import SimpleNamespace
    from .catalog import Catalog
    try:
        proof = row['buffer_component_parent_purpose']
        observed = facts['factory']['recipe_dependency_catalog']
        if (observed['schema'] != 1 or observed['tick'] != facts['tick']
                or observed['session_id'] != facts['session_id']
                or observed['version'] != facts['game_version']
                or row['local_target'] != plan.materials['local_objective']):
            return False
        catalog = Catalog(observed['version'], observed['recipes'], {}, observed.get('machines', {}),
                          observed['hand_categories'], observed['stack_sizes'])
        snapshot = SimpleNamespace(**{key: facts[key] for key in
            ('tick', 'session_id', 'world_kind', 'game_version', 'inventory', 'factory', 'researched')})
        return proof is not None and purpose(snapshot, catalog, plan) == proof
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return False
