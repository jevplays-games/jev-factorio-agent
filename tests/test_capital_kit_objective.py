"""Construction purpose stays separate from projected producer output."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.planning import capital
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.judgments import question_batch
from jev_factorio.state import GameSnapshot
from test_capital_investments import scenario, offer, ReadyWorkPlanner, ITEM, MACHINE


def test_kit_target_and_temporary_focus_do_not_change_investment_identity():
    data, state = scenario()
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner.focus = (ITEM, 20)
    before = deepcopy(state)
    plan = planner._need(ITEM, 20)
    assert plan.materials['local_objective'] == {
        'item': MACHINE, 'inventory_target': 1, 'ultimate_goal': 'rocket_launch'}
    assert planner.focus == (ITEM, 20)
    assert planner._economic_acquiring is False
    assert state == before
    assert plan.materials[capital.MARKER]['stage'] == 'kit'
    assert plan.steps == offer(data, state).steps


def native_case():
    saved = json.loads((Path(__file__).parent/'fixtures/native-v16-capital-kit.json').read_text())
    catalog = Catalog.from_dict(saved['catalog'])
    snapshot = GameSnapshot(**deepcopy(saved['facts']))
    snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    snapshot._atomic_inventory_verified = (snapshot.session_id, snapshot.tick)
    planner = MiningOutpostPlanner(catalog, snapshot, 'rocket_launch')
    planner.focus = ('automation-science-pack', 20)
    plan = capital.continuation(planner, saved['spec'])
    return saved, catalog, snapshot, planner, plan


def test_native_paid_plate_pickup_targets_assembler_kit_not_future_science():
    saved, catalog, snapshot, planner, plan = native_case()
    assert planner.focus == ('automation-science-pack', 20)
    assert plan.id == saved['expected_plan']['id']
    assert list(plan.to_dict()['steps']) == saved['expected_plan']['steps']
    support = scheduling_context(snapshot, catalog, [plan], 'rocket_launch')
    assert support['local_objective']['primary_target']['item'] == 'assembling-machine-1'
    assert 'payback are separate planner estimates' in support['local_objective']['instruction']
    proof = support['candidate_evidence'][plan.id]['output_pickup_start_evidence']
    assert proof['planner_item_path'] == ['assembling-machine-1', 'iron-gear-wheel', 'iron-plate']
    assert proof['ready_output_quantity_now'] == proof['planned_pickup_quantity'] == 1
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    facts = kind._model_facts(object.__new__(kind), snapshot)
    receipts = facts['factory'].pop('receipts')
    facts['factory'].pop('connectors', None)
    facts['factory']['native_transfer_receipt_count'] = len(receipts)
    state = {'facts': facts, 'candidate_plans': {plan.id: plan.to_dict()},
             'history': [], 'active_goal': 'rocket_launch', **support}
    _, questions, _ = question_batch(state, [plan], max_bytes=48000)
    assert 'output_pickup_start_evidence' in questions[plan.id+'/useful_progress']['instructions']
    assert support['candidate_evidence'][plan.id]['urgency'] == 1


@pytest.mark.parametrize('change', ['missing_output', 'owner', 'path', 'tick'])
def test_kit_target_does_not_bypass_native_pickup_evidence(change):
    _, catalog, snapshot, _, plan = native_case()
    role = plan.steps[0].parameters['role']
    if change == 'missing_output': snapshot.factory['entities'][role]['output'] = {}
    elif change == 'owner': snapshot.factory['production_sites']['sources'][role]['state'] = 'external'
    elif change == 'path': plan.materials['output_pickup']['planner_item_path'][0] = 'automation-science-pack'
    elif change == 'tick': plan.materials['output_pickup']['observed_tick'] -= 1
    support = scheduling_context(snapshot, catalog, [plan], 'rocket_launch')
    assert support['candidate_evidence'][plan.id]['output_pickup_start_evidence'] is None


def test_failed_kit_compilation_restores_callers_focus(monkeypatch):
    saved, _, _, planner, _ = native_case()
    def fail(*args): raise ValueError('synthetic unavailable native prerequisite')
    monkeypatch.setattr(planner, '_machine', fail)
    with pytest.raises(ValueError, match='synthetic'):
        capital.continuation(planner, saved['spec'])
    assert planner.focus == ('automation-science-pack', 20)
    assert planner._economic_acquiring is False
