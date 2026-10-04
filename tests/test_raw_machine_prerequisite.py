"""Synthetic evidence regressions for the October 4 missing-furnace stall."""
from copy import deepcopy

import pytest

from jev_factorio.judgments import question_batch
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.planning.decision_support import raw_machine_prerequisite
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from test_factory import catalog, machine, recipe, snapshot


def case():
    state = snapshot(inventory={'iron-ore': 36, 'copper-plate': 10})
    data = catalog()
    data.recipes['lab'] = recipe('lab', {'iron-gear-wheel': 12})
    data.recipes['iron-gear-wheel'] = recipe('iron-gear-wheel', {'iron-plate': 2})
    state.factory['entities']['recipe:copper-plate'] = machine(output={}, fuel={'coal': 4})
    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._need('lab', 1)
    assert plan.steps[0].parameters == {'resource': 'stone', 'quantity': 5}
    return state, data, plan


def test_existing_copper_furnace_does_not_erase_missing_iron_producer_evidence():
    state, data, plan = case()
    original = deepcopy((state, plan))
    context = {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')}
    witness = context['candidate_evidence'][plan.id]['raw_machine_prerequisite']
    assert [edge['kind'] for edge in witness['edges']] == [
        'recipe_input', 'recipe_input', 'missing_production_machine', 'recipe_input']
    assert witness['edges'][2]['role'] == 'recipe:iron-plate'
    assert witness['edges'][2]['machine'] == 'stone-furnace'
    assert witness['edges'][2]['recipe']['ingredients'][0]['name'] == 'iron-ore'
    assert witness['edges'][3]['recipe']['ingredients'][0]['name'] == 'stone'
    _, questions, offered = question_batch(context, [plan])
    assert offered == [plan]
    for suffix in ('/useful_progress', '/benefit'):
        text = questions[plan.id + suffix]['instructions']
        assert '`raw_machine_prerequisite` separates recipe-input edges' in text
        assert 'not a consumed ingredient' in text
        assert 'Contrary current facts can make usefulness unsupported' in text
    assert (state, plan) == original


@pytest.mark.parametrize('change', ['stale', 'path', 'role_present', 'carried_machine',
                                  'machine_category', 'handcraftable', 'disabled',
                                  'local_target_met', 'quantity', 'raw_recipe', 'scope'])
def test_contradictory_dependencies_do_not_receive_machine_evidence(change):
    state, data, plan = case()
    if change == 'stale': plan.materials['raw_prerequisite']['observed_tick'] -= 1
    elif change == 'path': plan.materials['raw_prerequisite']['planner_item_path'][1] = 'unrelated'
    elif change == 'role_present': state.factory['entities']['recipe:iron-plate'] = machine()
    elif change == 'carried_machine': state.inventory['stone-furnace'] = 1
    elif change == 'machine_category': data.machines['stone-furnace']['categories'] = {}
    elif change == 'handcraftable': data.hand_categories['smelting'] = True
    elif change == 'disabled': data.recipes['iron-gear-wheel']['enabled'] = False
    elif change == 'local_target_met': state.inventory['lab'] = 1
    elif change == 'quantity': plan.steps[0].parameters['quantity'] = 50
    elif change == 'raw_recipe': plan.materials['raw_prerequisite']['recipe'] = 'lab'
    elif change == 'scope': plan.materials['work_intent']['scope'] = 'lookahead'
    assert raw_machine_prerequisite(state, data, plan) is None


@pytest.mark.parametrize('change', ['session', 'tick', 'path', 'role_present', 'carried_machine'])
def test_question_boundary_rejects_stale_or_contrary_machine_witness(change):
    state, data, plan = case()
    context = {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')}
    witness = context['candidate_evidence'][plan.id]['raw_machine_prerequisite']
    if change == 'session': witness['session_id'] = 'other'
    elif change == 'tick': witness['observed_tick'] -= 1
    elif change == 'path': witness['planner_item_path'][0] = 'other'
    elif change == 'role_present': context['facts']['factory']['entities']['recipe:iron-plate'] = machine()
    elif change == 'carried_machine': context['facts']['inventory']['stone-furnace'] = 1
    _, questions, _ = question_batch(context, [plan])
    assert '`raw_machine_prerequisite` separates' not in questions[plan.id+'/useful_progress']['instructions']


def test_plain_recipe_input_path_does_not_invent_missing_machine():
    state, data, _ = case()
    state.factory['entities']['recipe:iron-plate'] = machine(fuel={'coal': 5})
    state.inventory['iron-ore'] = 0
    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._need('lab', 1)
    assert plan.steps[0].item == 'iron-ore'
    assert raw_machine_prerequisite(state, data, plan) is None
