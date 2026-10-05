from copy import deepcopy
import json
from pathlib import Path
import pytest

from jev_factorio.state import GameSnapshot
from jev_factorio.skills import Plan
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.planning.bootstrap_chain import catalog_projection
from jev_factorio.judgments import question_batch, select_plan, _qualified_output_pickup_chain
from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import outpost_loop_type


def frontier():
    saved = json.loads((Path(__file__).parent / 'fixtures/native-v17-owned-output.json').read_text())
    snapshot = GameSnapshot(**saved['facts'])
    snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    snapshot._atomic_inventory_verified = (snapshot.session_id, snapshot.tick)
    catalog = Catalog.from_dict(saved['catalog'])
    plans = [Plan.from_dict(p) for p in saved['plans']]
    support = scheduling_context(snapshot, catalog, plans, 'rocket_launch')
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    facts = kind._model_facts(object.__new__(kind), snapshot)
    facts['factory']['recipe_dependency_catalog'] = catalog_projection(snapshot, catalog, plans)
    receipts = facts['factory'].pop('receipts')
    facts['factory'].pop('connectors', None)
    facts['factory']['native_transfer_receipt_count'] = len(receipts)
    state = {'facts': facts, 'history': [], 'active_goal': 'rocket_launch', **support}
    return snapshot, catalog, plans, state, saved


def test_native_one_plate_pickup_has_current_partial_recipe_demand():
    _, _, plans, state, saved = frontier()
    plan = plans[0]
    row = state['candidate_evidence'][plan.id]
    proof = row['output_pickup_start_evidence']
    assert proof['current_input_demand'] == {
        'required_carried_quantity': 40, 'carried_quantity': 9, 'remaining_deficit': 31}
    assert [(e['product'], e['batches'], e['input_inventory_target'])
            for e in proof['recipe_dependency_chain']['edges']] == [
                ('automation-science-pack', 20, 20), ('iron-gear-wheel', 20, 40)]
    assert proof['planned_pickup_quantity'] == proof['ready_output_quantity_now'] == 1
    assert _qualified_output_pickup_chain(plan, state['facts'], row)
    context, questions, offered = question_batch(state, plans, max_bytes=48000)
    assert offered == plans
    assert len(json.dumps({'state': context, 'questions': questions}, ensure_ascii=False).encode()) <= 48000
    assert 'current_input_demand' in questions[plan.id + '/useful_progress']['instructions']
    class Recorded:
        def evaluate(self, *args): return deepcopy(saved['recorded_answers'])
    decision = select_plan(Recorded(), state, plans, max_bytes=48000)
    assert decision.plan_id is None
    assert decision.reason == 'Candidate evidence insufficient'


@pytest.mark.parametrize('change', ['tick', 'session', 'owner', 'stock', 'quantity',
    'inventory', 'satisfied', 'recipe', 'catalog', 'local', 'deficit', 'bool', 'lookahead'])
def test_changed_native_demand_or_identity_fails_closed(change):
    _, _, plans, state, _ = frontier()
    plan = plans[0]; row = state['candidate_evidence'][plan.id]
    facts = state['facts']; proof = row['output_pickup_start_evidence']
    if change == 'tick': facts['factory']['recipe_dependency_catalog']['tick'] += 1
    elif change == 'session': proof['session_id'] = 'different'
    elif change == 'owner': facts['factory']['production_sites']['sources']['recipe:iron-plate']['source_unit'] += 1
    elif change == 'stock': facts['factory']['entities']['recipe:iron-plate']['output'] = {}
    elif change == 'quantity': plan.steps[0].parameters['quantity'] += 1
    elif change == 'inventory': facts['inventory']['iron-plate'] += 1
    elif change == 'satisfied': facts['inventory']['automation-science-pack'] = 20
    elif change == 'recipe': proof['recipe_dependency_chain']['edges'][0]['recipe']['ingredients'][0]['amount'] += 1
    elif change == 'catalog': facts['factory']['recipe_dependency_catalog']['version'] = 'wrong'
    elif change == 'local': row['local_target']['inventory_target'] += 1
    elif change == 'deficit': proof['current_input_demand']['remaining_deficit'] += 1
    elif change == 'bool': proof['planned_pickup_quantity'] = True
    else: row['work_scope'] = 'lookahead'
    assert not _qualified_output_pickup_chain(plan, facts, row)
    _, questions, _ = question_batch(state, plans, max_bytes=48000)
    assert 'current_input_demand' not in questions[plan.id + '/useful_progress']['instructions']


def test_evidence_never_mutates_native_snapshot_or_catalog():
    snapshot, catalog, plans, state, _ = frontier()
    before = deepcopy(snapshot.__dict__); native = deepcopy(catalog.recipes)
    proof = state['candidate_evidence'][plans[0].id]['output_pickup_start_evidence']
    proof['recipe_dependency_chain']['edges'][0]['recipe']['ingredients'][0]['amount'] = 999
    assert snapshot.__dict__ == before and catalog.recipes == native
    assert not _qualified_output_pickup_chain(plans[0], state['facts'], state['candidate_evidence'][plans[0].id])


def test_controller_includes_current_catalog_for_ordinary_pickup_without_bootstrap():
    from test_capital_investments import Backend
    snapshot, catalog, plans, _, saved = frontier()
    backend = Backend(catalog, snapshot)
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    class Recorded:
        calls = 0
        def evaluate(self, context, questions):
            self.calls += 1
            projection = context['facts']['factory']['recipe_dependency_catalog']
            assert projection['tick'] == snapshot.tick
            assert 'iron-gear-wheel' in projection['recipes']
            assert 'current_input_demand' in questions[plans[0].id + '/useful_progress']['instructions']
            return deepcopy(saved['recorded_answers'])
    model = Recorded()
    loop = kind(backend, model, policy='jev', target='rocket_launch',
                factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    loop.memory = loop.memory_type(snapshot.session_id, 'rocket_launch',
        active_goal='rocket_launch', last_tick=snapshot.tick)
    # Replay a captured, already coherent observation without adopting its live ledger.
    loop._observe = lambda *args, **kwargs: deepcopy(snapshot)
    loop._refresh_goals = lambda s: setattr(loop.memory, 'active_goal', 'rocket_launch')
    loop._work_candidates = lambda s: (plans, '')
    result = loop.step()
    assert model.calls == 1 and backend.calls == []
    assert result['action'] == 'observe'
    assert result['decision']['reason'] == 'Candidate evidence insufficient'
