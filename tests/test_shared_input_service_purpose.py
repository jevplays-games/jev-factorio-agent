"""Retain the ordinary purpose when optional kit work needs the same action."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from jev_factorio.jev_client import MockJevClient
from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.planning.output_buffers import OutputBufferPlanner
from jev_factorio.two_stage_decision import NON_LAUNCH_PROJECTION, native_digest
from test_buffer_component_demand import captured, context


FIXTURE = 'native-v43-shared-service-purpose.json'
FUEL = 'factory:factory_insert:recipe:iron-plate'
TARGET = {'item': 'automation-science-pack', 'inventory_target': 20,
          'ultimate_goal': 'rocket_launch'}


def test_exact_native_hold_retains_immediate_production_purpose():
    saved = json.loads((Path(__file__).parent / 'fixtures' / FIXTURE).read_bytes())
    state, catalog, loop = captured(FIXTURE)
    retained = saved['saved_decision']
    assert retained['outcome'] == 'low_choice_confidence'
    assert retained['choice']['candidate']['confidence'] == .33
    assert retained['binding']['confidence_floor'] == .45
    assert native_digest(state, NON_LAUNCH_PROJECTION) == retained['binding']['native_sha256']
    old = next(plan for plan in retained['prepared']['plans'] if plan['id'] == FUEL)
    assert old['materials']['local_objective']['item'] == 'transport-belt'
    assert old['materials']['input_route_kit_prerequisite']['state'] == 'proposed'
    before = deepcopy(asdict(state)), deepcopy(asdict(loop.memory)), deepcopy(saved)

    plans, source = context(state, catalog, loop)
    fuel = next(plan for plan in plans if plan.id == FUEL)
    assert list(fuel.to_dict()['steps']) == old['steps']
    assert fuel.materials['local_objective'] == TARGET
    assert 'input_route_kit_prerequisite' not in fuel.materials
    assert fuel.materials['work_intent']['scope'] == 'immediate'
    assert len([plan for plan in plans if plan.id == FUEL]) == 1
    evidence = source['candidate_evidence'][FUEL]['fuel_transfer_start_evidence']
    assert evidence['coal_to_transfer'] == 5 and evidence['fuel_now'] == 0
    assert evidence['burner_unit'] == 2547
    assert evidence['service_basis'] == 'current_primary_fuel_service_consumer'
    assert fuel.description.startswith('Next production batch: 20 automation-science-pack.')
    wire, questions, offered = question_batch(source, plans, max_bytes=48000)
    assert fuel in offered
    assert wire['candidate_evidence'][FUEL]['local_target'] == TARGET
    assert len(json.dumps({'state': wire, 'questions': questions}).encode()) <= 48000
    assert before == (asdict(state), asdict(loop.memory), saved)
    assert loop.backend.calls == []


@pytest.mark.parametrize('planner_type', [InputRoutePlanner, MiningOutpostPlanner])
@pytest.mark.parametrize('limit', [1, 2, 3, 8])
def test_same_action_keeps_ordinary_purpose_within_existing_budget(planner_type, limit):
    state, catalog, _ = captured(FIXTURE)
    planner = planner_type(catalog, state, 'rocket_launch', max_candidates=limit)
    planner._economic_acquiring = True
    plans = planner.candidates()
    assert plans[0].id == FUEL
    assert plans[0].materials['local_objective'] == TARGET
    assert len(plans) <= limit and len({plan.id for plan in plans}) == len(plans)
    assert planner.expansions <= 512


def test_owned_building_route_retains_its_serial_build():
    state, catalog, _ = captured(FIXTURE)
    state.factory['input_routes']['sources']['recipe:iron-plate']['state'] = 'building'
    planner = MiningOutpostPlanner(catalog, state, 'rocket_launch')
    planner._economic_acquiring = True
    plans = planner.candidates()
    assert len(plans) == 1
    assert plans[0].steps[0].action == 'factory_input_build'


@pytest.mark.parametrize('change', ['other_target', 'lookahead', 'another_kit'])
def test_identical_action_needs_matching_immediate_parent_purpose(monkeypatch, change):
    state, catalog, loop = captured(FIXTURE)
    saved = json.loads((Path(__file__).parent / 'fixtures' / FIXTURE).read_bytes())
    from jev_factorio.skills import Plan
    kit = Plan.from_dict(next(p for p in saved['saved_decision']['prepared']['plans'] if p['id'] == FUEL))
    ordinary = next(p for p in context(state, catalog, loop)[0] if p.id == FUEL)
    assert ordinary.materials['local_objective'] == TARGET
    materials = deepcopy(ordinary.materials)
    if change == 'other_target':
        materials['local_objective']['item'] = 'logistic-science-pack'
    elif change == 'lookahead':
        materials['work_intent']['scope'] = 'lookahead'
    else:
        materials['input_route_kit_prerequisite'] = deepcopy(kit.materials['input_route_kit_prerequisite'])
    ordinary = replace(ordinary, materials=materials)
    monkeypatch.setattr(OutputBufferPlanner, 'candidates', lambda self:
                        [ordinary] if getattr(self, '_defer_proposed_input_route', False) else [kit])
    plans = MiningOutpostPlanner(catalog, state, 'rocket_launch').candidates()
    assert len(plans) == 1
    assert plans[0].materials['local_objective'] == kit.materials['local_objective']


def test_better_purpose_does_not_relax_strict_choice_confidence():
    state, catalog, loop = captured(FIXTURE)
    plans, source = context(state, catalog, loop)

    class LowChoice(MockJevClient):
        def evaluate(self, state, questions):
            answers = super().evaluate(state, questions)
            answers['candidate']['confidence'] = .33
            return answers

    decision = select_plan(LowChoice(), source, plans, confidence_floor=.45, max_bytes=48000)
    assert decision.plan_id is None and decision.reason == 'low choice confidence'
    assert loop.backend.calls == []
