"""Accepted native hold, independent demand checks and hypothetical continuation."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from jev_factorio.judgments import question_batch, _qualified_buffer_build
from jev_factorio.planning.buffer_demand import qualified_commissioning, scope_commissioning
from jev_factorio.planning.bootstrap_chain import catalog_projection
from jev_factorio.planning.decision_support import scheduling_context
from test_buffer_component_demand import captured, context


def frontier():
    snapshot, catalog, loop = captured('native-v34-buffer-commissioning.json')
    plans, state = context(snapshot, catalog, loop)
    plan = next(p for p in plans if p.steps[0].action == 'factory_buffer_build')
    return snapshot, catalog, loop, plans, state, plan


def test_captured_placement_retains_current_recipe_purpose_and_native_start():
    snapshot, _, loop, plans, state, plan = frontier()
    row = state['candidate_evidence'][plan.id]
    proof = row['buffer_commissioning_parent_purpose']
    assert proof['parent_item_path'] == ['logistic-science-pack', 'transport-belt', 'iron-plate']
    assert proof['source_item_shortfall'] == 10
    assert proof['source_item_carried'] == 0
    assert proof['paid_prefix_is_not_measured_payback'] is True
    assert proof['placement_transfer_flow_and_parent_output_require_native_verification'] is True
    assert qualified_commissioning(plan, state['facts'], row)
    assert _qualified_buffer_build(state['facts'], plan.steps[0], row['buffer_build_start_evidence'], snapshot.tick)
    packet, questions, offered = question_batch(state, plans, max_bytes=48000)
    assert plan in offered
    for name in ('benefit', 'useful_progress'):
        assert 'buffer_commissioning_parent_purpose' in questions[plan.id+'/'+name]['instructions']
    assert len(json.dumps({'state': packet, 'questions': questions}).encode()) <= 48000
    assert loop.backend.calls == []


@pytest.mark.parametrize('change', [
    'tick', 'session', 'unit', 'layout', 'paid', 'receipt', 'entity', 'source_item',
    'recipe', 'parent_satisfied', 'intermediate_satisfied', 'source_satisfied',
    'malformed_inventory', 'scope', 'action', 'parameters', 'costs', 'proof_only', 'catalog',
])
def test_commissioning_cannot_borrow_stale_or_unrelated_parent_demand(change):
    _, _, _, _, state, plan = frontier()
    facts = deepcopy(state['facts']); row = deepcopy(state['candidate_evidence'][plan.id])
    materials = deepcopy(plan.materials); marker = materials['buffer_commissioning_demand']
    if change == 'tick': marker['observed_tick'] -= 1
    elif change == 'session': marker['session_id'] = 'other'
    elif change == 'unit': marker['source_unit'] += 1
    elif change == 'layout': marker['layout'] = 'other'
    elif change == 'paid': facts['factory']['output_buffers']['sources']['recipe:iron-plate']['parts']['chest']['paid'] = 0
    elif change == 'receipt': marker['paid_parts']['chest']['receipt'] = 'other'
    elif change == 'entity': facts['factory']['entities']['output-chest:2547']['unit_number'] += 1
    elif change == 'source_item': marker['parent_item_path'][-1] = 'copper-plate'
    elif change == 'recipe': facts['factory']['recipe_dependency_catalog']['recipes']['transport-belt']['ingredients'] = []
    elif change == 'parent_satisfied': facts['inventory']['logistic-science-pack'] = 20
    elif change == 'intermediate_satisfied': facts['inventory']['transport-belt'] = 20
    elif change == 'source_satisfied': facts['inventory']['iron-plate'] = 10
    elif change == 'malformed_inventory': facts['inventory']['iron-plate'] = False
    elif change == 'scope': materials['work_intent']['scope'] = 'lookahead'
    elif change == 'action': marker['action'] = 'factory_extract'
    elif change == 'parameters': marker['parameters']['receipt'] = 'other'
    elif change == 'costs': marker['costs']['burner-inserter'] = 2
    elif change == 'proof_only': row['buffer_commissioning_parent_purpose']['source_item_shortfall'] += 1
    elif change == 'catalog': facts['factory']['recipe_dependency_catalog']['version'] = 'other'
    assert not qualified_commissioning(replace(plan, materials=materials), facts, row)


@pytest.mark.parametrize('change', ['no_item', 'duplicate_receipt', 'disconnected', 'queue'])
def test_parent_demand_alone_never_supplies_native_start_permission(change):
    _, _, _, plans, state, plan = frontier()
    before = deepcopy(state)
    if change == 'no_item': state['facts']['inventory'].pop('burner-inserter')
    elif change == 'duplicate_receipt': state['candidate_evidence'][plan.id]['buffer_build_start_evidence']['native_receipt_query']['present'] = True
    elif change == 'disconnected': state['facts']['factory']['player_connected'] = False
    else: state['facts']['factory']['crafting_queue'] = 1
    # The unchanged parent chain cannot replace the independently required start proof.
    _, questions, _ = question_batch(state, plans, max_bytes=48000)
    assert 'buffer_commissioning_parent_purpose' not in questions[plan.id+'/useful_progress']['instructions']
    assert before['candidate_evidence'][plan.id]['buffer_commissioning_parent_purpose'] == state['candidate_evidence'][plan.id]['buffer_commissioning_parent_purpose']


def test_hypothetical_paid_arm_fuel_carries_same_parent_purpose():
    from test_buffer_build_start_evidence import fuel_fixture
    catalog, snapshot, utility_plan = fuel_fixture()
    # A separate recipe-goal continuation, not the fixture's utility-power agenda.
    local = {'item': 'automation-science-pack', 'inventory_target': 20, 'ultimate_goal': 'rocket_launch'}
    materials = {'local_objective': local,
                 'work_intent': {'scope': 'immediate', 'observed_tick': snapshot.tick},
                 'fuel_service': deepcopy(utility_plan.materials['fuel_service'])}
    plan = replace(utility_plan, materials=materials)
    owner = snapshot.factory['output_buffers']['sources']['recipe:iron-plate']
    plan = scope_commissioning(snapshot, catalog, plan, owner,
                               ('item:automation-science-pack',))
    support = scheduling_context(snapshot, catalog, [plan], 'rocket_launch')
    facts = snapshot.for_jev()
    facts['factory']['recipe_dependency_catalog'] = catalog_projection(snapshot, catalog, [plan])
    facts['factory']['native_transfer_receipt_count'] = len(facts['factory'].pop('receipts'))
    state = {'facts': facts, **support}
    row = state['candidate_evidence'][plan.id]
    assert qualified_commissioning(plan, facts, row)
    _, questions, _ = question_batch(state, [plan], max_bytes=48000)
    assert 'buffer_commissioning_parent_purpose' in questions[plan.id+'/useful_progress']['instructions']


def test_unmarked_historical_request_does_not_gain_new_instruction():
    _, _, _, plans, state, plan = frontier()
    materials = deepcopy(plan.materials); materials.pop('buffer_commissioning_demand')
    legacy = replace(plan, materials=materials)
    state['candidate_evidence'][plan.id].pop('buffer_commissioning_parent_purpose')
    plans = [legacy if p.id == plan.id else p for p in plans]
    _, questions, _ = question_batch(state, plans, max_bytes=48000)
    assert 'buffer_commissioning_parent_purpose' not in questions[plan.id+'/useful_progress']['instructions']
