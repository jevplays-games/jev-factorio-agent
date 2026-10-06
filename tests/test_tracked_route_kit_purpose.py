"""Replay a settled native V37r2 hold through the composed background loop."""
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from jev_factorio.judgments import question_batch
from jev_factorio.planning.decision_support import _input_route_kit_parent_purpose
from jev_factorio.planning.input_routes import InputRoutePlanner
from test_buffer_component_demand import captured, context


def replay():
    state, catalog, loop = captured('native-v37r2-tracked-route-kit.json')
    plans, source = context(state, catalog, loop)
    return state, catalog, loop, plans, source


def test_composed_background_kit_keeps_exact_current_parent_without_dispatch():
    state, catalog, loop, plans, source = replay()
    before = deepcopy(asdict(state)), deepcopy(asdict(loop.memory))
    plan = plans[0]
    assert plan.id == 'factory:factory_craft:iron-gear-wheel'
    assert plan.steps[0].action == 'factory_craft_job'
    assert plan.steps[0].costs == {'iron-plate': 2}
    receipt = plan.steps[0].parameters['receipt']
    row = source['candidate_evidence'][plan.id]
    assert row['local_target']['item'] == 'burner-inserter'
    assert row['craft_dependency']['planner_item_path'] == ['burner-inserter', 'iron-gear-wheel']
    parent = row['input_route_kit_parent_purpose']
    assert parent['parent_local_objective']['item'] == 'logistic-science-pack'
    assert parent['parent_local_objective']['inventory_target'] == 20
    assert parent['route_flow_and_parent_output_are_not_established'] is True
    wire, questions, offered = question_batch(source, plans, max_bytes=48000)
    assert plan in offered and plan.id + '/useful_progress' in questions
    assert wire['candidate_evidence'][plan.id]['input_route_kit_parent_purpose'] == parent
    assert plan.steps[0].parameters['receipt'] == receipt
    assert before == (asdict(state), asdict(loop.memory))
    assert loop.backend.calls == []


def test_recompile_requires_ordinary_mode_and_accepts_untracked_same_work():
    state, catalog, _, plans, _ = replay()
    plan = plans[0]
    normal = InputRoutePlanner(catalog, state, plan.goal).candidates()
    assert all(p.id != plan.id for p in normal)
    planner = InputRoutePlanner(catalog, state, plan.goal)
    planner._economic_acquiring = True
    ordinary = next(p for p in planner.candidates() if p.id == plan.id)
    assert ordinary.steps[0].action == 'factory_craft'
    assert _input_route_kit_parent_purpose(state, catalog, ordinary)
    assert _input_route_kit_parent_purpose(state, catalog, plan)


@pytest.mark.parametrize('change', [
    'receipt_missing', 'receipt_short', 'receipt_upper', 'receipt_type',
    'cost', 'threshold', 'timeout', 'recipe', 'batches', 'effect', 'item',
    'extra_parameter', 'verification', 'extra_step', 'id', 'tick', 'parent',
    'path', 'bill', 'local_target', 'dependency', 'coherent', 'atomic',
])
def test_only_exact_conversion_and_recompiled_material_purpose_qualify(change):
    state, catalog, _, plans, _ = replay()
    plan = deepcopy(plans[0])
    step = plan.steps[0]
    parameters = step.parameters
    if change == 'receipt_missing': parameters.pop('receipt')
    elif change == 'receipt_short': parameters['receipt'] = 'a' * 31
    elif change == 'receipt_upper': parameters['receipt'] = 'A' * 32
    elif change == 'receipt_type': parameters['receipt'] = 32
    elif change == 'cost': step = replace(step, costs={'iron-plate': 1})
    elif change == 'threshold': step = replace(step, threshold=2)
    elif change == 'timeout': step = replace(step, timeout_ticks=1801)
    elif change == 'recipe': parameters['recipe'] = 'burner-inserter'
    elif change == 'batches': parameters['batches'] = 2
    elif change == 'effect': step = replace(step, effect='inventory')
    elif change == 'item': step = replace(step, item='burner-inserter')
    elif change == 'extra_parameter': parameters['extra'] = True
    elif change == 'verification': step = replace(step, verification={'role': 'other'})
    elif change == 'id': plan = replace(plan, id='other')
    elif change == 'tick': plan.materials['input_route_kit_prerequisite']['observed_tick'] -= 1
    elif change == 'parent': plan.materials['input_route_kit_prerequisite']['parent_local_objective']['inventory_target'] += 1
    elif change == 'path': plan.materials['input_route_kit_prerequisite']['parent_planner_item_path'] = ['iron-plate']
    elif change == 'bill': plan.materials['input_route_kit_prerequisite']['remaining_route_bill']['transport-belt'] += 1
    elif change == 'local_target': plan.materials['local_objective']['inventory_target'] += 1
    elif change == 'dependency': plan.materials['craft_dependency']['planner_item_path'] = ['iron-gear-wheel']
    elif change == 'coherent': state._coherent_observation_verified = None
    elif change == 'atomic': state._atomic_inventory_verified = None
    plan = replace(plan, steps=(step, step) if change == 'extra_step' else (step,))
    assert _input_route_kit_parent_purpose(state, catalog, plan) is None
