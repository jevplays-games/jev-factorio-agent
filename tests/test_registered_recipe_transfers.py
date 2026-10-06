"""Native assembler refill regression and explicitly simulated later output."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.state import GameSnapshot
from jev_factorio.skills import Plan
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.planning.bootstrap_chain import catalog_projection
from jev_factorio.judgments import (_qualified_recipe_transfer_chain,
    _qualified_output_pickup_chain, question_batch, select_plan)
from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import outpost_loop_type


def native():
    fixtures = Path(__file__).parent / 'fixtures'
    saved = json.loads((fixtures / 'native-v26-assembler-refill.json').read_text())
    snapshot = GameSnapshot(**saved['snapshot'])
    catalog = Catalog.from_dict(saved['catalog'])
    assert sorted(name for name, tech in catalog.technologies.items() if tech['researched']) == sorted(snapshot.researched)
    plan = Plan.from_dict(saved['recorded_plan'])
    return snapshot, catalog, plan, saved


def state_for(snapshot, catalog, plan):
    support = scheduling_context(snapshot, catalog, [plan], 'rocket_launch')
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    facts = kind._model_facts(object.__new__(kind), snapshot)
    receipts = facts['factory'].pop('receipts', {})
    facts['factory'].pop('connectors', None)
    facts['factory']['native_transfer_receipt_count'] = len(receipts)
    facts['factory']['recipe_dependency_catalog'] = catalog_projection(snapshot, catalog, [plan])
    return {'facts': facts, 'history': [], 'active_goal': 'rocket_launch', **support}


def test_native_registered_assembler_refill_has_current_evidence():
    snapshot, catalog, plan, saved = native()
    before = deepcopy(snapshot.__dict__)
    assert 'recipe:copper-cable' not in snapshot.factory['production_sites']['sources']
    state = state_for(snapshot, catalog, plan)
    row = state['candidate_evidence'][plan.id]
    proof = row['recipe_input_transfer_start_evidence']
    assert proof['owned_source_unit'] == 2580
    assert proof['paid_quantity_to_transfer'] == 20
    assert _qualified_recipe_transfer_chain(plan, state['facts'], row)
    assert snapshot.__dict__ == before
    assert json.loads(json.dumps(plan.to_dict())) == saved['recorded_plan']
    _, questions, offered = question_batch(state, [plan], max_bytes=48000)
    assert offered == [plan]
    assert 'recipe_dependency_chain' in questions[plan.id + '/useful_progress']['instructions']
    class Recorded:
        def evaluate(self, *args): return deepcopy(saved['recorded_answers'])
    # The rejected response stays rejected. Better evidence is not permission
    # to reinterpret a prior low-confidence answer or change policy thresholds.
    decision = select_plan(Recorded(), state, [plan], max_bytes=48000)
    assert decision.plan_id is None and decision.reason == 'Candidate evidence insufficient'


@pytest.mark.parametrize('change', ['unit', 'recipe', 'stock', 'tick', 'world',
    'category', 'electric', 'burner', 'site_conflict', 'player', 'quantity', 'catalog'])
def test_assembler_refill_rejects_changed_facts(change):
    snapshot, catalog, plan, _ = native()
    state = state_for(snapshot, catalog, plan)
    facts = state['facts']; machine = facts['factory']['entities']['recipe:copper-cable']
    prototype = facts['factory']['recipe_dependency_catalog']['machines']['assembling-machine-1']
    if change == 'unit': machine['unit_number'] += 1
    elif change == 'recipe': machine['recipe'] = 'iron-gear-wheel'
    elif change == 'stock': facts['inventory']['copper-plate'] = 0
    elif change == 'tick': facts['factory']['tick'] -= 1
    elif change == 'world': facts['world_kind'] = 'mock'
    elif change == 'category': prototype['categories']['crafting'] = False
    elif change == 'electric': prototype['electric'] = 1
    elif change == 'burner': prototype['burner'] = True
    elif change == 'site_conflict': facts['factory']['production_sites']['sources']['recipe:copper-cable'] = {'state': 'owned', 'source_unit': 999}
    elif change == 'player': facts['factory']['player_bound'] = False
    elif change == 'quantity': plan.steps[0].parameters['quantity'] += 1
    else: facts['factory']['recipe_dependency_catalog']['session_id'] = 'other'
    assert not _qualified_recipe_transfer_chain(plan, facts, state['candidate_evidence'][plan.id])


def simulated_output():
    snapshot, catalog, _, _ = native()
    # Future output is a test scenario, not a production observation.
    snapshot.factory['entities']['recipe:copper-cable']['output'] = {'copper-cable': 20}
    from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
    plans = MiningOutpostPlanner(catalog, snapshot, 'rocket_launch').candidates()
    plan = next(p for p in plans if p.id == 'factory:factory_extract:recipe:copper-cable')
    return snapshot, catalog, plan


def test_simulated_assembler_output_uses_same_native_identity():
    snapshot, catalog, plan = simulated_output()
    state = state_for(snapshot, catalog, plan)
    row = state['candidate_evidence'][plan.id]
    assert row['output_pickup_start_evidence']['owned_source_unit'] == 2580
    assert _qualified_output_pickup_chain(plan, state['facts'], row)
    projection = state['facts']['factory']['recipe_dependency_catalog']
    assert projection['machines']['assembling-machine-1'] == catalog.machines['assembling-machine-1']
    state['facts']['factory']['entities']['recipe:copper-cable']['recipe'] = 'iron-gear-wheel'
    assert not _qualified_output_pickup_chain(plan, state['facts'], row)


def test_missing_ore_site_cannot_use_registered_machine_path():
    from test_recipe_transfer_chain import frontier
    _, _, plans, rows, state, _ = frontier()
    plan = next(p for p in plans if p.id == 'factory:factory_insert:recipe:iron-plate')
    del state['facts']['factory']['production_sites']['sources']['recipe:iron-plate']
    assert not _qualified_recipe_transfer_chain(plan, state['facts'], rows[plan.id])


def test_composed_controller_supplies_refill_catalog_and_keeps_recorded_refusal():
    from test_capital_investments import Backend
    snapshot, catalog, plan, saved = native()
    backend = Backend(catalog, snapshot)
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    class Recorded:
        calls = 0
        def evaluate(self, context, questions):
            self.calls += 1
            assert context['facts']['factory']['recipe_dependency_catalog']['tick'] == snapshot.tick
            assert 'recipe_dependency_chain' in questions[plan.id + '/useful_progress']['instructions']
            return deepcopy(saved['recorded_answers'])
    model = Recorded()
    loop = kind(backend, model, policy='jev', target='rocket_launch',
                factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    loop.memory = loop.memory_type(snapshot.session_id, 'rocket_launch',
        active_goal='rocket_launch', last_tick=snapshot.tick)
    loop._observe = lambda *args, **kwargs: deepcopy(snapshot)
    loop._refresh_goals = lambda s: setattr(loop.memory, 'active_goal', 'rocket_launch')
    loop._work_candidates = lambda s: ([plan], '')
    result = loop.step()
    assert model.calls == 1 and backend.calls == []
    assert result['action'] == 'observe'
    assert result['decision']['reason'] == 'Candidate evidence insufficient'
