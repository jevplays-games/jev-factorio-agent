"""Replay the accepted V36 hold without providers or native mutations."""
from copy import deepcopy

import pytest

from test_buffer_component_demand import captured, context
from jev_factorio.judgments import _qualified_output_pickup_chain, question_batch
from jev_factorio.planning.decision_support import distinct_candidates


def replay():
    state, catalog, loop = captured('native-v36-capital-hold.json')
    plans, source = context(state, catalog, loop)
    plan = next(p for p in plans if p.id == 'factory:factory_extract:output-chest:2547')
    return state, catalog, loop, plans, source, plan


def test_captured_active_kit_does_not_hide_current_work_after_conflicting_proposal():
    state, catalog, loop = captured('native-v36-capital-hold.json')
    before = deepcopy(loop.memory.capital_investment), deepcopy(loop.memory.failures)
    plans, source = context(state, catalog, loop)
    retained = distinct_candidates(plans)
    plan = next(p for p in retained if p.id == 'factory:factory_extract:output-chest:2547')
    assert plan.materials['work_intent']['scope'] == 'immediate'
    assert plan.materials['output_pickup']['planner_item_path'] == [
        'logistic-science-pack', 'transport-belt', 'iron-plate']
    proof = source['candidate_evidence'][plan.id]['output_pickup_start_evidence']
    assert proof['current_input_demand'] == {
        'required_carried_quantity': 10, 'carried_quantity': 9, 'remaining_deficit': 1}
    assert proof['paid_buffer_identity']['flow']['positive_samples'] == 3
    assert _qualified_output_pickup_chain(plan, source['facts'], source['candidate_evidence'][plan.id])
    wire, questions, offered = question_batch(source, retained, max_bytes=48000)
    assert plan in offered
    assert 'recipe_dependency_chain' in questions[plan.id + '/useful_progress']['instructions']
    assert (loop.memory.capital_investment, loop.memory.failures) == before
    assert loop.backend.calls == []


@pytest.mark.parametrize('mutation', ['tick', 'session', 'source', 'chest', 'arm',
    'paid', 'receipt', 'samples', 'conservation', 'topology', 'stock', 'satisfied',
    'path', 'quantity', 'forged_proof', 'catalog'])
def test_buffer_pickup_purpose_rejects_changed_independent_facts(mutation):
    _, _, _, _, source, plan = replay()
    facts = deepcopy(source['facts'])
    row = deepcopy(source['candidate_evidence'][plan.id])
    buffer = facts['factory']['output_buffers']['sources']['recipe:iron-plate']
    if mutation == 'tick': facts['factory']['output_buffers']['tick'] -= 1
    elif mutation == 'session': facts['factory']['output_buffers']['session_id'] = 'another'
    elif mutation == 'source': buffer['source_unit'] += 1
    elif mutation == 'chest': buffer['parts']['chest']['unit_number'] += 1
    elif mutation == 'arm': buffer['parts']['inserter']['unit_number'] += 1
    elif mutation == 'paid': buffer['parts']['chest']['paid'] = 0
    elif mutation == 'receipt': buffer['parts']['chest']['receipt'] = ''
    elif mutation == 'samples': buffer['flow']['positive_samples'] = 2
    elif mutation == 'conservation': buffer['flow']['conservation'] = False
    elif mutation == 'topology': buffer['topology'] = False
    elif mutation == 'stock': facts['factory']['entities']['output-chest:2547']['output']['iron-plate'] = 0
    elif mutation == 'satisfied': facts['inventory']['iron-plate'] = 10
    elif mutation == 'path': plan.materials['output_pickup']['planner_item_path'] = ['iron-plate']
    elif mutation == 'quantity': plan.steps[0].parameters['quantity'] = 2
    elif mutation == 'forged_proof': row['output_pickup_start_evidence']['paid_buffer_identity']['layout'] = 'forged'
    elif mutation == 'catalog': facts['factory']['recipe_dependency_catalog']['recipes'].pop('iron-plate')
    assert not _qualified_output_pickup_chain(plan, facts, row)
