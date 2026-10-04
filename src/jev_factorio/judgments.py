"""Bounded, explicit JEV candidate judgments; no implicit question dependencies."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import requests

from .skills import Plan
from .provider_health import ProviderBlocked


# Serialized request bytes (not provider tokens). One live-campaign candidate costs
# about 26 KB with context and each further candidate about 10 KB, so this fits at
# least two ranked candidates with margin where 32 KB offered only one.
DEFAULT_MAX_REQUEST_BYTES = 48000
MIN_MAX_REQUEST_BYTES = 8000
MAX_MAX_REQUEST_BYTES = 262144


class InvalidJudgment(ValueError):
    """Malformed or out-of-domain answers must not authorize an action."""


def _qualified_supplied_research(plan, facts, row):
    proof = row.get('supplied_research_start_evidence')
    if not isinstance(proof, dict) or len(plan.steps) != 1:
        return False
    step = plan.steps[0]; factory = facts.get('factory', {})
    if not isinstance(factory, dict) or not isinstance(factory.get('entities'), dict):
        return False
    lab = factory.get('entities', {}).get('utility:lab', {})
    if not isinstance(lab, dict):
        return False
    bill = proof.get('ingredients_per_unit')
    numeric = lambda x: type(x) in (int, float) and math.isfinite(x)
    return (facts.get('world_kind') == 'fle' and step.action == 'factory_research'
        and proof.get('basis') == 'current_powered_lab_supplied_for_native_research_selection'
        and proof.get('session_id') == facts.get('session_id')
        and isinstance(proof.get('session_id'), str) and bool(proof['session_id'])
        and type(proof.get('observed_tick')) is int
        and type(facts.get('tick')) is int and type(factory.get('tick')) is int
        and proof['observed_tick'] == facts.get('tick') == factory.get('tick')
        and proof.get('native_catalog_version') == facts.get('game_version')
        and proof.get('technology') == (step.parameters or {}).get('technology') == step.item
        and step.item not in facts.get('researched', []) and factory.get('research') == ''
        and factory.get('player_connected') is True and factory.get('player_bound') is True
        and type(factory.get('crafting_queue')) is int and factory['crafting_queue'] == 0
        and lab.get('name') == 'lab' and type(lab.get('unit_number')) is int
        and lab['unit_number'] > 0 and type(proof.get('lab_unit')) is int
        and proof.get('lab_unit') == lab['unit_number']
        and proof.get('lab_role') == 'utility:lab'
        and type(lab.get('electric_network_id')) is int and lab['electric_network_id'] > 0
        and type(proof.get('electric_network_id')) is int
        and proof.get('electric_network_id') == lab['electric_network_id']
        and numeric(lab.get('energy')) and lab['energy'] > 0
        and numeric(proof.get('energy_now')) and proof.get('energy_now') == lab['energy']
        and isinstance(lab.get('input'), dict) and proof.get('lab_input_now') == lab['input']
        and isinstance(bill, dict) and bool(bill)
        and all(isinstance(k, str) and k and numeric(v) and v > 0
                and numeric(lab['input'].get(k)) and lab['input'][k] >= v for k,v in bill.items())
        and all(proof.get(k) is True for k in ('technology_enabled_and_unresearched',
            'prerequisites_researched','research_selection_and_later_progress_require_native_verification',
            'technology_completion_not_established')))



def _qualified_recipe_transfer_chain(plan, facts, row):
    """Recompile current input handling and downstream arithmetic from independent facts."""
    try:
        from types import SimpleNamespace
        from .planning.catalog import Catalog
        from .planning.decision_support import _recipe_input_transfer_start_evidence
        from .planning.bootstrap_chain import validate_dependency_chain
        proof = row['recipe_input_transfer_start_evidence']
        factory = facts['factory']; observed = factory['recipe_dependency_catalog']
        local = plan.materials['local_objective']; annotation = plan.materials['recipe_input_transfer']
        if (facts['world_kind'] != 'fle' or type(facts['tick']) is not int
                or type(factory.get('tick')) is not int or factory['tick'] != facts['tick']
                or proof['session_id'] != facts['session_id']
                or proof['native_catalog_version'] != facts['game_version']
                or row['local_target'] != local or local.get('ultimate_goal') != plan.goal
                or not isinstance(observed.get('machines'), dict)):
            return False
        machine = factory['entities'][proof['owned_source_role']]
        prototype = observed['machines'][machine['name']]
        categories = prototype.get('categories')
        if (not isinstance(prototype, dict) or type(prototype.get('burner')) is not bool
                or type(prototype.get('electric')) is not bool
                or not isinstance(categories, dict)
                or any(type(name) is not str or type(value) is not bool
                       for name, value in categories.items())
                or categories.get(observed['recipes'][proof['direct_native_recipe']]['category']) is not True):
            return False
        snapshot = SimpleNamespace(**{key: facts[key] for key in
            ('tick','session_id','world_kind','inventory','factory')}, researched=facts.get('researched', []))
        catalog = Catalog(observed['version'], observed['recipes'], {}, observed['machines'],
                          observed['hand_categories'], observed['stack_sizes'])
        expected = _recipe_input_transfer_start_evidence(snapshot, catalog, plan, include_dependency_chain=True)
        return (expected == proof and validate_dependency_chain(facts, local,
            annotation['planner_item_path'], proof['recipe_dependency_chain'],
            plan.steps[0].parameters['quantity']))
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return False


def _qualified_paid_service_input(plan, facts, row):
    """Independently bind the first paid insert, without certifying later service."""
    import hashlib
    try:
        proof = row['paid_service_input_start_evidence']
        marker = plan.materials['service_visit']
        factory, tick = facts['factory'], facts['tick']
        first, second = plan.steps
        p, tail = first.parameters, second.parameters
        role, item = p['role'], p['item']
        machine = factory['entities'][role]
        source = factory['production_sites']['sources'][role]
        start = proof['first_recipe_input']
        local = plan.materials['local_objective']
        annotation = plan.materials['recipe_input_transfer']
        recipe = proof['native_recipe']
        ingredients = recipe['ingredients']
        matches = [entry for entry in ingredients if entry.get('type') == 'item'
                   and entry.get('name') == item]
        stock, costs = marker['paid_stock_now'], {}
        identity = [{k: v for k, v in step.parameters.items() if k != 'receipt'}
                    | {'action': step.action} for step in plan.steps]
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
        if (facts.get('world_kind') != 'fle' or type(tick) is not int or type(factory.get('tick')) is not int
                or factory['tick'] != tick or type(proof.get('observed_tick')) is not int
                or proof['observed_tick'] != tick or proof['session_id'] != facts['session_id']
                or proof['native_catalog_version'] != facts['game_version']
                or not facts['game_version'].startswith('2.0.')
                or proof['basis'] != 'current_recompiled_paid_service_first_recipe_input'
                or proof['service_visit'] != marker or plan.id != f'service:{role}:{digest}'
                or type(marker['schema']) is not int or marker['schema'] != 1
                or type(marker['observed_tick']) is not int or marker['observed_tick'] != tick
                or marker['scope'] != 'same_cell_paid_service' or marker['first_role'] != role
                or type(marker['steps']) is not int or marker['steps'] != 2
                or marker['collections_are_spendable'] is not False
                or marker['unit_numbers'] != [machine['unit_number']] * 2
                or any(type(unit) is not int for unit in marker['unit_numbers'])
                or marker['max_extra_ticks'] != 900 or marker['max_leg_tiles'] != 8
                or type(marker['extra_ticks_estimate']) is not int
                or not 0 < marker['extra_ticks_estimate'] <= 900
                or marker['research_deadline_tick'] is not None
                or marker['estimate_basis'] != 'Manhattan_distance_and_declared_policy_not_native_timing'
                or row.get('work_scope') != 'immediate' or row.get('unknowns') != []
                or row.get('requires_investment') is not False
                or row.get('reasons') != [f'observed_low_fuel:{role}']
                or type(row.get('urgency')) is not int or row['urgency'] != 3
                or factory.get('craft_job', {}).get('status') not in {None, 'completed'}
                or row.get('local_target') != local
                or local.get('ultimate_goal') != plan.goal
                or factory.get('player_connected') is not True
                or factory.get('player_bound') is not True
                or type(factory.get('crafting_queue')) is not int or factory['crafting_queue'] != 0
                or machine['name'] not in {'stone-furnace', 'steel-furnace'}
                or type(machine['unit_number']) is not int or machine['unit_number'] <= 0
                or source['state'] != 'owned' or source['source_unit'] != machine['unit_number']
                or type(source['source_unit']) is not int
                or role != 'recipe:' + recipe['name'] or recipe['hidden']
                or recipe['category'] != 'smelting' or recipe.get('enabled') is not True
                or item == 'coal' or tail.get('role') != role or tail.get('item') != 'coal'
                or type(machine.get('crafting')) is not bool
                or type(machine['input'].get(item, 0)) is not int
                or type(machine['fuel'].get('coal', 0)) is not int
                or not 0 < machine['fuel']['coal'] < 5 or len(matches) != 1
                or type(matches[0]['amount']) not in {int, float}
                or not math.isfinite(matches[0]['amount']) or matches[0]['amount'] <= 0
                or type(annotation['planned_batches']) is not int or annotation['planned_batches'] < 1
                or type(annotation['observed_tick']) is not int or annotation['observed_tick'] != tick
                or annotation['source_role'] != role or annotation['source_unit'] != machine['unit_number']
                or type(annotation['source_unit']) is not int
                or annotation['observed_input'] != machine['input'].get(item, 0)
                or type(annotation['observed_input']) is not int
                or annotation['observed_crafting'] is not machine['crafting']
                or annotation['recipe'] != recipe['name'] or annotation['ingredient'] != item):
            return False
        path = annotation['planner_item_path']
        if (not isinstance(path, list) or not 2 <= len(path) <= 32
                or any(not isinstance(part, str) or not part for part in path)
                or path[0] != local['item'] or path[-2:] != [recipe['name'], item]):
            return False
        native_path = proof['native_parent_recipes']
        if set(native_path) != set(path[:-1]) or native_path[recipe['name']] != recipe:
            return False
        for product, ingredient in zip(path, path[1:]):
            native = native_path[product]
            if (native.get('name') != product or native.get('hidden')
                    or native.get('enabled') is not True
                    or not any(entry.get('type') == 'item' and entry.get('name') == product
                               for entry in native.get('products', []))
                    or not any(entry.get('type') == 'item' and entry.get('name') == ingredient
                               for entry in native.get('ingredients', []))):
                return False
        required = max(0, math.ceil(matches[0]['amount'] * annotation['planned_batches']
            - machine['input'].get(item, 0) - (matches[0]['amount'] if machine['crafting'] else 0)))
        if (p['quantity'] != required or type(p['quantity']) is not int or required < 1
                or tail['quantity'] != min(50 - machine['fuel']['coal'], stock['coal'])
                or proof['native_receiver_capacity_and_each_step_require_rechecks'] is not True
                or proof['later_fuel_output_and_target_completion_unverified'] is not True):
            return False
        for step, query in zip(plan.steps, proof['native_receipt_queries'], strict=True):
            params = step.parameters
            name, quantity = params['item'], params['quantity']
            if (step.action != 'factory_insert' or step.effect != 'transfer'
                    or step.item != '' or set(params) != {'role', 'item', 'quantity', 'receipt'}
                    or type(quantity) is not int or not 1 <= quantity <= 200
                    or step.costs != {name: quantity}
                    or params['receipt'] != f'{tick}:factory_insert:{role}:{name}'
                    or not _qualified_unused_buffer_receipt(facts,
                        {'native_receipt_query': query}, params['receipt'], tick)):
                return False
            costs[name] = costs.get(name, 0) + quantity
        if (set(stock) != set(costs) or proof['combined_paid_costs'] != costs
                or any(type(stock[name]) is not int or type(facts['inventory'].get(name)) is not int
                    or not costs[name] <= stock[name] <= facts['inventory'][name] for name in costs)):
            return False
        expected = {
            'observed_tick': tick, 'planner_item_path': path,
            'direct_native_recipe': recipe['name'], 'owned_source_role': role,
            'owned_source_unit': machine['unit_number'], 'ingredient': item,
            'ingredient_in_machine_now': machine['input'].get(item, 0),
            'ingredient_in_inventory_now': facts['inventory'][item],
            'burner_fuel_coal_now': machine['fuel']['coal'],
            'paid_quantity_to_transfer': required, 'planned_native_receipt_id': p['receipt'],
            'basis': 'current_planner_recipe_input_and_owned_native_machine',
            'native_transfer_and_later_output_require_verification': True,
        }
        return start == expected and all(type(start[key]) is int for key in (
            'observed_tick', 'owned_source_unit', 'ingredient_in_machine_now',
            'ingredient_in_inventory_now', 'burner_fuel_coal_now', 'paid_quantity_to_transfer'))
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return False

def _qualified_research_science_transfer(plan, facts, row):
    """Recheck current paid inputs; disclosed technology bill is native producer evidence."""
    proof = row.get('research_science_transfer_start_evidence')
    if not isinstance(proof, dict) or len(plan.steps) != 1:
        return False
    try:
        step = plan.steps[0]; parameters = step.parameters or {}
        factory = facts['factory']; lab = factory['entities']['utility:lab']
        tick, item, quantity = facts['tick'], parameters['item'], parameters['quantity']
        bill, name = proof['ingredients_per_unit'], proof['technology']
        annotation = (plan.materials or {}).get('research_science_transfer')
        amount, supplied = bill[item], lab['input'].get(item, 0)
        count, progress = proof['research_count'], factory.get('research_progress', 0)
        numeric = lambda value: type(value) in (int, float) and math.isfinite(value)
        return (facts.get('world_kind') == 'fle'
            and type(tick) is int and type(factory.get('tick')) is int and factory['tick'] == tick
            and proof.get('basis') == 'current_native_technology_paid_science_input'
            and isinstance(proof.get('session_id'), str) and bool(proof['session_id'])
            and proof['session_id'] == facts.get('session_id')
            and type(proof.get('observed_tick')) is int and proof['observed_tick'] == tick
            and proof.get('native_catalog_version') == facts.get('game_version')
            and isinstance(name, str) and bool(name) and name not in facts.get('researched', [])
            and isinstance(annotation, dict) and type(annotation.get('observed_tick')) is int
            and annotation == {'observed_tick': tick, 'technology': name, 'ingredient': item}
            and isinstance(proof.get('prerequisites'), list)
            and all(isinstance(parent, str) and parent in facts.get('researched', []) for parent in proof['prerequisites'])
            and factory.get('research') in ('', name)
            and factory.get('player_connected') is True and factory.get('player_bound') is True
            and type(factory.get('crafting_queue')) is int and factory['crafting_queue'] == 0
            and lab.get('name') == 'lab' and type(lab.get('unit_number')) is int and lab['unit_number'] > 0
            and type(proof.get('lab_unit')) is int and proof['lab_unit'] == lab['unit_number']
            and proof.get('lab_role') == parameters.get('role') == 'utility:lab'
            and step.action == 'factory_insert' and step.effect == 'transfer'
            and isinstance(bill, dict) and bool(bill)
            and all(isinstance(key, str) and bool(key) and numeric(value) and value > 0 for key, value in bill.items())
            and numeric(amount) and amount > 0 and numeric(supplied) and 0 <= supplied < amount
            and proof.get('lab_input_now') == lab['input'] and proof.get('ingredient') == item
            and numeric(count) and count > 0 and numeric(progress) and 0 <= progress <= 1
            and numeric(proof.get('research_progress_now')) and proof['research_progress_now'] == progress
            and type(quantity) is int and quantity == max(1, min(20, math.ceil(count * (1 - progress) * amount)))
            and type(proof.get('paid_quantity_to_transfer')) is int and proof['paid_quantity_to_transfer'] == quantity
            and step.costs == {item: quantity}
            and type(facts['inventory'].get(item)) is int and facts['inventory'][item] >= quantity
            and type(proof.get('actor_science_now')) is int and proof['actor_science_now'] == facts['inventory'][item]
            and proof.get('planned_native_receipt') == parameters.get('receipt')
            and isinstance(parameters.get('receipt'), str) and bool(parameters['receipt'])
            and _qualified_unused_buffer_receipt(facts, proof, parameters['receipt'], tick)
            and all(proof.get(key) is True for key in ('technology_enabled_and_unresearched',
                'transfer_selection_and_research_progress_require_native_verification',
                'research_selection_or_completion_not_established')))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _number(value, maximum: float = 1.0) -> float:
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not 0 <= value <= maximum):
        raise InvalidJudgment("Expected a finite in-range number")
    return float(value)


def _rounded_score_bounds(probabilities: dict, quantum: float) -> tuple[float, float]:
    lower = [max(0, probabilities[str(index)] - quantum / 2)
             for index in range(len(probabilities))]
    upper = [min(1, probabilities[str(index)] + quantum / 2)
             for index in range(len(probabilities))]

    def extreme(order):
        remaining = max(0, 1 - sum(lower))
        score = sum(index * value for index, value in enumerate(lower))
        for index in order:
            extra = min(remaining, upper[index] - lower[index])
            score += index * extra
            remaining -= extra
        return score

    return extreme(range(len(lower))), extreme(reversed(range(len(lower))))


def validate_answers(questions: dict, answers: dict, quantum: float = 0) -> None:
    if quantum not in (0, 0.01):
        raise InvalidJudgment("Unsupported answer rounding precision")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise InvalidJudgment("Missing or unexpected answers")
    for key, question in questions.items():
        answer = answers[key]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise InvalidJudgment("Answer type mismatch")
        if kind == "noul":
            _number(answer.get("noul"))
            continue
        _number(answer.get("confidence"))
        probabilities = answer.get("probabilities")
        labels = (set(question["criteria"]) if kind == "choice"
                  else {str(i) for i in range(len(question["criteria"]))})
        if not isinstance(probabilities, dict) or set(probabilities) != labels:
            raise InvalidJudgment("Incomplete answer distribution")
        values = [_number(p) for p in probabilities.values()]
        total = sum(values)
        valid_total = (
            sum(max(0, value - quantum / 2) for value in values) <= 1 + 1e-9
            and sum(min(1, value + quantum / 2) for value in values) >= 1 - 1e-9
        ) if quantum else math.isclose(total, 1, abs_tol=1e-3)
        if not valid_total:
            raise InvalidJudgment("Probabilities must sum to one")
        if kind == "choice":
            choice = answer.get("choice")
            if (not isinstance(choice, str) or choice not in labels
                    or probabilities[choice] + 1e-6 < max(probabilities.values())):
                raise InvalidJudgment("Choice must be an offered maximum-probability label")
        else:
            score = _number(answer.get("score"), len(labels) - 1)
            if answer.get("legend") != {str(i): level for i, level in enumerate(question["criteria"])}:
                raise InvalidJudgment("Score legend does not match the supplied rubric")
            expected = sum(int(key) * value for key, value in probabilities.items())
            if quantum:
                minimum, maximum = _rounded_score_bounds(probabilities, quantum)
                consistent = (score + quantum / 2 >= minimum - 1e-9
                              and score - quantum / 2 <= maximum + 1e-9)
            else:
                consistent = math.isclose(score, expected, abs_tol=0.02 + 1e-9)
            if not consistent:
                raise InvalidJudgment("Score conflicts with its probability distribution")


@dataclass
class Decision:
    plan_id: str | None
    source: str
    reason: str = ""
    state: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    answers: dict = field(default_factory=dict)
    utilities: dict[str, float] = field(default_factory=dict)
    model_called: bool = False
    diagnostics: dict = field(default_factory=dict)


def _native_additive_connection_contract(plan, facts) -> bool:
    """Identify the paid native connector contract, not a surveyed route."""
    if not isinstance(facts, dict) or facts.get('world_kind') != 'fle' or len(plan.steps) != 1:
        return False
    step = plan.steps[0]
    parameters = step.parameters
    factory = facts.get('factory')
    tick, session = facts.get('tick'), facts.get('session_id')
    if (step.action != 'factory_connect' or step.effect != 'connection'
            or not isinstance(parameters, dict)
            or set(parameters) != {'source', 'target', 'kind', 'fluid'}
            or parameters.get('kind') not in {'pipe', 'small-electric-pole'}
            or any(not isinstance(parameters.get(key), str) or not parameters[key]
                   for key in ('source', 'target', 'fluid'))
            or parameters['source'] == parameters['target']
            or (parameters['kind'] == 'small-electric-pole') != (parameters['fluid'] == 'electricity')
            or type(tick) is not int or not isinstance(session, str) or not session
            or not isinstance(factory, dict) or type(factory.get('tick')) is not int
            or factory['tick'] != tick):
        return False
    ownership = factory.get('connector_ownership')
    entities = factory.get('entities')
    if (not isinstance(ownership, dict) or type(ownership.get('protocol')) is not int
            or ownership['protocol'] != 1 or ownership.get('session_id') != session
            or type(ownership.get('tick')) is not int or ownership['tick'] != tick
            or not isinstance(ownership.get('routes'), dict)
            or not isinstance(entities, dict)):
        return False
    source, target = entities.get(parameters['source']), entities.get(parameters['target'])
    return (isinstance(source, dict) and isinstance(target, dict)
            and all(type(entity.get('unit_number')) is int and entity['unit_number'] > 0
                    and isinstance(entity.get('name'), str) and bool(entity['name'])
                    for entity in (source, target))
            and source['unit_number'] != target['unit_number'])


def _qualified_candidate_local_raw_demand(plan, facts, row, evidence):
    """Check a separate current parent purpose without changing the kit target."""
    proof = row.get("candidate_local_raw_demand")
    local, raw = row.get("local_target"), row.get("raw_prerequisite")
    if not isinstance(proof, dict) or not isinstance(local, dict) or not isinstance(raw, dict):
        return False
    if (type(proof.get("parent_source_unit")) is not int or proof["parent_source_unit"] <= 0
            or not isinstance(local.get("item"), str) or not local["item"]):
        return False
    parents = [value.get("input_route_kit_parent_purpose") for value in evidence.values()
        if isinstance(value, dict) and isinstance(value.get("input_route_kit_parent_purpose"), dict)
        and value["input_route_kit_parent_purpose"].get("source_unit") == proof["parent_source_unit"]]
    if len(parents) != 1:
        return False
    parent = parents[0]
    tick = facts.get("tick")
    return (facts.get("world_kind") == "fle" and type(tick) is int
        and type(proof.get("schema")) is int and proof["schema"] == 1
        and type(proof.get("tick")) is int and proof["tick"] == tick
        and set(proof) == {"schema", "tick", "session_id", "catalog_version", "parent_source_unit", "basis"}
        and proof.get("basis") == "recompiled_current_parent_raw_demand"
        and proof.get("session_id") == facts.get("session_id")
        and isinstance(facts.get("session_id"), str) and bool(facts["session_id"])
        and proof.get("catalog_version") == facts.get("game_version")
        and isinstance(parent, dict)
        and parent.get("basis") == "same_tick_recompiled_owned_input_route_kit_need"
        and type(parent.get("observed_tick")) is int and parent["observed_tick"] == tick
        and parent.get("session_id") == facts["session_id"]
        and parent.get("parent_local_objective") == local
        and local.get("ultimate_goal") == plan.goal
        and type(local.get("inventory_target")) is int and local["inventory_target"] > 0
        and isinstance(facts.get("inventory"), dict)
        and type(facts["inventory"].get(local.get("item"), 0)) is int
        and facts["inventory"].get(local.get("item"), 0) < local["inventory_target"]
        and (plan.materials or {}).get("local_objective") == local
        and isinstance(raw.get("planner_item_path"), list)
        and raw["planner_item_path"][:1] == [local.get("item")])


def _qualified_bootstrap_output_pickup(plan, facts, row):
    """Consume the distinct current-asset witness, never generic chest stock."""
    from .bootstrap_output import ROLE, ORIGINS
    from .planning.decision_support import _bootstrap_output_ownership_digest
    if not isinstance(facts, dict) or not isinstance(row, dict) or len(plan.steps) != 1:
        return False
    proof = row.get('bootstrap_output_pickup_start_evidence')
    local = row.get('local_target')
    factory = facts.get('factory')
    if not all(isinstance(value, dict) for value in (proof, local, factory)):
        return False
    if set(proof) != {'schema', 'observed_tick', 'session_id', 'catalog_version',
            'source_role', 'source_unit', 'binding_id', 'ownership_sha256',
            'planner_item_path', 'inventory_now', 'current_raw_demand', 'recipe_dependency_chain', 'planned_pickup_quantity',
            'planned_native_receipt_id', 'basis',
            'native_pickup_and_inventory_delta_require_verification',
            'later_recipe_output_and_target_completion_unverified'}:
        return False
    demand = proof.get('current_raw_demand')
    provenance_row = (plan.materials or {}).get('bootstrap_output_pickup')
    provenance_demand = provenance_row.get('current_raw_demand') if isinstance(provenance_row, dict) else None
    if (not isinstance(demand, dict) or demand != provenance_demand
            or set(demand) != {'schema', 'item', 'observed_tick', 'session_id',
                'required_carried_quantity', 'carried_inventory', 'carried_deficit',
                'scope', 'direct_recipe', 'direct_product', 'owned_source_stock', 'inventory_headroom', 'planned_pickup_quantity', 'accounting'}
            or any(type(demand.get(key)) is not int for key in ('schema', 'observed_tick',
                'required_carried_quantity', 'carried_inventory', 'carried_deficit',
                'owned_source_stock', 'inventory_headroom', 'planned_pickup_quantity'))
            or demand['schema'] != 1 or demand['item'] != 'iron-ore'
            or demand['observed_tick'] != facts.get('tick')
            or demand['session_id'] != facts.get('session_id')
            or demand['carried_inventory'] != facts.get('inventory', {}).get('iron-ore', 0)
            or demand['carried_deficit'] != demand['required_carried_quantity'] - demand['carried_inventory']
            or demand['carried_deficit'] <= 0
            or demand['accounting'] != 'carried_deficit_before_owned_stock_allocation'
            or demand['scope'] != 'next_recursive_recipe_input_batch'
            or not isinstance(proof.get('planner_item_path'), list)
            or len(proof['planner_item_path']) < 2
            or demand['direct_product'] != proof['planner_item_path'][-2]
            or not isinstance(demand['direct_recipe'], dict)):
        return False
    try:
        from .planning.catalog import Catalog
        recipe = demand['direct_recipe']
        if (type(recipe.get('enabled')) is not bool or type(recipe.get('hidden', False)) is not bool
                or recipe.get('hidden', False) or not isinstance(recipe.get('name'), str)
                or not isinstance(recipe.get('category'), str)
                or not all(isinstance(recipe.get(key), list) and recipe[key]
                           and all(isinstance(entry, dict)
                                   and isinstance(entry.get('name'), str)
                                   and entry.get('type') == 'item'
                                   and type(entry.get('amount')) in (int, float)
                                   and math.isfinite(entry['amount']) and entry['amount'] > 0
                                   for entry in recipe[key])
                           for key in ('ingredients', 'products'))):
            return False
        catalog = Catalog(proof['catalog_version'], {recipe['name']: recipe}, {}, {}, {})
        current_recipe = catalog.recipe_for(demand['direct_product'])
        if (not catalog.enabled(current_recipe, facts.get('researched', []))
                or not any(entry.get('type') == 'item' and entry.get('name') == 'iron-ore'
                           and type(entry.get('amount')) in (int, float) and entry['amount'] > 0
                           for entry in current_recipe['ingredients'])
                or not any(entry.get('type') == 'item' and entry.get('name') == demand['direct_product']
                           and entry.get('probability', 1) == 1 and entry.get('amount', 0) > 0
                           for entry in current_recipe['products'])):
            return False
    except (KeyError, TypeError, ValueError, IndexError, ArithmeticError):
        return False
    from .planning.bootstrap_chain import validate_dependency_chain
    if not validate_dependency_chain(facts, local, proof['planner_item_path'],
            proof.get('recipe_dependency_chain'), demand['required_carried_quantity']):
        return False
    owned = factory.get('bootstrap_output')
    entities = factory.get('entities')
    inventory = facts.get('inventory')
    if not all(isinstance(value, dict) for value in (owned, entities, inventory)):
        return False
    machine = entities.get(ROLE)
    capacity = owned.get('capacity')
    output = owned.get('output')
    if not all(isinstance(value, dict) for value in (machine, capacity, output)):
        return False
    step = plan.steps[0]
    p = step.parameters if isinstance(step.parameters, dict) else {}
    path = proof.get('planner_item_path')
    provenance = (plan.materials or {}).get('bootstrap_output_pickup')
    intent = (plan.materials or {}).get('work_intent')
    tick = facts.get('tick')
    if (not isinstance(provenance, dict) or not isinstance(intent, dict)
            or type(tick) is not int or tick < 0
            or not isinstance(facts.get('session_id'), str) or not facts['session_id']
            or not isinstance(local.get('item'), str) or not local['item']
            or not isinstance(path, list) or not 1 <= len(path) <= 32
            or any(not isinstance(item, str) or not item for item in path)
            or len(set(path)) != len(path)
            or path[0] != local['item'] or path[-1] != 'iron-ore'
            or type(local.get('inventory_target')) is not int or local['inventory_target'] <= 0
            or type(inventory.get(local['item'], 0)) is not int
            or inventory.get(local['item'], 0) >= local['inventory_target']
            or type(p.get('quantity')) is not int or not 1 <= p['quantity'] <= 200
            or type(output.get('iron-ore')) is not int or output['iron-ore'] < p['quantity']
            or type(capacity.get('count')) is not int or capacity['count'] < p['quantity']
            or type(inventory.get('iron-ore', 0)) is not int or inventory.get('iron-ore', 0) < 0
            or any(type(owned.get(key)) is not int or owned[key] <= 0
                for key in ('actor_unit', 'surface_index', 'force_index', 'drill_unit', 'chest_unit'))
            or owned['drill_unit'] == owned['chest_unit']
            or type(machine.get('unit_number')) is not int or machine['unit_number'] != owned['chest_unit']
            or not isinstance(owned.get('binding_id'), str) or not owned['binding_id']
            or type(owned.get('bound_at_tick')) is not int or not 0 <= owned['bound_at_tick'] <= tick
            or not isinstance(owned.get('origin'), str) or owned['origin'] not in ORIGINS):
        return False
    digest = _bootstrap_output_ownership_digest(owned)
    if digest is None or proof.get('ownership_sha256') != digest:
        return False
    if owned['origin'] == 'legacy_authorized_current_asset':
        authority = owned.get('authorization_sha256')
        if (not isinstance(authority, str) or len(authority) != 64
                or authority == '0' * 64 or any(c not in '0123456789abcdef' for c in authority)
                or owned.get('historical_paid_placement_proven') is not False
                or owned.get('paid_drill_unit') is not False
                or owned.get('paid_chest_unit') is not False
                or owned['binding_id'] != authority):
            return False
    elif (owned['binding_id'] != f"paid:{owned['drill_unit']}:{owned['chest_unit']}"
            or owned.get('historical_paid_placement_proven') is not True
            or owned.get('authorization_sha256') is not False
            or any(type(owned.get('paid_' + key)) is not int or owned['paid_' + key] != owned[key]
                   for key in ('drill_unit', 'chest_unit'))):
        return False
    return (facts.get('world_kind') == 'fle'
        and type(proof.get('schema')) is int and proof['schema'] == 1
        and type(proof.get('observed_tick')) is int and proof['observed_tick'] == tick
        and proof.get('session_id') == facts['session_id'] == owned.get('session_id')
        and isinstance(facts.get('game_version'), str)
        and proof.get('catalog_version') == facts['game_version']
        and type(owned.get('protocol')) is int and owned['protocol'] == 1
        and type(owned.get('tick')) is int and owned['tick'] == tick
        and owned.get('role') == factory.get('drill_output_role') == ROLE
        and owned.get('ownership_effective_now') is True and owned.get('native_pending') is False
        and machine.get('name') == 'wooden-chest' and machine.get('position') == owned.get('chest_position')
        and machine.get('output') == output and type(facts.get('iron_ore_collected')) is int
        and output['iron-ore'] == facts['iron_ore_collected']
        and factory.get('player_bound') is True and factory.get('player_connected') is True
        and capacity.get('schema') == 1 and type(capacity.get('schema')) is int
        and type(capacity.get('tick')) is int and capacity['tick'] == tick
        and capacity.get('session_id') == facts['session_id']
        and all(type(capacity.get(key)) is int and capacity[key] == owned[key]
                for key in ('actor_unit', 'surface_index', 'force_index'))
        and capacity.get('quality') == 'normal' and capacity.get('inventory') == 'character_main'
        and capacity.get('item') == 'iron-ore'
        and step.action == 'factory_extract' and step.effect == 'transfer' and step.costs == {}
        and step.verification is None and step.item == ''
        and type(step.threshold) is int and step.threshold == 0
        and set(p) == {'role', 'item', 'quantity', 'receipt'} and p['role'] == ROLE and p['item'] == 'iron-ore'
        and proof.get('source_role') == ROLE and type(proof.get('source_unit')) is int
        and proof['source_unit'] == owned['chest_unit'] and proof.get('binding_id') == owned['binding_id']
        and demand['owned_source_stock'] == output['iron-ore']
        and demand['inventory_headroom'] == capacity['count']
        and demand['planned_pickup_quantity'] == p['quantity'] == min(200, demand['carried_deficit'], output['iron-ore'], capacity['count'])
        and proof.get('planned_pickup_quantity') == p['quantity']
        and type(proof.get('planned_pickup_quantity')) is int
        and p['receipt'] == proof.get('planned_native_receipt_id') == f'{tick}:factory_extract:{ROLE}:iron-ore'
        and type(proof.get('inventory_now')) is int and proof['inventory_now'] == inventory.get('iron-ore', 0)
        and proof.get('basis') == 'recompiled_current_local_demand_and_owned_bootstrap_output'
        and proof.get('native_pickup_and_inventory_delta_require_verification') is True
        and proof.get('later_recipe_output_and_target_completion_unverified') is True
        and row.get('work_scope') == intent.get('scope') == 'immediate'
        and row.get('unknowns') == [] and row.get('reasons') == []
        and row.get('requires_investment') is False
        and local.get('ultimate_goal') == plan.goal
        and (plan.materials or {}).get('local_objective') == local
        and type(intent.get('observed_tick')) is int and intent['observed_tick'] == tick
        and type(provenance.get('observed_tick')) is int and provenance['observed_tick'] == tick
        and provenance.get('planner_item_path') == path and provenance.get('source_role') == ROLE
        and type(provenance.get('source_unit')) is int and provenance['source_unit'] == owned['chest_unit']
        and provenance.get('item') == 'iron-ore' and type(provenance.get('observed_output')) is int
        and provenance['observed_output'] == output['iron-ore'])



def _qualified_shared_parent_comparison(facts, plans, rows):
    """Expose two proven branches of one parent; never prefer or authorize either."""
    try:
        from copy import deepcopy
        from types import SimpleNamespace
        from .input_routes import sources, current, remaining
        if len(plans) != 2 or not isinstance(rows, dict):
            return None
        transfers = [p for p in plans if _qualified_recipe_transfer_chain(p, facts, rows[p.id])]
        pickups = [p for p in plans if _qualified_bootstrap_output_pickup(p, facts, rows[p.id])]
        if len(transfers) != 1 or len(pickups) != 1 or transfers[0].id == pickups[0].id:
            return None
        transfer, pickup = transfers[0], pickups[0]
        row = rows[transfer.id]; other = rows[pickup.id]
        marker = row['input_route_kit_parent_purpose']
        annotation = transfer.materials['input_route_kit_prerequisite']
        expected = dict(annotation, basis='same_tick_recompiled_owned_input_route_kit_need',
                        route_flow_and_parent_output_are_not_established=True,
                        later_steps_require_fresh_native_preconditions=True)
        canonical = lambda x: json.dumps(x, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=False, allow_nan=False)
        if (canonical(marker) != canonical(expected)
                or type(marker['schema']) is not int or marker['schema'] != 1
                or type(marker['observed_tick']) is not int or marker['observed_tick'] != facts['tick']
                or marker['session_id'] != facts['session_id']
                or type(marker['source_unit']) is not int or marker['source_unit'] <= 0
                or type(marker['kit_inventory_target']) is not int or marker['kit_inventory_target'] <= 0
                or canonical(marker['parent_local_objective']) != canonical(other['local_target'])
                or canonical(row['local_target']) != canonical({'item': marker['kit_item'],
                    'inventory_target': marker['kit_inventory_target'], 'ultimate_goal': transfer.goal})
                or transfer.goal != pickup.goal
                or marker['parent_local_objective']['ultimate_goal'] != transfer.goal):
            return None
        factory = dict(facts['factory'])
        factory['input_routes'] = facts['factory']['recipe_dependency_catalog'].get(
            'comparison_input_route', facts['factory']['input_routes'])
        snapshot = SimpleNamespace(factory=factory, session_id=facts['session_id'],
                                   tick=facts['tick'], inventory=facts['inventory'])
        route = sources(snapshot)[marker['source']]
        compact_route = facts['factory']['input_routes']['sources'][marker['source']]
        if any(canonical(compact_route[key]) != canonical(route[key]) for key in
               ('source_unit', 'layout', 'state', 'item', 'ore', 'reserve_belts', 'topology', 'flow')):
            return None
        reserve = 0
        if route['state'] == 'proposed':
            recipe = facts['factory']['recipe_dependency_catalog']['recipes'].get('logistic-science-pack')
            if recipe is not None:
                if not isinstance(recipe, dict) or recipe.get('name') != 'logistic-science-pack':
                    return None
                for key in ('ingredients', 'products'):
                    if (not isinstance(recipe.get(key), list) or not recipe[key]
                            or any(not isinstance(part, dict) or type(part.get('amount')) not in (int, float)
                                   or not math.isfinite(part['amount']) or part['amount'] <= 0
                                   or part.get('type') != 'item' or type(part.get('name')) is not str
                                   or type(part.get('probability', 1)) not in (int, float)
                                   or part.get('probability', 1) != 1 for part in recipe[key])):
                        return None
                products = [part for part in recipe['products'] if part['name'] == 'logistic-science-pack']
                if len(products) != 1:
                    return None
                per_batch = sum(part['amount'] for part in recipe['ingredients'] if part['name'] == 'transport-belt')
                reserve = math.ceil(per_batch * math.ceil(20 / products[0]['amount']))
                if not 0 <= reserve <= 200:
                    return None
        else:
            reserve = route['reserve_belts']
        pickup_proof = other['bootstrap_output_pickup_start_evidence']
        transfer_proof = row['recipe_input_transfer_start_evidence']
        path = pickup_proof['planner_item_path']
        if (not current(route, snapshot) or route['state'] not in {'proposed', 'building'}
                or route['layout'] != marker['layout'] or route['source_unit'] != marker['source_unit']
                or marker['state'] != route['state']
                or marker['source'] != transfer_proof['owned_source_role']
                or marker['source_unit'] != transfer_proof['owned_source_unit']
                or marker['source'] != 'recipe:' + path[-2]
                or canonical(marker['parent_planner_item_path']) != canonical(path[:-1])
                or transfer_proof['ingredient'] != path[-1]
                or canonical(marker['remaining_route_bill']) != canonical(remaining(route, reserve))
                or type(marker['construction_fuel_inventory_target']) is not int
                or marker['construction_fuel_inventory_target'] < 0):
            return None
        return {'schema': 1, 'observed_tick': facts['tick'], 'session_id': facts['session_id'],
            'basis': 'current_owned_route_and_independently_qualified_recipe_branches',
            'parent_target': deepcopy(marker['parent_local_objective']),
            'kit_branch': {'plan_id': transfer.id,
                'physical_route_bill': deepcopy(remaining(route, 0)),
                'belt_reserve_for_twenty_logistic_science': reserve},
            'direct_parent_branch': {'plan_id': pickup.id},
            'scope': 'alternative_partial_branches_not_joint_completion',
            'future_route_flow_and_recipe_outputs_unverified': True,
            'preference_or_execution_authorized': False}
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None

def _qualified_direct_parent_demand(plan, row, selected, facts) -> bool:
    """Check bindings before explaining a validated current gather purpose."""
    witness = row.get('direct_alternative_parent_demand_start_evidence')
    if (not isinstance(witness, dict) or not isinstance(facts, dict)
            or len(plan.steps) != 1):
        return False
    step = plan.steps[0]
    parameters = step.parameters if isinstance(step.parameters, dict) else {}
    parent_id = witness.get('parent_plan_id')
    parents = [candidate for candidate in selected if candidate.id == parent_id]
    path = witness.get('current_direct_recipe_path')
    raw = row.get('raw_prerequisite')
    start = row.get('gather_start_evidence')
    local = row.get('local_target')
    provenance = row.get('work_scope_provenance')
    intent = (plan.materials or {}).get('work_intent')
    return (
        type(witness.get('schema')) is int and witness['schema'] == 1
        and type(facts.get('tick')) is int
        and type(witness.get('observed_tick')) is int
        and witness['observed_tick'] == facts['tick']
        and isinstance(facts.get('session_id'), str) and bool(facts['session_id'])
        and witness.get('session_id') == facts['session_id']
        and witness.get('basis') == 'same_tick_current_parent_demand_and_catalog_recipe_input_path'
        and len(parents) == 1 and parents[0].id != plan.id
        and len(parents[0].steps) == 1
        and parents[0].steps[0].action == witness.get('parent_action') == 'factory_outpost_build'
        and witness.get('parent_outpost_still_proposed_and_allowed') is True
        and witness.get('parent_work_intent_scope') == row.get('work_scope') == 'immediate'
        and isinstance(provenance, dict) and isinstance(intent, dict)
        and provenance.get('compiled_scope') == intent.get('scope')
        and provenance.get('compiled_scope') in {'immediate', 'lookahead'}
        and provenance.get('qualified_current_scope') == 'immediate'
        and provenance.get('basis') == witness.get('basis')
        and step.action == 'factory_gather' and step.effect == 'inventory'
        and step.costs in (None, {})
        and parameters.get('resource') == step.item == witness.get('gather_resource')
        and all(type(witness.get(key)) is int for key in (
            'gather_quantity', 'gather_inventory_now', 'gather_inventory_target',
            'parent_local_target_inventory', 'parent_proposed_request_amount'))
        and type(parameters.get('quantity')) is int
        and 1 <= parameters['quantity'] == witness.get('gather_quantity') <= 50
        and witness['gather_inventory_now'] >= 0
        and witness['parent_proposed_request_amount'] >= 1
        and type(step.threshold) is int
        and step.threshold == witness.get('gather_inventory_target')
        and step.threshold == witness['gather_inventory_now'] + parameters['quantity']
        and isinstance(local, dict)
        and local.get('item') == witness.get('parent_local_target_item')
        and local.get('inventory_target') == witness.get('parent_local_target_inventory')
        and isinstance(path, list) and 2 <= len(path) <= 32
        and path[0] == local.get('item') and path[-1] == step.item
        and isinstance(raw, dict) and raw.get('observed_tick') == facts['tick']
        and raw.get('planner_item_path') == path
        and isinstance(start, dict) and start.get('observed_tick') == facts['tick']
        and start.get('session_id') == facts['session_id']
        and start.get('resource_in_current_observation') is True
        and start.get('fair_target_identity_observed') is True
        and start.get('resource_inventory_now') == witness.get('gather_inventory_now')
        and start.get('target_inventory_after_this_step') == step.threshold
        and witness.get('native_actor_bound_and_inventory_fresh') is True
        and type(witness.get('useful_partial_benefit_level')) is int
        and witness['useful_partial_benefit_level'] == 1
        and witness.get('gather_and_later_recipe_output_require_fresh_native_verification') is True
        and witness.get('does_not_establish_gathered_output_or_local_target_completion') is True
        and witness.get('does_not_establish_outpost_payback_or_completion') is True
        and witness.get('parent_utility_annotation_is_not_power_start_evidence') is True)


def _qualified_utility_lab_dependency(plan, row, local, tick) -> bool:
    if not isinstance(row, dict) or not isinstance(local, dict):
        return False
    step = plan.steps[0] if len(plan.steps) == 1 else None
    dependency = row.get('utility_lab_research_dependency')
    primary = local.get('primary_target')
    technology_target = primary.get('primary_target') if isinstance(primary, dict) else None
    technology = (technology_target.get('technology')
                  if isinstance(technology_target, dict) else None)
    return (
        step is not None
        and step.action == 'factory_place'
        and step.parameters == {'role': 'utility:lab', 'name': 'lab', 'anchor': 'factory'}
        and step.costs == {'lab': 1}
        and row.get('work_scope') == 'immediate'
        and isinstance(primary, dict)
        and primary.get('kind') == 'research_prerequisite'
        and primary.get('ultimate_goal') == plan.goal
        and primary.get('immediate_prerequisite') == 'utility:lab'
        and primary.get('observed_tick') == tick
        and primary.get('basis') == 'current_capability_research_plan'
        and type(tick) is int
        and isinstance(dependency, dict)
        and dependency.get('observed_tick') == tick
        and dependency.get('technology') == technology
        and dependency.get('basis') ==
            'same_tick_capability_research_plan_and_paid_lab_prerequisite'
        and all(dependency.get(key) is True for key in (
            'technology_not_researched_now', 'technology_unlocks_basic_assembler',
            'current_research_idle', 'current_technology_prerequisites_satisfied',
            'lab_required_by_native_research_walk', 'utility_lab_absent_now',
            'player_connected_and_bound_now', 'crafting_queue_empty_now',
            'placement_site_clearance_unknown_until_dispatch',
            'travel_and_arrival_unverified',
            'existing_native_action_performs_bounded_search_and_fresh_build_checks',
            'native_build_result_and_fresh_role_postcondition_required',
            'lab_power_and_research_require_later_native_verification'))
        and dependency.get('native_placement_site_preflight_performed') is False
        and isinstance(row.get('unknowns'), list)
        and {'placement_site:factory_place', 'travel:factory_place'} <=
            set(row.get('unknowns', []))
        and type(dependency.get('paid_lab_in_inventory_now')) is int
        and dependency['paid_lab_in_inventory_now'] >= 1
    )


def _qualified_power_child(step, row, evidence, tick, facts=None):
    child = evidence.get('child_start_evidence')
    kind = evidence.get('next_action_kind')
    contracts = {
        'utility_chain_raw_gather_start': ('factory_gather', {'gather_start_evidence', 'raw_prerequisite'}),
        'utility_chain_furnace_fuel_gather_start': ('factory_gather', {'gather_start_evidence', 'fuel_prerequisite'}),
        'utility_chain_handcraft_start': (step.action if step.action in {'factory_craft', 'factory_craft_job'} else None, {'craft_start_evidence'}),
        'utility_chain_output_pickup_start': ('factory_extract', {'output_pickup_start_evidence'}),
        'utility_chain_recipe_input_transfer_start': ('factory_insert', {'recipe_input_transfer_start_evidence'}),
        'utility_chain_furnace_fuel_transfer_start': ('factory_insert', {'fuel_transfer_start_evidence'}),
        'utility_chain_buffer_build_start': ('factory_buffer_build', {'buffer_build_start_evidence'}),
        'utility_chain_buffer_fuel_transfer_start': ('factory_insert', {'buffer_fuel_start_evidence'}),
    }
    if kind not in contracts or not isinstance(child, dict):
        return False
    action, fields = contracts[kind]
    parameters = step.parameters or {}
    witnesses = child.get('witnesses')
    path = child.get('planner_item_path')
    if (step.action != action or child.get('action') != action or child.get('kind') != kind
            or type(child.get('observed_tick')) is not int or child['observed_tick'] != tick
            or child.get('item') != (parameters.get('item')
                                     if action in {'factory_insert', 'factory_extract'} else step.item)
            or child.get('role') != (parameters.get('source') if action == 'factory_buffer_build'
                                    else parameters.get('role') or parameters.get('resource'))
            or child.get('quantity') != parameters.get('quantity')
            or child.get('step_costs') != (step.costs or {})
            or child.get('witness_fields') != sorted(fields)
            or not isinstance(witnesses, dict) or set(witnesses) != fields
            or any(not isinstance(witness, dict) or row.get(name) != witness
                   for name, witness in witnesses.items())
            or not isinstance(path, list) or len(path) > 32
            or any(not isinstance(item, str) or not item for item in path)
            or child.get('completion_requires_fresh_native_receipt_or_inventory') is not True):
        return False
    if any(type(w.get('observed_tick')) is not int or w.get('observed_tick') != tick
           for w in witnesses.values()):
        return False
    if action == 'factory_buffer_build':
        return _qualified_buffer_build(facts, step, witnesses['buffer_build_start_evidence'], tick)
    if kind == 'utility_chain_buffer_fuel_transfer_start':
        return _qualified_buffer_fuel(facts, step, witnesses['buffer_fuel_start_evidence'], tick)
    if action == 'factory_gather':
        gather = witnesses['gather_start_evidence']
        quantity = parameters.get('quantity')
        if (type(quantity) is not int or not 1 <= quantity <= 50
                or gather.get('session_id') != evidence.get('session_id')
                or gather.get('resource_in_current_observation') is not True
                or gather.get('fair_target_identity_observed') is not True
                or type(gather.get('resource_inventory_now')) is not int
                or step.item != parameters.get('resource')
                or step.threshold != gather['resource_inventory_now'] + quantity):
            return False
        if kind == 'utility_chain_raw_gather_start':
            raw = witnesses['raw_prerequisite']
            return (raw.get('basis') == 'current_planner_dependency_and_native_catalog_recipe'
                    and raw.get('planner_item_path') == path and path[-1:] == [step.item])
        fuel = witnesses['fuel_prerequisite']
        return (fuel.get('basis') == 'current_planner_fuel_need_and_owned_native_burner'
                and step.item == 'coal' and fuel.get('planned_gather_units') == quantity
                and type(fuel.get('current_unfunded_units')) is int
                and fuel['current_unfunded_units'] > 0)
    if action in {'factory_craft', 'factory_craft_job'}:
        craft = witnesses['craft_start_evidence']
        return (craft.get('native_recipe') == parameters.get('recipe')
                and type(parameters.get('batches')) is int and parameters['batches'] > 0
                and all(craft.get(key) is True for key in (
                    'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                    'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                    'crafting_queue_empty'))
                and (action != 'factory_craft_job' or all(craft.get(key) is True for key in (
                    'craft_job_protocol_ready', 'native_receipt_required_for_completion'))))
    witness = next(iter(witnesses.values()))
    role, item, quantity = parameters.get('role'), parameters.get('item'), parameters.get('quantity')
    receipt = f'{tick}:{action}:{role}:{item}'
    if type(quantity) is not int or quantity < 1 or parameters.get('receipt') != receipt:
        return False
    if action == 'factory_extract':
        return (witness.get('basis') == 'current_planner_output_and_owned_native_machine'
                and witness.get('owned_source_role') == role and witness.get('ready_output_item') == item
                and witness.get('planned_pickup_quantity') == quantity
                and witness.get('planned_native_receipt_id') == receipt)
    if kind == 'utility_chain_recipe_input_transfer_start':
        return (witness.get('basis') == 'current_planner_recipe_input_and_owned_native_machine'
                and witness.get('owned_source_role') == role and witness.get('ingredient') == item
                and witness.get('paid_quantity_to_transfer') == quantity
                and witness.get('planned_native_receipt_id') == receipt)
    local = row.get('local_target')
    return (isinstance(role, str) and role.startswith('recipe:')
            and bool(path) and path[-1] == role.removeprefix('recipe:')
            and witness.get('planner_item_path') == path
            and (not isinstance(local, dict) or path[0] == local.get('item'))
            and witness.get('basis') == 'current_planner_need_owned_burner_and_paid_inventory'
            and witness.get('burner_role') == role and item == 'coal'
            and witness.get('coal_to_transfer') == quantity and witness.get('native_receipt') == receipt)


def _qualified_utility_power_dependency(plan, row, tick, facts=None):
    """Give prerequisite guidance only for a current, action-bound witness."""
    evidence = row.get('utility_power_prerequisite_start_evidence')
    annotation = (plan.materials or {}).get('utility_power_prerequisite')
    if (len(plan.steps) != 1 or not isinstance(evidence, dict)
            or not isinstance(annotation, dict) or type(tick) is not int
            or type(evidence.get('observed_tick')) is not int
            or evidence.get('observed_tick') != tick
            or evidence.get('basis') != 'exact_current_power_planner_step_and_coherent_native_chain'
            or evidence.get('does_not_establish_electricity_or_research_completion') is not True
            or evidence.get('boiler_catalog_burner_current') is not True
            or evidence.get('placement_or_connection_rechecks_native_preconditions') is not True
            or not isinstance(evidence.get('session_id'), str) or not evidence['session_id']
            or row.get('work_scope') != 'immediate'):
        return False
    role, unit = evidence.get('consumer_role'), evidence.get('consumer_unit')
    path, research = evidence.get('planner_path'), evidence.get('research')
    if (not isinstance(role, str) or not role or type(unit) is not int or unit < 1
            or not isinstance(path, list) or len(path) > 32
            or any(not isinstance(entry, str) or not entry for entry in path)
            or annotation != {'observed_tick': tick, 'consumer_role': role,
                              'consumer_unit': unit, 'planner_path': path, 'research': research}
            or research != next((entry.removeprefix('technology:')
                                 for entry in reversed(path)
                                 if entry.startswith('technology:')), None)):
        return False
    demand = evidence.get('consumer_demand')
    if not isinstance(demand, dict):
        return False
    if role == 'utility:lab':
        if research is None:
            if plan.goal != 'steam_power' or demand.get('kind') != 'explicit_steam_power_goal':
                return False
        elif (plan.goal != 'rocket_launch' or demand.get('kind') != 'current_technology_lab_demand'
              or demand.get('technology') != research
              or demand.get('technology_not_researched_now') is not True
              or demand.get('technology_prerequisites_satisfied_now') is not True):
            return False
    elif (not role.startswith('recipe:') or plan.goal != 'rocket_launch'
          or demand.get('kind') != 'current_native_recipe_demand'
          or demand.get('recipe') != role.removeprefix('recipe:')
          or demand.get('recipe_enabled_now') is not True
          or demand.get('machine_recipe_matches_now') is not True):
        return False
    connections, units = evidence.get('connections_current'), evidence.get('utility_units_current')
    if (not isinstance(connections, dict) or set(connections) != {
            'water_to_boiler', 'boiler_to_engine_steam', 'engine_to_consumer_electricity'}
            or any(type(value) is not bool for value in connections.values())
            or not isinstance(units, dict) or set(units) != {'water_pump', 'boiler', 'steam_engine'}
            or any(value is not None and (type(value) is not int or value < 1)
                   for value in units.values())):
        return False
    observed_units = [unit, *(value for value in units.values() if value is not None)]
    if len(observed_units) != len(set(observed_units)):
        return False
    step = plan.steps[0]
    if evidence.get('next_action') != step.action:
        return False
    kind = evidence.get('next_action_kind')
    if _qualified_power_child(step, row, evidence, tick, facts):
        return True
    if kind in {'boiler_fuel_transfer_start', 'boiler_fuel_gather_start'}:
        fuel, deficit, carried = (evidence.get('boiler_coal_now'),
                                  evidence.get('boiler_coal_deficit_to_five'),
                                  evidence.get('actor_coal_now'))
        if (not all(connections.values()) or any(type(value) is not int for value in units.values())
                or len({unit, *units.values()}) != 4
                or type(fuel) is not int or not 0 <= fuel < 5
                or type(carried) is not int or carried < 0 or type(deficit) is not int
                or row.get('unknowns') != []):
            return False
        if kind == 'boiler_fuel_transfer_start':
            receipt = f'{tick}:factory_insert:utility:boiler:coal'
            return (step.action == 'factory_insert' and step.effect == 'transfer'
                    and deficit == 5 - fuel and carried >= deficit
                    and step.parameters == {'role': 'utility:boiler', 'item': 'coal',
                                            'quantity': deficit, 'receipt': receipt}
                    and step.costs == {'coal': deficit}
                    and evidence.get('planned_native_receipt') == receipt
                    and evidence.get('planned_receipt_absent_now') is True
                    and evidence.get('transfer_receipt_observed_now') is False
                    and evidence.get('paid_inventory_sufficient_now') is True
                    and evidence.get('native_transfer_rechecks_reach_capacity_receipt_and_postcondition') is True)
        gather = evidence.get('gather_start_evidence')
        return (step.action == 'factory_gather' and deficit == 5 - fuel - carried and deficit > 0
                and (step.parameters or {}).get('resource') == 'coal'
                and (step.parameters or {}).get('quantity') == deficit
                and step.threshold == carried + deficit and isinstance(gather, dict)
                and gather.get('observed_tick') == tick and gather.get('target_item') == 'coal'
                and gather.get('would_close_current_shortfall_if_native_inventory_verifies') is True
                and gather.get('inventory_basis') == 'coherent_snapshot_and_atomic_native_inventory')
    if kind == 'utility_entity_construction_start' and step.action == 'factory_place':
        expected = next((parameters for key, parameters in (
            ('water_pump', {'role': 'utility:water', 'name': 'offshore-pump', 'anchor': 'water'}),
            ('boiler', {'role': 'utility:boiler', 'name': 'boiler', 'anchor': 'utility:water'}),
            ('steam_engine', {'role': 'utility:engine', 'name': 'steam-engine', 'anchor': 'utility:boiler'}),
        ) if units[key] is None), None)
        return (expected is not None and step.parameters == expected
                and step.costs == {expected['name']: 1})
    if kind == 'utility_connection_start' and step.action == 'factory_connect':
        if any(type(value) is not int for value in units.values()):
            return False
        expected = next((parameters for key, parameters in (
            ('water_to_boiler', {'source': 'utility:water', 'target': 'utility:boiler',
                                 'kind': 'pipe', 'fluid': 'water'}),
            ('boiler_to_engine_steam', {'source': 'utility:boiler', 'target': 'utility:engine',
                                       'kind': 'pipe', 'fluid': 'steam'}),
            ('engine_to_consumer_electricity', {'source': 'utility:engine', 'target': role,
                                               'kind': 'small-electric-pole', 'fluid': 'electricity'}),
        ) if not connections[key]), None)
        return expected is not None and step.parameters == expected
    return False


def _json_identity(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def _qualified_unused_buffer_receipt(facts, proof, receipt, tick):
    """Check the producer's bounded same-tick query of the native receipt map."""
    try:
        observation = proof['native_receipt_query']
        factory = facts['factory']
        count = observation['receipt_count']
        if (set(observation) != {'schema','session_id','tick','receipt_count','receipt','present','map_verified'}
                or type(observation['schema']) is not int or observation['schema'] != 1
                or observation['session_id'] != facts['session_id']
                or type(observation['tick']) is not int or observation['tick'] != tick
                or observation['receipt'] != receipt or observation['present'] is not False
                or observation['map_verified'] is not True
                or type(count) is not int or count < 0):
            return False
        if ('native_transfer_receipt_count' in factory
                and (type(factory['native_transfer_receipt_count']) is not int
                     or factory['native_transfer_receipt_count'] != count)):
            return False
        if 'receipts' in factory:
            receipts = factory['receipts']
            return isinstance(receipts, dict) and len(receipts) == count and receipt not in receipts
        return (type(factory.get('native_transfer_receipt_count')) is int
                and factory['native_transfer_receipt_count'] == count)
    except (KeyError,TypeError,ValueError):
        return False


def _qualified_buffer_fuel(facts, step, proof, tick):
    """Qualify bounded fuel for a paid output arm, never commissioned flow."""
    from .output_buffers import PARTS, SOURCES, SUCCESSOR_SOURCES, validate_commitments
    try:
        p = step.parameters
        factory, inventory = facts['factory'], facts['inventory']
        buffers = factory['output_buffers']
        row = buffers['sources'][proof['source_role']]
        source = factory['entities'][row['source']]
        arm = factory['entities'][p['role']]
        quantity, fuel, capacity = p['quantity'], arm['fuel'].get('coal', 0), arm['fuel_insertable']['coal']
        coal, ready = inventory['coal'], source['output'][row['item']]
        if (step.action != 'factory_insert' or step.effect != 'transfer'
                or set(p) != {'role', 'item', 'quantity', 'receipt'} or p['item'] != 'coal'
                or type(quantity) is not int or step.costs != {'coal': quantity}
                or type(tick) is not int or type(proof.get('observed_tick')) is not int
                or proof['observed_tick'] != tick or proof.get('session_id') != facts['session_id']
                or type(buffers.get('protocol')) is not int or buffers['protocol'] != 1
                or buffers.get('session_id') != facts['session_id'] or buffers.get('tick') != tick
                or not isinstance(facts.get('game_version'), str) or not facts['game_version'].startswith('2.0.')
                or proof.get('native_catalog_version') != facts['game_version']
                or proof.get('basis') != 'current_paid_output_buffer_arm_commissioning'
                or row.get('source') != proof['source_role'] or row.get('item') != row['source'].removeprefix('recipe:')
                or row.get('source_unit') != proof.get('source_unit') or row['item'] != proof.get('source_item')
                or row.get('layout') != proof.get('layout') or row.get('parts') != proof.get('paid_parts')
                or set(row['parts']) != set(PARTS) or row.get('state') != 'ready' or row.get('topology') is not True
                or row['parts']['inserter']['role'] != p['role'] or proof.get('burner_role') != p['role']
                or row['parts']['inserter']['unit_number'] != proof.get('burner_unit')
                or type(fuel) is not int or not 0 <= fuel < 2
                or type(capacity) is not int or capacity <= 0
                or type(coal) is not int or type(ready) is not int or ready < 1
                or not 0 < quantity <= min(coal, 5-fuel, capacity)
                or any(type(proof.get(k)) is not int for k in ('fuel_now','fuel_insertable_now',
                    'coal_in_inventory_now','coal_to_transfer','current_coal_deficit','ready_source_output_now'))
                or proof['fuel_now'] != fuel or proof['fuel_insertable_now'] != capacity
                or proof['coal_in_inventory_now'] != coal or proof['coal_to_transfer'] != quantity
                or proof['current_coal_deficit'] != min(5-fuel, capacity) or proof['ready_source_output_now'] != ready
                or proof.get('actor_inventory_now') != inventory
                or p['receipt'] != f"{tick}:factory_insert:{p['role']}:coal" or proof.get('native_receipt') != p['receipt']
                or not _qualified_unused_buffer_receipt(facts, proof, p['receipt'], tick)
                or factory.get('player_connected') is not True or factory.get('player_bound') is not True
                or type(factory.get('crafting_queue')) is not int or factory['crafting_queue'] != 0
                or any(proof.get(k) is not True for k in ('planned_receipt_absent_now',
                    'player_connected_and_bound_now','crafting_queue_empty_now',
                    'native_transfer_and_later_flow_require_verification','flow_not_established'))):
            return False
        owners = buffers['sources']
        if len(owners) > len(SOURCES | SUCCESSOR_SOURCES):
            return False
        validate_commitments({role: {'source_unit': owner['source_unit'], 'layout': owner['layout'],
                                    'parts': owner['parts']} for role, owner in owners.items()},
                             successors=bool(set(owners) & SUCCESSOR_SOURCES))
        if any(paid['receipt'] == p['receipt'] for owner in owners.values() for paid in owner['parts'].values()):
            return False
        if source.get('unit_number') != row['source_unit'] or source.get('name') != 'stone-furnace':
            return False
        for name, paid in row['parts'].items():
            entity = factory['entities'][paid['role']]
            if entity.get('unit_number') != paid['unit_number'] or entity.get('name') != PARTS[name]:
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _qualified_buffer_build(facts, step, proof, tick):
    """Qualify owned paid start conditions, leaving geometry and flow native."""
    from .output_buffers import PARTS, SOURCES, SUCCESSOR_SOURCES, validate, validate_commitments
    try:
        parameters = step.parameters
        validate(parameters)
        factory, inventory = facts['factory'], facts['inventory']
        buffers = factory['output_buffers']
        role, part = parameters['source'], parameters['part']
        row = buffers['sources'][role]
        component = PARTS[part]
        if (step.action != 'factory_buffer_build' or step.effect != 'buffer_component'
                or step.costs != {component: 1}
                or type(tick) is not int or type(proof.get('observed_tick')) is not int
                or proof['observed_tick'] != tick or proof.get('session_id') != facts['session_id']
                or type(buffers.get('protocol')) is not int or buffers['protocol'] != 1
                or buffers.get('session_id') != facts['session_id'] or buffers.get('tick') != tick
                or proof.get('basis') != 'current_paid_partial_output_buffer_next_component'
                or not isinstance(facts.get('game_version'), str)
                or not facts['game_version'].startswith('2.0.')
                or proof.get('native_catalog_version') != facts['game_version']
                or proof.get('source_role') != role or proof.get('source_unit') != row['source_unit']
                or proof.get('source_item') != row['item'] or row['item'] != role.removeprefix('recipe:')
                or row.get('source') != role or row.get('state') != 'building'
                or proof.get('layout') != parameters['layout'] or row['layout'] != parameters['layout']
                or proof.get('part') != part or proof.get('component_item') != component
                or type(proof.get('component_quantity')) is not int or proof['component_quantity'] != 1
                or not row['parts'] or proof.get('paid_parts') != row['parts']
                or next((p for p in PARTS if p not in row['parts']), None) != part
                or proof.get('actor_inventory_now') != inventory
                or type(inventory.get(component)) is not int or inventory[component] < 1
                or parameters['receipt'] != f"buffer:{tick}:{row['source_unit']}:{part}"
                or proof.get('receipt') != parameters['receipt']
                or not _qualified_unused_buffer_receipt(facts, proof, parameters['receipt'], tick)
                or factory.get('player_connected') is not True or factory.get('player_bound') is not True
                or type(factory.get('crafting_queue')) is not int or factory['crafting_queue'] != 0
                or any(proof.get(key) is not True for key in (
                    'paid_component_in_inventory_now', 'planned_receipt_absent_now',
                    'player_connected_and_bound_now', 'crafting_queue_empty_now',
                    'native_prepare_rechecks_geometry_and_clearance',
                    'approach_and_placement_require_native_verification', 'flow_not_established'))):
            return False
        owners = buffers['sources']
        if not isinstance(owners, dict) or len(owners) > len(SOURCES | SUCCESSOR_SOURCES):
            return False
        validate_commitments({source: {'source_unit': owner['source_unit'], 'layout': owner['layout'],
                                      'parts': owner['parts']} for source, owner in owners.items()},
                             successors=bool(set(owners) & SUCCESSOR_SOURCES))
        if any(paid['receipt'] == parameters['receipt'] for owner in owners.values() for paid in owner['parts'].values()):
            return False
        source = factory['entities'][role]
        if source.get('unit_number') != row['source_unit'] or source.get('name') != 'stone-furnace':
            return False
        for name, paid in row['parts'].items():
            entity = factory['entities'][paid['role']]
            if entity.get('unit_number') != paid['unit_number'] or entity.get('name') != PARTS[name]:
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _qualified_buffer_component(facts, plan, proof, start, path, tick):
    """Check a current paid construction bridge, not a target recipe edge."""
    from .output_buffers import PARTS, SOURCES, SUCCESSOR_SOURCES, validate_commitments
    from .planning.catalog import Catalog

    try:
        if len(plan.steps) != 1:
            return False
        step = plan.steps[0]
        materials = plan.materials or {}
        factory = facts['factory']
        inventory = facts['inventory']
        buffers = factory['output_buffers']
        row = buffers['sources'][proof['source_role']]
        part = proof['next_part']
        component = proof['component_item']
        item = proof['pickup_item']
        required = proof['component_input_required']
        carried = proof['actor_item_now']
        deficit = proof['component_input_deficit']
        if (not isinstance(proof, dict) or proof != materials.get('buffer_component_prerequisite')
                or proof.get('basis') != 'current_paid_output_buffer_missing_component_bill'
                or type(tick) is not int or type(proof.get('observed_tick')) is not int
                or proof.get('observed_tick') != tick
                or type(buffers.get('protocol')) is not int or buffers['protocol'] != 1
                or buffers.get('session_id') != facts['session_id'] or buffers.get('tick') != tick
                or proof.get('component_craft_build_and_flow_require_native_verification') is not True
                or type(proof.get('component_quantity')) is not int or proof['component_quantity'] != 1
                or part not in PARTS or PARTS[part] != component
                or row.get('state') != 'building' or row.get('source') != proof['source_role']
                or row.get('item') != row['source'].removeprefix('recipe:')
                or row.get('source_unit') != proof['source_unit'] or row.get('layout') != proof['layout']
                or row.get('parts') != proof['paid_parts'] or not row.get('parts')
                or next((name for name in PARTS if name not in row['parts']), None) != part
                or proof['actor_inventory_now'] != inventory
                or type(required) is not int or type(carried) is not int or type(deficit) is not int
                or not 0 <= carried < required <= 200 or deficit != required - carried
                or inventory.get(item, 0) != carried
                or not isinstance(path, list) or component not in path or path[-1] != item):
            return False
        if step.action == 'factory_extract':
            if (item != start['ready_output_item']
                    or type(start['planned_pickup_quantity']) is not int
                    or not 0 < start['planned_pickup_quantity'] <= deficit):
                return False
        elif step.action == 'factory_craft':
            if (item != step.item or start.get('observed_tick') != tick
                    or start.get('native_recipe') != step.parameters.get('recipe')
                    or type(step.parameters.get('batches')) is not int
                    or step.parameters['batches'] < 1
                    or any(start.get(key) is not True for key in (
                        'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                        'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                        'crafting_queue_empty'))
                    or type(start.get('expected_products_after_native_verification', {}).get(item)) is not int
                    or not 0 < start['expected_products_after_native_verification'][item] <= deficit
                    or any(inventory.get(name, 0) < count for name, count in step.costs.items())):
                return False
        else:
            return False
        owners = buffers['sources']
        if not isinstance(owners, dict) or len(owners) > len(SOURCES | SUCCESSOR_SOURCES):
            return False
        validate_commitments({role: {
            'source_unit': owner['source_unit'], 'layout': owner['layout'], 'parts': owner['parts']}
            for role, owner in owners.items()}, successors=bool(set(owners) & SUCCESSOR_SOURCES))
        if factory['entities'][row['source']]['unit_number'] != row['source_unit']:
            return False
        for name, paid in row['parts'].items():
            entity = factory['entities'][paid['role']]
            if entity['unit_number'] != paid['unit_number'] or entity['name'] != PARTS[name]:
                return False
        recipes = proof['native_recipes']
        batches = proof['native_recipe_batches']
        if (not isinstance(recipes, dict) or len(recipes) > 32
                or not isinstance(batches, dict) or set(batches) != set(recipes)
                or any(type(count) is not int or not 0 < count <= 200 for count in batches.values())):
            return False
        # The producer bound these native recipes to its current catalog. Here
        # recompute the disclosed bill so altered quantities cannot gain hints.
        version = facts['game_version']
        if (not isinstance(version, str) or not version.startswith('2.0.')
                or proof.get('native_catalog_version') != version):
            return False
        catalog = Catalog(version, recipes, {}, {}, {})
        for name, recipe in recipes.items():
            if (recipe.get('name') != name or recipe.get('hidden')
                    or recipe.get('enabled') is not True):
                return False
        stock = dict(inventory)
        stock[item] = 1000000
        bill = catalog.material_plan(component, 1, stock, [])
        if (bill.batches != batches or 1000000 - bill.remaining.get(item, 0) != required):
            return False
        start = path.index(component)
        for product, ingredient in zip(path[start:], path[start + 1:]):
            recipe = catalog.recipe_for(product)
            if not any(entry.get('type') == 'item' and entry.get('name') == ingredient
                       and entry.get('amount', 0) > 0 for entry in recipe['ingredients']):
                return False
        return path[-1] == item
    except (ArithmeticError, KeyError, TypeError, ValueError, AttributeError, StopIteration):
        return False


def _compact_plan_documents(plans):
    """Factor identical large material records without deleting any evidence."""
    documents = {plan.id: plan.to_dict() for plan in plans}
    if len(documents) < 2:
        return documents, {}
    materials = [document.get("materials") or {} for document in documents.values()]
    shared = {}
    for key, value in materials[0].items():
        if (isinstance(value, (dict, list))
                and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 256
                and all(key in row and _json_identity(row[key]) == _json_identity(value)
                        for row in materials[1:])):
            shared[key] = value
    if shared:
        for document in documents.values():
            document["materials"] = {key: value for key, value in
                                     (document.get("materials") or {}).items() if key not in shared}
            document["shared_materials_keys"] = sorted(shared)
    return documents, shared


def _factor_bootstrap_recipes(context):
    """Losslessly factor identical recipe records for qualified chain packets."""
    from copy import deepcopy
    if not any(isinstance(row, dict) and isinstance(row.get('bootstrap_output_pickup_start_evidence'), dict)
            and 'recipe_dependency_chain' in row['bootstrap_output_pickup_start_evidence']
            for row in (context.get('candidate_evidence') or {}).values()):
        return context
    def reserved(value):
        if isinstance(value, dict):
            return ('shared_recipes' in value or 'shared_recipe_key' in value
                    or any(reserved(child) for child in value.values()))
        return isinstance(value, list) and any(reserved(child) for child in value)
    if reserved(context):
        return context  # Existing caller fields must never be overwritten or reinterpreted.
    counts = {}; records = {}
    def collect(value):
        if isinstance(value, dict):
            if {'name', 'category', 'ingredients', 'products', 'enabled'} <= set(value):
                key = _json_identity(value)
                counts[key] = counts.get(key, 0) + 1; records[key] = value
            else:
                for child in value.values(): collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(context)
    keys = {identity: record['name'] for identity, record in records.items() if counts[identity] > 1}
    if len(set(keys.values())) != len(keys):
        return context  # Never merge unequal recipes with the same name.
    if not keys:
        return context
    def project(value):
        if isinstance(value, dict):
            identity = _json_identity(value)
            if identity in keys:
                return {'shared_recipe_key': keys[identity]}
            return {key: project(child) for key, child in value.items()}
        if isinstance(value, list):
            return [project(child) for child in value]
        return deepcopy(value)
    result = project(context)
    result['shared_recipes'] = {keys[key]: deepcopy(records[key]) for key in sorted(keys)}
    result['execution_contract'] += ' Resolve each shared_recipe_key through shared_recipes; it is the identical complete recipe record.'
    return result


def question_batch(state: dict, plans: list[Plan], max_bytes: int = 32000,
                   max_candidates: int = 16) -> tuple[dict, dict, list[Plan]]:
    """Bound serialized request bytes, NOT estimated tokens or provider limits."""
    if max_bytes < 1 or not 1 <= max_candidates <= 254:
        raise ValueError("Invalid request budget")
    selected = plans[:max_candidates]
    objective = "local_objective" if "local_objective" in state else "active_goal"
    if len({p.id for p in selected}) != len(selected):
        raise ValueError("Duplicate candidate IDs")
    while selected:
        plan_documents, shared_materials = _compact_plan_documents(selected)
        context = {
            **state, "candidate_plans": plan_documents,
            "judgment_contract": {
                "schema": 2,
                "eligibility": "explicit_useful_progress_choice",
                "benefit": "ordinal_ranking_with_negative_evidence_check",
            },
            "execution_contract": (
                "These are bounded tool plans, not keyboard commands or full-game strategies. "
                "Code filters plans for current resource, inventory, and placement preconditions "
                "and checks them again before dispatch. walk_to_coal and walk_to_iron move "
                "to an observed patch; mine_coal harvests five coal into inventory; "
                "place_burner_drill consumes one drill and one chest on iron; fuel_drill "
                "inserts five carried coal. factory_* steps execute the explicit parameters "
                "using native recipes, paid inventory transfers, machines, physical connections, "
                "and research. Native crafting waits for its real queue; native machines and "
                "labs must actually produce or research. Judge the supplied local_objective when present; "
                "otherwise judge active_goal. Estimates are not facts or execution permission. "
                "Each action needs a fresh observed postcondition before it counts as success."
            ),
        }
        if shared_materials:
            context["shared_plan_materials"] = shared_materials
            context["execution_contract"] += (
                " Each candidate's shared_materials_keys names material records in "
                "shared_plan_materials that also apply to that candidate. Read those "
                "records together with its own materials; no material proof is omitted.")
        if "candidate_evidence" in context:
            context["candidate_evidence"] = {p.id: state["candidate_evidence"][p.id]
                                             for p in selected if p.id in state["candidate_evidence"]}
        if "deterministic_ranking" in context:
            context["deterministic_ranking"] = [key for key in state["deterministic_ranking"]
                                                if key in context["candidate_plans"]]
        evidence = context.get('candidate_evidence') or {}
        facts = state.get('facts')
        tick = facts.get('tick') if isinstance(facts, dict) else None
        if any(_native_additive_connection_contract(plan, facts) for plan in selected):
            context['execution_contract'] += (
                " For a `factory_connect` candidate bound to the current paid native "
                "connector protocol, dispatch surveys a collision-aware route, reuses "
                "matching existing connectors, and places only missing pipes or poles "
                "from carried stock. It does not mine, remove, stop or rebuild existing "
                "factory entities. A blocked route or insufficient actual materials "
                "can reject native preparation; the planner's material allowance is "
                "not a surveyed placement count. Approach, placement receipts and "
                "fresh topology verification remain required, and flow is not established.")
        local = state.get('local_objective')
        primary = local.get('primary_target') if isinstance(local, dict) else None
        target = primary.get('item') if isinstance(primary, dict) else None
        def observed_gather(row):
            start = row.get('gather_start_evidence')
            if not isinstance(start, dict):
                return False
            return (start.get('resource_in_current_observation') is True
                    and start.get('fair_target_identity_observed') is True)
        current_prerequisite = any(
            isinstance(target, str) and target
            and row.get('work_scope') == 'immediate' and observed_gather(row)
            and isinstance(row.get('raw_prerequisite'), dict)
            and row['raw_prerequisite'].get('observed_tick') == tick
            and isinstance(row['raw_prerequisite'].get('planner_item_path'), list)
            and row['raw_prerequisite']['planner_item_path'][:1] == [target]
            for row in evidence.values())
        unlinked_lookahead = any(
            row.get('work_scope') == 'lookahead' and observed_gather(row)
            and row.get('raw_prerequisite') is None
            and row.get('fuel_prerequisite') is None
            and row.get('urgency') == 0
            for row in evidence.values())
        choice_priority_hint = (
            " When current observed start facts support both an immediate raw "
            "prerequisite with a current planner recipe path and an unlinked "
            "lookahead bulk gather with no observed urgency, favor the immediate "
            "prerequisite unless another current fact justifies the lookahead work. "
            "A larger pickup quantity alone is not such a fact. Later crafting "
            "and output still require fresh native verification."
            if current_prerequisite and unlinked_lookahead else "")
        bill_craft = any(
            isinstance(row, dict) and row.get('work_scope') == 'lookahead'
            and row.get('unknowns') == [] and row.get('urgency') == 0
            and isinstance(row.get('shared_bill_craft'), dict)
            and row['shared_bill_craft'].get('observed_tick') == tick
            and row['shared_bill_craft'].get('local_target_item') == target
            and row['shared_bill_craft'].get('forecast_is_not_paid_stock_or_completed_output') is True
            for row in evidence.values())
        bill_craft_hint = (
            " The lookahead handcraft has a current catalog-bill shortfall and "
            "receipt-tracked start facts. Compare its bounded contribution with "
            "the immediate raw prerequisite: an admitted receipt-tracked job "
            "may overlap a later independent gather, but overlap and output are not "
            "yet verified. Do not treat lack of a recursive craft path alone as "
            "evidence that this bill-linked craft is useless."
            if current_prerequisite and bill_craft else "")
        craft_choice_hint = ""
        if len(selected) == 1 and type(tick) is int and isinstance(target, str) and target:
            plan = selected[0]
            row = evidence.get(plan.id)
            if isinstance(row, dict) and len(plan.steps) == 1:
                step = plan.steps[0]
                start = row.get('craft_start_evidence')
                dependency = row.get('craft_dependency')
                path = dependency.get('planner_item_path') if isinstance(dependency, dict) else None
                expected = (start.get('expected_products_after_native_verification')
                            if isinstance(start, dict) else None)
                if (step.action == 'factory_craft_job'
                        and isinstance(step.item, str) and step.item
                        and isinstance(step.parameters, dict)
                        and isinstance(step.parameters.get('receipt'), str)
                        and bool(step.parameters['receipt'])
                        and row.get('work_scope') == 'immediate'
                        and row.get('unknowns') == []
                        and isinstance(start, dict)
                        and start.get('observed_tick') == tick
                        and start.get('native_recipe') == step.parameters.get('recipe')
                        and all(start.get(key) is True for key in (
                            'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                            'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                            'crafting_queue_empty', 'craft_job_protocol_ready',
                            'native_receipt_required_for_completion'))
                        and isinstance(expected, dict)
                        and type(expected.get(step.item)) is int
                        and expected[step.item] > 0
                        and isinstance(dependency, dict)
                        and dependency.get('observed_tick') == tick
                        and dependency.get('current_craft_product') == step.item
                        and dependency.get('basis') == (
                            'current_recursive_planner_provenance_and_native_recipe')
                        and isinstance(path, list) and len(path) >= 2
                        and path[0] == target and path[-1] == step.item):
                    craft_choice_hint = (
                        " The sole offered handcraft has current native recipe, "
                        "carried-input, actor, queue, and receipt-protocol start facts "
                        "plus a current planner path to the local target. Prefer this "
                        "bounded craft over observe unless another current fact "
                        "identifies a specific missing or contradictory start condition. "
                        "Do not require certainty that the eventual target will finish; "
                        "this craft and later output still require native receipt and "
                        "fresh postcondition checks.")
        utility_lab_choice_hint = ""
        for candidate_plan in selected:
            candidate_row = evidence.get(candidate_plan.id)
            if not _qualified_utility_lab_dependency(
                    candidate_plan, candidate_row, local, tick):
                continue
            utility_lab_choice_hint += (
                " This paid lab is the current planner's immediate prerequisite for "
                "starting the named, enabled capability technology that unlocks the "
                "basic assembler. The native placement site and walking outcome have "
                "not been observed: the existing placement action performs its bounded "
                "native search and fresh build checks; only the native action result and "
                "fresh role observation verify placement. "
                "Judge this next action from its current paid item, absent role, idle bound "
                "actor, and exact research dependency; do not infer a clear site, arrival, "
                "lab power, completed research, or assembler. Observe only for a specific "
                "missing current start fact, not to demand certainty about those later outcomes."
            )
        outpost_kit_choice_hint = ""
        for candidate_plan in selected:
            candidate_row = evidence.get(candidate_plan.id)
            nested = (candidate_row.get('outpost_kit_prerequisite_start_evidence')
                      if isinstance(candidate_row, dict) else None)
            if (not isinstance(nested, dict) or nested.get('observed_tick') != tick
                    or nested.get('parent_target_item') != target
                    or nested.get('native_step_allowed_now') is not True
                    or nested.get('admission_is_not_native_payback_evidence') is not True
                    or nested.get('outpost_placement_arrival_flow_output_and_parent_completion_unverified')
                        is not True):
                continue
            outpost_kit_choice_hint += (
                " This candidate is a current, native-guarded child-kit step for a separately "
                "traced outpost prerequisite under the outer local target. A proposed outpost's "
                "admission reflects the existing planner's direct-route and minimum-runway "
                "policy heuristic, not measured payback. Judge this bounded child step from its "
                "current start evidence; its own outcome still requires normal native verification. "
                "Do not require proof of arrival, outpost flow/output, or completion of the outer target."
            )
        research_trigger_choice_hint = ""
        for candidate_plan in selected:
            candidate_row = evidence.get(candidate_plan.id)
            trigger = (candidate_row.get('native_research_trigger_start_evidence')
                       if isinstance(candidate_row, dict) else None)
            if (not isinstance(trigger, dict) or trigger.get('observed_tick') != tick
                    or trigger.get('outer_recipe_is_not_a_direct_recipe_edge') is not True
                    or trigger.get('does_not_establish_trigger_item_output_or_unlock') is not True
                    or trigger.get('useful_partial_benefit_level') != 1):
                continue
            research_trigger_choice_hint += (
                " This candidate is a bounded input to the current enabled recipe for a "
                "native craft-item research trigger. The outer recipe remains locked; the "
                "typed technology edge and its current produced/required counter are separate "
                "from the recipe input path. If this action verifies, it is only useful input "
                "progress (level 1), not trigger-item production, research completion, or an "
                "outer-recipe unlock. Prefer this finite immediate trigger input over starting "
                "a proposed whole outpost kit when the current candidate directly supplies it; "
                "do not assume payback or future output."
            )
        questions = {
            "candidate": {
                "type": "choice",
                "instructions": (f"Choose the best supplied candidate plan for `{objective}` using "
                                 "`facts`, `candidate_evidence` when present, and `history`. "
                                 "Select observe only when evidence needed to start is missing. "
                                 "For a gather candidate, `gather_start_evidence` when present "
                                 "summarizes the current observed resource and fair target; an estimated "
                                 "travel distance is not proof of arrival. Uncertain later "
                                 "crafting, research, or travel outcome is checked after this "
                                 "bounded step and does not by itself require another observation. "
                                 "Compare observe by the specific start fact it could resolve now: "
                                 "when the current gather has an observed resource and fair target "
                                 "and no start fact is missing, repeating the same observation alone "
                                 "does not establish a future travel or crafting outcome. Keep observe "
                                 "available for a genuinely missing or disputed start fact. "
                                 "For a handcraft, `craft_start_evidence` describes current inputs "
                                 "and actor readiness; expected output still needs native verification. "
                                 "Report confidence in choosing the best next action from this "
                                 "observed frontier, not confidence in completing the ultimate goal. "
                                 "Do not assume other questions' answers are available."
                                 + choice_priority_hint + bill_craft_hint
                                 + craft_choice_hint + utility_lab_choice_hint
                                 + outpost_kit_choice_hint + research_trigger_choice_hint),
                "criteria": {**{p.id: p.description for p in selected},
                             "observe": "Gather another observation without mutating the factory"},
            }
        }
        for plan in selected:
            pointer = f"`candidate_plans[{json.dumps(plan.id)}]`"
            row = evidence.get(plan.id)
            row = row if isinstance(row, dict) else {}
            direct_parent_hint = (
                " `direct_alternative_parent_demand_start_evidence` binds this bounded "
                "gather to the current immediate parent target through a same-tick "
                "native-catalog recipe input path. The plan's `work_intent.scope` "
                "is its compilation provenance; `work_scope_provenance` distinguishes "
                "that scope from the immediate scope qualified by current evidence. "
                "The proposed outpost request and "
                "the compiled gather target are distinct recorded quantities. If "
                "the native gather postcondition verifies, this is evidenced recipe "
                "input progress (level 1), not harvested output already observed, "
                "local-target completion, outpost payback, electricity, or research "
                "completion. Later steps need fresh native checks. A contrary "
                "current fact can make usefulness unsupported or lower benefit."
                if _qualified_direct_parent_demand(plan, row, selected, facts) else "")
            if direct_parent_hint:
                questions['candidate']['instructions'] += (
                    f" For {pointer}:" + direct_parent_hint)
            candidate_local = _qualified_candidate_local_raw_demand(plan, facts, row, evidence)
            bootstrap_local = _qualified_bootstrap_output_pickup(plan, facts, row)
            candidate_target = row["local_target"]["item"] if candidate_local or bootstrap_local else target
            candidate_objective = "this candidate's local_target" if candidate_local else objective
            raw = row.get('raw_prerequisite')
            raw_path = raw.get('planner_item_path') if isinstance(raw, dict) else None
            gather_start = row.get('gather_start_evidence')
            gather_step = plan.steps[0] if len(plan.steps) == 1 else None
            gather_parameters = gather_step.parameters if gather_step is not None else None
            qualified_raw_gather = (
                gather_step is not None
                and gather_step.action == 'factory_gather'
                and gather_step.effect == 'inventory'
                and gather_step.costs in (None, {})
                and isinstance(gather_parameters, dict)
                and isinstance(gather_parameters.get('resource'), str)
                and bool(gather_parameters['resource'])
                and gather_parameters['resource'] == gather_step.item
                and row.get('work_scope') == 'immediate'
                and row.get('unknowns') == [] and row.get('reasons') == []
                and type(row.get('urgency')) is int and row['urgency'] == 0
                and row.get('research_deadline_tick') is None
                and row.get('requires_investment') is False
                and type(tick) is int and isinstance(target, str) and bool(target)
                and isinstance(raw, dict) and isinstance(gather_start, dict)
                and raw.get('observed_tick') == tick
                and raw.get('basis') ==
                    'current_planner_dependency_and_native_catalog_recipe'
                and raw.get('later_steps_require_fresh_native_preconditions') is True
                and isinstance(raw_path, list) and 2 <= len(raw_path) <= 32
                and all(isinstance(item, str) and bool(item) for item in raw_path)
                and raw_path[0] == candidate_target
                and raw_path[-2] == raw.get('direct_product')
                and raw_path[-1] == gather_step.item
                and isinstance(raw.get('direct_recipe'), str)
                and bool(raw['direct_recipe'])
                and gather_start.get('resource_in_current_observation') is True
                and gather_start.get('fair_target_identity_observed') is True
                and gather_start.get('observed_tick') == tick
                and isinstance(facts.get('session_id'), str) and bool(facts['session_id'])
                and gather_start.get('session_id') == facts['session_id']
                and isinstance(facts.get('inventory'), dict)
                and type(facts['inventory'].get(gather_step.item, 0)) is int
                and facts['inventory'].get(gather_step.item, 0) ==
                    gather_start.get('resource_inventory_now')
                and gather_start.get('travel_is_lower_bound_not_arrival_proof') is True
                and type(gather_start.get('resource_inventory_now')) is int
                and gather_start['resource_inventory_now'] >= 0
                and type(gather_start.get('target_inventory_after_this_step')) is int
                and type(gather_step.threshold) is int
                and gather_start['target_inventory_after_this_step'] ==
                    gather_step.threshold > gather_start['resource_inventory_now']
                and type(gather_parameters.get('quantity')) is int
                and gather_parameters['quantity'] == (
                    gather_step.threshold - gather_start['resource_inventory_now']))
            candidate_local = (candidate_local and qualified_raw_gather) or bootstrap_local
            machine_prerequisite = row.get('raw_machine_prerequisite')
            machine_edges = (machine_prerequisite.get('edges', [])
                             if isinstance(machine_prerequisite, dict) else [])
            missing_machines = [edge for edge in machine_edges if isinstance(edge, dict)
                                and edge.get('kind') == 'missing_production_machine']
            machine_prerequisite_hint = ''
            if (qualified_raw_gather and isinstance(machine_prerequisite, dict)
                    and type(machine_prerequisite.get('schema')) is int
                    and machine_prerequisite['schema'] == 1
                    and machine_prerequisite.get('session_id') == facts['session_id']
                    and type(machine_prerequisite.get('observed_tick')) is int
                    and machine_prerequisite['observed_tick'] == tick
                    and machine_prerequisite.get('planner_item_path') == raw_path
                    and machine_prerequisite.get('local_target') == row.get('local_target')
                    and machine_prerequisite.get('gather_resource') == gather_step.item
                    and machine_prerequisite.get('gather_quantity') == gather_parameters['quantity']
                    and machine_prerequisite.get('gather_inventory_now') ==
                        gather_start['resource_inventory_now']
                    and machine_prerequisite.get('gather_inventory_target') == gather_step.threshold
                    and machine_prerequisite.get('basis') ==
                        'current_catalog_input_edges_and_observed_missing_machine'
                    and machine_prerequisite.get(
                        'gather_craft_placement_and_production_require_native_verification') is True
                    and len(missing_machines) == 1
                    and missing_machines[0].get('role') not in facts.get('factory', {}).get('entities', {})
                    and facts['inventory'].get(missing_machines[0].get('machine'), 0) == 0):
                machine_prerequisite_hint = (
                    ' `raw_machine_prerequisite` separates recipe-input edges from a '
                    'missing-production-machine edge using the current native catalog. '
                    'The required machine is absent from its production role and carried '
                    'inventory. The gather supplies material for constructing that machine, '
                    'which can then process an intermediate needed by the local target; '
                    'the machine is not a consumed ingredient of that intermediate. '
                    'An existing machine assigned to another recipe does not establish '
                    'this missing producer. Consider this bounded construction prerequisite '
                    'when judging usefulness; a later craft, placement, fuel supply or '
                    'production result need not already exist. Those later actions and '
                    'the gather still need native verification. This is partial progress '
                    'evidence, not evidence of target completion or a removed blocker. '
                    'Contrary current facts can make usefulness unsupported.'
                )
                questions['candidate']['instructions'] += (
                    f' For {pointer}, `raw_machine_prerequisite` supplies the current '
                    'catalog edges and observed missing producer behind this construction '
                    'input. Compare gathering with observe using the missing start fact, '
                    'if any; a repeated observation cannot supply the material or build '
                    'the missing machine. Later craft, placement and production outcomes '
                    'still require verification and are not assumed by this choice.'
                )
            candidate_objective = "this candidate's local_target" if candidate_local else objective
            candidate_context_hint = (
                " Recompiled parent demand supports recipe input; science output and route flow remain unverified."
                if candidate_local else "")
            raw_gather_hint = (
                " This sole current raw gather has observed resource and fair-target "
                "start facts and a same-tick native-recipe path to the local target. "
                "Gathering its bounded quantity supplies a useful recipe input "
                "(level 1); the path alone does not prove an already removed "
                "production blocker (level 2) or completed downstream output. "
                "Use level 2 only with an independent current blocker fact. "
                "A contrary current fact can lower the score. Native harvest and "
                "later recipe steps still require fresh verification."
                if qualified_raw_gather and len(selected) == 1 else "")
            target_completion = row.get('local_target_completion_evidence')
            local_target = row.get('local_target')
            local_target_completion_hint = ""
            current_target_craft_usefulness_hint = ""
            target_step = plan.steps[0] if len(plan.steps) == 1 else None
            if target_step is not None:
                parameters = target_step.parameters if isinstance(target_step.parameters, dict) else {}
                current_target = (primary.get('inventory_target')
                                  if isinstance(primary, dict) else None)
                target_start = row.get('craft_start_evidence')
                target_dependency = row.get('craft_dependency')
                expected_products = (target_start.get('expected_products_after_native_verification')
                                     if isinstance(target_start, dict) else None)
                expected_output = (expected_products.get(target)
                                   if isinstance(expected_products, dict) else None)
                current = (target_completion.get('inventory_now')
                           if isinstance(target_completion, dict) else None)
                shortfall = (target_completion.get('shortfall_now')
                             if isinstance(target_completion, dict) else None)
                reported_output = (target_completion.get('expected_output_after_native_receipt')
                                   if isinstance(target_completion, dict) else None)
                qualified_target_completion = (
                    target_step.action == 'factory_craft_job'
                    and target_step.effect == 'craft_job_complete'
                    and target_step.item == target
                    and isinstance(parameters.get('recipe'), str)
                    and isinstance(parameters.get('receipt'), str)
                    and bool(parameters['receipt'])
                    and type(parameters.get('batches')) is int
                    and parameters['batches'] > 0
                    and isinstance(local_target, dict)
                    and local_target.get('item') == target
                    and isinstance(primary, dict)
                    and primary.get('inventory_target') == current_target
                    and type(current_target) is int and current_target > 0
                    and row.get('work_scope') == 'immediate'
                    and row.get('unknowns') == []
                    and isinstance(target_completion, dict)
                    and type(target_completion.get('observed_tick')) is int
                    and target_completion.get('observed_tick') == tick
                    and isinstance(facts, dict)
                    and target_completion.get('session_id') == facts.get('session_id')
                    and target_completion.get('target_item') == target
                    and type(target_completion.get('target_inventory')) is int
                    and target_completion.get('target_inventory') == current_target
                    and type(current) is int and current >= 0
                    and type(shortfall) is int and shortfall == max(0, current_target - current)
                    and type(expected_output) is int and expected_output > 0
                    and reported_output == expected_output
                    and type(target_completion.get('shortfall_after_expected_output')) is int
                    and target_completion.get('shortfall_after_expected_output') ==
                        max(0, shortfall - expected_output)
                    and target_completion.get(
                        'would_close_current_shortfall_if_native_receipt_verifies') is
                        (shortfall > 0 and expected_output >= shortfall)
                    and target_completion.get('native_recipe') == parameters.get('recipe')
                    and type(target_completion.get('native_batches')) is int
                    and target_completion.get('native_batches') == parameters.get('batches')
                    and target_completion.get('native_receipt_required_for_completion') is True
                    and target_completion.get('forecast_is_not_completed_output') is True
                    and target_completion.get('inventory_basis') ==
                        'coherent_snapshot_and_atomic_craft_inventory'
                    and isinstance(target_start, dict)
                    and target_start.get('observed_tick') == tick
                    and target_start.get('native_recipe') == parameters.get('recipe')
                    and target_start.get('native_receipt_required_for_completion') is True
                    and all(target_start.get(key) is True for key in (
                        'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                        'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                        'crafting_queue_empty', 'craft_job_protocol_ready'))
                    and isinstance(target_dependency, dict)
                    and target_dependency.get('observed_tick') == tick
                    and target_dependency.get('current_craft_product') == target
                    and target_dependency.get('planner_item_path') == [target]
                    and target_dependency.get('basis') ==
                        'current_recursive_planner_provenance_and_native_recipe')
                if qualified_target_completion:
                    inventory = facts.get('inventory')
                    factory = facts.get('factory')
                    costs = target_step.costs
                    if (isinstance(inventory, dict) and isinstance(costs, dict) and costs
                            and isinstance(factory, dict)
                            and factory.get('player_connected') is True
                            and factory.get('player_bound') is True
                            and type(factory.get('crafting_queue')) is int
                            and factory['crafting_queue'] == 0
                            and type(factory.get('craft_jobs_protocol')) is int
                            and factory['craft_jobs_protocol'] == 1
                            and local_target.get('inventory_target') == current_target
                            and type(inventory.get(target, 0)) is int
                            and inventory.get(target, 0) == current
                            and 0 < expected_output < shortfall
                            and all(isinstance(item, str) and bool(item)
                                    and type(amount) is int and amount > 0
                                    and type(inventory.get(item, 0)) is int
                                    and inventory.get(item, 0) >= amount
                                    for item, amount in costs.items())):
                        current_target_craft_usefulness_hint = (
                            " `local_target_completion_evidence` binds this direct craft to "
                            "the same-tick current target, carried stock and native recipe "
                            "output. Its currently carried ingredients can supply a useful "
                            "partial target batch; the remaining shortfall stays open. This "
                            "does not establish crafted inventory, target completion, blocker "
                            "removal or future research. Output requires the native receipt "
                            "and fresh postcondition; contrary current facts can make "
                            "usefulness unsupported.")
                    closes = target_completion[
                        'would_close_current_shortfall_if_native_receipt_verifies']
                    if closes:
                        local_target_completion_hint = (
                            " `local_target_completion_evidence` is same-tick, session-bound "
                            "native inventory and recipe evidence. It supports level 2 only "
                            "because the current local-target shortfall would close after "
                            "the required native receipt verifies. It forecasts output and "
                            "never reports completion. Do not infer blocker removal from "
                            "future research or an unverified plan. A separate blocker or "
                            "due-starvation claim needs its own specific same-tick observed "
                            "evidence.")
                    elif shortfall > 0:
                        local_target_completion_hint = (
                            " `local_target_completion_evidence` shows a same-tick native "
                            "craft that leaves the current local-target shortfall open; score "
                            "it as partial progress, not target closure or blocker removal. "
                            "Its output still requires the native receipt. A separate blocker "
                            "or due-starvation claim needs its own specific same-tick observed "
                            "evidence.")
                    else:
                        local_target_completion_hint = (
                            " `local_target_completion_evidence` shows the observed target "
                            "is already met before this craft. Do not assign target-closure "
                            "benefit to surplus output; any level-2 blocker-removal claim still "
                            "needs separate, specific same-tick observed evidence. The craft "
                            "output itself requires its native receipt.")
                gather_start = row.get('gather_start_evidence')
                gather_parameters = (target_step.parameters
                                     if isinstance(target_step.parameters, dict) else {})
                target_factory = facts.get('factory') if isinstance(facts, dict) else None
                gather_capacity = (target_completion.get('insertable_headroom_now')
                                   if isinstance(target_completion, dict) else None)
                qualified_target_gather_completion = (
                    target_step.action == 'factory_gather'
                    and target_step.effect == 'inventory'
                    and target_step.item == target
                    and set(gather_parameters) == {'resource', 'quantity'}
                    and gather_parameters.get('resource') == target
                    and type(gather_parameters.get('quantity')) is int
                    and 1 <= gather_parameters['quantity'] <= 200
                    and target_step.costs in (None, {})
                    and type(target_step.threshold) is int
                    and target_step.threshold == current_target
                    and isinstance(local_target, dict)
                    and local_target.get('item') == target
                    and isinstance(primary, dict)
                    and primary.get('inventory_target') == current_target
                    and type(current_target) is int and current_target > 0
                    and row.get('work_scope') == 'immediate'
                    and isinstance(target_completion, dict)
                    and target_completion.get('observed_tick') == tick
                    and isinstance(facts, dict)
                    and target_completion.get('session_id') == facts.get('session_id')
                    and target_completion.get('target_item') == target
                    and target_completion.get('target_inventory') == current_target
                    and type(current) is int and current >= 0
                    and type(shortfall) is int
                    and shortfall == max(0, current_target - current)
                    and shortfall > 0
                    and gather_parameters['quantity'] == shortfall
                    and type(target_completion.get('observed_tick')) is int
                    and isinstance(target_completion.get('session_id'), str)
                    and bool(target_completion['session_id'])
                    and type(target_completion.get('target_inventory')) is int
                    and type(target_completion.get('inventory_now')) is int
                    and type(target_completion.get('shortfall_now')) is int
                    and type(target_completion.get('requested_gather_quantity')) is int
                    and type(target_completion.get('target_inventory_threshold')) is int
                    and target_completion.get('inventory_now') == current
                    and target_completion.get('shortfall_now') == shortfall
                    and target_completion.get('requested_gather_quantity') ==
                        gather_parameters['quantity']
                    and target_completion.get('target_inventory_threshold') ==
                        target_step.threshold
                    and target_completion.get(
                        'requested_quantity_equals_current_shortfall') is True
                    and target_completion.get(
                        'would_close_current_shortfall_if_native_inventory_verifies') is True
                    and type(gather_capacity) is int
                    and gather_capacity >= gather_parameters['quantity']
                    and type(target_completion.get('fair_target_surface_index')) is int
                    and target_completion['fair_target_surface_index'] > 0
                    and isinstance(target_completion.get('fair_target_name'), str)
                    and bool(target_completion['fair_target_name'].strip())
                    and (target == 'wood' or target_completion['fair_target_name'] == target)
                    and target_completion.get('inventory_basis') ==
                        'coherent_snapshot_and_atomic_native_inventory'
                    and target_completion.get('fresh_native_inventory_threshold_required') is True
                    and target_completion.get('travel_is_lower_bound_not_arrival_proof') is True
                    and target_completion.get('forecast_is_not_harvested_output') is True
                    and isinstance(gather_start, dict)
                    and gather_start.get('observed_tick') == tick
                    and gather_start.get('session_id') == facts.get('session_id')
                    and gather_start.get('resource_in_current_observation') is True
                    and gather_start.get('fair_target_identity_observed') is True
                    and gather_start.get('resource_inventory_now') == current
                    and gather_start.get('target_inventory_after_this_step') ==
                        target_step.threshold
                    and gather_start.get('travel_is_lower_bound_not_arrival_proof') is True
                    and isinstance(target_factory, dict)
                    and target_factory.get('player_connected') is True
                    and target_factory.get('player_bound') is True)
                if qualified_target_gather_completion:
                    local_target_completion_hint = (
                        " `local_target_completion_evidence` describes an exact immediate "
                        "gather for the observed local-target shortfall, with same-tick "
                        "resource identity, actor readiness, and insertable headroom. It "
                        "supports level 2 only if a fresh native inventory observation "
                        "confirms the target threshold. It does not establish arrival, patch "
                        "yield, harvested quantity, or completion before that verification.")
            placement_start = row.get('placement_start_evidence')
            buffer_build_start = row.get('buffer_build_start_evidence')
            buffer_fuel_start = row.get('buffer_fuel_start_evidence')
            qualified_buffer_fuel = (len(plan.steps) == 1 and row.get('work_scope') == 'immediate'
                and _qualified_buffer_fuel(facts, plan.steps[0], buffer_fuel_start, tick))
            buffer_fuel_hint = (
                " `buffer_fuel_start_evidence` binds bounded carried coal to this current paid "
                "output-buffer arm, its observed coal deficit and capacity, ready source stock "
                "and unused receipt. Fuel prepares the current transport prerequisite; native "
                "transfer, inventory delta and later commissioning flow remain unverified. "
                "Contrary current facts can make progress unsupported."
                if qualified_buffer_fuel else "")
            qualified_buffer_build = (len(plan.steps) == 1
                and row.get('work_scope') == 'immediate'
                and _qualified_buffer_build(facts, plan.steps[0], buffer_build_start, tick))
            buffer_build_hint = (
                " `buffer_build_start_evidence` binds this one carried component to the current "
                "paid partial output-buffer owner, its next missing part, native identities and "
                "planned receipt. This is bounded construction preparation; native prepare "
                "must recheck geometry and clearance, approach and placement need their receipt "
                "and fresh postcondition, and transport flow remains unverified. Contrary current "
                "facts can make progress unsupported."
                if qualified_buffer_build else "")
            placement_dependency = row.get('placement_dependency')
            placement_step = plan.steps[0] if len(plan.steps) == 1 else None
            placement_path = (placement_dependency.get('planner_item_path')
                              if isinstance(placement_dependency, dict) else None)
            qualified_placement = (
                isinstance(placement_start, dict)
                and isinstance(placement_dependency, dict)
                and placement_step is not None
                and placement_step.action == 'factory_place'
                and isinstance(placement_step.parameters, dict)
                and placement_step.parameters.get('name') == 'stone-furnace'
                and isinstance(placement_step.parameters.get('role'), str)
                and placement_step.parameters['role'].startswith('recipe:')
                and isinstance(placement_step.parameters.get('anchor'), str)
                and bool(placement_step.parameters['anchor'])
                and placement_step.costs == {'stone-furnace': 1}
                and row.get('work_scope') == 'immediate'
                and row.get('unknowns') == []
                and type(tick) is int
                and placement_start.get('observed_tick') == tick
                and placement_dependency.get('observed_tick') == tick
                and placement_start.get('site_state') == 'proposed'
                and all(placement_start.get(key) is True for key in (
                    'native_offer_checked_current_site_clearance',
                    'paid_furnace_in_inventory_now', 'no_source_owned_at_role_now',
                    'player_connected_and_bound_now', 'crafting_queue_empty_now',
                    'native_preflight_rechecks_offer_and_actor'))
                and placement_start.get('site_anchor') ==
                    placement_step.parameters.get('anchor')
                and placement_start.get('source_role') ==
                    placement_step.parameters.get('role')
                and placement_dependency.get('machine_for_recipe') ==
                    placement_step.parameters.get('role')
                and placement_dependency.get('basis') ==
                    'current_recursive_planner_and_validated_native_site'
                and isinstance(target, str) and bool(target)
                and isinstance(placement_path, list) and len(placement_path) >= 2
                and placement_path[0] == target
                and placement_path[-1] ==
                    placement_step.parameters['role'].removeprefix('recipe:'))
            craft_start = row.get('craft_start_evidence')
            craft_dependency = row.get('craft_dependency')
            craft_path = (craft_dependency.get('planner_item_path')
                          if isinstance(craft_dependency, dict) else None)
            craft_step = plan.steps[0] if len(plan.steps) == 1 else None
            craft_parameters = (craft_step.parameters if craft_step is not None
                                and isinstance(craft_step.parameters, dict) else {})
            expected_craft_products = (
                craft_start.get('expected_products_after_native_verification')
                if isinstance(craft_start, dict) else None)
            qualified_intermediate_craft = (
                craft_step is not None
                and craft_step.action in {'factory_craft', 'factory_craft_job'}
                and isinstance(target, str) and bool(target)
                and isinstance(craft_step.item, str) and bool(craft_step.item)
                and craft_step.item != target
                and row.get('work_scope') == 'immediate'
                and row.get('unknowns') == []
                and type(tick) is int
                and isinstance(craft_start, dict)
                and craft_start.get('observed_tick') == tick
                and craft_start.get('native_recipe') == craft_parameters.get('recipe')
                and craft_start.get('recipe_unlocked_and_handcraftable') is True
                and isinstance(expected_craft_products, dict)
                and type(expected_craft_products.get(craft_step.item)) is int
                and expected_craft_products[craft_step.item] > 0
                and isinstance(craft_dependency, dict)
                and craft_dependency.get('observed_tick') == tick
                and craft_dependency.get('current_craft_product') == craft_step.item
                and isinstance(craft_path, list) and len(craft_path) >= 2
                and craft_path[0] == target and craft_path[-1] == craft_step.item)
            craft_hint = (
                " `craft_start_evidence` shows the current actor, queue, recipe, "
                "and carried ingredients needed to start this handcraft; "
                "`craft_dependency` traces its product along the current planner "
                "recipe path to the local target. This is level-1 partial progress "
                "from this current planner-linked intermediate craft: it does not close the "
                "local-target shortfall or establish blocker removal. The craft and "
                "later production still need fresh native receipt and precondition "
                "checks."
                if qualified_intermediate_craft else ""
            )
            shared_bill = row.get('shared_bill_craft')
            craft_start = row.get('craft_start_evidence')
            craft_step = plan.steps[0] if len(plan.steps) == 1 else None
            craft_parameters = craft_step.parameters if craft_step is not None else None
            bill_output = (craft_start.get('expected_products_after_native_verification')
                           if isinstance(craft_start, dict) else None)
            qualified_bill_start = (
                len(selected) == 1
                and craft_step is not None and craft_step.action == 'factory_craft_job'
                and isinstance(craft_step.item, str) and bool(craft_step.item)
                and isinstance(craft_parameters, dict)
                and isinstance(craft_parameters.get('receipt'), str)
                and bool(craft_parameters['receipt'])
                and isinstance(craft_parameters.get('recipe'), str)
                and bool(craft_parameters['recipe'])
                and type(craft_parameters.get('batches')) is int
                and craft_parameters['batches'] > 0
                and row.get('work_scope') == 'lookahead'
                and row.get('unknowns') == [] and row.get('reasons') == []
                and type(row.get('urgency')) is int and row['urgency'] == 0
                and row.get('research_deadline_tick') is None
                and type(tick) is int and isinstance(target, str) and bool(target)
                and isinstance(shared_bill, dict) and isinstance(craft_start, dict)
                and shared_bill.get('observed_tick') == tick
                and craft_start.get('observed_tick') == tick
                and shared_bill.get('local_target_item') == target
                and shared_bill.get('craft_item') == craft_step.item
                and shared_bill.get('basis') ==
                    'current_catalog_shared_material_bill_and_native_recipe'
                and shared_bill.get('forecast_is_not_paid_stock_or_completed_output') is True
                and shared_bill.get('background_overlap_requires_native_admission') is True
                and type(shared_bill.get('bounded_bill_inventory_target')) is int
                and type(shared_bill.get('inventory_now')) is int
                and type(shared_bill.get('unfilled_bill_units')) is int
                and shared_bill['inventory_now'] >= 0
                and shared_bill['bounded_bill_inventory_target'] - shared_bill['inventory_now']
                    == shared_bill['unfilled_bill_units'] > 0
                and type(shared_bill.get('expected_products_after_native_verification')) is int
                and shared_bill['expected_products_after_native_verification']
                    >= shared_bill['unfilled_bill_units']
                and isinstance(bill_output, dict)
                and bill_output.get(craft_step.item) ==
                    shared_bill['expected_products_after_native_verification']
                and craft_start.get('native_recipe') == craft_parameters.get('recipe')
                and all(craft_start.get(key) is True for key in (
                    'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                    'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                    'crafting_queue_empty', 'craft_job_protocol_ready',
                    'native_receipt_required_for_completion')))
            bill_craft_hint = (
                " `shared_bill_craft` ties this ready handcraft to a current "
                "bounded catalog bill shortfall. It can supply a useful forecast "
                "intermediate (level 1). Output needs native verification; "
                "background overlap is only possible after native job admission."
                if (isinstance(shared_bill, dict)
                    and shared_bill.get('observed_tick') == tick
                    and shared_bill.get('local_target_item') == target
                    and isinstance(row.get('craft_start_evidence'), dict)
                    and row.get('unknowns') == []) else ""
            )
            place_hint = (
                " `placement_start_evidence` and `placement_dependency` bind this "
                "paid furnace to a currently offered, unoccupied source role on "
                "the local planner path. Placing it provides evidenced bounded "
                "capacity (score level 1); it does not yet demonstrate downstream "
                "production-blocker removal (level 2). Another current contrary "
                "fact can lower the score. Native placement receipt, fuel, input, "
                "transport and output remain unverified and need fresh checks."
                if qualified_placement
                else ""
            )
            qualified_utility_lab = _qualified_utility_lab_dependency(
                plan, row, local, tick)
            utility_lab_hint = (
                " `utility_lab_research_dependency` ties this paid placement to the "
                "current planner's immediate prerequisite for a specific enabled "
                "capability technology. It is useful prerequisite progress (score "
                "level 1) only; no native site or clearance has been observed, and "
                "this does not establish a powered lab, started research, or an "
                "unlocked assembler. The existing native search/build checks and "
                "action result and fresh role postcondition remain authoritative; "
                "a contrary current fact "
                "can lower the score."
                if qualified_utility_lab else ""
            )
            power_hint = (
                " `utility_power_prerequisite_start_evidence` binds this step to "
                "the current consumer's native power prerequisite and observed "
                "research or production demand. This dependency alone supports "
                "useful prerequisite progress (score level 1). Separately qualified "
                "local-target closure can support score level 2 under the usual "
                "rubric; use that closure evidence when present. Neither establishes "
                "electricity generation, science consumption, or completed research. "
                "Boiler fuel evidence requires an "
                "observed connected water, steam and electrical chain. A fuel "
                "transfer also requires paid carried coal; a gather only prepares "
                "the current shortfall and leaves that later transfer unverified. "
                "Construction still needs the existing native site, "
                "receipt and fresh postcondition checks. A contrary current "
                "fact can lower the score."
                if _qualified_utility_power_dependency(plan, row, tick, facts) else ""
            )
            fuel = row.get('fuel_prerequisite')
            fuel_step = plan.steps[0] if len(plan.steps) == 1 else None
            fuel_path = fuel.get('planner_item_path') if isinstance(fuel, dict) else None
            gather_start = row.get('gather_start_evidence')
            gather_start = gather_start if isinstance(gather_start, dict) else {}
            qualified_established_fuel = (
                isinstance(fuel, dict) and fuel_step is not None
                and fuel_step.action == 'factory_gather'
                and isinstance(fuel_step.parameters, dict)
                and fuel_step.parameters.get('resource') == 'coal'
                and row.get('work_scope') == 'immediate' and row.get('unknowns') == []
                and type(tick) is int and fuel.get('observed_tick') == tick
                and fuel.get('basis') == 'current_planner_fuel_need_and_owned_native_burner'
                and gather_start.get('resource_in_current_observation') is True
                and gather_start.get('fair_target_identity_observed') is True
                and isinstance(target, str) and bool(target)
                and isinstance(fuel_path, list) and len(fuel_path) >= 2
                and fuel_path[0] == target
                and fuel.get('burner_role') == 'recipe:' + fuel_path[-1]
                and type(fuel.get('burner_unit')) is int and fuel['burner_unit'] > 0
                and type(fuel.get('fuel_now')) is int and 0 <= fuel['fuel_now'] < 5
                and type(fuel.get('coal_in_inventory_now')) is int
                and fuel['coal_in_inventory_now'] >= 0
                and type(fuel.get('current_required_units')) is int
                and fuel['current_required_units'] == 5 - fuel['fuel_now']
                and type(fuel.get('current_unfunded_units')) is int
                and fuel['current_unfunded_units'] == max(
                    0, fuel['current_required_units'] - fuel['coal_in_inventory_now'])
                and fuel['current_unfunded_units'] > 0
                and type(fuel.get('planned_gather_units')) is int
                and fuel['planned_gather_units'] == fuel_step.parameters.get('quantity')
                and type(fuel.get('gather_units_beyond_current_need')) is int
                and fuel['gather_units_beyond_current_need'] == (
                    fuel['planned_gather_units'] - fuel['current_unfunded_units'])
                and type(fuel.get('established_service_target')) is int
                and fuel['established_service_target'] == (
                    fuel['fuel_now'] + fuel['coal_in_inventory_now']
                    + fuel['planned_gather_units'])
                and fuel['established_service_target'] > 5
                and fuel.get('startup_target') is None)
            fuel_hint = (
                " `fuel_prerequisite` ties this bounded coal pickup to the current "
                "owned burner's startup need; later transfer and production remain unverified."
                if isinstance(fuel, dict) and fuel.get('startup_target') is not None
                else (" `fuel_prerequisite` identifies an owned established burner with "
                      f"{fuel['current_unfunded_units']} coal still needed for its current "
                      f"five-coal operating threshold. Of the planned {fuel['planned_gather_units']} "
                      f"coal, the other {fuel['gather_units_beyond_current_need']} support "
                      "the established producer's bulk refill, not an urgent blocker. "
                      "Score the evidenced bounded current need at level 1 without "
                      "treating the whole trip as urgent or claiming later output. "
                      "Another current contrary fact can lower the score. Native "
                      "transfer and production still require fresh verification."
                      if qualified_established_fuel else ""))
            transfer_start = ((state.get('candidate_evidence') or {}).get(plan.id) or {}).get(
                'fuel_transfer_start_evidence')
            transfer_hint = (
                " `fuel_transfer_start_evidence` ties the paid coal transfer to the current "
                "owned burner, carried quantity, planner path, and native receipt. The transfer "
                "and later production still require native verification."
                if transfer_start else ""
            )
            recipe_input_start = ((state.get('candidate_evidence') or {}).get(plan.id) or {}).get(
                'recipe_input_transfer_start_evidence')
            input_step = plan.steps[0] if len(plan.steps) == 1 else None
            input_parameters = input_step.parameters if input_step is not None else None
            input_path = (recipe_input_start.get('planner_item_path')
                          if isinstance(recipe_input_start, dict) else None)
            qualified_recipe_input = (
                len(selected) == 1 and input_step is not None
                and input_step.action == 'factory_insert'
                and input_step.effect == 'transfer'
                and isinstance(input_parameters, dict)
                and isinstance(recipe_input_start, dict)
                # The current _transfer plan carries its item in parameters/costs.
                and input_step.item == ''
                and isinstance(input_parameters.get('item'), str)
                and input_parameters['item'] == recipe_input_start.get('ingredient')
                and row.get('work_scope') == 'immediate'
                and row.get('unknowns') == [] and row.get('reasons') == []
                and type(row.get('urgency')) is int and row['urgency'] == 0
                and row.get('research_deadline_tick') is None
                and row.get('requires_investment') is False
                and type(tick) is int and isinstance(target, str) and bool(target)
                and isinstance(row.get('local_target'), dict)
                and row['local_target'].get('item') == target
                and recipe_input_start.get('observed_tick') == tick
                and recipe_input_start.get('basis') ==
                    'current_planner_recipe_input_and_owned_native_machine'
                and recipe_input_start.get(
                    'native_transfer_and_later_output_require_verification') is True
                and isinstance(input_path, list) and 2 <= len(input_path) <= 32
                and all(isinstance(part, str) and bool(part) for part in input_path)
                and input_path[0] == target
                and isinstance(recipe_input_start.get('direct_native_recipe'), str)
                and recipe_input_start['direct_native_recipe']
                and input_path[-2:] == [recipe_input_start['direct_native_recipe'],
                                         input_parameters['item']]
                and recipe_input_start.get('owned_source_role') ==
                    input_parameters.get('role') == (
                        'recipe:' + recipe_input_start['direct_native_recipe'])
                and type(recipe_input_start.get('owned_source_unit')) is int
                and recipe_input_start['owned_source_unit'] > 0
                and type(recipe_input_start.get('ingredient_in_machine_now')) is int
                and recipe_input_start['ingredient_in_machine_now'] >= 0
                and type(recipe_input_start.get('ingredient_in_inventory_now')) is int
                and type(recipe_input_start.get('paid_quantity_to_transfer')) is int
                and recipe_input_start['paid_quantity_to_transfer'] > 0
                and recipe_input_start['ingredient_in_inventory_now'] >=
                    recipe_input_start['paid_quantity_to_transfer']
                and type(input_parameters.get('quantity')) is int
                and recipe_input_start['paid_quantity_to_transfer'] ==
                    input_parameters.get('quantity')
                and input_step.costs == {
                    input_parameters['item']:
                        recipe_input_start['paid_quantity_to_transfer']}
                and isinstance(input_parameters.get('receipt'), str)
                and input_parameters['receipt'] ==
                    recipe_input_start.get('planned_native_receipt_id') == (
                        f"{tick}:factory_insert:{input_parameters['role']}:"
                        f"{input_parameters['item']}"))
            input_hint = (
                " `recipe_input_transfer_start_evidence` ties this paid ingredient transfer "
                "to the current planner path, native recipe, owned machine, carried input, "
                "and planned receipt ID. This sole same-tick, bounded transfer would "
                "supply a useful recipe input if its paid receipt verifies (level 1); "
                "it does not itself prove an observed "
                "production blocker was removed (level 2) or downstream output. Use "
                "level 2 only with an independent current blocker fact. A contrary "
                "current fact can lower the score. The native transfer receipt and "
                "later output still require verification."
                if qualified_recipe_input else
                " A reported recipe-input transfer witness alone does not establish "
                "a current paid transfer or downstream output. Check its recipe path, "
                "owned machine, carried input, and planned receipt before assigning benefit."
                if isinstance(recipe_input_start, dict) else ""
            )
            nested_kit = row.get('outpost_kit_prerequisite_start_evidence')
            nested_action = (nested_kit.get('action_start_facts')
                             if isinstance(nested_kit, dict) else None)
            nested_parent_path = (nested_kit.get('parent_planner_item_path')
                                  if isinstance(nested_kit, dict) else None)
            nested_child_path = (nested_kit.get('child_planner_item_path')
                                 if isinstance(nested_kit, dict) else None)
            nested_action_kinds = {
                'factory_gather': 'observed_raw_gather_start',
                'factory_insert': 'owned_native_recipe_input_transfer_start',
                'factory_extract': 'owned_native_output_pickup_start',
                'factory_craft': 'paid_native_handcraft_start',
                'factory_craft_job': 'paid_native_handcraft_start',
            }
            qualified_nested_kit = (
                len(selected) == 1 and input_step is not None
                and input_step.action in nested_action_kinds
                and input_step.effect in {'inventory', 'transfer', 'craft_job_complete'}
                and isinstance(nested_kit, dict)
                and row.get('work_scope') == 'immediate'
                and row.get('unknowns') == [] and row.get('reasons') == []
                and type(row.get('urgency')) is int and row['urgency'] == 0
                and row.get('research_deadline_tick') is None
                and row.get('requires_investment') is False
                and type(tick) is int and isinstance(target, str) and bool(target)
                and isinstance(row.get('local_target'), dict)
                and row['local_target'].get('item') == target
                and nested_kit.get('schema') == 1
                and nested_kit.get('observed_tick') == tick
                and nested_kit.get('parent_target_item') == target
                and nested_kit.get('parent_request_item') == nested_kit.get('outpost_resource')
                and nested_kit.get('parent_and_child_paths_are_separate') is True
                and isinstance(nested_parent_path, list) and 1 <= len(nested_parent_path) <= 32
                and nested_parent_path[0] == target
                and nested_parent_path[-1] == nested_kit.get('outpost_resource')
                and isinstance(nested_child_path, list) and 1 <= len(nested_child_path) <= 32
                and nested_child_path[0] == nested_kit.get('child_kit_item')
                and type(nested_kit.get('child_kit_quantity')) is int
                and nested_kit['child_kit_quantity'] >= 1
                and nested_kit.get('child_request_kind') in {
                    'outpost_component', 'outpost_construction_fuel'}
                and nested_kit.get('current_action') == input_step.action
                and nested_kit.get('native_step_allowed_now') is True
                and nested_kit.get('native_action_outcome_requires_verification') is True
                and nested_kit.get('useful_partial_benefit_level') == 1
                and nested_kit.get('does_not_establish_level_two_blocker_removal') is True
                and nested_kit.get('admission_is_not_native_payback_evidence') is True
                and nested_kit.get(
                    'outpost_placement_arrival_flow_output_and_parent_completion_unverified') is True
                and isinstance(nested_action, dict)
                and nested_action.get('kind') == nested_action_kinds[input_step.action])
            if qualified_nested_kit and input_step.action == 'factory_insert':
                nested_transfer = nested_action.get('transfer')
                nested_capacity = (nested_transfer.get('receiver_capacity')
                                   if isinstance(nested_transfer, dict) else None)
                transfer_parameters = input_step.parameters or {}
                qualified_nested_kit = (
                    nested_kit.get('child_request_kind') == 'outpost_component'
                    and isinstance(nested_transfer, dict)
                    and nested_transfer.get('observed_tick') == tick
                    and nested_transfer.get('native_transfer_and_later_output_require_verification') is True
                    and isinstance(nested_capacity, dict)
                    and nested_capacity.get('observed_tick') == tick
                    and nested_capacity.get('source_role') == transfer_parameters.get('role')
                    and nested_capacity.get('source_unit') == nested_transfer.get('owned_source_unit')
                    and nested_capacity.get('item') == transfer_parameters.get('item')
                    and type(nested_capacity.get('insertable_count_now')) is int
                    and type(transfer_parameters.get('quantity')) is int
                    and nested_capacity['insertable_count_now'] >= transfer_parameters['quantity']
                    and type(nested_capacity.get('actor_count_now')) is int
                    and nested_capacity['actor_count_now'] >= transfer_parameters['quantity']
                    and nested_action.get('receiver_capacity_observed') is True
                    and nested_action.get('fresh_native_dispatch_capacity_check_required') is True
                    and nested_action.get('native_dispatch_checks_receiver_insertable_count') is True)
            if qualified_nested_kit and input_step.action == 'factory_gather':
                qualified_nested_kit = (
                    nested_action.get('fair_target_identity_observed') is True
                    and nested_action.get('native_target_session_bound') is True
                    and type(nested_action.get('fair_target_surface_index')) is int
                    and nested_action['fair_target_surface_index'] > 0
                    and nested_action.get('travel_is_lower_bound_not_arrival_proof') is True
                    and nested_action.get('native_harvest_requires_fresh_verification') is True)
            if qualified_nested_kit and input_step.action == 'factory_extract':
                nested_pickup = nested_action.get('pickup')
                qualified_nested_kit = (
                    isinstance(nested_pickup, dict)
                    and nested_pickup.get('observed_tick') == tick
                    and nested_pickup.get('native_pickup_and_inventory_delta_require_verification') is True)
            if qualified_nested_kit and input_step.action in {'factory_craft', 'factory_craft_job'}:
                nested_craft = nested_action.get('craft')
                qualified_nested_kit = (
                    isinstance(nested_craft, dict)
                    and nested_craft.get('observed_tick') == tick
                    and nested_craft.get('native_recipe') == (input_step.parameters or {}).get('recipe')
                    and nested_craft.get('inputs_in_inventory_now') is True
                    and nested_craft.get('player_connected_and_bound') is True
                    and nested_craft.get('crafting_queue_empty') is True
                    and nested_action.get(
                        'native_output_and_child_completion_require_verification') is True)
            outpost_kit_hint = (
                " `outpost_kit_prerequisite_start_evidence` keeps the outer target and the "
                "current child request on separate, same-tick planner paths. The outpost "
                "admission is only the existing direct-route/minimum-runway policy heuristic "
                "or a verified paid-prefix continuation, not native payback. This current "
                "gather, input transfer, output pickup, or handcraft supplies useful partial "
                "progress to that child request if its ordinary native outcome verifies (level 1); "
                "it does not establish level-two blocker removal, outpost arrival/flow/output, "
                "or completion of the outer target. A contrary current fact can lower the score."
                + (" This is the bounded five-coal construction-fuel request; the request is "
                   "an inventory target, not the amount gathered in this one step."
                   if nested_kit.get('child_request_kind') == 'outpost_construction_fuel' else "")
                + (" This same-tick snapshot includes actor-bound insertable capacity for the "
                   "selected receiver and item; the native transfer dispatch still rechecks "
                   "exact capacity before removing the carried input."
                   if input_step.action == 'factory_insert' else "")
                if qualified_nested_kit else ""
            )
            pickup_start = row.get('output_pickup_start_evidence')
            pickup_step = plan.steps[0] if len(plan.steps) == 1 else None
            pickup_path = (pickup_start.get('planner_item_path')
                           if isinstance(pickup_start, dict) else None)
            qualified_pickup = (
                pickup_step is not None and pickup_step.action == 'factory_extract'
                and pickup_step.effect == 'transfer' and pickup_step.costs == {}
                and isinstance(pickup_step.parameters, dict)
                and row.get('work_scope') == 'immediate' and row.get('unknowns') == []
                and isinstance(pickup_start, dict) and pickup_start.get('observed_tick') == tick
                and pickup_start.get('basis') ==
                    'current_planner_output_and_owned_native_machine'
                and pickup_start.get('player_connected_and_bound_now') is True
                and pickup_start.get('native_pickup_and_inventory_delta_require_verification') is True
                and pickup_start.get('owned_source_role') == pickup_step.parameters.get('role')
                and pickup_start.get('ready_output_item') == pickup_step.parameters.get('item')
                and pickup_start.get('planned_pickup_quantity') ==
                    pickup_step.parameters.get('quantity')
                and pickup_start.get('planned_native_receipt_id') ==
                    pickup_step.parameters.get('receipt')
                and type(pickup_start.get('ready_output_quantity_now')) is int
                and type(pickup_start.get('planned_pickup_quantity')) is int
                and pickup_start['ready_output_quantity_now'] >=
                    pickup_start['planned_pickup_quantity'] > 0
                and isinstance(target, str) and bool(target)
                and isinstance(pickup_path, list) and len(pickup_path) >= 1
                and pickup_path[0] == target
                and pickup_path[-1] == pickup_step.parameters.get('item'))
            pickup_hint = (
                " `output_pickup_start_evidence` ties already observed output at an "
                "owned native machine to the current local planner path and planned "
                "receipt. Collecting that output supplies a bounded useful intermediate "
                "(score level 1); it does not finish the downstream target or prove "
                "pickup. Another current contrary fact can lower the score. The native "
                "pickup receipt and player inventory delta still require verification."
                if qualified_pickup else ""
            )
            component_proof = row.get('buffer_component_prerequisite_start_evidence')
            qualified_component = (
                row.get('work_scope') == 'immediate' and row.get('unknowns') == []
                and ((qualified_pickup and _qualified_buffer_component(
                    facts, plan, component_proof, pickup_start, pickup_path, tick))
                    or (qualified_intermediate_craft and _qualified_buffer_component(
                        facts, plan, component_proof, craft_start, craft_path, tick))))
            component_hint = (
                " `buffer_component_prerequisite_start_evidence` discloses the current paid output-buffer "
                "construction bridge: the next missing component, native recipe bill, carried "
                "inventory and remaining input deficit. This action prepares that bounded "
                "component requirement; this is a construction prerequisite along the planner path, "
                "not a claim that the component is an ingredient of the local target recipe. "
                "Component crafting, paid building and actual material flow still need native "
                "verification. Contrary current facts can make usefulness unsupported."
                if qualified_component else ""
            )
            if qualified_component and qualified_intermediate_craft:
                craft_hint = (
                    " `craft_start_evidence` records the current native recipe, actor readiness "
                    "and carried inputs for this bounded craft. Its product prepares the "
                    "current missing output-buffer component; the construction bridge is "
                    "separate from the local target's native recipe ingredients. The craft "
                    "and later paid construction still require fresh native verification."
                )
            trigger_start = row.get('native_research_trigger_start_evidence')
            trigger_action = (trigger_start.get('action_start_facts')
                              if isinstance(trigger_start, dict) else None)
            qualified_research_trigger = (
                input_step is not None and isinstance(trigger_start, dict)
                and trigger_start.get('observed_tick') == tick
                and trigger_start.get('basis') ==
                    'typed_native_research_trigger_plus_direct_current_recipe_input'
                and trigger_start.get('outer_recipe_is_not_a_direct_recipe_edge') is True
                and trigger_start.get('technology_enabled_and_unresearched') is True
                and trigger_start.get('technology_prerequisites_satisfied') is True
                and trigger_start.get('outer_recipe_locked_now') is True
                and trigger_start.get('trigger_type') == 'craft-item'
                and type(trigger_start.get('trigger_produced_now')) is int
                and type(trigger_start.get('trigger_count')) is int
                and 0 <= trigger_start['trigger_produced_now'] < trigger_start['trigger_count']
                and trigger_start.get('trigger_remaining_now') ==
                    trigger_start['trigger_count'] - trigger_start['trigger_produced_now']
                and isinstance(trigger_action, dict)
                and trigger_start.get('useful_partial_benefit_level') == 1
                and trigger_start.get('does_not_establish_trigger_item_output_or_unlock') is True)
            if qualified_research_trigger and input_step.action == 'factory_gather':
                qualified_research_trigger = (
                    trigger_action.get('kind') == 'direct_enabled_trigger_recipe_input_gather'
                    and trigger_action.get('native_gather_outcome_requires_verification') is True
                    and trigger_action.get('resource') ==
                        (input_step.parameters or {}).get('resource')
                    and trigger_action.get('quantity') ==
                        (input_step.parameters or {}).get('quantity')
                    and isinstance(row.get('gather_start_evidence'), dict)
                    and row['gather_start_evidence'].get(
                        'fair_target_identity_observed') is True)
            elif qualified_research_trigger and input_step.action == 'factory_insert':
                trigger_transfer = trigger_action.get('transfer')
                trigger_capacity = (trigger_transfer.get('receiver_capacity')
                                   if isinstance(trigger_transfer, dict) else None)
                qualified_research_trigger = (
                    trigger_action.get('kind') == 'current_owned_trigger_recipe_input_transfer'
                    and isinstance(trigger_transfer, dict)
                    and trigger_transfer.get('observed_tick') == tick
                    and isinstance(trigger_capacity, dict)
                    and type(trigger_capacity.get('insertable_count_now')) is int
                    and type((input_step.parameters or {}).get('quantity')) is int
                    and trigger_capacity['insertable_count_now'] >=
                        (input_step.parameters or {})['quantity']
                    and trigger_action.get(
                        'native_dispatch_rechecks_insertable_count_before_removal') is True)
            else:
                qualified_research_trigger = False
            research_trigger_hint = (
                " `native_research_trigger_start_evidence` distinguishes the locked outer "
                "recipe's craft-item technology trigger from the enabled input recipe. The "
                "current produced/required counter is native, while this gather or paid input "
                "transfer is only level-1 preparation: it does not itself produce the trigger "
                "item, advance the counter, research the technology, or unlock the outer recipe. "
                "Verify each action normally and reevaluate from a fresh native observation."
                if qualified_research_trigger else ""
            )
            supplied_research_hint = (
                ' `supplied_research_start_evidence` binds this selection to the current '
                'powered lab and observed science inputs covering one native research unit. '
                'The disclosed catalog bill is a producer-validated native prerequisite, '
                'not an independently projected completion. Selecting this technology '
                'enables that supplied lab to attempt research; native selection and later '
                'science consumption, progress and unlock still require verification.'
                if _qualified_supplied_research(plan, facts, row) else '')
            science_transfer_hint = (
                ' `research_science_transfer_start_evidence` binds these paid, carried science '
                'packs to the current owned lab missing an ingredient of the enabled, '
                'unresearched native technology. The disclosed native bill and remaining '
                'count bound this immediate science-supply prerequisite. This transfer '
                'does not select research or establish consumption, progress, or an unlock; '
                'transfer, selection, power and later research require native verification.'
                if _qualified_research_science_transfer(plan, facts, row) else '')
            paid_service_hint = (
                ' `paid_service_input_start_evidence` binds the first ingredient insert '
                'to the current native recipe, owned furnace, carried input and exact '
                'unused receipt, within a same-tick recompiled two-step paid service visit. '
                'Both inserts fit the disclosed planner-admission carried budget and current stock. '
                'The first insert supplies a bounded current recipe input; it does not '
                'by itself remove fuel starvation or establish later fuel transfer, '
                'smelted output, science production or target completion. Receiver '
                'capacity and each step still require fresh native rechecks and receipts; '
                'contrary current evidence can make usefulness unsupported.'
                if _qualified_paid_service_input(plan, facts, row) else '')
            # Reuse the same qualifications for independent eligibility and
            # magnitude; score-level guidance belongs only to magnitude.
            contribution_hint = (
                raw_gather_hint + direct_parent_hint + craft_hint + bill_craft_hint
                + place_hint + fuel_hint + utility_lab_hint + power_hint
                + transfer_hint + input_hint + outpost_kit_hint + pickup_hint
                + research_trigger_hint + component_hint + buffer_build_hint + buffer_fuel_hint
                + supplied_research_hint + science_transfer_hint + paid_service_hint
            )
            usefulness_contribution_hint = ''
            if (qualified_intermediate_craft
                    and craft_dependency.get('basis') ==
                    'current_recursive_planner_provenance_and_native_recipe'
                    and all(craft_start.get(key) is True for key in (
                        'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                        'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                        'crafting_queue_empty', 'craft_job_protocol_ready',
                        'native_receipt_required_for_completion'))):
                usefulness_contribution_hint += (
                    ' `craft_start_evidence` binds this bounded handcraft to current '
                    'native recipe, carried ingredients, actor, queue and receipt-protocol '
                    'facts. `craft_dependency` links its intermediate product to the '
                    'current local target through the validated planner recipe path. '
                    'If the native receipt and fresh postcondition verify, this can '
                    'supply a useful intermediate; it does not establish crafted '
                    'inventory, target completion or removal of a blocker. Later '
                    'production still requires fresh native preconditions and '
                    'verification. Missing, stale, mismatched or contrary current '
                    'facts can make usefulness unsupported.'
                )
            if qualified_raw_gather and not candidate_local:
                usefulness_contribution_hint += (
                    ' `raw_prerequisite` binds this bounded gather to the same-tick '
                    'current local target through a validated native recipe input path. '
                    '`gather_start_evidence` binds its observed resource, fair target '
                    'and current carried raw inventory. Gathering can supply a useful '
                    'recipe input; it does not establish harvested inventory, later '
                    'recipe output, target completion or removal of a blocker. Harvest '
                    'and subsequent steps require fresh native verification. Contrary '
                    'current facts can make usefulness unsupported.'
                )
            usefulness_contribution_hint += machine_prerequisite_hint
            contribution_hint += machine_prerequisite_hint
            if power_hint:
                usefulness_contribution_hint += (
                    ' `utility_power_prerequisite_start_evidence` binds this exact bounded child action '
                    'to a same-tick current consumer demand and paid native prerequisite '
                    'path; preparation need not already supply operating power. The '
                    'receipt and fresh postcondition checks still apply, and contrary '
                    'current facts can make usefulness unsupported.'
                )
            if pickup_hint:
                usefulness_contribution_hint += (
                    ' `output_pickup_start_evidence` binds already observed output at an '
                    'owned native machine, its current planner path, bounded quantity '
                    'and planned receipt. Collecting it can supply a bounded useful '
                    'intermediate; the pickup and inventory delta still need verification. '
                    'Contrary current facts can make usefulness unsupported.'
                )
            questions[plan.id + "/useful_progress"] = {
                "type": "choice",
                "criteria": {
                    "useful": "Current evidence supports useful progress toward the supplied objective",
                    "unsupported": "Useful progress is unsupported or contradicted by current evidence",
                },
                "instructions": (
                    f"Would the next bounded action in {pointer} make useful progress toward "
                    f"`{candidate_objective}` if its native receipt and fresh postcondition verify? "
                    "Judge independently using `facts`, this plan's current `candidate_evidence`, "
                    "and `execution_contract`; other questions' answers are unavailable. "
                    "Useful progress includes an evidenced prerequisite or intermediate, not "
                    "only completing the target. Distinguish whether any useful progress is "
                    "supported from its magnitude (partial progress versus removing a blocker). "
                    "Use unsupported for missing, stale, mismatched or contrary dependency "
                    "evidence. A planner proposal or future unverified result alone is not proof. "
                    "Report confidence in this usefulness choice, not in completing the game. "
                    "This judgment does not authorize execution or waive native checks."
                    + candidate_context_hint
                    + direct_parent_hint.replace(' (level 1)', '')
                    + usefulness_contribution_hint
                    + current_target_craft_usefulness_hint
                    + component_hint
                    + buffer_build_hint
                    + buffer_fuel_hint
                    + supplied_research_hint + science_transfer_hint + paid_service_hint
                ),
            }
            if candidate_local:
                context["execution_contract"] = context["execution_contract"].replace(
                    "Judge the supplied local_objective when present; otherwise judge active_goal.",
                    "For qualified candidate-local raw demand, judge that candidate's evidence row local_target; "
                    "otherwise judge supplied local_objective or active_goal.")
                local_instruction = " Compare qualified candidate-local parent contributions separately from the kit target."
                if local_instruction not in questions["candidate"]["instructions"]:
                    questions["candidate"]["instructions"] += local_instruction
                questions[plan.id + "/useful_progress"]["criteria"]["useful"] = (
                    "Current evidence supports progress toward this candidate's evidence row local_target")
                questions[plan.id + "/useful_progress"]["instructions"] = (
                    f"For {pointer}, use `candidate_evidence[{json.dumps(plan.id)}]` "
                    "to judge progress toward that row's local_target from current facts and raw-demand/start proof. "
                    "Raw input is partial progress, not kit completion, science output, route flow or blocker removal. "
                    "Missing, stale, mismatched or contrary evidence means unsupported. "
                    "Judge independently; native verification is required.")
                if bootstrap_local:
                    questions[plan.id + '/useful_progress']['instructions'] = (
                        f'For {pointer}, use `candidate_evidence[{json.dumps(plan.id)}]` and '
                        'bootstrap_output_pickup_start_evidence: would pickup advance its local_target '
                        'IF native receipt and fresh inventory delta verify? Read recipe_dependency_chain '
                        'for enabled inputs/yields, carried inventory, bounded batches and machine inputs; '
                        'it proves only the selected recursive branch, not the full target bill. '
                        'current_raw_demand gives required carried quantity, deficit, stock, headroom and pickup. '
                        'Allocation-ledger remaining is not carried inventory. Match planner_item_path and '
                        'planned_pickup_quantity to facts.factory.bootstrap_output.output and capacity. '
                        'Judge prospective recipe-input usefulness independently; an unexecuted pickup has no '
                        'receipt or delta, and their absence alone is not contrary start evidence. Missing, stale, '
                        'mismatched or contrary CURRENT ownership, stock, headroom or recipe demand means unsupported. '
                        'No historical placement proof, completed pickup/output, route flow or blocker removal is proved; '
                        'execution and success require native verification.')
            if _qualified_recipe_transfer_chain(plan, facts, row):
                questions[plan.id + '/useful_progress']['instructions'] = (
                    f'For {pointer}, read recipe_input_transfer_start_evidence and its '
                    'recipe_dependency_chain: enabled inputs/yields, carried products, bounded batches '
                    'and current machine input/fuel prove only the selected branch toward the evidence-row '
                    'local_target. Would this paid input advance that branch IF its native receipt and '
                    'fresh postcondition verify? Future receipt/output absence alone is not contrary '
                    'start evidence. Missing, stale, mismatched or contrary current ownership, inputs, '
                    'fuel or recipe dependencies means unsupported. Later output/full completion remain '
                    'unverified; this judgment waives no native checks.')
            questions[plan.id + "/benefit"] = {
                "type": "score",
                "instructions": (
                    f"How directly do the steps in {pointer} advance `{candidate_objective}` "
                    "given `facts`, current `candidate_evidence`, and `execution_contract`? Do not demand a full-game plan "
                    "from one bounded local production action. "
                    + ("`bootstrap_output_pickup_start_evidence` binds observed owned raw stock "
                       "to a current recipe-input need; pickup and later output remain unverified."
                       if bootstrap_local else
                       "A current `raw_prerequisite` is evidence that gathering supplies an input to "
                       "the named native recipe, not that the later craft already happened.")
                    + candidate_context_hint
                    + contribution_hint
                    + local_target_completion_hint
                ),
                "criteria": ([
                    "No demonstrated contribution to the bounded production objective",
                    "Makes useful partial progress through useful inputs, a current "
                    "planner-linked intermediate craft, an immediate planner-linked "
                    "research prerequisite backed by same-tick evidence, or evidenced "
                    "bounded capacity, "
                    "but does not establish receipt-conditional closure of an observed "
                    "local-target shortfall for a craft or fresh-inventory-conditional closure "
                    "of one for a direct gather, "
                    "and does not remove a separately evidenced "
                    "current blocker or due starvation",
                    "Same-tick qualified evidence shows the action would close the current "
                    "local-target shortfall only after its native receipt verifies for a craft "
                    "or fresh native postcondition verifies for a direct gather, or separate "
                    "same-tick evidence shows it directly removes a specific observed blocker "
                    "or due starvation",
                ] if objective == "local_objective" else [
                    "The steps do not improve the active goal's required state",
                    "The steps make partial progress but leave a required action unplanned",
                    "The steps supply all actions needed to satisfy the active goal",
                ]),
            }
            questions[plan.id + "/disruption"] = {
                "type": "score",
                "instructions": (
                    f"How disruptive are the steps in {pointer} to the existing factory "
                    "in `facts`, under `execution_contract`?" + (
                        " Judge construction or alteration of infrastructure separately "
                        "from uncertainty about approach, route clearance or execution "
                        "success; native preconditions and postconditions still apply."
                        if _native_additive_connection_contract(plan, facts) else "")
                ),
                "criteria": [
                    "Only moves, gathers resources, waits, fuels an existing machine, "
                    "or handcrafts from carried inputs without changing existing entities",
                    "Places new machinery or connectors, such as pipes, poles or belts, "
                    "without removing any existing entity",
                    "Stops, removes, or rebuilds existing factory infrastructure",
                ],
            }
            questions[plan.id + "/needs_observation"] = {
                "type": "noul",
                "instructions": (
                    f"Is a fact required to start the next step of {pointer} missing "
                    "from `facts`, given `execution_contract`? Consider only resource location, "
                    "carried materials, and the entities used by that step. Unknown later-game "
                    "research or victory is not required for gathering an observed raw resource "
                    "or fueling a drill. For a gather, use current `gather_start_evidence` "
                    "when present; do not treat the unverified travel outcome as a missing "
                    "start fact. "
                    "Future action outcomes will be verified after execution, not assumed now."
                    + (" For a handcraft, use current `craft_start_evidence` to judge "
                       "the actor, queue, native recipe, and carried ingredients "
                       "needed to start. The output still requires native receipt "
                       "verification; its future completion is not a missing "
                       "start observation."
                       if ((state.get('candidate_evidence') or {}).get(plan.id) or {}).get(
                           'craft_start_evidence') else "")
                    + (" This current bill-linked handcraft has a complete, same-tick "
                       "catalog shortfall and native actor, queue, recipe, carried-input, "
                       "and receipt-protocol start evidence. No required start fact is "
                       "missing from those witnesses; identify a specific contrary "
                       "current fact before marking observation needed. Future output "
                       "still needs a native receipt and fresh verification."
                       if qualified_bill_start else "")
                    + (" For a paid fuel transfer, `fuel_transfer_start_evidence` describes "
                       "the current carried coal, owned burner, and exact receipt. Judge "
                       "start facts from those values; the future transfer outcome is "
                       "verified by the native receipt."
                       if transfer_start else "")
                    + (" For a paid recipe-input transfer, "
                       "`recipe_input_transfer_start_evidence` describes current "
                       "carried input, owned machine, recipe, and planned receipt ID. "
                       "Judge only missing start facts; the transfer and output "
                       "still need native verification."
                       if recipe_input_start else "")
                    + (" For a current output pickup, `output_pickup_start_evidence` "
                       "records ready output, owned source, actor, planner path and "
                       "planned receipt. Judge missing start facts from those values; "
                       "the future pickup and inventory delta require native verification."
                       if qualified_pickup else "")
                    + (" This nested outpost-kit step has separate same-tick parent and child "
                       "paths plus current native start facts. A proposed outpost's admission "
                       "is a planner policy heuristic, not payoff evidence. Judge only whether "
                       "a start fact is missing for this child step; future placement, arrival, "
                       "outpost flow/output, and outer-target completion remain unverified."
                       if qualified_nested_kit else "")
                    + (" This candidate's direct recipe input and current native research-trigger "
                       "counter are both same-tick evidence. The fair resource/receiver start "
                       "facts are present, so do not mark another observation needed merely "
                       "because later recipe output or technology unlock is unverified."
                       if qualified_research_trigger else "")
                    + supplied_research_hint + science_transfer_hint + paid_service_hint
                    + (" This paid output arm has observed ownership, coal headroom, a bounded "
                       "current deficit, carried coal, actor readiness and unused receipt. Judge "
                       "start observations from those values; future transfer and flow verification "
                       "are not missing current start observations."
                       if qualified_buffer_fuel else "")
                    + (" This paid buffer component has observed ownership, actor readiness, "
                       "inventory and an unused planned receipt. Native prepare performs bounded "
                       "geometry/clearance checks before placement; future approach, receipt and "
                       "flow verification are not additional missing observations before preparation. "
                       "Judge any contrary current start fact independently."
                       if qualified_buffer_build else "")
                    + (" For a placement, `placement_start_evidence` combines a current "
                       "surveyed site offer with observed actor/queue facts. Judge missing "
                       "start facts from those "
                       "values; an unverified walking path or future build receipt is not a "
                       "missing start observation."
                       if placement_start else "")
                    + (" For this utility lab, current carried stock, role absence, "
                       "actor readiness, idle research state, and the named capability "
                       "technology dependency are observed. Site clearance and travel "
                       "remain unknown; the existing bounded native placement action "
                       "resolves them and checks again before building. Do not claim "
                       "a site, arrival, power, or research result."
                       if qualified_utility_lab else "")
                ),
            }
        if all(_qualified_bootstrap_output_pickup(plan, facts,
                (state.get('candidate_evidence') or {}).get(plan.id)) for plan in selected):
            questions['candidate']['instructions'] = (
                'Choose the best pickup from facts, candidate_evidence, history and execution_contract. '
                'Judge each row local_target; kit and parent purposes differ. Observe only for missing or '
                'disputed current start facts. Owned stock/capacity and recipe_dependency_chain/current_raw_demand '
                'support a bounded input branch; travel, pickup and output still need native verification. '
                'Unchanged observation cannot establish future outcomes. Confidence concerns this action, '
                'not ultimate completion. Judge independently of other answers.')
        for plan in selected:
            row = (state.get('candidate_evidence') or {}).get(plan.id)
            if _qualified_bootstrap_output_pickup(plan, facts, row):
                questions[plan.id + '/benefit']['instructions'] = (
                    f'How directly would `candidate_plans[{json.dumps(plan.id)}]` advance its evidence-row local_target? '
                    'Use facts, execution_contract and bootstrap_output_pickup_start_evidence with recipe_dependency_chain/current_raw_demand. '
                    'Owned stock can supply this bounded recipe-input branch; pickup, output, '
                    'science/route flow remain unverified; no full-game plan required.')
        comparison = _qualified_shared_parent_comparison(facts, selected, state.get('candidate_evidence') or {})
        if comparison is not None:
            context['shared_parent_comparison'] = comparison
            context['local_objective'] = {
                'kind': 'qualified_shared_parent_branches',
                'primary_target': comparison['parent_target'].copy(),
                'ultimate_goal': comparison['parent_target']['ultimate_goal'],
                'instruction': (
                    'Compare partial branches toward this parent using each row local_target and current start facts. '
                    'The kit includes route work and future belt reserve; neither step proves parent completion, '
                    'future flow or recipe output.'),
                'success_authority': 'unchanged native step and goal predicates, never model scores',
            }
            # Some callers retain full route facts; ordinary model snapshots
            # compact them. Elide only an identical duplicated route contract.
            route_projection = facts['factory']['recipe_dependency_catalog'].get('comparison_input_route')
            original_routes = facts['factory']['input_routes']
            if (route_projection is not None
                    and json.dumps({key: original_routes.get(key) for key in ('protocol', 'tick', 'session_id')}, sort_keys=True, allow_nan=False)
                        == json.dumps({key: route_projection[key] for key in ('protocol', 'tick', 'session_id')}, sort_keys=True, allow_nan=False)
                    and all(json.dumps(original_routes['sources'].get(key), sort_keys=True, allow_nan=False)
                            == json.dumps(value, sort_keys=True, allow_nan=False)
                            for key, value in route_projection['sources'].items())):
                from copy import deepcopy
                context['facts'] = deepcopy(context['facts'])
                del context['facts']['factory']['recipe_dependency_catalog']['comparison_input_route']
            questions['candidate']['instructions'] = (
                'Compare shared_parent_comparison and keyed candidate_evidence. '
                'The kit covers a proposed route and future science belt reserve; '
                'the other branch supplies the same parent recipe. Compare current start facts, candidate-local '
                'scope and remaining work. Ranking/costs are heuristics, not native measurements. '
                'Neither proves future flow/output; their quantities are not joint completion. '
                'Observe for disputed start facts. No answer or confidence is imposed.')
        context = _factor_bootstrap_recipes(context)
        size = len(json.dumps({"state": context, "questions": questions},
                              ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size <= max_bytes:
            return context, questions, selected
        selected = selected[:-1]
    raise ValueError("Decision request exceeds byte budget or has no candidates")


def benefit_gate(answer: dict, confidence_floor: float) -> dict:
    """Retain the legacy distribution summary for audit, never admission.

    ``select_plan`` now requires an independent useful-progress choice. Neither
    this positive probability mass nor its legacy ``passed`` field authorizes
    selection. The reported confidence describes ordinal magnitude uncertainty.
    """
    probabilities = answer["probabilities"]
    level0 = float(probabilities["0"])
    positive = {key: float(value) for key, value in probabilities.items() if key != "0"}
    support = max(0.0, min(1.0, 1.0 - level0))
    strongest_positive = max(positive.values()) if positive else 0.0
    return {
        "support": support,
        "level0": level0,
        "reported_confidence": float(answer["confidence"]),
        "floor": float(confidence_floor),
        "passed": bool(support >= confidence_floor and level0 < strongest_positive),
    }


def is_lone_passive_background_wait(plans) -> bool:
    """True only for the sole candidate being the tracked craft's passive wait.

    The background planner offers this plan only when no independent ready work
    exists, and it is a single observation-only ``factory_wait`` on the crafting
    queue. There is nothing to choose between and nothing to judge useful:
    asking the model can only abstain or reject, and the rejection used to end
    the run while the craft completed. Other waits (buffer, capital) keep their
    own ids and are judged as before.
    """
    if len(plans) != 1:
        return False
    plan = plans[0]
    return (isinstance(plan.id, str) and plan.id.startswith("background-wait:")
            and len(plan.steps) == 1 and plan.steps[0].action == "factory_wait"
            and plan.steps[0].effect == "crafting_idle")


def select_plan(client, state: dict, plans: list[Plan], confidence_floor: float = 0.45,
                max_bytes: int = DEFAULT_MAX_REQUEST_BYTES, *, prepared_batch=None) -> Decision:
    _number(confidence_floor)
    if prepared_batch is None:
        context, questions, offered = question_batch(state, plans, max_bytes=max_bytes)
    else:
        context, questions, offered = prepared_batch
        offered_ids = [plan.id for plan in offered]
        original_plans = {plan.id: plan for plan in plans}
        if (not offered_ids or len(offered_ids) != len(set(offered_ids))
                or len(original_plans) != len(plans)
                or any(plan.id not in original_plans
                       or _json_identity(plan.to_dict()) != _json_identity(
                           original_plans[plan.id].to_dict()) for plan in offered)
                or set(context.get("candidate_plans", {})) != set(offered_ids)
                or set(questions) != {"candidate", *(
                    plan_id + suffix for plan_id in offered_ids
                    for suffix in ("/useful_progress", "/benefit", "/disruption", "/needs_observation"))}
                or len(json.dumps({"state": context, "questions": questions},
                                  ensure_ascii=False, allow_nan=False).encode("utf-8")) > max_bytes):
            raise ValueError("Invalid or oversized prepared decision batch")
        # Validate the complete source-bound request without replacing the durable
        # objects that will actually be sent. JSON identity is type-sensitive.
        expected_context, expected_questions, expected_offered = question_batch(
            state, offered, max_bytes=max_bytes, max_candidates=len(offered))
        if (len(expected_offered) != len(offered)
                or _json_identity(context) != _json_identity(expected_context)
                or _json_identity(questions) != _json_identity(expected_questions)):
            raise ValueError("Prepared decision batch differs from current plans or evidence")
    request_bytes = len(json.dumps({"state": context, "questions": questions},
                                   ensure_ascii=False, allow_nan=False).encode("utf-8"))
    diagnostics = {"schema": 1, "input_candidates": len(plans),
                   "offered_candidates": len(offered),
                   "pruned_candidate_ids": [p.id for p in plans if p not in offered],
                   "request_bytes": request_bytes, "max_request_bytes": max_bytes,
                   "candidate_rejections": {}}
    if is_lone_passive_background_wait(plans) and [p.id for p in offered] == [plans[0].id]:
        # No model call: the write-ahead attempt and any one-use source
        # authorization were already recorded by the caller for this exact
        # fingerprint, so persistence and audit are unchanged.
        return Decision(plans[0].id, "passive-wait",
                        "Only the tracked craft's passive wait is available; nothing to judge",
                        context, questions, model_called=False,
                        diagnostics={**diagnostics, "outcome": "selected",
                                     "model_skipped": True, "passive_wait": True})
    try:
        answers = client.evaluate(context, questions)
    except ProviderBlocked as error:
        return Decision(None, "observe", str(error), context, questions,
                        model_called=error.called,
                        diagnostics={**diagnostics, "outcome": "provider_blocked",
                                     "provider": error.state})
    except (requests.Timeout, requests.ConnectionError) as error:
        return Decision(None, "observe", f"Transient provider failure: {type(error).__name__}",
                        context, questions, model_called=True,
                        diagnostics={**diagnostics, "outcome": "provider_failure"})
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        if status is None or not (500 <= status <= 599 or status in {408, 429}):
            raise
        return Decision(None, "observe", f"Transient provider failure: HTTP {status}",
                        context, questions, model_called=True,
                        diagnostics={**diagnostics, "outcome": "provider_failure"})
    except ValueError as error:
        return Decision(None, "observe", f"Invalid provider payload: {type(error).__name__}",
                        context, questions, model_called=True,
                        diagnostics={**diagnostics, "outcome": "invalid_provider_payload"})
    try:
        validate_answers(questions, answers, quantum=getattr(client, "answer_quantum", 0))
    except InvalidJudgment as error:
        return Decision(None, "observe", str(error), context, questions,
                        answers if isinstance(answers, dict) else {}, model_called=True,
                        diagnostics={**diagnostics, "outcome": "invalid_answer"})
    choice = answers["candidate"]
    utilities = {}
    diagnostics["benefit_gate"] = {}
    diagnostics["usefulness_gate"] = {}
    for plan in offered:
        benefit = answers[plan.id + "/benefit"]
        disruption = answers[plan.id + "/disruption"]
        usefulness = answers[plan.id + "/useful_progress"]
        gate = benefit_gate(benefit, confidence_floor)
        gate["eligibility_authority"] = False
        diagnostics["benefit_gate"][plan.id] = gate
        # Eligibility is judged from the validated answer distribution, not from
        # the model's separately reported confidence. For this two-label question
        # the reported number is not tied to the probabilities (a live answer put
        # 0.60 on `useful` while reporting 0.20), so using it as a veto rejected a
        # plan the model clearly favored. The same principle governs benefit_gate.
        # The reported confidence stays in the diagnostics for audit only.
        useful_probability = float(usefulness["probabilities"]["useful"])
        useful = (usefulness["choice"] == "useful"
                  and useful_probability >= confidence_floor)
        diagnostics["usefulness_gate"][plan.id] = {
            "choice": usefulness["choice"],
            "probability": useful_probability,
            "confidence": usefulness["confidence"],
            "floor": confidence_floor,
            "passed": useful,
        }
        rejected = []
        if answers[plan.id + "/needs_observation"]["noul"] >= 0.5:
            rejected.append("missing_start_evidence")
        if usefulness["choice"] != "useful":
            rejected.append("no_demonstrated_progress")
        elif useful_probability < confidence_floor:
            rejected.append("low_usefulness_confidence")
        # A negative ordinal judgment contradicts eligibility; never ignore it.
        # Positive-level ambiguity and its reported confidence only affect rank.
        probabilities = benefit["probabilities"]
        if probabilities["0"] >= max(value for key, value in probabilities.items()
                                     if key != "0"):
            rejected.append("low_benefit_confidence")
        if disruption["confidence"] < confidence_floor:
            rejected.append("low_disruption_confidence")
        if rejected:
            diagnostics["candidate_rejections"][plan.id] = rejected
            continue
        # Ranking heuristic, NOT a probability of plan success or game victory.
        benefit_maximum = len(questions[plan.id + "/benefit"]["criteria"]) - 1
        disruption_maximum = len(questions[plan.id + "/disruption"]["criteria"]) - 1
        utilities[plan.id] = (choice["probabilities"][plan.id]
                              + benefit["score"] / (2 * benefit_maximum)
                              - disruption["score"] / (4 * disruption_maximum)
                              - len(plan.steps) * 0.02)
    # A global rejection does not imply that candidate-level gates passed.
    # Report every validated answer's qualification before returning, while
    # retaining the same choice floor and abstention behavior. These diagnostics
    # never authorize a plan or cause another provider call.
    if choice["choice"] == "observe" or choice["confidence"] < confidence_floor:
        outcome = "model_abstention" if choice["choice"] == "observe" else "low_choice_confidence"
        return Decision(None, "observe", outcome.replace("_", " "),
                        context, questions, answers, model_called=True,
                        diagnostics={**diagnostics, "outcome": outcome})
    selected = max(utilities, key=utilities.get) if utilities else None
    source = "mock" if getattr(client, "is_mock", False) else "jev"
    if not selected and diagnostics["pruned_candidate_ids"]:
        # Distinguish "no alternative existed" from "alternatives were never shown":
        # the reason string is matched by persistence, re-evaluation and memory.
        diagnostics["alternatives_not_shown"] = list(diagnostics["pruned_candidate_ids"])
    return Decision(selected, source if selected else "observe",
                    "" if selected else "Candidate evidence insufficient",
                    context, questions, answers, utilities, model_called=True,
                    diagnostics={**diagnostics, "outcome": "selected" if selected else "all_candidates_rejected"})
