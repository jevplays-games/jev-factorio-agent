"""Retain immediate native work when only discretionary work claims urgency."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.decision_support import candidate_evidence
from jev_factorio.state import GameSnapshot


def native_frontier():
    from test_capital_investments import Backend
    saved = json.loads((Path(__file__).parent / 'fixtures/native-v27-urgent-lookahead.json').read_text())
    snapshot = GameSnapshot(**saved['snapshot'])
    assert saved['accepted_validation']['payload']['accepted'] is True
    catalog = Catalog.from_dict(saved['catalog'])
    assert sorted(name for name, tech in catalog.technologies.items() if tech['researched']) == sorted(snapshot.researched)
    # Restore the controller's internal acceptance markers for this already
    # accepted fixture; no snapshot values or material quantities are changed.
    snapshot._atomic_inventory_verified = snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    backend = Backend(catalog, snapshot)
    class NoModel:
        def evaluate(self, *args):
            raise AssertionError('Inspecting candidates cannot call a model')
    loop = kind(backend, NoModel(), policy='jev', target='rocket_launch', factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    loop.memory = loop.memory_type(snapshot.session_id, 'rocket_launch', active_goal='rocket_launch', last_tick=snapshot.tick)
    return loop, snapshot, catalog, saved


@pytest.mark.parametrize('lead_time_supply', [False, True])
def test_native_composed_frontier_retains_current_iron_pickup(lead_time_supply):
    loop, snapshot, catalog, saved = native_frontier()
    snapshot._lead_time_supply = lead_time_supply
    before = asdict(snapshot)
    original, _ = loop._compile_candidates(snapshot)
    evidence = candidate_evidence(snapshot, catalog, original)
    cable = 'factory:factory_insert:recipe:copper-cable'
    iron = 'factory:factory_extract:recipe:iron-plate'
    assert evidence[cable]['urgency'] == 2 and evidence[cable]['work_scope'] == 'lookahead'
    assert evidence[cable]['recipe_input_transfer_start_evidence'] is None
    assert evidence[iron]['work_scope'] == 'immediate'
    assert evidence[iron]['output_pickup_start_evidence']['planned_pickup_quantity'] == 20
    plans, blocker = loop._work_candidates(snapshot)
    assert not blocker
    assert {iron, cable} <= {p.id for p in plans}
    assert plans[0].id == iron
    assert asdict(snapshot) == before
    assert loop.backend.calls == []


@pytest.mark.parametrize('scope', ['lookahead', 'unclassified', 'stale'])
def test_urgent_lookahead_does_not_restore_noncurrent_annotations(scope):
    loop, snapshot, _, _ = native_frontier()
    original, _ = loop._compile_candidates(snapshot)
    iron = next(p for p in original if p.id == 'factory:factory_extract:recipe:iron-plate')
    cable = next(p for p in original if p.id == 'factory:factory_insert:recipe:copper-cable')
    # Deliberately altered plan metadata is a negative scenario, not native evidence.
    intent = {'scope': 'immediate' if scope == 'stale' else scope,
              'observed_tick': snapshot.tick - (scope == 'stale')}
    changed = replace(iron, materials={**iron.materials, 'work_intent': intent})
    loop._compile_candidates = lambda s: ([changed, cable], '')
    plans, _ = loop._work_candidates(snapshot)
    assert [p.id for p in plans] == [cable.id]


def test_restored_pickup_has_native_evidence_and_fits_actual_model_boundary():
    from jev_factorio.judgments import question_batch, _qualified_output_pickup_chain
    from jev_factorio.planning.decision_support import scheduling_context
    from jev_factorio.planning.bootstrap_chain import catalog_projection
    loop, snapshot, catalog, _ = native_frontier()
    plans, _ = loop._work_candidates(snapshot)
    facts = loop._model_facts(snapshot)
    facts['factory'].pop('receipts', None)
    facts['factory'].pop('connectors', None)
    facts['factory']['recipe_dependency_catalog'] = catalog_projection(snapshot, catalog, plans)
    state = {'facts': facts, 'history': [], 'active_goal': 'rocket_launch',
             **scheduling_context(snapshot, catalog, plans, 'rocket_launch')}
    iron = next(p for p in plans if p.id == 'factory:factory_extract:recipe:iron-plate')
    assert _qualified_output_pickup_chain(iron, facts, state['candidate_evidence'][iron.id])
    _, questions, offered = question_batch(state, plans, max_bytes=48000)
    assert iron in offered
    assert 'output_pickup_start_evidence' in questions[iron.id + '/useful_progress']['instructions']


def test_original_rejected_refill_response_is_not_reinterpreted():
    from jev_factorio.judgments import select_plan
    from jev_factorio.skills import Plan
    _, _, _, saved = native_frontier()
    state = saved['recorded_request']['state']
    plans = [Plan.from_dict(p) for p in state['candidate_plans'].values()]
    class Recorded:
        def evaluate(self, *args): return deepcopy(saved['recorded_answers'])
    result = select_plan(Recorded(), state, plans, max_bytes=48000)
    assert result.plan_id is None and result.reason == 'Candidate evidence insufficient'
