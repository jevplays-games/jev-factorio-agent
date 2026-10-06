"""Retained native horizon mismatch; no live model calls or gameplay dispatch."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.decision_support import candidate_evidence, scheduling_context
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.skills import Plan
from jev_factorio.state import GameSnapshot


def case():
    fixtures = Path(__file__).parent / 'fixtures'
    data = json.loads((fixtures / 'native-v23-horizon-bill.json').read_text())
    catalog = Catalog.from_dict(json.loads(
        (fixtures / 'native-v22-capital-power.json').read_text())['catalog'])
    state = GameSnapshot(**data['snapshot'])
    assert data['validation']['accepted'] is True
    assert data['validation']['factorio_tick'] == state.tick
    assert state.game_version == catalog.version
    state._coherent_observation_verified = (state.session_id, state.tick)
    state._atomic_inventory_verified = (state.session_id, state.tick)
    recorded = data['recorded_state']
    plans = [Plan.from_dict({**row, 'materials': {
        **recorded['shared_plan_materials'], **row['materials']}})
        for row in recorded['candidate_plans'].values()]
    belt = next(p for p in plans if p.steps[0].item == 'transport-belt')
    return state, catalog, plans, belt, data


def test_retained_native_horizon_craft_recomputes_larger_bill_without_changing_action():
    state, catalog, plans, belt, data = case()
    original = deepcopy(belt.to_dict())
    assert data['recorded_state']['candidate_evidence'][belt.id]['shared_bill_craft'] is None
    assert 'transport-belt' not in belt.materials['batches']
    assert state.inventory['transport-belt'] == 20
    assert belt.steps[0].parameters['batches'] == 8
    assert belt.materials['shared_bill_craft']['bill_inventory_target'] == 36
    worker = MiningOutpostPlanner(catalog, state, 'rocket_launch')
    worker._set_focus('logistic-science-pack', 20)
    assert worker.demands == {'logistic-science-pack': 36, 'automation-science-pack': 35}
    assert worker.targets['transport-belt'] == 36
    row = candidate_evidence(state, catalog, plans)[belt.id]['shared_bill_craft']
    assert row['bounded_workload_demands'] == worker.demands
    assert row['unfilled_bill_units'] == 16
    assert row['expected_products_after_native_verification'] == 16
    assert row['forecast_is_not_paid_stock_or_completed_output'] is True
    assert belt.to_dict() == original


@pytest.mark.parametrize('change', [
    'stale', 'target', 'local_amount', 'products', 'stock', 'research_progress',
    'research_absent', 'recipe_cost', 'recipe_disabled', 'batch_count', 'malformed_bill',
    'pending_queue', 'forged_future_target',
])
def test_horizon_witness_refuses_changed_or_fabricated_demands(change):
    state, catalog, _, belt, _ = case()
    materials = deepcopy(belt.materials)
    marker = materials['shared_bill_craft']
    step = belt.steps[0]
    if change == 'stale': marker['observed_tick'] -= 1
    elif change == 'target': marker['bill_inventory_target'] += 1
    elif change == 'local_amount': marker['local_target_amount'] += 1
    elif change == 'products': marker['planned_product_units'] += 1
    elif change == 'stock': state.inventory['transport-belt'] += 1
    elif change == 'research_progress': state.factory['research_progress'] = 1
    elif change == 'research_absent': state.factory['research'] = ''
    elif change == 'recipe_cost': catalog.recipes['transport-belt']['ingredients'][0]['amount'] += 1
    elif change == 'recipe_disabled': catalog.recipes['transport-belt']['hidden'] = True
    elif change == 'batch_count':
        step = replace(step, parameters={**step.parameters, 'batches': 9})
    elif change == 'malformed_bill': materials['batches'] = 'unavailable'
    elif change == 'pending_queue': state.factory['crafting_queue'] = 1
    elif change == 'forged_future_target':
        # Internally consistent marker/costs still cannot invent horizon demand.
        marker['bill_inventory_target'] = 38
        marker['planned_product_units'] = 18
        step = replace(step, threshold=38, costs={'iron-gear-wheel': 9, 'iron-plate': 9},
                       parameters={**step.parameters, 'batches': 9})
    belt = replace(belt, materials=materials, steps=(step,))
    assert candidate_evidence(state, catalog, [belt])[belt.id]['shared_bill_craft'] is None


def test_improved_horizon_evidence_does_not_release_original_strict_choice_rejection():
    state, catalog, plans, _, data = case()
    class Recorded:
        answer_quantum = .01
        def evaluate(self, state, questions):
            return deepcopy(data['recorded_answers'])
    support = scheduling_context(state, catalog, plans, 'rocket_launch')
    decision = select_plan(Recorded(), {**data['recorded_state'], **support}, plans,
                           # Keep the full recorded answer domain in this gate
                           # regression; production request budgeting is separate.
                           max_bytes=100000)
    assert decision.plan_id is None
    assert decision.reason == 'low choice confidence'
    assert decision.answers['candidate']['confidence'] == .42


def test_native_horizon_evidence_keeps_existing_request_budget_and_priority():
    state, catalog, plans, _, data = case()
    order = data['recorded_state']['deterministic_ranking']
    plans.sort(key=lambda p: order.index(p.id))
    support = scheduling_context(state, catalog, plans, 'rocket_launch')
    context, questions, offered = question_batch(
        {**data['recorded_state'], **support}, plans, max_bytes=48000)
    assert offered and offered == plans[:len(offered)]
    assert offered[0].steps[0].item == 'wood'
    assert len(json.dumps({'state': context, 'questions': questions},
                          separators=(',', ':'), ensure_ascii=False).encode()) <= 48000
    assert 'observe' in questions['candidate']['criteria']
