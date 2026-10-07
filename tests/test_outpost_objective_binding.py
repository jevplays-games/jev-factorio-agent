"""Replay V39's native hold; synthetic assessments are not gameplay acceptance."""
from copy import deepcopy
import json

import pytest

from jev_factorio import two_stage_decision as protocol
from jev_factorio.judgments import question_batch
from test_buffer_component_demand import captured, context
from test_two_stage_decision import Client, SOURCE


def frontier():
    snapshot, catalog, loop = captured('native-v39-outpost-objective.json')
    plans, state = context(snapshot, catalog, loop)
    parent = next(p for p in plans if p.steps[0].action == 'factory_outpost_build')
    gather = next(p for p in plans if p.steps[0].item == 'copper-ore')
    return snapshot, loop, state, parent, gather


def test_captured_gather_retains_qualified_target_without_changing_scope_or_costs():
    _, loop, state, parent, gather = frontier()
    before = deepcopy(state)
    packet, questions, offered = question_batch(state, [parent, gather], max_bytes=48000)
    assert offered == [parent, gather]
    assert gather.materials['work_intent']['scope'] == 'lookahead'
    assert packet['candidate_evidence'][gather.id]['work_scope'] == 'immediate'
    assert packet['local_objective']['primary_target'] == gather.materials['local_objective']
    assert 'unavailable for this candidate' not in questions[gather.id+'/benefit']['instructions']
    assert gather.steps[0].parameters == {'resource': 'copper-ore', 'quantity': 50}
    assert loop.backend.calls == [] and state == before


@pytest.mark.parametrize('version', [2, 3, 4])
def test_two_stage_choice_keeps_demand_parent_as_evidence_only(version):
    snapshot, _, state, parent, gather = frontier()
    state['selection_contract']['candidate_objective_binding'] = version
    packet, questions, offered = question_batch(state, [parent, gather], max_bytes=48000)
    binding = {'session_id': snapshot.session_id, 'target': 'rocket_launch',
               'source_revision': SOURCE, 'input_sha256': '1'*64, 'state_sha256': '2'*64,
               'frontier_sha256': '3'*64, 'native_sha256': protocol.native_digest(snapshot),
               'confidence_floor': .45, 'max_request_bytes': 48000}
    record = protocol.prepare(binding=binding, context=packet, questions=questions,
                              offered=offered, input_candidate_ids=[p.id for p in offered])
    record['assessment'] = Client(reject=parent.id).evaluate(*protocol.assessment_request(record))
    record['phase'] = 'assessment_received'
    before = deepcopy(record)
    choice = protocol.choice_request(record)
    assert set(choice['state']['candidate_plans']) == {gather.id}
    assert parent.id not in choice['questions']['candidate']['criteria']
    if version == 4:
        assert choice['state']['local_objective']['primary_target'] == gather.materials['local_objective']
        assert choice['state']['local_objective_parent_plans'] == {parent.id: parent.to_dict()}
        assert 'evidence only' in choice['state']['execution_contract']
    else:
        assert choice['state']['local_objective']['candidate_targets'] == {}
        assert 'local_objective_parent_plans' not in choice['state']
    assert len(json.dumps(choice).encode()) <= 48000
    assert record == before
    protocol.validate(record, snapshot.session_id, 'rocket_launch')


@pytest.mark.parametrize('mutation', ['tick', 'session', 'quantity', 'target', 'proof',
                                     'parent_id', 'parent_target', 'parent_request'])
def test_bad_parent_evidence_cannot_supply_a_target(mutation):
    _, _, state, parent, gather = frontier()
    proof = state['candidate_evidence'][gather.id]['direct_alternative_parent_demand_start_evidence']
    if mutation == 'tick': proof['observed_tick'] -= 1
    elif mutation == 'session': proof['session_id'] = 'different'
    elif mutation == 'quantity': proof['gather_quantity'] = True
    elif mutation == 'target': proof['parent_local_target_item'] = 'iron-plate'
    elif mutation == 'proof': state['candidate_evidence'][gather.id]['direct_alternative_parent_demand_start_evidence'] = None
    elif mutation == 'parent_id': proof['parent_plan_id'] = 'different'
    elif mutation == 'parent_target': parent.materials['local_objective']['inventory_target'] += 1
    else: parent.materials['proposed_outpost_request']['requested_amount'] += 1
    packet, _, _ = question_batch(state, [parent, gather], max_bytes=48000)
    assert gather.id not in packet['local_objective']['candidate_targets']


@pytest.mark.parametrize('trim', ['subset', 'candidate_limit', 'byte_limit'])
def test_unassessed_parent_removed_from_packet_cannot_qualify_gather(trim):
    _, _, state, parent, gather = frontier()
    options = {'max_bytes': 48000}
    plans = [gather] if trim == 'subset' else [gather, parent]
    if trim == 'candidate_limit': options['max_candidates'] = 1
    if trim == 'byte_limit':
        packet, questions, _ = question_batch(state, [gather], max_bytes=48000)
        options['max_bytes'] = len(json.dumps({'state': packet, 'questions': questions}, ensure_ascii=False).encode())
    packet, _, offered = question_batch(state, plans, **options)
    assert offered == [gather]
    assert packet['local_objective']['candidate_targets'] == {}
    assert 'local_objective_parent_plans' not in packet
