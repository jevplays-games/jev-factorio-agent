"""No provider calls: conditional overlap evidence must not alter authorization."""
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.jev_client import MockJevClient
from jev_factorio.planning.decision_support import add_craft_overlap_evidence
from jev_factorio.planning.decision_support import candidate_evidence, scheduling_context
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from test_factory import catalog, recipe, snapshot


def case():
    data = catalog()
    data.recipes.update({
        'copper-plate': recipe('copper-plate', {'copper-ore': 1}, 'smelting'),
        'iron-gear-wheel': recipe('iron-gear-wheel', {'iron-plate': 2}),
        'lab': recipe('lab', {'iron-gear-wheel': 10, 'copper-plate': 15}),
    })
    state = snapshot(inventory={'iron-plate': 16, 'iron-gear-wheel': 2, 'copper-plate': 10},
                     nearby_resources={'copper-ore': 95, 'iron-ore': 5, 'stone': 10})
    state.factory['craft_jobs_protocol'] = 1
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner.plan = lambda: planner._need('lab', 1)
    plans = planner.candidates()
    assert len(plans) == 2
    plans[0] = replace(plans[0], steps=(replace(plans[0].steps[0],
        action='factory_craft_job', effect='craft_job_complete',
        parameters={**plans[0].steps[0].parameters, 'receipt': 'test-overlap'}),))
    return state, data, plans


def test_overlap_explains_order_without_removing_alternatives_or_claiming_payment():
    state, data, plans = case()
    before = deepcopy((state, plans))
    rows = candidate_evidence(state, data, plans)
    prior = deepcopy(rows)
    add_craft_overlap_evidence(state, plans, rows)
    proof = rows[plans[0].id].pop('independent_gather_overlap')
    assert rows == prior
    assert (state, plans) == before
    assert proof['inputs_available_now_not_yet_paid'] == {'iron-plate': 16}
    assert proof['expected_outputs_locked_until_native_receipt'] == {'iron-gear-wheel': 8}
    assert proof['gather_plan_id'] == plans[1].id
    assert proof['requires_native_admission_then_fresh_gather_observation'] is True
    context = {'facts': state.for_jev(), **scheduling_context(state, data, plans, 'rocket_launch')}
    _, questions, offered = question_batch(context, plans)
    assert offered == plans
    assert set(questions['candidate']['criteria']) == {p.id for p in plans} | {'observe'}


@pytest.mark.parametrize('change', [
    'stale_craft', 'stale_raw', 'stale_gather', 'stale_dependency', 'unpaid',
    'queue', 'protocol', 'recipe', 'disconnected', 'receipt', 'extra',
    'output_conflict', 'gather_cost', 'different_target', 'unknown', 'urgency',
    'lookahead', 'unobserved_resource', 'unobserved_target', 'wrong_session',
])
def test_overlap_is_absent_with_missing_or_conflicting_witness(change):
    state, data, plans = case()
    rows = candidate_evidence(state, data, plans)
    cr, gr = rows[plans[0].id], rows[plans[1].id]
    if change.startswith('stale_'):
        row, key = {'craft': (cr, 'craft_start_evidence'),
                    'raw': (gr, 'raw_prerequisite'),
                    'gather': (gr, 'gather_start_evidence'),
                    'dependency': (cr, 'craft_dependency')}[change[6:]]
        row[key]['observed_tick'] -= 1
    elif change == 'unpaid': state.inventory['iron-plate'] = 15
    elif change in {'queue', 'protocol', 'recipe', 'disconnected'}:
        key = {'queue': 'crafting_queue_empty', 'protocol': 'craft_job_protocol_ready',
               'recipe': 'recipe_unlocked_and_handcraftable',
               'disconnected': 'player_connected_and_bound'}[change]
        cr['craft_start_evidence'][key] = False
    elif change == 'receipt':
        plans[0].steps[0].parameters['receipt'] = ''
    elif change == 'extra': plans.append(replace(plans[1], id='extra'))
    elif change == 'output_conflict':
        plans[1].steps[0].parameters['item'] = 'iron-gear-wheel'
    elif change == 'gather_cost':
        plans[1] = replace(plans[1], steps=(replace(plans[1].steps[0], costs={'iron-plate': 1}),))
    elif change == 'different_target': gr['local_target']['item'] = 'other'
    elif change == 'unknown': gr['unknowns'] = ['travel:unknown']
    elif change == 'urgency': gr['urgency'] = 1
    elif change == 'lookahead': cr['work_scope'] = 'lookahead'
    elif change == 'unobserved_resource': gr['gather_start_evidence']['resource_in_current_observation'] = False
    elif change == 'unobserved_target': gr['gather_start_evidence']['fair_target_identity_observed'] = False
    elif change == 'wrong_session': gr['gather_start_evidence']['session_id'] = 'other'
    add_craft_overlap_evidence(state, plans, rows)
    assert all('independent_gather_overlap' not in row for row in rows.values())


def test_new_evidence_cannot_turn_rejected_confidence_into_authority():
    state, data, plans = case()
    class LowConfidence(MockJevClient):
        def evaluate(self, state, questions):
            answers = super().evaluate(state, questions)
            answers['candidate']['confidence'] = .44
            return answers
    context = {'facts': state.for_jev(), **scheduling_context(state, data, plans, 'rocket_launch')}
    result = select_plan(LowConfidence(), context, plans)
    assert result.plan_id is None


def test_comparison_is_removed_when_peer_is_not_offered_without_mutating_state():
    state, data, plans = case()
    context = {'facts': state.for_jev(), **scheduling_context(state, data, plans, 'rocket_launch')}
    before = deepcopy(context)
    offered_context, _, offered = question_batch(context, [plans[0]])
    assert offered == [plans[0]]
    assert 'independent_gather_overlap' not in offered_context['candidate_evidence'][plans[0].id]
    assert context == before
