"""Captured V42 rejection: an optional route hid its owned output-arm service."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from jev_factorio.jev_client import MockJevClient
from jev_factorio.judgments import question_batch, select_plan, _qualified_candidate_local_raw_demand
from jev_factorio.planning.buffer_demand import qualified_commissioning
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.two_stage_decision import NON_LAUNCH_PROJECTION, native_digest
from test_buffer_component_demand import captured, context


FIXTURE = 'native-v42-ready-route-raw.json'
FUEL = 'factory:factory_insert:output-arm:2547'


def test_exact_rejected_capture_exposes_existing_serial_service_without_mutation():
    saved = json.loads((Path(__file__).parent / 'fixtures' / FIXTURE).read_bytes())
    snapshot, catalog, loop = captured(FIXTURE)
    record = saved['saved_decision']
    assert record['outcome'] == 'all_candidates_rejected'
    assert native_digest(snapshot, NON_LAUNCH_PROJECTION) == record['binding']['native_sha256']
    assert FUEL not in record['prepared']['input_candidate_ids']
    assert snapshot.factory['entities']['output-arm:2547']['fuel']['coal'] == 1
    assert snapshot.inventory['coal'] == 15
    before = deepcopy(asdict(snapshot)), deepcopy(asdict(loop.memory))

    plans, source = context(snapshot, catalog, loop)
    assert [plan.id for plan in plans] == [FUEL]
    plan = plans[0]
    assert plan.steps[0].costs == {'coal': 4}
    assert plan.steps[0].parameters == {
        'role': 'output-arm:2547', 'item': 'coal', 'quantity': 4,
        'receipt': '18514770:factory_insert:output-arm:2547:coal'}
    assert plan.steps[0].allowed(snapshot) and not plan.steps[0].satisfied(snapshot)
    row = source['candidate_evidence'][plan.id]
    assert row['work_scope'] == 'immediate'
    assert qualified_commissioning(plan, source['facts'], row)
    assert row['buffer_commissioning_parent_purpose']['parent_item_path'] == [
        'logistic-science-pack', 'inserter', 'iron-plate']
    packet, questions, offered = question_batch(source, plans, max_bytes=48000)
    assert offered == plans
    assert len(json.dumps({'state': packet, 'questions': questions}).encode()) <= 48000
    assert before == (asdict(snapshot), asdict(loop.memory))
    assert loop.backend.calls == []


@pytest.mark.parametrize('planner_type', [InputRoutePlanner, MiningOutpostPlanner])
@pytest.mark.parametrize('limit', [1, 2, 3, 8])
def test_serial_service_is_not_merged_with_optional_construction_or_lookahead(planner_type, limit):
    snapshot, catalog, _ = captured(FIXTURE)
    planner = planner_type(catalog, snapshot, 'rocket_launch', max_candidates=limit)
    planner._economic_acquiring = True
    plans = planner.candidates()
    assert [plan.id for plan in plans] == [FUEL]
    assert planner.expansions <= 512


def test_hypothetical_arm_refuel_retains_current_furnace_fuel_prerequisite():
    snapshot, catalog, loop = captured(FIXTURE)
    arm = snapshot.factory['entities']['output-arm:2547']
    arm['fuel']['coal'] = 5
    arm['fuel_insertable']['coal'] = 45
    snapshot.inventory['coal'] -= 4
    plans, source = context(snapshot, catalog, loop)
    manual = next(plan for plan in plans if plan.id == 'factory:factory_insert:recipe:iron-plate')
    assert manual.steps[0].action == 'factory_insert'
    assert manual.steps[0].costs == {'coal': 5}
    assert manual.materials['work_intent']['scope'] == 'immediate'
    assert source['candidate_evidence'][manual.id]['fuel_transfer_start_evidence']
    assert all(plan.id != FUEL for plan in plans)
    assert loop.backend.calls == []


def test_building_input_route_keeps_its_existing_serial_owner():
    snapshot, catalog, _ = captured(FIXTURE)
    snapshot.factory['input_routes']['sources']['recipe:iron-plate']['state'] = 'building'
    planner = MiningOutpostPlanner(catalog, snapshot, 'rocket_launch')
    planner._economic_acquiring = True
    plans = planner.candidates()
    assert len(plans) == 1 and plans[0].steps[0].action == 'factory_input_build'


def test_hypothetical_fuel_services_restore_qualified_current_ore_gather():
    snapshot, catalog, loop = captured(FIXTURE)
    arm = snapshot.factory['entities']['output-arm:2547']
    arm['fuel']['coal'] = 5
    arm['fuel_insertable']['coal'] = 45
    snapshot.factory['entities']['recipe:iron-plate']['fuel']['coal'] = 5
    snapshot.inventory['coal'] -= 9
    plans, source = context(snapshot, catalog, loop)
    manual = next(plan for plan in plans if plan.steps[0].item == 'iron-ore'
                  and plan.steps[0].threshold == 20)
    assert manual.materials['raw_prerequisite']['planner_item_path'] == [
        'logistic-science-pack', 'inserter', 'iron-plate', 'iron-ore']
    row = source['candidate_evidence'][manual.id]
    assert row['work_scope'] == 'immediate'
    assert _qualified_candidate_local_raw_demand(
        manual, source['facts'], row, source['candidate_evidence'])
    assert loop.backend.calls == []


def test_newly_visible_service_still_requires_confident_jev_choice():
    snapshot, catalog, loop = captured(FIXTURE)
    plans, source = context(snapshot, catalog, loop)

    class LowChoice(MockJevClient):
        def evaluate(self, state, questions):
            answers = super().evaluate(state, questions)
            answers['candidate']['confidence'] = .37
            return answers

    decision = select_plan(LowChoice(), source, plans, confidence_floor=.45, max_bytes=48000)
    assert decision.diagnostics['candidate_rejections'] == {}
    assert decision.plan_id is None and decision.reason == 'low choice confidence'
    assert loop.backend.calls == []
