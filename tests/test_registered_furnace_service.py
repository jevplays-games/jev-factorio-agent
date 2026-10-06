"""Captured native furnace stop plus explicitly simulated service scenarios."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.state import GameSnapshot
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.planning.output_buffers import OutputBufferPlanner
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.planning.decision_support import _recipe_source_matches, candidate_evidence

ROLE = 'recipe:steel-plate'


def native():
    saved = json.loads((Path(__file__).parent / 'fixtures/native-v28-registered-furnace.json').read_text())
    snapshot = GameSnapshot(**saved['snapshot'])
    catalog = Catalog.from_dict(saved['catalog'])
    assert saved['accepted_validation']['payload']['accepted'] is True
    assert saved['catalog_capture_tick'] > snapshot.tick
    assert sorted(name for name, tech in catalog.technologies.items() if tech['researched']) == sorted(snapshot.researched)
    snapshot._atomic_inventory_verified = snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    return snapshot, catalog


@pytest.mark.parametrize('kind', [ReadyWorkPlanner, OutputBufferPlanner, InputRoutePlanner, MiningOutpostPlanner])
@pytest.mark.parametrize('lead', [False, True])
def test_captured_native_frontier_no_longer_crashes_on_registered_steel_furnace(kind, lead):
    snapshot, catalog = native()
    snapshot._lead_time_supply = lead
    before = deepcopy(snapshot.__dict__)
    assert ROLE not in snapshot.factory['production_sites']['sources']
    assert snapshot.factory['entities'][ROLE]['recipe'] == ''
    plans = kind(catalog, snapshot, 'rocket_launch').candidates()
    assert plans
    assert snapshot.__dict__ == before


def test_empty_registered_furnace_uses_existing_five_coal_service():
    snapshot, catalog = native()
    planner = ReadyWorkPlanner(catalog, snapshot, 'rocket_launch')
    plan = planner._fuel(ROLE, ('item:steel-plate',))
    assert plan.steps[0].parameters == {'resource': 'coal', 'quantity': 5}
    assert plan.materials['fuel_service']['consumers'] == [
        {'role': ROLE, 'fuel': 0, 'target': 5, 'deficit': 5, 'insertable': None}]
    assert plan.materials['fuel_service']['reserve'] == 0


@pytest.mark.parametrize('coal', [1, 5])
def test_simulated_carried_coal_has_current_transfer_evidence(coal):
    snapshot, catalog = native()
    # This is a future test scenario, not recorded production inventory.
    snapshot.inventory['coal'] = coal
    plan = ReadyWorkPlanner(catalog, snapshot, 'rocket_launch')._fuel(ROLE, ('item:steel-plate',))
    assert plan.steps[0].parameters['quantity'] == coal
    row = candidate_evidence(snapshot, catalog, [plan])[plan.id]
    proof = row['fuel_transfer_start_evidence']
    if coal < 5:
        # Existing evidence requires the entire primary deficit; a partial
        # transfer remains a normal candidate without this stronger proof.
        assert proof is None
    else:
        assert proof['burner_unit'] == 2588 and proof['coal_to_transfer'] == coal
    snapshot.factory['entities'][ROLE]['recipe'] = 'copper-plate'
    assert candidate_evidence(snapshot, catalog, [plan])[plan.id]['fuel_transfer_start_evidence'] is None


@pytest.mark.parametrize('change', ['unit', 'world', 'tick', 'session', 'recipe', 'category',
    'burner', 'electric', 'hidden', 'research', 'site_conflict'])
def test_registered_furnace_identity_rejects_conflicting_or_unsupported_evidence(change):
    snapshot, catalog = native()
    machine = snapshot.factory['entities'][ROLE]
    if change == 'unit': machine['unit_number'] = True
    elif change == 'world': snapshot.world_kind = 'mock'
    elif change == 'tick': snapshot.factory['tick'] -= 1
    elif change == 'session': snapshot.session_id = ''
    elif change == 'recipe': machine['recipe'] = 'copper-plate'
    elif change == 'category': catalog.machines[machine['name']]['categories']['smelting'] = False
    elif change == 'burner': catalog.machines[machine['name']]['burner'] = 1
    elif change == 'electric': catalog.machines[machine['name']]['electric'] = True
    elif change == 'hidden': catalog.recipes['steel-plate']['hidden'] = True
    elif change == 'research':
        catalog.recipes['steel-plate']['enabled'] = False
        snapshot.researched = []
    else: snapshot.factory['production_sites']['sources'][ROLE] = {'state': 'owned', 'source_unit': 999}
    assert not _recipe_source_matches(snapshot, catalog, ROLE, machine)


@pytest.mark.parametrize('field', ['session_id', 'tick', 'protocol'])
def test_furnace_service_rejects_stale_site_observation(field):
    snapshot, catalog = native()
    snapshot.factory['production_sites'][field] = 'invalid'
    with pytest.raises(ValueError, match='observation is stale'):
        ReadyWorkPlanner(catalog, snapshot, 'rocket_launch')._fuel(ROLE, ())


def test_ore_furnace_still_requires_surveyed_identity():
    snapshot, catalog = native()
    role = 'recipe:iron-plate'
    del snapshot.factory['production_sites']['sources'][role]
    with pytest.raises(ValueError, match='current owned source identity'):
        ReadyWorkPlanner(catalog, snapshot, 'rocket_launch')._fuel(role, ())


def test_positive_registered_furnace_fuel_does_not_request_bulk_service():
    snapshot, catalog = native()
    snapshot.factory['entities'][ROLE]['fuel'] = {'coal': 1}
    assert ReadyWorkPlanner(catalog, snapshot, 'rocket_launch')._fuel(ROLE, ()) is None


def test_composed_native_frontier_reaches_unchanged_model_boundary():
    from test_capital_investments import Backend
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    from jev_factorio.planning.decision_support import scheduling_context
    from jev_factorio.planning.bootstrap_chain import catalog_projection
    from jev_factorio.judgments import question_batch
    snapshot, catalog = native()
    class NoModel:
        def evaluate(self, *args):
            raise AssertionError('Candidate inspection cannot call a model')
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    loop = kind(Backend(catalog, snapshot), NoModel(), policy='jev', target='rocket_launch',
                factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    # Inspect planner composition, not a live checkpoint resume or history replay.
    loop.memory = loop.memory_type(snapshot.session_id, 'rocket_launch',
        active_goal='rocket_launch', last_tick=snapshot.tick)
    plans, blocker = loop._work_candidates(snapshot)
    assert not blocker and plans
    facts = loop._model_facts(snapshot)
    facts['factory'].pop('receipts', None)
    facts['factory'].pop('connectors', None)
    facts['factory']['recipe_dependency_catalog'] = catalog_projection(snapshot, catalog, plans)
    state = {'facts': facts, 'history': [], 'active_goal': 'rocket_launch',
             **scheduling_context(snapshot, catalog, plans, 'rocket_launch')}
    _, questions, offered = question_batch(state, plans, max_bytes=48000)
    assert offered and questions and loop.backend.calls == []
