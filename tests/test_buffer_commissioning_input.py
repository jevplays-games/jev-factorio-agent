"""A paid transport cell cannot commission while its producer is empty."""
from copy import deepcopy
import json

import pytest

from jev_factorio.judgments import question_batch, _qualified_recipe_transfer_chain
from jev_factorio.output_buffers import flow_complete, permits
from test_buffer_component_demand import captured, context


def frontier():
    return captured('native-v35-empty-commissioning.json')


def test_captured_empty_producer_acquires_current_recipe_input_before_waiting():
    state, catalog, loop = frontier()
    failures = deepcopy(loop.memory.failures)
    owner = deepcopy(state.factory['output_buffers']['sources']['recipe:iron-plate'])
    plans, support = context(state, catalog, loop)
    plan = plans[0]
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters['resource'] == 'iron-ore'
    assert plan.steps[0].parameters['quantity'] == 3
    assert plan.steps[0].threshold == 10
    assert state.inventory['iron-ore'] == 7
    assert plan.materials['raw_prerequisite']['planner_item_path'] == [
        'logistic-science-pack', 'transport-belt', 'iron-plate', 'iron-ore']
    packet, questions, offered = question_batch(support, plans, max_bytes=48000)
    assert plan in offered
    assert len(json.dumps({'state': packet, 'questions': questions}).encode()) <= 48000
    assert loop.memory.failures == failures
    assert state.factory['output_buffers']['sources']['recipe:iron-plate'] == owner
    assert not flow_complete(owner['source'], owner['layout'], state)
    assert loop.backend.calls == []


def test_hypothetical_acquisition_then_paid_input_keeps_native_recipe_proof():
    state, catalog, loop = frontier()
    state.inventory['iron-ore'] = 10
    plans, support = context(state, catalog, loop)
    plan = plans[0]
    assert plan.steps[0].action == 'factory_insert'
    assert plan.steps[0].parameters['role'] == 'recipe:iron-plate'
    assert plan.steps[0].costs == {'iron-ore': 10}
    assert _qualified_recipe_transfer_chain(plan, support['facts'], support['candidate_evidence'][plan.id])
    assert permits(plan.steps[0].action, plan.steps[0].parameters, state)


@pytest.mark.parametrize('stock', ['source_output', 'held', 'input', 'in_flight'])
def test_existing_transportable_stock_waits_for_native_flow_without_extra_acquisition(stock):
    state, catalog, loop = frontier()
    machine = state.factory['entities']['recipe:iron-plate']
    owner = state.factory['output_buffers']['sources']['recipe:iron-plate']
    if stock == 'source_output': machine['output']['iron-plate'] = 10
    elif stock == 'held': owner['held'] = 1
    elif stock == 'input': machine['input']['iron-ore'] = 10
    else: machine['crafting'] = True
    plans, _ = context(state, catalog, loop)
    assert plans[0].steps[0].action == 'factory_wait'
    assert plans[0].steps[0].effect == 'buffer_flow'
    assert not plans[0].steps[0].satisfied(state)


def test_stored_uncommissioned_stock_cannot_supply_future_transport_samples():
    state, catalog, loop = frontier()
    state.factory['entities']['output-chest:2547']['output']['iron-plate'] = 10
    plans, _ = context(state, catalog, loop)
    assert plans[0].steps[0].action == 'factory_gather'
    assert not permits('factory_extract', {'role': 'output-chest:2547'}, state)
    assert not permits('factory_extract', {'role': 'recipe:iron-plate'}, state)


def test_buffered_ore_without_furnace_fuel_requests_fuel_before_flow_wait():
    state, catalog, loop = frontier()
    state.inventory['coal'] = 5
    machine = state.factory['entities']['recipe:iron-plate']
    machine['input']['iron-ore'] = 10
    machine['fuel']['coal'] = 0
    machine['energy'] = 0
    plans, _ = context(state, catalog, loop)
    assert plans[0].steps[0].action == 'factory_insert'
    assert plans[0].steps[0].parameters['role'] == 'recipe:iron-plate'
    assert plans[0].steps[0].parameters['item'] == 'coal'
    assert plans[0].steps[0].costs == {'coal': 5}


def test_completed_native_flow_releases_current_stock_without_new_commissioning_input():
    state, catalog, loop = frontier()
    owner = state.factory['output_buffers']['sources']['recipe:iron-plate']
    owner['flow'] = {'first_tick': state.tick-180, 'last_tick': state.tick,
        'positive_samples': 3, 'received': 10, 'layout': owner['layout'],
        'source_unit': owner['source_unit'], 'conservation': True}
    state.factory['entities']['output-chest:2547']['output']['iron-plate'] = 10
    plans, _ = context(state, catalog, loop)
    assert flow_complete(owner['source'], owner['layout'], state)
    assert plans[0].steps[0].action == 'factory_extract'
    assert plans[0].steps[0].parameters['role'] == 'output-chest:2547'
