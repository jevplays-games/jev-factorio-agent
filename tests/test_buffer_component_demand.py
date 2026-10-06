"""Captured strict hold and hypothetical receipt-qualified kit continuations."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.state import GameSnapshot
from jev_factorio.planning.bootstrap_chain import catalog_projection
from jev_factorio.planning.buffer_demand import purpose, qualified
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.judgments import question_batch, _qualified_recipe_transfer_chain
from test_capital_investments import Backend
from test_registered_furnace_service import native


def captured():
    saved = json.loads((Path(__file__).parent/'fixtures/native-v31-buffer-demand.json').read_bytes())
    state = GameSnapshot(**saved['snapshot'])
    _, catalog = native()
    assert saved['accepted_validation']['payload']['accepted'] is True
    state._atomic_inventory_verified = state._coherent_observation_verified = (state.session_id, state.tick)
    class NoModel:
        def evaluate(self, *args, **kwargs):
            raise AssertionError('Offline regression must not query JEV')
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    loop = kind(Backend(catalog, state), NoModel(), policy='jev', target='rocket_launch',
                factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    loop.memory = loop.memory_type(state.session_id, 'rocket_launch', last_tick=state.tick,
                                  **saved['planner_memory'])
    # Exercise the future changed-source planning path without editing history,
    # consuming an admission or calling a provider.
    loop._source_reevaluation_planning = True
    return state, catalog, loop


def context(state, catalog, loop):
    plans, _ = loop._work_candidates(state)
    support = scheduling_context(state, catalog, plans, 'rocket_launch')
    facts = loop._model_facts(state)
    facts['factory']['recipe_dependency_catalog'] = catalog_projection(state, catalog, plans)
    facts['factory']['native_transfer_receipt_count'] = len(facts['factory'].pop('receipts', {}))
    facts['factory'].pop('connectors', None)
    return plans, {'facts': facts, 'active_goal': {'name': 'rocket_launch'}, 'history': [], **support}


def test_captured_insert_has_separate_paid_component_and_parent_recipe_chains():
    state, catalog, loop = captured()
    failures = deepcopy(loop.memory.failures)
    plans, source = context(state, catalog, loop)
    plan = next(p for p in plans if 'buffer_component_demand' in (p.materials or {}))
    row = source['candidate_evidence'][plan.id]
    assert plan.steps[0].action == 'factory_insert'
    assert plan.steps[0].costs == {'iron-ore': 1}
    assert plan.materials['local_objective']['item'] == 'burner-inserter'
    assert plan.materials['recipe_input_transfer']['planner_item_path'] == [
        'burner-inserter', 'iron-gear-wheel', 'iron-plate', 'iron-ore']
    assert row['buffer_component_parent_purpose']['parent_item_path'] == [
        'automation-science-pack', 'iron-gear-wheel', 'iron-plate']
    assert qualified(plan, source['facts'], row)
    assert _qualified_recipe_transfer_chain(plan, source['facts'], row)
    wire, questions, offered = question_batch(source, plans, max_bytes=48000)
    assert plan.id in [p.id for p in offered]
    assert 'separate parent recipe demand' in questions[plan.id+'/useful_progress']['instructions']
    assert len(json.dumps({'state': wire, 'questions': questions}).encode()) <= 48000
    assert loop.memory.failures == failures
    assert loop.backend.calls == []


@pytest.mark.parametrize('mutation', [
    'stale', 'session', 'unit', 'layout', 'receipt', 'unpaid', 'wrong_part',
    'missing_chest', 'entity', 'parent_recipe', 'parent_satisfied', 'component_satisfied',
    'local_target', 'scope', 'proof_only', 'catalog_version'])
def test_component_purpose_rejects_changed_ownership_or_current_demand(mutation):
    state, catalog, loop = captured()
    plans, source = context(state, catalog, loop)
    plan = next(p for p in plans if 'buffer_component_demand' in (p.materials or {}))
    row = deepcopy(source['candidate_evidence'][plan.id])
    facts = deepcopy(source['facts'])
    materials = deepcopy(plan.materials)
    marker = materials['buffer_component_demand']
    if mutation == 'stale': marker['observed_tick'] -= 1
    elif mutation == 'session': marker['session_id'] = 'other'
    elif mutation == 'unit': marker['source_unit'] += 1
    elif mutation == 'layout': marker['layout'] = 'different'
    elif mutation == 'receipt': marker['paid_parts']['chest']['receipt'] = 'other'
    elif mutation == 'unpaid': facts['factory']['output_buffers']['sources']['recipe:iron-plate']['parts']['chest']['paid'] = 0
    elif mutation == 'wrong_part': marker['part'] = 'chest'
    elif mutation == 'missing_chest': facts['factory']['output_buffers']['sources']['recipe:iron-plate']['parts'] = {}
    elif mutation == 'entity': facts['factory']['entities']['output-chest:2547']['unit_number'] += 1
    elif mutation == 'parent_recipe': facts['factory']['recipe_dependency_catalog']['recipes']['automation-science-pack']['ingredients'] = []
    elif mutation == 'parent_satisfied': facts['inventory']['automation-science-pack'] = 20
    elif mutation == 'component_satisfied': facts['inventory']['burner-inserter'] = 1
    elif mutation == 'local_target': materials['local_objective']['inventory_target'] = 2
    elif mutation == 'scope': materials['work_intent']['scope'] = 'lookahead'
    elif mutation == 'proof_only': row['buffer_component_parent_purpose']['parent_recipe_demand']['raw_input_inventory_target'] = 999
    elif mutation == 'catalog_version': facts['factory']['recipe_dependency_catalog']['version'] = '2.0.76'
    plan = replace(plan, materials=materials)
    assert not qualified(plan, facts, row)


@pytest.mark.parametrize('inventory, expected', [
    ({'iron-plate': 2}, 'iron-gear-wheel'),
    ({'iron-plate': 1, 'iron-gear-wheel': 1}, 'burner-inserter'),
])
def test_hypothetical_paid_craft_keeps_component_goal(inventory, expected):
    state, catalog, loop = captured()
    state.inventory.update(inventory)
    plans, source = context(state, catalog, loop)
    plan = next(p for p in plans if 'buffer_component_demand' in (p.materials or {}))
    assert plan.steps[0].action in {'factory_craft', 'factory_craft_job'}
    assert plan.steps[0].item == expected
    assert plan.materials['local_objective']['item'] == 'burner-inserter'
    assert qualified(plan, source['facts'], source['candidate_evidence'][plan.id])
