"""One bounded, staged producer investment; estimates are never output evidence.

A machine may need the very intermediate it will produce. Only acquisition of
its first kit disables optional economic recursion, not the lifetime investment.
Every stage still compiles ordinary paid, individually verified native actions.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import replace

from .economics import MAX_PRODUCT_HORIZON, RECURRING, remaining_products, solid_recipe

MARKER = 'capital_investment'
STAGES = {'kit', 'build', 'configure', 'supply', 'verify'}
SPEC_FIELDS = {'schema', 'key', 'item', 'recipe', 'role', 'machine', 'catalog_sha256',
               'workload', 'investment_ticks', 'queue_ticks', 'batches'}
STATE_FIELDS = {'spec', 'stage', 'started_tick', 'deadline_tick', 'unit_number', 'products_baseline'}
MAX_INVESTMENT_TICKS = 216000
MAX_PROBES = 16


def _integer(value, low=0, high=2**53 - 1):
    return type(value) is int and low <= value <= high


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _catalog_graph(catalog, recipe, machine):
    # Hash the bounded relevant graph, not the entire catalog on every probe.
    graph, queue = {}, [catalog.recipes[recipe], catalog.recipes[machine]]
    while queue:
        current = queue.pop()
        if current['name'] in graph:
            continue
        if len(graph) >= 256:
            raise ValueError('Capital catalog dependency budget exceeded')
        graph[current['name']] = current
        for entry in current.get('ingredients', []):
            if entry['name'] in {'coal', 'iron-ore', 'copper-ore', 'stone', 'wood'}:
                continue
            try:
                queue.append(catalog.recipe_for(entry['name']))
            except ValueError:
                continue
    return {'version': catalog.version, 'recipes': graph,
            'machine': catalog.machines[machine], 'recipe': recipe,
            'hand_categories': catalog.hand_categories}


def catalog_digest(catalog, recipe, machine, *, schema=2):
    graph = _catalog_graph(catalog, recipe, machine)
    if schema == 2:
        graph['recipes'] = {name: {key: value for key, value in row.items() if key != 'enabled'}
                            for name, row in graph['recipes'].items()}
    elif schema != 1:
        raise ValueError('Unsupported capital catalog schema')
    return _digest(graph)


def _legacy_digests(catalog, graph, researched):
    # Reconstruct only research-driven false -> true bits. All other fields stay
    # exact. The caller must fail closed if the bounded search is unavailable.
    unlocked = set(researched)
    names = sorted(name for name, row in graph['recipes'].items()
                   if row.get('enabled') is True and set(catalog.unlocks(name)) & unlocked)
    if len(names) > 8:
        return None
    graph = deepcopy(graph)
    digests = set()
    for mask in range(1 << len(names)):
        for index, name in enumerate(names):
            graph['recipes'][name]['enabled'] = not bool(mask & (1 << index))
        digests.add(_digest(graph))
    return digests


def _legacy_catalog_matches(catalog, spec, researched):
    graph = _catalog_graph(catalog, spec['recipe'], spec['machine'])
    if _digest(graph) == spec['catalog_sha256']:
        return True
    return spec['catalog_sha256'] in (_legacy_digests(catalog, graph, researched) or set())


def failure_keys(catalog, spec, researched):
    """Retain exhausted legacy investment budgets without rewriting any key."""
    graph = _catalog_graph(catalog, spec['recipe'], spec['machine'])
    digests = _legacy_digests(catalog, graph, researched)
    if digests is None:
        return None
    digests.add(catalog_digest(catalog, spec['recipe'], spec['machine']))
    return {key_for({**spec, 'catalog_sha256': digest}) for digest in digests}


def key_for(spec):
    return 'capital:' + _digest({k: spec[k] for k in
        ('item', 'recipe', 'role', 'machine', 'catalog_sha256')})[:24]


def validate_spec(spec, catalog=None, researched=None):
    if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS:
        raise ValueError('Invalid capital investment specification')
    if (type(spec['schema']) is not int or spec['schema'] not in {1, 2}
            or any(not isinstance(spec[k], str) or not 0 < len(spec[k]) <= 128
                   for k in ('key', 'item', 'recipe', 'role', 'machine', 'catalog_sha256'))
            or len(spec['catalog_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in spec['catalog_sha256'])
            or spec['role'] != 'recipe:' + spec['recipe'] or spec['key'] != key_for(spec)
            or not _integer(spec['workload'], 40, MAX_PRODUCT_HORIZON)
            or not _integer(spec['investment_ticks'], 1)
            or not _integer(spec['queue_ticks'], spec['investment_ticks'] + 1200)
            or not _integer(spec['batches'], 1, 20)):
        raise ValueError('Invalid capital investment bounds or identity')
    if catalog is not None:
        recipe = catalog.recipes.get(spec['recipe'], {})
        machine = catalog.machines.get(spec['machine'], {})
        if (not solid_recipe(recipe) or recipe['category'] != 'crafting'
                or recipe['products'][0]['name'] != spec['item']
                or not catalog.hand_categories.get(recipe['category'])
                or not machine.get('categories', {}).get(recipe['category'])
                or not catalog.enabled(recipe, researched or [])
                or not catalog.enabled(catalog.recipes.get(spec['machine'], {}), researched or [])
                or not (_legacy_catalog_matches(catalog, spec, researched or []) if spec['schema'] == 1
                        else catalog_digest(catalog, spec['recipe'], spec['machine']) == spec['catalog_sha256'])):
            raise ValueError('Capital investment catalog or capability changed')


def validate_state(state, last_tick):
    if not isinstance(state, dict) or set(state) != STATE_FIELDS:
        raise ValueError('Invalid capital investment checkpoint')
    validate_spec(state['spec'])
    if (state['stage'] not in STAGES
            or not _integer(state['started_tick'], 0, last_tick)
            or not _integer(state['deadline_tick'], state['started_tick'] + 1,
                            state['started_tick'] + MAX_INVESTMENT_TICKS)
            or state['unit_number'] is not None and not _integer(state['unit_number'], 1)
            or state['products_baseline'] is not None and not _integer(state['products_baseline'])
            or state['products_baseline'] is not None and state['unit_number'] is None):
        raise ValueError('Invalid capital investment checkpoint bounds')


def proposal(planner, recipe, name, cost, work, amount):
    """Use existing profitability gates; do not broaden supported recipe kinds."""
    if (recipe['category'] != 'crafting' or not planner.catalog.hand_categories.get('crafting')
            or not solid_recipe(recipe)):
        return None
    item = recipe['products'][0]['name']
    spec = {'schema': 2, 'item': item, 'recipe': recipe['name'], 'role': 'recipe:' + recipe['name'],
            'machine': name, 'catalog_sha256': catalog_digest(planner.catalog, recipe['name'], name),
            'workload': math.ceil(work), 'investment_ticks': math.ceil(cost),
            'queue_ticks': math.floor(work / recipe['products'][0]['amount'] * recipe['energy'] * 60),
            'batches': min(20, max(1, math.ceil(amount / recipe['products'][0]['amount'])))}
    spec['key'] = key_for(spec)
    validate_spec(spec, planner.catalog, planner.researched)
    return continuation(planner, spec)


def continuation(planner, spec):
    """Recompile one stage from current facts, with no optional nested investment.

    Start with a fresh dependency path: the producer's eventual output must not
    appear as an ancestor of the paid ingredients needed to bootstrap its kit.
    Capability-aware buffer/route/outpost acquisition paths remain in use.
    """
    validate_spec(spec, planner.catalog, planner.researched)
    old = getattr(planner, '_economic_acquiring', False)
    old_focus = getattr(planner, 'focus', None)
    planner._economic_acquiring = True
    try:
        recipe = planner.catalog.recipes[spec['recipe']]
        machine = planner.entities.get(spec['role'])
        if machine is None:
            # Kit recipes begin at the machine, not at its eventual product.
            # Preserve that immediate target while composing native child actions.
            if hasattr(planner, 'focus'):
                planner.focus = (spec['machine'], 1)
            plan = planner._machine(spec['role'], spec['machine'], ())
            stage = 'build' if plan.steps[0].action == 'factory_place' and (
                plan.steps[0].parameters['role'] == spec['role']) else 'kit'
        else:
            if machine['name'] != spec['machine']:
                raise ValueError('Capital investment producer was replaced')
            plan = planner._production(recipe, spec['role'], spec['batches'], ())
            if plan:
                stage = 'configure' if plan.steps[0].action == 'factory_configure' and (
                    plan.steps[0].parameters['role'] == spec['role']) else 'supply'
            else:
                stage = 'verify'
                plan = planner._wait('machine_output', spec['item'], recipe['products'][0]['amount'],
                                     spec['role'], timeout=7200, identity=spec['key'])
    finally:
        planner._economic_acquiring = old
        if hasattr(planner, 'focus'):
            planner.focus = old_focus
    return replace(plan, id=spec['key'] + ':' + stage + ':' + plan.id,
                   description=f"Invest in {spec['item']} ({stage}): " + plan.description,
                   materials={**(plan.materials or {}), MARKER: {'spec': deepcopy(spec), 'stage': stage, 'observed_tick': planner.snapshot.tick},
                              'economics': {'basis': 'catalog-and-policy-estimate',
                                  'objective': 'recurring_machine_production', 'item': spec['item'],
                                  'machine': spec['machine'], 'investment_ticks': spec['investment_ticks'],
                                  'workload': spec['workload']}})


def held_kit(state, snapshot, catalog):
    """Earmark only observed stock consumed by one remaining construction kit.

    Expand shortages without crediting imaginary co-products as carried stock.
    No separate physical inventory or mutation is introduced by these holds.
    """
    if state['unit_number'] is not None:
        return {}
    stock, held, probes = dict(snapshot.inventory), {}, 0
    def need(item, amount, path=()):
        nonlocal probes
        probes += 1
        if probes > 256 or item in path:
            raise ValueError('Capital kit dependency cycle or expansion budget exceeded')
        used = min(stock.get(item, 0), amount)
        stock[item] = stock.get(item, 0) - used
        if used:
            held[item] = held.get(item, 0) + used
        missing = amount - used
        if missing <= 0:
            return
        try:
            recipe = catalog.recipe_for(item)
        except ValueError:
            return  # A missing raw resource contributes no carried reservation.
        if not catalog.enabled(recipe, snapshot.researched or []):
            return
        if not solid_recipe(recipe):
            raise ValueError('Unsupported capital kit dependency')
        batches = math.ceil(missing / recipe['products'][0]['amount'])
        for entry in sorted(recipe['ingredients'], key=lambda i: i['name']):
            need(entry['name'], entry['amount'] * batches, (*path, item))
    need(state['spec']['machine'], 1)
    return held


def matches(plan, state):
    marker = (plan.materials or {}).get(MARKER)
    return (isinstance(marker, dict) and marker.get('spec') == state['spec']
            and marker.get('stage') in STAGES and plan.id.startswith(state['spec']['key'] + ':'))


def costs_allowed(plan, snapshot, state, catalog):
    if state is None:
        return True
    if matches(plan, state):
        # The planner's frontier is not revisited while a committed plan is
        # retained. Recheck its bounded lifetime at the final pre-dispatch gate.
        return (type(snapshot.tick) is int
                and snapshot.tick < state['deadline_tick'])
    held = held_kit(state, snapshot, catalog)
    costs = {}
    for step in plan.steps:
        for item, amount in (step.costs or {}).items():
            costs[item] = costs.get(item, 0) + amount
    return all(amount <= snapshot.inventory.get(item, 0) - held.get(item, 0)
               for item, amount in costs.items())


def offers(planner):
    """Expose at most a bounded frontier of new recurring investments during research."""
    result = []
    products = getattr(planner, '_economic_products', None)
    if products is None:
        products = remaining_products(planner.snapshot, planner.catalog)
    items = [item for item in sorted(products) if item in RECURRING or item.endswith('-science-pack')]
    for item in items[:MAX_PROBES]:
        if len(result) >= 4:
            break
        if item not in RECURRING and not item.endswith('-science-pack'):
            continue
        try:
            recipe = planner.catalog.recipe_for(item)
            if 'recipe:' + recipe['name'] in planner.entities:
                continue
            work = planner._workload(item)
            if (not solid_recipe(recipe) or recipe['category'] != 'crafting' or work < 40
                    or not planner.catalog.enabled(recipe, planner.researched)):
                continue
            choice = planner._investment_machine(recipe)
            if not choice:
                continue
            name, cost = choice
            if work / recipe['products'][0]['amount'] * recipe['energy'] * 60 < cost + 1200:
                continue
            plan = proposal(planner, recipe, name, cost, work, min(20, work))
            if plan:
                result.append(plan)
        except (KeyError, ValueError):
            continue
    return result
