from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import pytest

from jev_factorio.skills import Plan
from jev_factorio.state import GameSnapshot
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.craft_demand import craft_demand, qualified_craft_demand
from jev_factorio.planning.bootstrap_chain import catalog_projection
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.judgments import question_batch, select_plan


def case():
    data = json.loads((Path(__file__).parent/'fixtures/native-v18-craft-demand.json').read_text())
    snapshot = GameSnapshot(**deepcopy(data['facts']))
    catalog = Catalog.from_dict(data['catalog'])
    plans = [Plan.from_dict(p) for p in data['plans']]
    return snapshot, catalog, plans, data


def context(snapshot, catalog, plans):
    facts = snapshot.for_jev()
    facts['factory']['recipe_dependency_catalog'] = catalog_projection(snapshot, catalog, plans)
    return {'facts': facts, 'active_goal': 'rocket_launch', 'history': [],
            **scheduling_context(snapshot, catalog, plans, 'rocket_launch')}


def test_recorded_gear_hold_gets_native_recipe_quantities_without_overriding_rejection():
    snapshot, catalog, plans, data = case(); plan = plans[0]
    state = context(snapshot, catalog, plans)
    proof = state['candidate_evidence'][plan.id]['craft_recipe_demand']
    assert proof['required_product_inventory'] == proof['remaining_product_deficit'] == 20
    assert proof['carried_product'] == 0 and proof['carried_inputs'] == {'iron-plate': 40}
    assert proof['native_craft_recipe']['ingredients'][0]['amount'] == 2
    assert proof['recipe_dependency_chain']['edges'][0]['input_inventory_target'] == 20
    assert qualified_craft_demand(plan, state['facts'], state['candidate_evidence'][plan.id])
    packet, questions, offered = question_batch(state, plans, max_bytes=48000)
    assert offered == plans
    assert 'craft_recipe_demand' in questions[plan.id+'/useful_progress']['instructions']
    assert len(json.dumps({'state': packet, 'questions': questions}).encode()) <= 48000
    class Recorded:
        def evaluate(self, *args): return deepcopy(data['recorded_answers'])
    decision = select_plan(Recorded(), state, plans)
    assert decision.plan_id is None and decision.reason == 'Candidate evidence insufficient'


@pytest.mark.parametrize('change', ['tick', 'session', 'catalog', 'recipe', 'recipe_bool',
    'inventory', 'satisfied', 'path', 'queue', 'actor', 'quantity', 'threshold', 'receipt',
    'scope', 'local', 'hand_category', 'proof', 'cost', 'protocol', 'foreground'])
def test_native_contradictions_remove_the_demand_witness(change):
    snapshot, catalog, plans, _ = case(); plan = plans[0]
    state = context(snapshot, catalog, plans); row = state['candidate_evidence'][plan.id]
    facts = state['facts']; observed = facts['factory']['recipe_dependency_catalog']
    if change == 'tick': observed['tick'] += 1
    elif change == 'session': facts['session_id'] = 'wrong'
    elif change == 'catalog': observed['version'] = 'wrong'
    elif change == 'recipe': observed['recipes']['automation-science-pack']['ingredients'][1]['amount'] = 2
    elif change == 'recipe_bool': observed['recipes']['iron-gear-wheel']['products'][0]['probability'] = True
    elif change == 'inventory': facts['inventory']['iron-plate'] = 39
    elif change == 'satisfied': facts['inventory']['automation-science-pack'] = 20
    elif change == 'path': plan.materials['craft_dependency']['planner_item_path'] = ['copper-plate','iron-gear-wheel']
    elif change == 'queue': facts['factory']['crafting_queue'] = 1
    elif change == 'actor': facts['factory']['player_connected'] = False
    elif change == 'quantity': plan.steps[0].parameters['batches'] = 21
    elif change == 'threshold': plan = replace(plan, steps=(replace(plan.steps[0], threshold=19),))
    elif change == 'receipt': plan.steps[0].parameters['receipt'] = 'different'
    elif change == 'scope': row['work_scope'] = 'lookahead'
    elif change == 'local': row['local_target']['inventory_target'] += 1
    elif change == 'hand_category': observed['hand_categories']['crafting'] = 1
    elif change == 'proof': row['craft_recipe_demand']['remaining_product_deficit'] = 21
    elif change == 'cost': plan.steps[0].costs['iron-plate'] = 39
    elif change == 'protocol': facts['factory']['craft_jobs_protocol'] = 0
    else: plan = replace(plan, steps=(replace(plan.steps[0], action='factory_craft', effect='inventory',
        parameters={k:v for k,v in plan.steps[0].parameters.items() if k != 'receipt'}),))
    assert not qualified_craft_demand(plan, facts, row)
    _, questions, _ = question_batch(state, [plan], max_bytes=48000)
    assert 'craft_recipe_demand' not in questions[plan.id+'/useful_progress']['instructions']


def test_direct_target_and_partial_intermediate_use_current_inventory_demand():
    snapshot, catalog, plans, _ = case(); plan = plans[0]
    snapshot.inventory['iron-gear-wheel'] = 15
    plan = replace(plan, steps=(replace(plan.steps[0], threshold=20, costs={'iron-plate': 10},
        parameters={**plan.steps[0].parameters, 'batches': 5}),))
    proof = craft_demand(snapshot, catalog, plan)
    assert proof['remaining_product_deficit'] == proof['expected_product_after_receipt'] == 5
    snapshot.inventory['iron-gear-wheel'] = 20
    materials = deepcopy(plan.materials)
    materials['craft_dependency'].update(recipe='automation-science-pack', product='automation-science-pack',
                                         planner_item_path=['automation-science-pack'])
    direct = replace(plan, id='science', materials=materials, steps=(replace(plan.steps[0],
        item='automation-science-pack', threshold=20, costs={'iron-gear-wheel': 20, 'copper-plate': 20},
        parameters={**plan.steps[0].parameters, 'recipe': 'automation-science-pack', 'batches': 20}),))
    proof = craft_demand(snapshot, catalog, direct)
    assert proof['recipe_dependency_chain'] is None and proof['remaining_product_deficit'] == 20
    state = context(snapshot, catalog, [direct])
    assert qualified_craft_demand(direct, state['facts'], state['candidate_evidence']['science'])


def test_native_evidence_has_no_alias_to_snapshot_or_catalog():
    snapshot, catalog, plans, _ = case()
    before = deepcopy(snapshot.__dict__); recipes = deepcopy(catalog.recipes)
    proof = craft_demand(snapshot, catalog, plans[0])
    proof['native_craft_recipe']['ingredients'][0]['amount'] = 999
    proof['recipe_dependency_chain']['edges'][0]['recipe']['ingredients'][0]['amount'] = 999
    assert snapshot.__dict__ == before and catalog.recipes == recipes


def test_actual_controller_request_projects_craft_catalog_and_preserves_strict_gate():
    from test_capital_investments import Backend
    snapshot, catalog, plans, data = case(); backend = Backend(catalog, snapshot)
    from jev_factorio.controller import HierarchicalLoop
    kind = HierarchicalLoop
    class Recorded:
        calls = 0
        def evaluate(self, packet, questions):
            self.calls += 1
            assert 'craft_recipe_demand' in questions[plans[0].id+'/useful_progress']['instructions']
            assert 'iron-gear-wheel' in packet['facts']['factory']['recipe_dependency_catalog']['recipes']
            return deepcopy(data['recorded_answers'])
    model = Recorded(); loop = kind(backend, model, policy='jev', target='rocket_launch',
                                   factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    loop.memory = loop.memory_type(snapshot.session_id, 'rocket_launch', active_goal='rocket_launch', last_tick=snapshot.tick)
    loop._observe = lambda *args, **kwargs: deepcopy(snapshot)
    loop._refresh_goals = lambda s: setattr(loop.memory, 'active_goal', 'rocket_launch')
    loop._work_candidates = lambda s: (plans, '')
    result = loop.step()
    assert model.calls == 1 and backend.calls == [] and result['action'] == 'observe'
    assert result['decision']['reason'] == 'Candidate evidence insufficient'
