"""V44 native hold: future boiler service acquired an immediate-work witness."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from jev_factorio.jev_client import MockJevClient
from jev_factorio.judgments import (
    _qualified_recipe_transfer_chain, _qualified_utility_power_dependency,
    question_batch, select_plan,
)
from jev_factorio.planning.decision_support import (
    candidate_evidence, _utility_power_prerequisite_start_evidence,
)
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.skills import Plan
from jev_factorio import two_stage_decision as protocol
from jev_factorio.two_stage_decision import NON_LAUNCH_PROJECTION, native_digest
from test_buffer_component_demand import captured, context
from test_utility_power_prerequisite_evidence import fixture


FIXTURE = 'native-v44-power-lookahead-scope.json'
IRON = 'factory:factory_insert:recipe:iron-plate'
BOILER = 'factory:factory_insert:utility:boiler'


def test_exact_capture_retains_future_power_scope_and_current_iron_purpose():
    saved = json.loads((Path(__file__).parent / 'fixtures' / FIXTURE).read_bytes())
    state, catalog, loop = captured(FIXTURE)
    record = saved['saved_decision']
    assert native_digest(state, NON_LAUNCH_PROJECTION) == record['binding']['native_sha256']
    assert record['outcome'] == 'low_choice_confidence'
    assert record['choice']['candidate']['confidence'] == .2
    assert record['binding']['confidence_floor'] == .45
    retained = {plan['id']: plan for plan in record['prepared']['plans']}
    assert retained[BOILER]['materials']['work_intent']['scope'] == 'lookahead'
    assert record['prepared']['context']['candidate_evidence'][BOILER]['work_scope'] == 'immediate'
    assert state.factory['entities']['utility:boiler']['fuel']['coal'] == 4
    consumer = state.factory['entities']['recipe:copper-cable']
    assert consumer['recipe'] == 'copper-cable' and consumer['input'] == {}
    assert consumer['crafting'] is False
    before = deepcopy(asdict(state)), deepcopy(asdict(loop.memory))

    plans, source = context(state, catalog, loop)
    by_id = {plan.id: plan for plan in plans}
    boiler = by_id[BOILER]
    assert list(boiler.to_dict()['steps']) == retained[BOILER]['steps']
    assert boiler.materials['work_intent']['scope'] == 'lookahead'
    row = source['candidate_evidence'][BOILER]
    assert row['work_scope'] == 'lookahead'
    assert row['utility_power_prerequisite_start_evidence'] is None
    assert 'current_power_consumer_prerequisite' not in row['reasons']
    assert not _qualified_utility_power_dependency(boiler, row, state.tick, source['facts'])
    iron = by_id[IRON]
    assert list(iron.to_dict()['steps']) == retained[IRON]['steps']
    assert _qualified_recipe_transfer_chain(iron, source['facts'], source['candidate_evidence'][IRON])
    packet, questions, offered = question_batch(source, [iron, boiler], max_bytes=48000)
    assert {plan.id for plan in offered} == {IRON, BOILER}
    assert 'current consumer\'s native power prerequisite' not in questions[BOILER + '/benefit']['instructions']
    assert len(json.dumps({'state': packet, 'questions': questions}).encode()) <= 48000
    assert before == (asdict(state), asdict(loop.memory))
    assert loop.backend.calls == []


def test_retained_false_promotion_cannot_bypass_the_judgment_consumer():
    saved = json.loads((Path(__file__).parent / 'fixtures' / FIXTURE).read_bytes())
    record = saved['saved_decision']
    plan = next(Plan.from_dict(value) for value in record['prepared']['plans']
                if value['id'] == BOILER)
    source = record['prepared']['context']
    row = source['candidate_evidence'][BOILER]
    assert row['work_scope'] == 'immediate'
    assert row['utility_power_prerequisite_start_evidence'] is not None
    assert not _qualified_utility_power_dependency(plan, row, saved['snapshot']['tick'], source['facts'])
    # New requests cannot present this retained false witness as current demand.
    source = deepcopy(source)
    source['selection_contract']['power_intent_scope'] = 1
    _, questions, offered = question_batch(source, [plan], max_bytes=48000)
    assert offered == [plan]
    assert "current consumer's native power prerequisite" not in questions[BOILER + '/benefit']['instructions']


def test_saved_power_choice_reconstructs_without_rewriting_or_reauthorizing_it():
    saved = json.loads((Path(__file__).parent / 'fixtures' / FIXTURE).read_bytes())
    record = saved['saved_decision']
    original = protocol.encoded(record)
    assert 'power_intent_scope' not in record['prepared']['context']['selection_contract']
    protocol.validate(record, record['binding']['session_id'], record['binding']['target'])
    assert protocol.encoded(record) == original
    assert record['phase'] == 'settled' and record['outcome'] == 'low_choice_confidence'


@pytest.mark.parametrize('marker', [None, True, 0, 2, '1'])
def test_malformed_power_contract_cannot_use_historical_projection(marker):
    state, catalog, loop = captured(FIXTURE)
    plans, source = context(state, catalog, loop)
    assert source['selection_contract']['power_intent_scope'] == 1
    source['selection_contract']['power_intent_scope'] = marker
    with pytest.raises(ValueError, match='Invalid power intent scope contract'):
        question_batch(source, plans, max_bytes=48000)


@pytest.mark.parametrize('intent', [
    {'scope': 'lookahead', 'observed_tick': 10},
    {'scope': 'immediate', 'observed_tick': 9},
    {'scope': 'immediate', 'observed_tick': True},
    {'scope': 'unknown', 'observed_tick': 10},
    {}, 'immediate', None,
])
def test_future_or_invalid_intent_cannot_be_promoted_by_a_power_annotation(intent):
    catalog, state, goal = fixture(fuel=1, coal=5)
    plan = ReadyWorkPlanner(catalog, state, goal).plan()
    plan = replace(plan, materials={**plan.materials, 'work_intent': deepcopy(intent)})
    assert _utility_power_prerequisite_start_evidence(
        state, catalog, plan, None, None) is None


@pytest.mark.parametrize('with_intent', [False, True])
def test_current_power_prerequisites_retain_their_native_start_witness(with_intent):
    catalog, state, goal = fixture(fuel=1, coal=5)
    plan = ReadyWorkPlanner(catalog, state, goal).plan()
    if with_intent:
        plan = replace(plan, materials={**plan.materials,
            'work_intent': {'scope': 'immediate', 'observed_tick': state.tick}})
    row = candidate_evidence(state, catalog, [plan])[plan.id]
    assert row['work_scope'] == 'immediate'
    assert _qualified_utility_power_dependency(plan, row, state.tick)


def test_corrected_evidence_does_not_override_a_low_confidence_jev_choice():
    state, catalog, loop = captured(FIXTURE)
    plans, source = context(state, catalog, loop)
    plans = [plan for plan in plans if plan.id in {IRON, BOILER}]

    class LowChoice(MockJevClient):
        def evaluate(self, model_state, questions):
            answers = super().evaluate(model_state, questions)
            answers['candidate']['confidence'] = .2
            return answers

    decision = select_plan(LowChoice(), source, plans, confidence_floor=.45, max_bytes=48000)
    assert decision.plan_id is None and decision.reason == 'low choice confidence'
    assert loop.backend.calls == []
