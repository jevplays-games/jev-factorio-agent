"""Offline contract regressions; these do not assert live model approval."""
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.jev_client import MockJevClient
from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from test_factory import catalog, recipe, snapshot


def craft_batch():
    state = snapshot(world_kind='fle', inventory={'iron-plate': 40, 'copper-plate': 20})
    state.factory['craft_jobs_protocol'] = 1
    data = catalog()
    data.recipes['iron-gear-wheel'] = recipe('iron-gear-wheel', {'iron-plate': 2})
    data.recipes['automation-science-pack'] = recipe(
        'automation-science-pack', {'iron-gear-wheel': 1, 'copper-plate': 1})
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('automation-science-pack', 20)
    plan = planner._need('automation-science-pack', 20)
    assert plan.steps[0].item == 'iron-gear-wheel'
    step = plan.steps[0]
    plan = replace(plan, steps=(replace(
        step, action='factory_craft_job', effect='craft_job_complete',
        parameters={**step.parameters, 'receipt': 'gear-test'}),))
    return {'facts': state.for_jev(),
            **scheduling_context(state, data, [plan], 'rocket_launch')}, plan


def useful_instructions(state, plan):
    _, questions, _ = question_batch(state, [plan])
    return questions[plan.id + '/useful_progress']['instructions']


def test_intermediate_dependency_reaches_independent_usefulness():
    state, plan = craft_batch()
    text = useful_instructions(state, plan)
    assert '`craft_start_evidence` binds this bounded handcraft' in text
    assert '`craft_dependency` links its intermediate product' in text
    assert 'native receipt and fresh postcondition verify' in text
    assert 'does not establish crafted inventory, target completion' in text
    assert 'contrary current facts can make usefulness unsupported' in text
    assert 'level 1' not in text and 'level-1' not in text


@pytest.mark.parametrize('field', [
    'input_costs_match_native_recipe', 'inputs_in_inventory_now',
    'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
    'crafting_queue_empty', 'craft_job_protocol_ready',
    'native_receipt_required_for_completion',
])
def test_contrary_start_evidence_does_not_receive_qualified_guidance(field):
    state, plan = craft_batch()
    state['candidate_evidence'][plan.id]['craft_start_evidence'][field] = False
    assert '`craft_dependency` links its intermediate product' not in useful_instructions(state, plan)


@pytest.mark.parametrize('change', ['stale_start', 'stale_path', 'missing_path',
                                  'wrong_product', 'wrong_target', 'wrong_recipe',
                                  'wrong_basis', 'lookahead', 'unknown'])
def test_stale_or_mismatched_dependency_does_not_receive_guidance(change):
    state, plan = craft_batch()
    row = state['candidate_evidence'][plan.id]
    if change == 'stale_start': row['craft_start_evidence']['observed_tick'] -= 1
    elif change == 'stale_path': row['craft_dependency']['observed_tick'] -= 1
    elif change == 'missing_path': row['craft_dependency'] = None
    elif change == 'wrong_product': row['craft_dependency']['current_craft_product'] = 'pipe'
    elif change == 'wrong_target': row['craft_dependency']['planner_item_path'][0] = 'pipe'
    elif change == 'wrong_recipe': row['craft_start_evidence']['native_recipe'] = 'pipe'
    elif change == 'wrong_basis': row['craft_dependency']['basis'] = 'proposal_only'
    elif change == 'lookahead': row['work_scope'] = 'lookahead'
    else: row['unknowns'] = ['missing actor']
    assert '`craft_dependency` links its intermediate product' not in useful_instructions(state, plan)


def test_guidance_is_bound_to_each_candidate_in_a_multiple_candidate_batch():
    state, plan = craft_batch()
    other = replace(plan, id=plan.id + ':other')
    state = deepcopy(state)
    state['candidate_evidence'][other.id] = deepcopy(state['candidate_evidence'][plan.id])
    state['candidate_evidence'][other.id]['craft_dependency'] = None
    _, questions, offered = question_batch(state, [plan, other])
    assert offered == [plan, other]
    assert '`craft_dependency` links its intermediate product' in questions[plan.id + '/useful_progress']['instructions']
    assert '`craft_dependency` links its intermediate product' not in questions[other.id + '/useful_progress']['instructions']


def test_recorded_contrary_answer_still_rejects_the_qualified_craft():
    state, plan = craft_batch()

    class Contrary(MockJevClient):
        def evaluate(self, state, questions):
            answers = super().evaluate(state, questions)
            # The native blocked decision's independent usefulness distribution.
            answers[plan.id + '/useful_progress'] = {
                'type': 'choice', 'choice': 'unsupported', 'confidence': .15,
                'probabilities': {'useful': .42, 'unsupported': .58},
            }
            return answers

    decision = select_plan(Contrary(), state, [plan])
    assert decision.plan_id is None
    assert 'no_demonstrated_progress' in decision.diagnostics['candidate_rejections'][plan.id]
