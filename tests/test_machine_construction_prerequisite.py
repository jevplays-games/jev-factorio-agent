"""Keep a producer's construction purpose through gather, craft and placement."""
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.judgments import question_batch
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from test_raw_machine_prerequisite import case
from test_factory import machine


def craft_case():
    state, data, _ = case()
    state.inventory['stone'] = 5
    state.factory['craft_jobs_protocol'] = 1
    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._need('lab', 1)
    step = plan.steps[0]
    return state, data, replace(plan, steps=(replace(step,
        action='factory_craft_job', effect='craft_job_complete',
        parameters={**step.parameters, 'receipt': 'native-craft-attempt'}),))


def placement_case():
    state, data, _ = case()
    state.inventory['stone-furnace'] = 1
    role, anchor = 'recipe:iron-plate', 'cell-site:iron-ore:41:-111:0:1'
    state.factory['production_sites'] = {
        'protocol': 1, 'session_id': state.session_id, 'tick': state.tick,
        'sources': {role: {'state': 'proposed', 'reason': 'joint_layout_available',
            'anchor': anchor, 'position': {'x': 41, 'y': -111}, 'belt_count': 5,
            'bill': {'stone-furnace': 1, 'burner-mining-drill': 1, 'burner-inserter': 2,
                     'wooden-chest': 1, 'transport-belt': 5}}}}
    planner = InputRoutePlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._machine(role, 'stone-furnace',
                            ('item:lab', 'item:iron-gear-wheel', 'item:iron-plate'))
    return state, data, plan


@pytest.mark.parametrize('fixture', [craft_case, placement_case])
def test_paid_construction_keeps_native_machine_edge_and_independent_judgments(fixture):
    state, data, plan = fixture()
    context = {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')}
    proof = context['candidate_evidence'][plan.id]['machine_construction_prerequisite']
    assert proof['machine_role'] == 'recipe:iron-plate'
    assert proof['machine_item'] == 'stone-furnace'
    assert proof['planner_item_path'] == ['lab', 'iron-gear-wheel', 'iron-plate', 'stone-furnace']
    assert proof['edges'][-1]['kind'] == 'missing_production_machine'
    assert proof['step'] == plan.to_dict()['steps'][0]
    _, questions, offered = question_batch(context, [plan])
    assert offered == [plan]
    for key in ('candidate', plan.id + '/useful_progress', plan.id + '/benefit'):
        assert '`machine_construction_prerequisite`' in questions[key]['instructions']
    assert 'native verification' in questions[plan.id+'/useful_progress']['instructions']
    assert 'level 1' not in questions[plan.id+'/useful_progress']['instructions']


@pytest.mark.parametrize('fixture', [craft_case, placement_case])
@pytest.mark.parametrize('change', ['path', 'parent_recipe', 'stale', 'inventory', 'role', 'queue'])
def test_builder_rejects_broken_paid_construction_chain(fixture, change):
    state, data, plan = fixture()
    key = 'craft_dependency' if fixture == craft_case else 'placement_dependency'
    if change == 'path': plan.materials[key]['planner_item_path'][1] = 'unrelated'
    elif change == 'parent_recipe': data.recipes['iron-gear-wheel']['ingredients'] = []
    elif change == 'stale': plan.materials[key]['observed_tick'] -= 1
    elif change == 'inventory': state.inventory['stone' if fixture == craft_case else 'stone-furnace'] = 0
    elif change == 'role': state.factory['entities']['recipe:iron-plate'] = machine()
    elif change == 'queue': state.factory['crafting_queue'] = 1
    context = scheduling_context(state, data, [plan], 'rocket_launch')
    assert 'machine_construction_prerequisite' not in context['candidate_evidence'][plan.id]


@pytest.mark.parametrize('fixture', [craft_case, placement_case])
@pytest.mark.parametrize('change', ['session', 'tick', 'path', 'step', 'inventory', 'role', 'start'])
def test_question_boundary_rejects_stale_or_contrary_construction_proof(fixture, change):
    state, data, plan = fixture()
    context = {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')}
    proof = context['candidate_evidence'][plan.id]['machine_construction_prerequisite']
    if change == 'session': proof['session_id'] = 'other'
    elif change == 'tick': proof['observed_tick'] -= 1
    elif change == 'path': proof['planner_item_path'][1] = 'other'
    elif change == 'step': proof['step']['parameters']['receipt'] = 'another-attempt'
    elif change == 'inventory':
        context['facts']['inventory']['stone' if fixture == craft_case else 'stone-furnace'] = 0
    elif change == 'role': context['facts']['factory']['entities']['recipe:iron-plate'] = machine()
    elif change == 'start': proof['start_evidence']['observed_tick'] -= 1
    _, questions, _ = question_batch(context, [plan])
    assert '`machine_construction_prerequisite`' not in questions[plan.id+'/useful_progress']['instructions']


@pytest.mark.parametrize('fixture', [craft_case, placement_case])
@pytest.mark.parametrize('change', ['goal', 'infinite_input', 'nan_input', 'zero_output',
                                  'overflow'])
def test_invalid_goal_or_recipe_arithmetic_never_qualifies_construction(fixture, change):
    from jev_factorio.planning.decision_support import (
        machine_construction_prerequisite, _craft_start_evidence, _placement_start_evidence)
    state, data, plan = fixture()
    recipe = data.recipes['iron-gear-wheel']
    if change == 'goal': plan.materials['local_objective']['ultimate_goal'] = 'other'
    elif change == 'infinite_input': recipe['ingredients'][0]['amount'] = float('inf')
    elif change == 'nan_input': recipe['ingredients'][0]['amount'] = float('nan')
    elif change == 'zero_output': recipe['products'][0]['amount'] = 0
    elif change == 'overflow': recipe['products'][0]['amount'] = 1e-320
    assert machine_construction_prerequisite(state, data, plan,
        _craft_start_evidence(state, data, plan.steps[0]),
        _placement_start_evidence(state, plan)) is None
