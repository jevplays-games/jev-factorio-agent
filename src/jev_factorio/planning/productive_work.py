"""Bounded useful work during production/research, never dispatch authority.

Stockpile forecasts are optional work, not immediate prerequisites. Coverage
uses native recipe/speed facts plus labeled travel/service policy estimates;
unknown geometry or speed remains unknown. All choices use the existing action
contracts, and the controller still owns reservations, receipts and job locks.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

from ..skills import Plan
from ..state import GameSnapshot
from .demand import SupplyLedger

from .economics import solid_recipe
from .scheduling import SAFETY_TICKS, SERVICE_TICKS, TRAVEL_TICKS_PER_TILE, research_schedule

if TYPE_CHECKING:
    from .ready_work import ReadyWorkPlanner

MAX_PRODUCERS = 64
MAX_PREPARATION_PROBES = 32
OPERATING_BATCHES = 20
MIN_REFILL = 10
PREPARATION_ACTIONS = {'factory_gather', 'factory_insert', 'factory_extract', 'factory_craft'}


def _finite(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _admissible(plan: Plan | None, snapshot: GameSnapshot, ledger: SupplyLedger) -> bool:
    if not plan or len(plan.steps) != 1:
        return False
    step = plan.steps[0]
    return (step.action in PREPARATION_ACTIONS and step.allowed(snapshot)
            and not step.satisfied(snapshot)
            and all(count <= ledger.carried.get(item, 0)
                    for item, count in (step.costs or {}).items()))


def _lead_ticks(snapshot: GameSnapshot, machine: dict) -> int | None:
    origin, target = snapshot.player_position, machine.get('position', {})
    if (not isinstance(origin, (tuple, list)) or len(origin) != 2
            or not isinstance(target, dict)
            or not all(_finite(v) for v in (*origin, target.get('x'), target.get('y')))):
        return None
    distance = abs(origin[0] - target['x']) + abs(origin[1] - target['y'])
    return math.ceil(SERVICE_TICKS + distance * TRAVEL_TICKS_PER_TILE)


def producer_resupply(planner: ReadyWorkPlanner, primary: Plan) -> Plan | None:
    """Refill relevant existing solid producers before their inputs run out.

    No new machine, recipe, fuel or power assumption is introduced here. Only
    carried stock may be delivered. A low/high watermark limits tiny recurring
    visits, but a small delivery that starts a complete batch or finishes the
    remaining demand is permitted. Route/output ownership is checked by allowed.
    """
    demand = dict(planner._remaining_products())
    for item, amount in planner.demands.items():
        demand[item] = max(demand.get(item, 0), amount)
    first = primary.steps[0]
    if first.effect == 'machine_output':
        demand[first.item] = max(demand.get(first.item, 0), first.threshold)
    choices = []
    seen = set()
    for role, machine in sorted(planner.entities.items())[:MAX_PRODUCERS]:
        if not role.startswith(('recipe:', 'capacity:')):
            continue
        unit = machine.get('unit_number')
        if type(unit) is not int or unit <= 0 or unit in seen:
            continue
        seen.add(unit)
        recipe = planner.catalog.recipes.get(machine.get('recipe', ''), {})
        prototype = planner.catalog.machines.get(machine.get('name', ''), {})
        if (not solid_recipe(recipe) or not planner.catalog.enabled(recipe, planner.researched)
                or not prototype.get('categories', {}).get(recipe['category'])
                or type(machine.get('crafting')) is not bool):
            continue
        energy, fuel = machine.get('energy'), machine.get('fuel', {}).get('coal')
        if not ((prototype.get('electric') is True and _finite(energy) and energy > 0)
                or (prototype.get('burner') is True and _finite(fuel) and fuel >= 5)):
            continue
        product = recipe['products'][0]
        wanted = demand.get(product['name'], 0)
        if wanted <= 0:
            continue
        inputs = machine.get('input', {})
        batches = min(OPERATING_BATCHES, math.ceil(wanted / product['amount']))
        running = int(machine['crafting'])
        speed = prototype.get('speed')
        batch_ticks = recipe['energy'] * 60 / speed if _finite(speed) and speed > 0 else None
        lead = _lead_ticks(planner.snapshot, machine)
        for ingredient in recipe['ingredients']:
            item, each = ingredient['name'], ingredient['amount']
            buffered = inputs.get(item, 0)
            stock = min(planner.snapshot.inventory.get(item, 0), planner.ledger.carried.get(item, 0))
            stack = planner.catalog.stack_sizes.get(item, 200)
            if not all(_finite(v) and v >= 0 for v in (buffered, stock, stack)):
                continue
            missing = max(0, math.ceil(each * batches - buffered - each * running))
            count = math.floor(min(200, missing, stock, max(0, stack - buffered)))
            if count <= 0:
                continue
            unblocks = (not machine['crafting'] and buffered < each
                        and all(inputs.get(i['name'], 0) + (count if i['name'] == item else 0) >= i['amount']
                                for i in recipe['ingredients']))
            coverage = (buffered / each + running) * batch_ticks if batch_ticks is not None else None
            due = (coverage is not None and lead is not None and coverage <= lead + SAFETY_TICKS)
            if not unblocks and (not due or count < min(MIN_REFILL, math.ceil(each * batches))):
                continue
            plan = planner._transfer(role, item, count)
            if not _admissible(plan, planner.snapshot, planner.ledger):
                continue
            details = {'kind': 'producer_resupply', 'role': role, 'item': item,
                       'observed_tick': planner.snapshot.tick,
                       'immediate_requirement': max(0, math.ceil(each - buffered)),
                       'operating_target': math.ceil(each * batches),
                       'forecast_target': planner.targets.get(item, 0),
                       'coverage_ticks': coverage, 'lead_ticks': lead,
                       'reason': 'unblocks_complete_batch' if unblocks else 'input_coverage_low',
                       'basis': 'catalog-and-policy-estimate'}
            plan = replace(plan, description=f"Prevent input starvation at {role}: deliver {count} {item}",
                           materials={**(plan.materials or {}), 'scheduling': details})
            choices.append((not unblocks, coverage is None, coverage or 0, role, item, plan))
    return min(choices, key=lambda row: row[:-1])[-1] if choices else None



def _preparation_parent_path(catalog, researched, root, material):
    """Keep a bounded current recipe ancestry when probing one ingredient."""
    remaining = 128
    def visit(item, path):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or len(path) >= 32 or item in path:
            return None
        if item == material:
            return tuple('item:' + parent for parent in path)
        try:
            recipe = catalog.recipe_for(item)
            if not catalog.enabled(recipe, researched):
                return None
            ingredients = sorted({row['name'] for row in recipe['ingredients']
                                  if row.get('type') == 'item'})
        except (KeyError, TypeError, ValueError):
            return None
        for ingredient in ingredients:
            found = visit(ingredient, (*path, item))
            if found is not None:
                return found
        return None
    return visit(root, ())


def prepare_research_batch(planner: ReadyWorkPlanner, primary: Plan) -> Plan:
    """Prepare at most one extra current-research batch beyond lab inventory.

    Existing queued, in-flight and acknowledged outputs count only in the
    forecast, so repeated observations do not order the same batch repeatedly.
    Fresh capability-aware workers expose independent ingredients when the
    first prerequisite is waiting. No lab stock is borrowed for preparation.
    """
    rows = research_schedule(planner.snapshot, planner.catalog)
    forecast = planner.ledger.forecast_stock()
    probes = 0
    rejected = set()

    def worker():
        result = type(planner)(planner.catalog, planner.snapshot, planner.goal,
                               planner.collection_batch, planner.max_candidates)
        result.ledger = planner.ledger
        result.allow_service_visits = planner.allow_service_visits
        return result

    for row in rows[:8]:
        item = row['item']
        target = min(OPERATING_BATCHES, max(0, row['remaining'] - row['available']))
        if target <= forecast.get(item, 0):
            continue
        current = worker()
        try:
            candidate = current._need(item, target)
        except (ValueError, KeyError):
            rejected.add('unsupported_preparation')
            continue
        options = [candidate]
        for material, amount in sorted(current.targets.items()):
            if probes >= MAX_PREPARATION_PROBES:
                rejected.add('preparation_probe_budget')
                break
            probes += 1
            probe = worker()
            probe.focus, probe.demands = current.focus, dict(current.demands)
            probe.raw_targets = dict(current.raw_targets)
            probe.speculative = True
            parent_path = _preparation_parent_path(
                planner.catalog, planner.snapshot.researched or [], item, material)
            if parent_path is None:
                rejected.add('unlinked_preparation_ingredient')
                continue
            try:
                options.append(probe._need(material, amount, parent_path))
            except (ValueError, KeyError):
                rejected.add('unsupported_ingredient')
        for option in options:
            if _admissible(option, planner.snapshot, planner.ledger):
                return replace(option, description=f"Prepare during research: {target} {item}. " + option.description,
                    materials={**(option.materials or {}), 'scheduling': {
                        'kind': 'research_preparation', 'item': item, 'inventory_target': target,
                        'lab_committed': row['available'], 'forecast_output': forecast.get(item, 0),
                        'basis': 'bounded_current_research_demand'}})
        rejected.add('no_admissible_preparation')
    return replace(primary, materials={**(primary.materials or {}), 'productive_work': {
        'reason': 'no_ready_independent_work', 'preparation_probes': probes,
        'rejections': sorted(rejected)}})


def productive_work(planner: ReadyWorkPlanner, primary: Plan | None) -> Plan | None:
    """Preserve urgent/in-flight/infrastructure work; improve gather/passive slots."""
    if (not primary or (primary.materials or {}).get('capital_investment') or planner.goal != 'rocket_launch'
            or planner.factory.get('player_bound') is not True
            or planner.factory.get('player_connected') is not True
            or planner.factory.get('crafting_queue', 0)
            or getattr(planner, '_buffer_service', False)
            or getattr(planner, '_economic_acquiring', False)):
        return primary
    boiler = planner.entities.get('utility:boiler')
    if boiler and boiler.get('fuel', {}).get('coal', 0) < 5:
        return primary
    step = primary.steps[0]
    if step.action != 'factory_gather' and not (
            step.action == 'factory_wait' and step.effect in {'machine_output', 'research_progress'}):
        return primary
    service = producer_resupply(planner, primary)
    if service:
        return service
    if step.action == 'factory_wait' and step.effect == 'research_progress':
        return prepare_research_batch(planner, primary)
    return primary
