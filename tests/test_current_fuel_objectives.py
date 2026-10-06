"""Replay the accepted V33 fuel frontier without native/model calls."""
from copy import deepcopy

import pytest

from jev_factorio.judgments import question_batch, _qualified_utility_power_dependency
from jev_factorio.planning.decision_support import candidate_target_objective, candidate_evidence
from test_buffer_component_demand import captured, context


def frontier():
    snapshot, catalog, loop = captured('native-v33r3-fuel-frontier.json')
    plans, state = context(snapshot, catalog, loop)
    furnace = next(p for p in plans if p.steps[0].parameters.get('role') == 'recipe:steel-plate')
    boiler = next(p for p in plans if p.steps[0].parameters.get('role') == 'utility:boiler')
    return snapshot, catalog, loop, plans, state, furnace, boiler


def test_registered_furnace_fuel_retains_its_direct_kit_recipe_purpose():
    snapshot, _, loop, plans, state, furnace, _ = frontier()
    assert 'assembling-machine-2' not in snapshot.inventory  # Native zero omission.
    assert 'recipe:steel-plate' not in snapshot.factory['production_sites']['sources']
    proof = state['candidate_evidence'][furnace.id]['fuel_transfer_start_evidence']
    dependency = proof['local_recipe_dependency']
    assert dependency['local_target_inventory_now'] == 0
    assert dependency['local_target_shortfall_now'] == 1
    assert dependency['planner_item_path'] == ['assembling-machine-2', 'steel-plate']
    assert dependency['producer_registered_source_identity_current'] is True
    assert 'producer_owned_source_identity_current' not in dependency
    assert proof['coal_to_transfer'] == furnace.steps[0].costs['coal'] == 5
    packet, _, offered = question_batch(state, plans, max_bytes=48000)
    assert furnace in offered and packet['candidate_evidence'][furnace.id] == state['candidate_evidence'][furnace.id]
    assert loop.backend.calls == []


def test_current_power_promotion_keeps_its_own_target_under_new_binding():
    snapshot, _, _, plans, state, _, boiler = frontier()
    row = state['candidate_evidence'][boiler.id]
    assert boiler.materials['work_intent']['scope'] == 'lookahead'
    assert row['work_scope'] == 'immediate'
    assert _qualified_utility_power_dependency(boiler, row, snapshot.tick)
    before = deepcopy(state)
    packet, questions, offered = question_batch(state, plans, max_bytes=48000)
    assert boiler in offered
    assert packet['local_objective']['candidate_targets'][boiler.id] == boiler.materials['local_objective']
    assert 'unavailable for this candidate' not in questions[boiler.id+'/benefit']['instructions']
    assert state == before
    # Version two must reproduce the already-saved exclusion without rewriting it.
    legacy = deepcopy(state)
    legacy['selection_contract']['candidate_objective_binding'] = 2
    packet, questions, _ = question_batch(legacy, plans, max_bytes=48000)
    assert boiler.id not in packet['local_objective']['candidate_targets']
    assert 'unavailable for this candidate' in questions[boiler.id+'/benefit']['instructions']


@pytest.mark.parametrize('change', ['tick', 'unit', 'receipt', 'paid', 'connections', 'scope_only'])
def test_scope_promotion_requires_the_existing_action_bound_power_witness(change):
    snapshot, _, _, _, state, _, boiler = frontier()
    row = deepcopy(state['candidate_evidence'][boiler.id])
    proof = row['utility_power_prerequisite_start_evidence']
    if change == 'tick': proof['observed_tick'] -= 1
    elif change == 'unit': proof['consumer_unit'] += 1
    elif change == 'receipt': proof['planned_native_receipt'] = 'different'
    elif change == 'paid': proof['paid_inventory_sufficient_now'] = False
    elif change == 'connections': proof['connections_current']['boiler_to_engine_steam'] = False
    else: row['utility_power_prerequisite_start_evidence'] = None
    assert candidate_target_objective(boiler, row, snapshot.tick, boiler.goal,
                                      allow_power_promotion=True) is None


@pytest.mark.parametrize('change', ['owned_conflict', 'recipe', 'local_satisfied', 'malformed_zero',
                                     'stale_sites', 'no_coherence'])
def test_registered_fuel_purpose_rejects_missing_or_contrary_native_facts(change):
    snapshot, catalog, _, _, _, furnace, _ = frontier()
    if change == 'owned_conflict':
        snapshot.factory['production_sites']['sources']['recipe:steel-plate'] = {
            'state': 'owned', 'source_unit': 999}
    elif change == 'recipe': snapshot.factory['entities']['recipe:steel-plate']['recipe'] = 'copper-plate'
    elif change == 'local_satisfied': snapshot.inventory['assembling-machine-2'] = 1
    elif change == 'malformed_zero': snapshot.inventory['assembling-machine-2'] = False
    elif change == 'stale_sites': snapshot.factory['production_sites']['tick'] -= 1
    else: del snapshot._coherent_observation_verified
    row = candidate_evidence(snapshot, catalog, [furnace])[furnace.id]
    assert (row.get('fuel_transfer_start_evidence') or {}).get('local_recipe_dependency') is None
