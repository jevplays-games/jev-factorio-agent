"""Paid buffer and production goals must survive filtering without cross-talk.

Inventory changes below are hypothetical continuations of an accepted capture;
these tests neither call JEV nor claim native gameplay acceptance.
"""
from copy import deepcopy
import json

import pytest

from jev_factorio.judgments import question_batch
from jev_factorio import two_stage_decision as protocol
from test_buffer_component_demand import captured, context
from test_two_stage_decision import Client, SOURCE


def frontier():
    snapshot, catalog, loop = captured()
    snapshot.inventory.update({'iron-gear-wheel': 1, 'iron-plate': 1,
                               'copper-plate': 7, 'automation-science-pack': 13})
    plans, state = context(snapshot, catalog, loop)
    component = next(p for p in plans if p.steps[0].item == 'burner-inserter')
    science = next(p for p in plans if p.steps[0].item == 'automation-science-pack')
    return snapshot, state, component, science


def test_construction_and_production_use_distinct_targets_without_multi_science():
    _, state, component, science = frontier()
    original = deepcopy(state)
    packet, questions, offered = question_batch(state, [component, science], max_bytes=262144)
    assert offered == [component, science]
    assert packet['local_objective']['primary_target'] is None
    assert packet['local_objective']['candidate_targets'] == {
        p.id: p.materials['local_objective'] for p in offered}
    assert 'candidate_targets' in questions['candidate']['instructions']
    for plan in offered:
        assert packet['candidate_evidence'][plan.id]['local_target'] == plan.materials['local_objective']
    assert state == original


@pytest.mark.parametrize('selection', ['alternative_batch', 'candidate_limit', 'byte_limit'])
def test_retained_science_never_inherits_component_objective(selection):
    _, state, component, science = frontier()
    # Also cover a context whose earlier primary belonged to an excluded plan.
    state['local_objective'].pop('candidate_targets')
    state['local_objective'].update(primary_target=deepcopy(component.materials['local_objective']),
                                     instruction='Acquire the missing burner inserter.')
    original = deepcopy(state)
    plans = [science] if selection == 'alternative_batch' else [science, component]
    options = {'max_bytes': 262144}
    if selection == 'candidate_limit': options['max_candidates'] = 1
    if selection == 'byte_limit':
        packet, questions, _ = question_batch(state, [science], max_bytes=262144)
        options['max_bytes'] = len(json.dumps({'state': packet, 'questions': questions},
                                             ensure_ascii=False).encode())
    packet, _, offered = question_batch(state, plans, **options)
    assert offered == [science]
    assert packet['local_objective']['primary_target'] == science.materials['local_objective']
    assert 'burner' not in packet['local_objective']['instruction']
    assert 'candidate_targets' not in packet['local_objective']
    assert state == original


@pytest.mark.parametrize('mutation', ['stale', 'swapped', 'missing', 'wrong_goal'])
def test_unqualified_retained_target_is_not_borrowed_from_removed_component(mutation):
    _, state, component, science = frontier()
    if mutation == 'stale': science.materials['work_intent']['observed_tick'] -= 1
    elif mutation == 'swapped':
        state['candidate_evidence'][science.id]['local_target'] = component.materials['local_objective']
    elif mutation == 'missing': science.materials['local_objective'] = None
    else: science.materials['local_objective']['ultimate_goal'] = 'stockpile_fuel'
    packet, questions, offered = question_batch(state, [science], max_bytes=262144)
    assert offered == [science]
    assert packet['local_objective']['primary_target'] is None
    assert packet['local_objective']['candidate_targets'] == {}
    assert 'unavailable for this candidate' in questions[science.id+'/benefit']['instructions']


@pytest.mark.parametrize('versioned', [False, True])
def test_two_stage_filter_rebinds_new_requests_and_preserves_legacy_records(versioned):
    snapshot, state, component, science = frontier()
    if not versioned:
        state['selection_contract'].pop('candidate_objective_binding')
        state['local_objective'].pop('candidate_targets')
        state['local_objective'].update(primary_target=deepcopy(component.materials['local_objective']),
                                        instruction='Acquire the missing burner inserter.')
    packet, questions, offered = question_batch(state, [component, science], max_bytes=262144)
    binding = {'session_id': snapshot.session_id, 'target': 'rocket_launch',
               'source_revision': SOURCE, 'input_sha256': '1'*64, 'state_sha256': '2'*64,
               'frontier_sha256': '3'*64, 'native_sha256': protocol.native_digest(snapshot),
               'confidence_floor': .45, 'max_request_bytes': 262144}
    record = protocol.prepare(binding=binding, context=packet, questions=questions,
                              offered=offered, input_candidate_ids=[p.id for p in offered])
    # Explicit synthetic assessment: only science passes the unchanged gates.
    client = Client(reject=component.id)
    assess_context, assess_questions = protocol.assessment_request(record)
    record['assessment'] = client.evaluate(assess_context, assess_questions)
    record['phase'] = 'assessment_received'
    original = deepcopy(record)
    choice = protocol.choice_request(record)
    assert set(choice['state']['candidate_plans']) == {science.id}
    expected = science if versioned else component
    assert choice['state']['local_objective']['primary_target'] == expected.materials['local_objective']
    assert set(choice['questions']['candidate']['criteria']) == {science.id, 'observe'}
    assert record == original
    record.update(choice_request=choice, phase='choice_ready')
    protocol.validate(record, snapshot.session_id, 'rocket_launch')
    record['phase'] = 'choice_pending'
    result = protocol.advance(record, Client(), commit=lambda _: None,
                              fresh_native_digest=lambda: binding['native_sha256'])
    assert result.plan_id is None  # Neither version replays an uncertain call.
