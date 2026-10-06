"""Synthetic scheduling evidence; never a native throughput measurement."""
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.controller import HierarchicalLoop
from jev_factorio.craft_jobs import CraftJob, permits_locked_outputs
from jev_factorio.judgments import question_batch
from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.background_work import independent_candidates
from jev_factorio.planning.decision_support import (
    defer_gather_until_bill_craft, scheduling_context,
)
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from test_factory import FactorySimulation, catalog, machine, recipe, snapshot
from test_hierarchical import CountingModel


def frontier():
    state = snapshot(inventory={'iron-gear-wheel': 12, 'iron-plate': 6},
                     nearby_resources={'iron-ore': 50, 'coal': 80, 'stone': 38})
    state.factory['craft_jobs_protocol'] = 1
    state.factory['entities']['recipe:iron-plate'] = machine(fuel={'coal': 5}, energy=100)
    for key in ('output_buffers', 'input_routes', 'mining_outposts'):
        state.factory[key] = {'protocol': 1, 'session_id': state.session_id,
                              'tick': state.tick, 'sources': {}}
    data = catalog()
    data.recipes.update({
        'lab': recipe('lab', {'electronic-circuit': 10, 'iron-gear-wheel': 10,
                             'transport-belt': 4}),
        'electronic-circuit': recipe('electronic-circuit', {'iron-plate': 1}),
        'iron-plate': recipe('iron-plate', {'iron-ore': 1}, 'smelting'),
        'iron-gear-wheel': recipe('iron-gear-wheel', {'iron-plate': 2}),
        'transport-belt': recipe('transport-belt', {'iron-plate': 1,
                                                  'iron-gear-wheel': 1}),
    })
    data.recipes['transport-belt']['products'][0]['amount'] = 2
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    primary = planner._need('lab', 1)
    planner.plan = lambda: primary
    plans = planner.candidates()
    gather = next(p for p in plans if p.steps[0].action == 'factory_gather'
                  and p.steps[0].item == 'iron-ore')
    belt = next(p for p in plans if p.steps[0].item == 'transport-belt')
    step = belt.steps[0]
    belt = replace(belt, steps=(replace(step, action='factory_craft_job',
        effect='craft_job_complete', parameters={**step.parameters, 'receipt': 'belt-test'}),))
    assert belt.materials['shared_bill_craft']['bill_inventory_target'] == 4
    assert gather.steps[0].parameters['resource'] == 'iron-ore'
    return state, data, [gather, belt]


def context(state, data, plans):
    return scheduling_context(state, data, plans, 'rocket_launch')


def memory(state):
    return CampaignMemory(session_id=state.session_id, target='rocket_launch',
                          active_goal='rocket_launch')


def craft_observation_question(state, data, plans, support=None):
    support = support if support is not None else context(state, data, plans)
    _, questions, selected = question_batch(
        {'facts': state.for_jev(), **support}, [plans[1]])
    assert [p.id for p in selected] == [plans[1].id]
    return questions[plans[1].id + '/needs_observation']['instructions']


def test_qualified_current_bill_craft_clarifies_missing_start_fact_without_receipt_claim():
    state, data, plans = frontier()
    question = craft_observation_question(state, data, plans)
    assert 'No required start fact is missing from those witnesses' in question
    assert 'specific contrary current fact' in question
    assert 'Future output still needs a native receipt' in question


def test_bill_craft_start_question_requires_single_offered_candidate():
    state, data, plans = frontier()
    support = context(state, data, plans)
    _, questions, selected = question_batch({'facts': state.for_jev(), **support}, plans)
    assert len(selected) == 2
    assert 'No required start fact is missing from those witnesses' not in (
        questions[plans[1].id + '/needs_observation']['instructions'])


@pytest.mark.parametrize('field,value', [
    ('observed_tick', -1), ('inputs_in_inventory_now', False),
    ('crafting_queue_empty', False), ('player_connected_and_bound', False),
    ('craft_job_protocol_ready', False), ('native_receipt_required_for_completion', False),
    ('native_recipe', 'wrong-recipe'),
])
def test_bill_craft_start_question_refuses_stale_or_unready_native_fact(field, value):
    state, data, plans = frontier()
    support = context(state, data, plans)
    support['candidate_evidence'][plans[1].id]['craft_start_evidence'][field] = value
    assert 'No required start fact is missing from those witnesses' not in (
        craft_observation_question(state, data, plans, support))


@pytest.mark.parametrize('field,value', [
    ('observed_tick', -1), ('local_target_item', 'unrelated'),
    ('unfilled_bill_units', 5), ('expected_products_after_native_verification', 3),
    ('forecast_is_not_paid_stock_or_completed_output', False),
    ('background_overlap_requires_native_admission', False),
    ('basis', 'unsupported'), ('inventory_now', -1),
])
def test_bill_craft_start_question_refuses_stale_or_false_bill(field, value):
    state, data, plans = frontier()
    support = context(state, data, plans)
    support['candidate_evidence'][plans[1].id]['shared_bill_craft'][field] = value
    assert 'No required start fact is missing from those witnesses' not in (
        craft_observation_question(state, data, plans, support))


@pytest.mark.parametrize('field,value', [
    ('unknowns', ['actor:unknown']), ('reasons', ['site:unclear']),
    ('urgency', 2), ('research_deadline_tick', 99), ('work_scope', 'immediate'),
    ('craft_start_evidence', 'malformed'),
])
def test_bill_craft_start_question_refuses_contrary_or_malformed_frontier(field, value):
    state, data, plans = frontier()
    support = context(state, data, plans)
    support['candidate_evidence'][plans[1].id][field] = value
    assert 'No required start fact is missing from those witnesses' not in (
        craft_observation_question(state, data, plans, support))


def test_complete_bill_job_defers_only_independent_raw_gather_until_admission():
    state, data, plans = frontier()
    support = context(state, data, plans)
    assert support['candidate_evidence'][plans[0].id]['raw_prerequisite']
    assert support['candidate_evidence'][plans[1].id]['shared_bill_craft'][
        'expected_products_after_native_verification'] == 4
    retained, reason = defer_gather_until_bill_craft(plans, support, state, memory(state))
    assert retained == [plans[1]]
    assert reason == 'complete_current_bill_craft_before_independent_raw_gather'
    # The same native-shaped gather remains an independent option after a paid
    # job is admitted; the actual next observation still controls its offer.
    assert permits_locked_outputs(plans[0].steps[0], {'transport-belt'})
    state.factory.update(research='study', research_progress=0.0)
    state.factory['entities']['utility:lab'] = machine('lab', input={})
    data.technologies['study'] = {
        'name': 'study', 'count': 1, 'energy_ticks': 60, 'trigger': False,
        'ingredients': [{'name': 'automation-science-pack', 'amount': 1}],
        'effects': [], 'enabled': True,
    }
    data.recipes['automation-science-pack'] = recipe(
        'automation-science-pack', {'electronic-circuit': 10})
    job = CraftJob(parameters=plans[1].steps[0].parameters, plan_id=plans[1].id,
                   goal='rocket_launch', session_id=state.session_id,
                   actor={'player_index': 1, 'unit_number': 1,
                          'surface_index': 1, 'force_index': 1},
                   inputs={'iron-gear-wheel': 2, 'iron-plate': 2},
                   outputs={'transport-belt': 4}, baseline={'transport-belt': 0},
                   started_tick=state.tick, deadline_tick=state.tick + 1800)
    following = independent_candidates('rocket_launch', state, data, job,
                                       MiningOutpostPlanner)
    assert any(p.steps[0].action == 'factory_gather'
               and p.steps[0].item == 'iron-ore' and job.permits(p.steps[0])
               for p in following)


@pytest.mark.parametrize('plan_edit', [
    lambda gather, belt: [gather, replace(belt, materials={**belt.materials,
        'shared_bill_craft': {**belt.materials['shared_bill_craft'],
                              'observed_tick': -1}})],
    lambda gather, belt: [gather, replace(belt, steps=(replace(belt.steps[0],
        action='factory_craft', effect='inventory',
        parameters={'recipe': 'transport-belt', 'batches': 2}),))],
    lambda gather, belt: [replace(gather, steps=(replace(gather.steps[0],
        costs={'transport-belt': 1}),)), belt],
    lambda gather, belt: [gather, belt, replace(gather, id='third')],
])
def test_deferment_refuses_stale_receipt_conflict_or_extra_choice(plan_edit):
    state, data, plans = frontier()
    plans = plan_edit(*plans)
    retained, reason = defer_gather_until_bill_craft(
        plans, context(state, data, plans), state, memory(state))
    assert retained == plans and reason is None


def test_deferment_refuses_gather_of_craft_byproduct_locked_by_native_job():
    state, data, plans = frontier()
    data.recipes['transport-belt']['products'].append(
        {'type': 'item', 'name': 'iron-ore', 'amount': 1, 'probability': 1})
    support = context(state, data, plans)
    # The altered byproduct makes this material expansion unsupported. Native
    # craft output can remain known, but the shared bill must fail closed.
    assert support['candidate_evidence'][plans[1].id]['shared_bill_craft'] is None
    assert support['candidate_evidence'][plans[1].id]['craft_start_evidence'][
        'expected_products_after_native_verification'] == {
            'transport-belt': 4, 'iron-ore': 2}
    retained, reason = defer_gather_until_bill_craft(plans, support, state, memory(state))
    assert retained == plans and reason is None
    assert not permits_locked_outputs(plans[0].steps[0], {'transport-belt', 'iron-ore'})


@pytest.mark.parametrize('field', [
    'forecast_is_not_paid_stock_or_completed_output',
    'background_overlap_requires_native_admission',
])
def test_deferment_requires_forecast_and_native_admission_boundaries(field):
    state, data, plans = frontier()
    support = context(state, data, plans)
    support['candidate_evidence'][plans[1].id]['shared_bill_craft'][field] = False
    retained, reason = defer_gather_until_bill_craft(plans, support, state, memory(state))
    assert retained == plans and reason is None


@pytest.mark.parametrize('field,value', [
    ('pending', {'unresolved': True}), ('attempt', {'unresolved': True}),
    ('background_job', {'unresolved': True}), ('reservations', {'other': {'iron-ore': 1}}),
    ('solid_commitments', {'route': {}}), ('coal_commitments', {'route': {}}),
    ('output_commitments', {'source': {}}), ('input_commitments', {'source': {}}),
    ('outpost_commitments', {'iron-ore': {}}),
    ('successor_projects', {'growth:iron-plate': {'status': 'active'}}),
    ('connector_ownership', {'routes': {'pipe': {}}}),
])
def test_deferment_refuses_other_owned_work(field, value):
    state, data, plans = frontier()
    held = memory(state)
    setattr(held, field, value)
    retained, reason = defer_gather_until_bill_craft(plans, context(state, data, plans), state, held)
    assert retained == plans and reason is None


@pytest.mark.parametrize('plan_id,key,value', [
    (0, 'urgency', 2), (1, 'urgency', 2),
    (0, 'research_deadline_tick', 11), (1, 'research_deadline_tick', 11),
    (0, 'unknowns', ['travel:factory_gather']),
])
def test_deferment_refuses_urgent_deadline_or_missing_start_evidence(plan_id, key, value):
    state, data, plans = frontier()
    support = context(state, data, plans)
    support['candidate_evidence'][plans[plan_id].id][key] = value
    retained, reason = defer_gather_until_bill_craft(plans, support, state, memory(state))
    assert retained == plans and reason is None


@pytest.mark.parametrize('field,value', [
    ('player_connected', False), ('player_bound', False),
    ('crafting_queue', 1), ('craft_jobs_protocol', 0),
])
def test_deferment_refuses_unready_actor_or_craft_queue(field, value):
    state, data, plans = frontier()
    state.factory[field] = value
    retained, reason = defer_gather_until_bill_craft(
        plans, context(state, data, plans), state, memory(state))
    assert retained == plans and reason is None


def test_jev_low_confidence_still_observes_without_dispatch_after_deferment():
    state, data, plans = frontier()
    for key in ('output_buffers', 'input_routes', 'mining_outposts'):
        state.factory.pop(key)
    backend = FactorySimulation()
    backend.observe = lambda: deepcopy(state)
    backend.enable_factory = lambda: data

    class LowConfidence(CountingModel):
        def evaluate(self, context, questions):
            self.criteria = set(questions['candidate']['criteria'])
            answer = super().evaluate(context, questions)
            answer['candidate']['confidence'] = 0.34
            return answer

    model = LowConfidence()
    loop = HierarchicalLoop(backend, model, policy='jev', target='rocket_launch',
                            factory_scheduling='ready-work', tick_seconds=0)
    loop._refresh_goals = lambda observation: setattr(loop.memory, 'active_goal', 'rocket_launch')
    loop._work_candidates = lambda observation: (plans, '')
    record = loop.step()
    assert model.calls == 1
    assert backend.actions == []
    assert record['decision']['diagnostics']['outcome'] == 'low_choice_confidence'
    assert record['planning_diagnostics']['deferred_plan_ids'] == [plans[0].id]
    assert model.criteria == {plans[1].id, 'observe'}


@pytest.mark.parametrize('policy', ['hybrid', 'deterministic'])
def test_non_jev_policy_keeps_original_frontier(policy):
    state, data, plans = frontier()
    for key in ('output_buffers', 'input_routes', 'mining_outposts'):
        state.factory.pop(key)
    backend = FactorySimulation()
    backend.observe = lambda: deepcopy(state)
    backend.enable_factory = lambda: data
    loop = HierarchicalLoop(backend, CountingModel(), policy=policy,
                            target='rocket_launch', factory_scheduling='ready-work',
                            tick_seconds=0)
    loop._refresh_goals = lambda observation: setattr(loop.memory, 'active_goal', 'rocket_launch')
    loop._work_candidates = lambda observation: (plans, '')
    record = loop.step()
    assert record['planning_diagnostics']['deferred_plan_ids'] == []
    assert set(record['planning_diagnostics']['ranked_plan_ids']) == {p.id for p in plans}


def test_failed_craft_budget_leaves_gather_available_without_deferment():
    state, data, plans = frontier()
    for key in ('output_buffers', 'input_routes', 'mining_outposts'):
        state.factory.pop(key)
    backend = FactorySimulation()
    backend.observe = lambda: deepcopy(state)
    backend.enable_factory = lambda: data

    class LowConfidence(CountingModel):
        def evaluate(self, context, questions):
            answer = super().evaluate(context, questions)
            answer['candidate']['confidence'] = 0.34
            return answer

    loop = HierarchicalLoop(backend, LowConfidence(), policy='jev',
                            target='rocket_launch', factory_scheduling='ready-work',
                            tick_seconds=0)
    loop._refresh_goals = lambda observation: setattr(loop.memory, 'active_goal', 'rocket_launch')
    loop._work_candidates = lambda observation: (plans, '')
    loop._plan_failure_count = lambda plan: 2 if plan.id == plans[1].id else 0
    record = loop.step()
    assert record['planning_diagnostics']['deferred_plan_ids'] == []
    assert record['planning_diagnostics']['ranked_plan_ids'] == [plans[0].id]
    assert backend.actions == []
