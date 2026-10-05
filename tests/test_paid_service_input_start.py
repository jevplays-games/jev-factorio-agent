from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_factorio.state import GameSnapshot
from jev_factorio.skills import Plan
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.factory import FactoryPlanner
from jev_factorio.planning.service_visits import service_visit
from jev_factorio.planning.decision_support import (
    _paid_service_input_start_evidence, _recipe_input_transfer_start_evidence,
    candidate_evidence,
)
from jev_factorio.judgments import _qualified_paid_service_input, question_batch


def setup():
    capture = json.loads((Path(__file__).parent / 'fixtures/native058-paid-service-input.json').read_text())
    snapshot = GameSnapshot(**deepcopy(capture['snapshot']))
    snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    snapshot._atomic_inventory_verified = (snapshot.session_id, snapshot.tick)
    catalog = Catalog.from_dict(capture['catalog'])
    original = next(Plan.from_dict(value) for key, value in capture['state']['candidate_plans'].items()
                    if key.startswith('service:'))
    atomic = replace(original, id='factory:factory_insert:recipe:copper-plate',
        steps=(original.steps[0],), materials={k:v for k,v in original.materials.items() if k != 'service_visit'})
    planner = FactoryPlanner(catalog, snapshot, original.goal)
    planner.ledger = SimpleNamespace(carried=dict(snapshot.inventory))
    planner.targets = {}
    plan = service_visit(planner, atomic)
    assert plan.id == original.id and plan.steps == original.steps
    facts = deepcopy(capture['state']['facts'])
    row = candidate_evidence(snapshot, catalog, [plan])[plan.id]
    return catalog, snapshot, plan, facts, row, capture


def test_actual_two_step_paid_service_has_truthful_first_input_proof():
    catalog, snapshot, plan, facts, row, capture = setup()
    assert _recipe_input_transfer_start_evidence(snapshot, catalog, plan) is None
    proof = row['paid_service_input_start_evidence']
    assert proof and proof['combined_paid_costs'] == {'copper-ore':20, 'coal':4}
    assert proof['first_recipe_input']['burner_fuel_coal_now'] == 1
    assert _qualified_paid_service_input(plan, facts, row)
    state = deepcopy(capture['state'])
    state['candidate_evidence'][plan.id] = row
    other = [Plan.from_dict(value) for key,value in state['candidate_plans'].items() if key != plan.id]
    packet, questions, _ = question_batch(state, [plan, *other], max_bytes=48000)
    assert len(json.dumps({'state':packet,'questions':questions}).encode()) < 48000
    for suffix in ('useful_progress','benefit','needs_observation'):
        assert 'paid_service_input_start_evidence' in questions[plan.id+'/'+suffix]['instructions']
    assert state['facts'] == capture['state']['facts']
    assert state['history'] == capture['state']['history']
    assert 'unsupported' in json.dumps(capture['recorded_response'])


@pytest.mark.parametrize('change', ['tick','coherence','atomic','catalog','ore','coal','owner',
    'unit','receipt','raw_receipts','reverse','marker','stock','fuel','world','certificate','job','reserved'])
def test_producer_rejects_stale_unpaid_or_forged_visit(change):
    catalog, snapshot, plan, _, _, _ = setup()
    if change == 'world': snapshot.world_kind = 'mock'
    elif change == 'certificate': snapshot._paid_service_admissions = {}
    elif change == 'job': snapshot.factory['craft_job']['status'] = 'running'
    elif change == 'reserved':
        # A current planner ledger reserving one coal cannot admit the old four-coal tail.
        planner = FactoryPlanner(catalog, snapshot, plan.goal)
        planner.ledger = SimpleNamespace(carried={**snapshot.inventory, 'coal': 3})
        planner.targets = {}
        atomic = replace(plan, id='factory:factory_insert:recipe:copper-plate', steps=(plan.steps[0],),
            materials={k:v for k,v in plan.materials.items() if k != 'service_visit'})
        current = service_visit(planner, atomic)
        assert current.steps[1].costs == {'coal': 3}
        assert plan.id not in snapshot._paid_service_admissions
    elif change == 'tick': snapshot.factory['tick'] -= 1
    elif change == 'coherence': snapshot._coherent_observation_verified = None
    elif change == 'atomic': snapshot._atomic_inventory_verified = None
    elif change == 'catalog': snapshot.game_version = 'wrong'
    elif change == 'ore': snapshot.inventory['copper-ore'] = 19
    elif change == 'coal': snapshot.inventory['coal'] = 3
    elif change == 'owner': snapshot.factory['production_sites']['sources']['recipe:copper-plate']['state'] = 'external'
    elif change == 'unit': snapshot.factory['entities']['recipe:copper-plate']['unit_number'] = True
    elif change == 'receipt': snapshot.factory['receipts'][plan.steps[1].parameters['receipt']] = {}
    elif change == 'raw_receipts': snapshot.factory.pop('receipts')
    elif change == 'reverse': plan = replace(plan, steps=tuple(reversed(plan.steps)))
    elif change == 'marker': plan.materials['service_visit']['unit_numbers'][1] += 1
    elif change == 'stock': plan.materials['service_visit']['paid_stock_now']['coal'] = 3
    elif change == 'fuel': snapshot.factory['entities']['recipe:copper-plate']['fuel']['coal'] = 0
    assert _paid_service_input_start_evidence(snapshot,catalog,plan) is None


@pytest.mark.parametrize('change', ['tick','owner','unit','ore','coal','receipt_count','receipt',
    'present','verified','reverse','marker','stock','path','fuel','recipe','later_claim','world','parent_recipe','job'])
def test_consumer_rejects_crosswired_or_contrary_visit(change):
    _, _, plan, facts, row, _ = setup()
    proof = row['paid_service_input_start_evidence']
    if change == 'world': facts['world_kind'] = 'mock'
    elif change == 'job': facts['factory']['craft_job']['status'] = 'running'
    elif change == 'parent_recipe': proof['native_parent_recipes']['automation-science-pack']['ingredients'] = []
    elif change == 'tick': facts['tick'] = True
    elif change == 'owner': facts['factory']['production_sites']['sources']['recipe:copper-plate']['state'] = 'external'
    elif change == 'unit': facts['factory']['entities']['recipe:copper-plate']['unit_number'] = True
    elif change == 'ore': facts['inventory']['copper-ore'] = 19
    elif change == 'coal': facts['inventory']['coal'] = 3
    elif change == 'receipt_count': proof['native_receipt_queries'][1]['receipt_count'] += 1
    elif change == 'receipt': proof['native_receipt_queries'][1]['receipt'] = 'other'
    elif change == 'present': proof['native_receipt_queries'][1]['present'] = True
    elif change == 'verified': proof['native_receipt_queries'][1]['map_verified'] = False
    elif change == 'reverse': plan = replace(plan,steps=tuple(reversed(plan.steps)))
    elif change == 'marker': proof['service_visit']['unit_numbers'][1] += 1
    elif change == 'stock': proof['service_visit']['paid_stock_now']['coal'] = 3
    elif change == 'path': plan.materials['recipe_input_transfer']['planner_item_path'][0] = 'rocket-part'
    elif change == 'fuel': facts['factory']['entities']['recipe:copper-plate']['fuel']['coal'] = 0
    elif change == 'recipe': proof['native_recipe']['ingredients'][0]['amount'] = 2
    elif change == 'later_claim': proof['later_fuel_output_and_target_completion_unverified'] = False
    assert not _qualified_paid_service_input(plan,facts,row)


def test_full_native_planner_propagates_fresh_service_admission_without_restored_certificate():
    from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
    catalog, snapshot, _, facts, _, capture = setup()
    snapshot._paid_service_admissions = {}
    plans = MiningOutpostPlanner(catalog, snapshot, 'rocket_launch').candidates()
    assert {plan.id for plan in plans} == set(capture['state']['candidate_plans'])
    service = next(plan for plan in plans if plan.id.startswith('service:'))
    assert [step.costs for step in service.steps] == [{'copper-ore': 20}, {'coal': 4}]
    row = candidate_evidence(snapshot, catalog, plans)[service.id]
    assert _qualified_paid_service_input(service, facts, row)


@pytest.mark.parametrize('coal', [1, 2, 3, 4])
def test_current_paid_input_evidence_does_not_require_urgent_refuel(coal):
    catalog, snapshot, plan, facts, _, capture = setup()
    role = plan.steps[0].parameters['role']
    snapshot.factory['entities'][role]['fuel']['coal'] = coal
    facts['factory']['entities'][role]['fuel']['coal'] = coal
    row = candidate_evidence(snapshot, catalog, [plan])[plan.id]
    assert row['urgency'] == (3 if coal < 2 else 0)
    assert row['reasons'] == ([f'observed_low_fuel:{role}'] if coal < 2 else [])
    assert _qualified_paid_service_input(plan, facts, row)
    state = deepcopy(capture['state'])
    state['facts'] = facts
    state['candidate_evidence'] = {plan.id: row}
    _, questions, _ = question_batch(state, [plan], max_bytes=48000)
    for suffix in ('useful_progress', 'benefit', 'needs_observation'):
        assert 'paid_service_input_start_evidence' in questions[plan.id+'/'+suffix]['instructions']
    # Native start proof cannot launder contradictory urgency or starvation claims.
    row['urgency'] = 0 if coal < 2 else 3
    assert not _qualified_paid_service_input(plan, facts, row)
    row['urgency'] = 3 if coal < 2 else 0
    row['reasons'] = [] if coal < 2 else [f'observed_low_fuel:{role}']
    assert not _qualified_paid_service_input(plan, facts, row)


def test_recorded_three_coal_hold_retains_independent_judgments_and_native_guards():
    capture = json.loads((Path(__file__).parent / 'fixtures/native-v16-paid-input-nonurgent.json').read_text())
    state = capture['state']
    plan = Plan.from_dict(next(iter(state['candidate_plans'].values())))
    row = state['candidate_evidence'][plan.id]
    assert row['urgency'] == 0 and row['reasons'] == []
    assert row['paid_service_input_start_evidence']['first_recipe_input']['burner_fuel_coal_now'] == 3
    assert _qualified_paid_service_input(plan, state['facts'], row)
    _, questions, _ = question_batch(state, [plan], max_bytes=48000)
    for suffix in ('useful_progress', 'benefit', 'needs_observation'):
        assert 'paid_service_input_start_evidence' not in capture['questions'][plan.id+'/'+suffix]['instructions']
        assert 'paid_service_input_start_evidence' in questions[plan.id+'/'+suffix]['instructions']
    assert 'contrary current evidence' in questions[plan.id+'/useful_progress']['instructions']
    assert 'fresh native rechecks and receipts' in questions[plan.id+'/needs_observation']['instructions']
