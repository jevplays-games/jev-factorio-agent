from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio.state import GameSnapshot
from jev_factorio.skills import Plan
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.planning.productive_work import prepare_research_batch, _preparation_parent_path
from jev_factorio.planning.service_visits import service_visit
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.judgments import select_plan


def case():
    data = json.loads((Path(__file__).parent / 'fixtures/native-v19-research-preparation.json').read_text())
    snapshot = GameSnapshot(**data['snapshot'])
    snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    snapshot._atomic_inventory_verified = (snapshot.session_id, snapshot.tick)
    return snapshot, Catalog.from_dict(data['catalog']), data


def prepare(snapshot, catalog):
    planner = MiningOutpostPlanner(catalog, snapshot, 'rocket_launch')
    wait = planner._wait('research_progress', snapshot.factory['research'], .9)
    return service_visit(planner, prepare_research_batch(planner, wait))


def test_native_preparation_retains_science_ancestry_without_changing_paid_steps():
    snapshot, catalog, data = case()
    inventory = deepcopy(snapshot.inventory)
    plan = prepare(snapshot, catalog)
    recorded = Plan.from_dict(data['recorded_plan'])
    assert plan.id == recorded.id and plan.steps == recorded.steps
    path = plan.materials['recipe_input_transfer']['planner_item_path']
    assert path == ['automation-science-pack', 'iron-gear-wheel', 'iron-plate', 'iron-ore']
    assert path[0] == plan.materials['local_objective']['item']
    assert plan.materials['work_intent']['scope'] == 'lookahead'
    assert plan.materials['scheduling'] == recorded.materials['scheduling']
    assert plan.materials['service_visit'] == recorded.materials['service_visit']
    support = scheduling_context(snapshot, catalog, [plan], 'rocket_launch')
    proof = support['candidate_evidence'][plan.id]['paid_service_input_start_evidence']
    assert proof['first_recipe_input']['planner_item_path'] == path
    assert set(proof['native_parent_recipes']) == set(path[:-1])
    assert snapshot.inventory == inventory
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    facts = kind._model_facts(object.__new__(kind), snapshot)
    facts['factory']['native_transfer_receipt_count'] = len(facts['factory'].pop('receipts'))
    facts['factory'].pop('connectors', None)
    state = {'facts': facts, 'history': [], **support}
    class Recorded:
        answer_quantum = .01
        def evaluate(self, state, questions): return deepcopy(data['recorded_answers'])
    decision = select_plan(Recorded(), state, [plan], max_bytes=48000)
    assert decision.plan_id is None and decision.reason == 'Candidate evidence insufficient'


def test_missing_parent_still_cannot_claim_paid_service_start_evidence():
    snapshot, catalog, data = case()
    plan = prepare(snapshot, catalog)
    materials = deepcopy(plan.materials)
    materials['recipe_input_transfer']['planner_item_path'].pop(0)
    wrong = replace(plan, materials=materials)
    support = scheduling_context(snapshot, catalog, [wrong], 'rocket_launch')
    assert support['candidate_evidence'][wrong.id]['paid_service_input_start_evidence'] is None


@pytest.mark.parametrize('change', ['unrelated', 'disabled', 'cycle', 'deep'])
def test_preparation_path_rejects_unlinked_or_unbounded_ancestry(change):
    _, catalog, _ = case()
    root, target = 'automation-science-pack', 'iron-ore'
    if change == 'unrelated': target = 'unrelated'
    elif change == 'disabled': catalog.recipes['iron-gear-wheel']['enabled'] = False
    elif change == 'cycle':
        catalog.recipes['iron-gear-wheel']['ingredients'] = [
            {'name': root, 'type': 'item', 'amount': 1}]
    else:
        root, target = 'depth0', 'depth40'
        for i in range(40):
            catalog.recipes['depth'+str(i)] = {
                'name': 'depth'+str(i), 'enabled': True, 'hidden': False,
                'category': 'crafting', 'products': [{'name': 'depth'+str(i), 'type': 'item', 'amount': 1}],
                'ingredients': [{'name': 'depth'+str(i+1), 'type': 'item', 'amount': 1}]}
    assert _preparation_parent_path(catalog, [], root, target) is None


def test_preparation_path_is_native_deterministic_and_does_not_mutate_catalog():
    _, catalog, _ = case(); before = deepcopy(catalog.recipes)
    assert _preparation_parent_path(catalog, [], 'automation-science-pack', 'iron-gear-wheel') == (
        'item:automation-science-pack',)
    assert _preparation_parent_path(catalog, [], 'automation-science-pack', 'automation-science-pack') == ()
    assert catalog.recipes == before
