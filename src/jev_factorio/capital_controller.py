"""Durable capital intent over the existing single, write-ahead dispatcher.

This module never dispatches, replays, clears pending work, or grants inventory.
Only selected plans acquire intent; inspecting candidates is not a commitment.
"""
from __future__ import annotations

from copy import deepcopy
import math

from .planning import capital
from .planning.ready_work import ReadyWorkPlanner
from .planning.decision_support import candidate_evidence
from .skills import Plan, Step


def enabled(loop):
    return loop.factory_scheduling == 'ready-work' and loop.catalog is not None and loop.target == 'rocket_launch'


def _available(loop, spec, snapshot):
    keys = capital.failure_keys(loop.catalog, spec, snapshot.researched or [])
    return (keys is not None and spec['role'] not in snapshot.factory.get('entities', {})
            and all(loop.memory.failures.get(key, 0) < 2 for key in keys))


def commit(loop, plan, snapshot):
    marker = (plan.materials or {}).get(capital.MARKER)
    if not marker:
        return
    if not enabled(loop) or set(marker) != {'spec', 'stage', 'observed_tick'} or marker['stage'] not in capital.STAGES:
        raise ValueError('Unsupported capital commitment')
    spec = marker['spec']
    capital.validate_spec(spec, loop.catalog, snapshot.researched or [])
    state = loop.memory.capital_investment
    if state is None:
        if (not _available(loop, spec, snapshot) or loop.memory.pending
                or getattr(loop.memory, 'background_job', None)
                or getattr(loop.memory, 'solid_funding', None) is not None
                or getattr(loop.memory, 'coal_funding', None) is not None):
            raise ValueError('Cannot start capital commitment at this boundary')
        state = {'spec': deepcopy(spec), 'stage': marker['stage'], 'started_tick': snapshot.tick,
                 'deadline_tick': snapshot.tick + min(capital.MAX_INVESTMENT_TICKS,
                                                    max(7200, spec['investment_ticks'] * 4)),
                 'unit_number': None, 'products_baseline': None}
        loop.memory.capital_investment = state
        loop.memory.event('capital_committed', spec=deepcopy(spec), tick=snapshot.tick)
        loop._trace.emit('capital_committed', {'spec': spec, 'tick': snapshot.tick})
    if not capital.matches(plan, state):
        raise ValueError('Conflicting capital commitment')
    if state['stage'] != marker['stage']:
        loop.memory.event('capital_stage', key=spec['key'], stage=marker['stage'], tick=snapshot.tick)
    state['stage'] = marker['stage']


def observe(loop, snapshot):
    """Bind only our pending placement, then require native counter AND output."""
    loop._capital_deadline_snapshot = snapshot
    state = loop.memory.capital_investment
    if state is None:
        return
    try:
        capital.validate_state(state, snapshot.tick)
        if not enabled(loop):
            raise ValueError('Active capital intent requires the ready-work controller')
        spec = state['spec']
        capital.validate_spec(spec, loop.catalog, snapshot.researched or [])
        entity = snapshot.factory.get('entities', {}).get(spec['role'])
        if entity is None:
            if state['unit_number'] is not None:
                raise ValueError('Owned capital producer disappeared')
            return
        unit = entity.get('unit_number')
        if type(unit) is not int or unit <= 0 or entity.get('name') != spec['machine']:
            raise ValueError('Capital producer identity invalid')
        if state['unit_number'] is None:
            plan = Plan.from_dict(loop.memory.active_plan) if loop.memory.active_plan else None
            step = plan.steps[loop.memory.step_index] if plan else None
            if (not plan or not capital.matches(plan, state) or not loop.memory.pending
                    or step.action != 'factory_place' or step.parameters.get('role') != spec['role']
                    or step.parameters.get('name') != spec['machine'] or not step.satisfied(snapshot)):
                raise ValueError('Untracked producer cannot satisfy capital placement')
            state['unit_number'] = unit
            loop.memory.event('capital_machine_bound', key=spec['key'], unit_number=unit, tick=snapshot.tick)
        elif state['unit_number'] != unit:
            raise ValueError('Owned capital producer was replaced')
        recipe = entity.get('recipe', '')
        if state['products_baseline'] is not None and recipe != spec['recipe']:
            raise ValueError('Capital producer recipe changed')
        if recipe != spec['recipe']:
            return
        finished = entity.get('products_finished')
        if finished is None:
            return  # Missing telemetry is not completion; bounded wait remains in force.
        if type(finished) is not int or finished < 0:
            raise ValueError('Invalid capital production counter')
        if state['products_baseline'] is None:
            state['products_baseline'] = finished
            return
        if finished < state['products_baseline']:
            raise ValueError('Capital production counter regressed')
        output = entity.get('output', {}).get(spec['item'], 0)
        if type(output) not in {int, float} or not math.isfinite(output) or output < 0:
            raise ValueError('Invalid capital output')
        if finished > state['products_baseline'] and output > 0:
            evidence = {'key': spec['key'], 'unit_number': unit, 'recipe': recipe,
                        'products_finished': finished, 'output': output, 'tick': snapshot.tick}
            loop.memory.event('capital_completed', **evidence)
            loop._trace.emit('capital_completed', evidence)
            loop.memory.capital_investment = None
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        loop._capital_fault = True
        loop.memory.status = 'uncertain'
        loop.memory.reason = str(error) + '; preserve capital intent and pending work'


def fail(loop, plan):
    """Bound an optional investment after two reconciled failures of the same step."""
    state = loop.memory.capital_investment
    if not state or not capital.matches(plan, state):
        return
    snapshot = getattr(loop, '_capital_deadline_snapshot', None)
    if _unresolved_work(loop.memory, snapshot):
        return
    if snapshot is not None and snapshot.tick >= state['deadline_tick']:
        abandon(loop, 'bounded_investment_deadline')
        loop._capital_deadline_expired_tick = snapshot.tick
    elif loop.memory.failures.get(plan.id, 0) >= 2:
        abandon(loop, 'step_failure_budget')


def abandon(loop, reason):
    state = loop.memory.capital_investment
    loop.memory.failures[state['spec']['key']] = 2
    loop.memory.event('capital_abandoned', key=state['spec']['key'], reason=reason,
                      unit_number=state['unit_number'], tick=loop.memory.last_tick)
    loop._trace.emit('capital_abandoned', {'key': state['spec']['key'], 'reason': reason})
    # Paid entities stay in place and the ordinary planner can reuse them.
    loop.memory.capital_investment = None


def _unresolved_work(memory, snapshot=None):
    """Keep every durable action/receipt owner intact until reconciliation."""
    if any(getattr(memory, name, None) is not None for name in (
            'pending', 'attempt', 'background_job', 'background_attempt',
            'background_step', 'transfer_recovery')):
        return True
    return bool(snapshot is not None and snapshot.factory.get('crafting_queue', 0))


def _protected_work(snapshot):
    for capability in ('input_routes', 'output_buffers', 'mining_outposts'):
        for row in snapshot.factory.get(capability, {}).get('sources', {}).values():
            if row.get('state') in {'building', 'fault'}:
                return True
    return False


def frontier(loop, snapshot):
    """Final capability-composed frontier: urgent work, committed kit, optional work."""
    original, blocker = loop._compile_candidates(snapshot)
    if any('successor_project' in (p.materials or {}) for p in original):
        return original, blocker  # An explicit successor proposal is not another capital kit.
    if not enabled(loop) or loop.memory.active_goal != 'rocket_launch':
        return original, blocker
    state = loop.memory.capital_investment
    loop._capital_deadline_snapshot = snapshot
    if (loop._execution_barrier(snapshot)
            or (loop.memory.status != 'running' and not loop._persistent_block_active())):
        return original, blocker
    if (state and snapshot.tick >= state['deadline_tick']
            and not _unresolved_work(loop.memory, snapshot)):
        abandon(loop, 'bounded_investment_deadline')
        loop._capital_deadline_expired_tick = snapshot.tick
        state = None
    planner = None
    def current_planner():
        # Most frontiers return ready work without inspecting any investment.
        # Do not rebuild a supply ledger unless a distinct capital query needs it.
        nonlocal planner
        if planner is None:
            planner = getattr(loop, 'planner_type', ReadyWorkPlanner)(loop.catalog, snapshot, 'rocket_launch')
        return planner
    def admissible(plan):
        marker = (plan.materials or {}).get(capital.MARKER)
        return (not marker or (capital.matches(plan, state) if state is not None
                               else _available(loop, marker['spec'], snapshot)))
    def feasible(plan):
        return (admissible(plan) and len(plan.steps) == 1 and loop._step_allowed(plan.steps[0], snapshot)
                and not plan.steps[0].satisfied(snapshot)
                and loop.memory.failures.get(plan.id, 0) < 2
                and capital.costs_allowed(plan, snapshot, state, loop.catalog))
    safe = [plan for plan in original
            if admissible(plan)
            and capital.costs_allowed(plan, snapshot, state, loop.catalog)]
    # An in-flight craft retains output locks, and no capital construction joins it.
    if snapshot.factory.get('crafting_queue', 0) or getattr(loop.memory, 'background_job', None):
        safe = [p for p in safe if not (p.materials or {}).get(capital.MARKER)]
        return safe or [Plan('capital:crafting-wait', 'rocket_launch',
                            'Protect the committed kit while native crafting continues',
                            (Step('factory_wait', 'crafting_idle', timeout_ticks=1800),))], blocker
    rejected_capital = any((p.materials or {}).get(capital.MARKER)
                           and not admissible(p) for p in original)
    if state is None and original and (not safe or rejected_capital):
        # Recompile the ordinary primary after rejecting an investment, even
        # when an old lookahead alternative survived. Keep those alternatives;
        # they must not hide the current production need indefinitely.
        planner = current_planner()
        planner._economic_acquiring = True
        try:
            ordinary = [p for p in planner.candidates() if feasible(p)]
            tracked = getattr(loop, '_tracked_plan', None)
            if tracked:
                ordinary = [tracked(p, snapshot) for p in ordinary]
            retained = {}
            for candidate in [*ordinary, *safe]:
                retained.setdefault(candidate.id, candidate)
            safe = list(retained.values())
        finally:
            planner._economic_acquiring = False
    # Preserve native binding and urgent power/burner maintenance.
    boiler = snapshot.factory.get('entities', {}).get('utility:boiler', {})
    if (snapshot.factory.get('player_bound') is not True or snapshot.factory.get('player_connected') is not True
            or boiler and boiler.get('fuel', {}).get('coal', 0) < 5):
        return safe, blocker
    evidence = candidate_evidence(snapshot, loop.catalog, safe)
    urgent = [p for p in safe if evidence[p.id]['urgency'] >= 2]
    if urgent:
        return urgent, blocker
    if state:
        try:
            plan = capital.continuation(current_planner(), state['spec'])
            tracked = getattr(loop, '_tracked_plan', None)
            if tracked:
                plan = tracked(plan, snapshot)
            if feasible(plan):
                if plan.steps[0].action == 'factory_wait':
                    work = [p for p in safe if capital.MARKER not in (p.materials or {})
                            and all(s.action not in {'factory_wait', 'factory_place', 'factory_configure',
                                                     'factory_connect', 'factory_craft', 'factory_craft_job'}
                                    for s in p.steps)]
                    return work or [plan], ''
                return [plan], ''
        except (ValueError, KeyError):
            pass  # Never bypass capability guards with a less capable planner.
        return safe, blocker or 'No safe continuation for committed capital investment'
    if (getattr(loop.memory, 'solid_funding', None) is not None
            or getattr(loop.memory, 'coal_funding', None) is not None
            or _protected_work(snapshot) or not snapshot.factory.get('research')
            or any(p.steps[0].action not in {'factory_wait', 'factory_gather'}
                   and capital.MARKER not in (p.materials or {}) for p in safe)):
        return safe, blocker
    if getattr(loop, '_capital_deadline_expired_tick', None) == snapshot.tick:
        return safe, blocker
    for plan in capital.offers(current_planner()):
        if loop.memory.failures.get(plan.materials[capital.MARKER]['spec']['key'], 0) >= 2:
            continue
        tracked = getattr(loop, '_tracked_plan', None)
        if tracked:
            plan = tracked(plan, snapshot)
        if feasible(plan):
            # A single profitable investment is a committed strategic alternative,
            # not eight interchangeable acquisition tasks that can eat one another's kit.
            return [plan, *[p for p in safe if p.id != plan.id]][:8], blocker
    return safe, blocker
