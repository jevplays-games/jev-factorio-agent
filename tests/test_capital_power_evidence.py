"""Retained native rejection, plus explicitly simulated missing capacity facts."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio.state import GameSnapshot
from jev_factorio.skills import Plan
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.capital import continuation, catalog_digest
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.judgments import _qualified_utility_power_dependency, select_plan


def case(*, simulated_capacity=False):
    data = json.loads((Path(__file__).parent / 'fixtures/native-v22-capital-power.json').read_text())
    state = GameSnapshot(**data['snapshot'])
    # The retained matching observation_validated event accepted this snapshot.
    state._coherent_observation_verified = (state.session_id, state.tick)
    state._atomic_inventory_verified = (state.session_id, state.tick)
    catalog = Catalog.from_dict(data['catalog'])
    if simulated_capacity:
        # The original observer reported coal only. Do not present this test
        # extension as a reading from the captured production observation.
        items = {**state.factory['inventory_insertable'], 'wood': 100,
                 'stone': 100, 'iron-ore': 100, 'copper-ore': 100}
        state.factory['inventory_insertable'] = items
        state.factory['inventory_insertable_evidence']['items'] = dict(items)
    recorded = Plan.from_dict(data['recorded_plan'])
    spec = recorded.materials['capital_investment']['spec']
    assert catalog_digest(catalog, spec['recipe'], spec['machine']) == spec['catalog_sha256']
    plan = continuation(MiningOutpostPlanner(catalog, state, 'rocket_launch'), spec)
    return state, catalog, plan, recorded, data


def evidence(state, catalog, plan):
    return scheduling_context(state, catalog, [plan], 'rocket_launch')['candidate_evidence'][plan.id]


def test_native_rejection_keeps_exact_paid_step_and_requires_missing_wood_capacity():
    state, catalog, plan, recorded, _ = case()
    assert plan.id == recorded.id and plan.steps == recorded.steps
    assert recorded.materials['utility_power_prerequisite']['planner_path'] == []
    assert plan.materials['utility_power_prerequisite']['planner_path'] == ['item:copper-cable']
    assert evidence(state, catalog, plan)['utility_power_prerequisite_start_evidence'] is None


def test_bound_capital_power_child_qualifies_only_with_complete_current_facts():
    state, catalog, plan, recorded, data = case(simulated_capacity=True)
    inventory = deepcopy(state.inventory)
    support = scheduling_context(state, catalog, [plan], 'rocket_launch')
    row = support['candidate_evidence'][plan.id]
    proof = row['utility_power_prerequisite_start_evidence']
    assert proof['next_action_kind'] == 'utility_chain_raw_gather_start'
    assert proof['does_not_establish_electricity_or_research_completion'] is True
    assert _qualified_utility_power_dependency(plan, row, state.tick)
    assert state.inventory == inventory and plan.steps == recorded.steps
    class Recorded:
        answer_quantum = .01
        def evaluate(self, state, questions):
            return deepcopy(data['recorded_answers'])
    model_state = {**data['recorded_state'], **support}
    # Better evidence never changes an already rejected strict-JEV answer.
    decision = select_plan(Recorded(), model_state, [plan], max_bytes=48000)
    assert decision.plan_id is None and decision.reason == 'Candidate evidence insufficient'


@pytest.mark.parametrize(('carried', 'action'), [
    ({'wood': 10}, 'factory_craft'),
    ({'wood': 10, 'copper-cable': 20}, 'factory_craft'),
    ({'wood': 10, 'copper-cable': 20, 'small-electric-pole': 100}, 'factory_connect'),
])
def test_simulated_later_power_steps_do_not_cycle_through_consumer_product(carried, action):
    state, catalog, plan, _, _ = case(simulated_capacity=True)
    state.inventory.update(carried)
    spec = plan.materials['capital_investment']['spec']
    planner = MiningOutpostPlanner(catalog, state, 'rocket_launch')
    current = continuation(planner, spec)
    assert current.steps[0].action == action
    assert current.materials['utility_power_prerequisite']['planner_path'] == ['item:copper-cable']
    row = evidence(state, catalog, current)
    assert _qualified_utility_power_dependency(current, row, state.tick)
    # Recipe expansion remains the original physical bootstrap calculation.
    planner._economic_acquiring = True
    original = planner._production(catalog.recipes[spec['recipe']], spec['role'], spec['batches'], ())
    assert current.steps == original.steps


@pytest.mark.parametrize('change', [
    'missing_path', 'foreign_path', 'stale_marker', 'extra_marker', 'wrong_stage',
    'wrong_catalog', 'wrong_role', 'wrong_id', 'wrong_step', 'wrong_unit',
    'foreign_actor', 'stale_capacity', 'zero_capacity', 'no_atomic', 'no_coherence',
])
def test_capital_wrapper_cannot_qualify_unbound_or_altered_power_work(change):
    state, catalog, plan, _, _ = case(simulated_capacity=True)
    materials = deepcopy(plan.materials)
    if change == 'missing_path': materials['utility_power_prerequisite']['planner_path'] = []
    elif change == 'foreign_path': materials['utility_power_prerequisite']['planner_path'] = ['item:iron-plate']
    elif change == 'stale_marker': materials['capital_investment']['observed_tick'] -= 1
    elif change == 'extra_marker': materials['capital_investment']['trusted'] = True
    elif change == 'wrong_stage': materials['capital_investment']['stage'] = 'kit'
    elif change == 'wrong_catalog': materials['capital_investment']['spec']['catalog_sha256'] = '0' * 64
    elif change == 'wrong_role': materials['capital_investment']['spec']['role'] = 'recipe:iron-plate'
    elif change == 'wrong_id': plan = replace(plan, id=plan.id + ':forged')
    elif change == 'wrong_step': plan = replace(plan, steps=(replace(plan.steps[0], timeout_ticks=1),))
    elif change == 'wrong_unit': materials['utility_power_prerequisite']['consumer_unit'] += 1
    elif change == 'foreign_actor': state.factory['inventory_insertable_evidence']['actor_unit'] += 1
    elif change == 'stale_capacity': state.factory['inventory_insertable_evidence']['tick'] -= 1
    elif change == 'zero_capacity':
        state.factory['inventory_insertable']['wood'] = 0
        state.factory['inventory_insertable_evidence']['items']['wood'] = 0
    elif change == 'no_atomic': del state._atomic_inventory_verified
    elif change == 'no_coherence': del state._coherent_observation_verified
    plan = replace(plan, materials=materials)
    assert evidence(state, catalog, plan)['utility_power_prerequisite_start_evidence'] is None
