"""Selection regressions are synthetic, not native performance measurements."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from jev_factorio.controller import HierarchicalLoop
from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.jev_client import MockJevClient
from jev_factorio.planning.decision_support import candidate_evidence, distinct_candidates, scheduling_context
from jev_factorio.planning.factory import FactoryPlanner
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.planning.output_buffers import OutputBufferPlanner
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.skills import Plan, Step, compile_plans
from test_factory import FactorySimulation, catalog, machine, recipe, snapshot
from test_causal_trace import Sink, events
from test_deadline_scheduling import scenario
from test_hierarchical import CountingModel


def transfers(state=None, data=None):
    state, data = state or snapshot(), data or catalog()
    state.inventory['iron-plate'] = 0
    state.factory['entities'].update({
        'far': machine('wooden-chest', unit_number=81, position={'x': 50, 'y': 0},
                       output={'iron-plate': 10}),
        'near': machine('wooden-chest', unit_number=82, position={'x': 5, 'y': 0},
                        output={'iron-plate': 10}),
    })
    worker = ReadyWorkPlanner(data, state, 'rocket_launch')
    return state, data, [worker._transfer(role, 'iron-plate', 10, extracting=True)
                         for role in ('far', 'near')]


def test_observed_bootstrap_coal_is_a_local_objective_with_bounded_travel():
    state, data = snapshot(), catalog()
    state.inventory['coal'] = 0
    state.nearby_resources['coal'] = 4
    state.player_position = (0, 0)
    state.factory['fair_resource_targets'] = {
        'coal': {'position': {'x': 3, 'y': 4}}}
    plans, blocker = compile_plans('stockpile_fuel', state)
    assert not blocker and [p.id for p in plans] == [
        'stockpile_fuel:coal:5', 'stockpile_fuel:coal:10']
    support = scheduling_context(state, data, plans, 'stockpile_fuel')
    assert support['local_objective']['kind'] == 'stockpile_fuel'
    assert support['local_objective']['primary_target']['inventory_target'] == 5
    assert support['candidate_evidence'][plans[0].id]['travel_tiles_lower_bound'] == 5
    assert support['candidate_evidence'][plans[0].id]['processed_units'] == 5
    assert support['candidate_evidence'][plans[1].id]['processed_units'] == 10
    assert not support['candidate_evidence'][plans[0].id]['unknowns']
    assert support['candidate_evidence'][plans[0].id]['work_scope'] == 'immediate'
    assert support['candidate_evidence'][plans[1].id]['work_scope'] == 'lookahead'
    assert support['deterministic_ranking'][0] == plans[0].id
    context, questions, offered = question_batch({'facts': state.for_jev(), **support}, plans)
    assert offered == plans
    assert context['local_objective']['primary_target']['item'] == 'coal'
    assert 'five-coal construction buffer' in context['local_objective']['instruction']
    assert 'local_objective' in questions[plans[0].id + '/benefit']['instructions']
    decision = select_plan(MockJevClient(), {'facts': state.for_jev(),
                                              'active_goal': 'stockpile_fuel', **support}, plans)
    assert decision.plan_id == plans[0].id


def test_missing_bootstrap_coal_geometry_stays_unknown_and_absent_coal_has_no_plan():
    state, data = snapshot(), catalog()
    state.inventory['coal'] = 0
    state.nearby_resources['coal'] = 4
    state.factory['fair_resource_targets'] = {}
    plans, _ = compile_plans('stockpile_fuel', state)
    evidence = candidate_evidence(state, data, plans)
    assert evidence[plans[0].id]['travel_tiles_lower_bound'] is None
    assert 'travel:walk_to_coal' in evidence[plans[0].id]['unknowns']
    state.nearby_resources.pop('coal')
    assert compile_plans('stockpile_fuel', state)[0] == []


def test_focus_is_structured_without_mutating_material_bill():
    state, data = snapshot(), catalog()
    worker = ReadyWorkPlanner(data, state, 'rocket_launch')
    plan = worker._need('iron-plate', 20)
    assert plan.materials['local_objective'] == {
        'item': 'iron-plate', 'inventory_target': 20, 'ultimate_goal': 'rocket_launch'}
    assert 'local_objective' not in (worker.materials or {})
    serial = FactoryPlanner(data, state, 'rocket_launch')._need('iron-plate', 20)
    assert 'local_objective' not in (serial.materials or {})


def test_stone_gather_explains_current_lab_recipe_dependency_without_claiming_output():
    state, data = snapshot(), catalog()
    data.recipes['lab'] = recipe('lab', {'iron-plate': 1})
    worker = ReadyWorkPlanner(data, state, 'rocket_launch')
    plan = worker._need('lab', 1)
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters['resource'] == 'stone'
    assert plan.materials['local_objective']['item'] == 'lab'
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['raw_prerequisite'] == {
        'observed_tick': state.tick,
        'direct_recipe': 'stone-furnace',
        'direct_product': 'stone-furnace',
        'planner_item_path': ['lab', 'iron-plate', 'stone-furnace', 'stone'],
        'basis': 'current_planner_dependency_and_native_catalog_recipe',
        'later_steps_require_fresh_native_preconditions': True,
    }
    assert row['gather_start_evidence'] == {
        'observed_tick': state.tick,
        'session_id': state.session_id,
        'resource_in_current_observation': True,
        'fair_target_identity_observed': True,
        'resource_inventory_now': 0,
        'target_inventory_after_this_step': 5,
        'travel_is_lower_bound_not_arrival_proof': True,
    }
    context, questions, offered = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert offered == [plan]
    assert context['candidate_evidence'][plan.id]['gather_start_evidence'] == row['gather_start_evidence']
    assert 'best next action' in questions['candidate']['instructions']
    assert 'ultimate goal' in questions['candidate']['instructions']
    assert 'specific start fact it could resolve now' in questions['candidate']['instructions']
    assert 'Keep observe available' in questions['candidate']['instructions']
    assert 'observed raw resource' in questions[plan.id + '/needs_observation']['instructions']
    assert row['delivers_or_crafts'] == []
    assert row['processed_units_basis'] == 'handling_volume_not_useful_production'
    assert not row['requires_investment']
    assert not plan.steps[0].allowed(snapshot(nearby_resources={}))


def test_choice_prefers_current_recipe_prerequisite_over_unlinked_lookahead_only_when_observed():
    state, data = snapshot(), catalog()
    data.recipes['lab'] = recipe('lab', {'iron-plate': 1})
    worker = ReadyWorkPlanner(data, state, 'rocket_launch')
    worker._set_focus('lab', 1)
    stone = worker._need('lab', 1)
    worker.speculative = True
    iron = worker._need('iron-ore', 36)
    assert stone.steps[0].parameters['resource'] == 'stone'
    assert iron.steps[0].parameters['resource'] == 'iron-ore'
    support = scheduling_context(state, data, [stone, iron], 'rocket_launch')
    immediate = support['candidate_evidence'][stone.id]
    lookahead = support['candidate_evidence'][iron.id]
    assert immediate['work_scope'] == 'immediate'
    assert immediate['raw_prerequisite']['planner_item_path'][0] == 'lab'
    assert lookahead['work_scope'] == 'lookahead' and lookahead['raw_prerequisite'] is None
    assert lookahead['urgency'] == 0

    def choice_instruction(rows, plans=(stone, iron)):
        _, questions, _ = question_batch({'facts': state.for_jev(), **rows}, list(plans))
        return questions['candidate']['instructions']

    assert 'larger pickup quantity alone' in choice_instruction(support)
    assert 'Later crafting and output still require fresh native verification' in choice_instruction(support)
    single = scheduling_context(state, data, [stone], 'rocket_launch')
    assert 'larger pickup quantity alone' not in choice_instruction(single, (stone,))

    stale = deepcopy(support)
    stale['candidate_evidence'][stone.id]['raw_prerequisite']['observed_tick'] -= 1
    assert 'larger pickup quantity alone' not in choice_instruction(stale)
    unknown = deepcopy(support)
    unknown['candidate_evidence'][stone.id]['gather_start_evidence'][
        'fair_target_identity_observed'] = False
    assert 'larger pickup quantity alone' not in choice_instruction(unknown)
    due = deepcopy(support)
    due['candidate_evidence'][iron.id]['urgency'] = 1
    assert 'larger pickup quantity alone' not in choice_instruction(due)
    linked = deepcopy(support)
    linked['candidate_evidence'][iron.id]['raw_prerequisite'] = {
        'observed_tick': state.tick, 'planner_item_path': ['lab', 'iron-ore']}
    assert 'larger pickup quantity alone' not in choice_instruction(linked)


def test_sole_current_iron_ore_prerequisite_has_bounded_level_one_benefit_cue():
    # Native-shaped 0073 frontier: verified belts are carried, but iron ore is
    # still required through the current lab/electronic-circuit/plate path.
    state, data = snapshot(inventory={'transport-belt': 4, 'iron-gear-wheel': 10,
                                      'iron-plate': 4, 'copper-plate': 10,
                                      'iron-ore': 0}), catalog()
    data.recipes['electronic-circuit'] = recipe(
        'electronic-circuit', {'iron-plate': 2})
    data.recipes['lab'] = recipe(
        'lab', {'electronic-circuit': 2, 'transport-belt': 4})
    worker = ReadyWorkPlanner(data, state, 'rocket_launch')
    worker._set_focus('lab', 1)
    plan = worker._need('iron-ore', 6, ('item:lab', 'item:electronic-circuit',
                                       'item:iron-plate'))
    step = plan.steps[0]
    assert step.action == 'factory_gather'
    assert step.parameters == {'resource': 'iron-ore', 'quantity': 6}
    support = scheduling_context(state, data, [plan], 'rocket_launch')
    row = support['candidate_evidence'][plan.id]
    assert row['raw_prerequisite']['planner_item_path'] == [
        'lab', 'electronic-circuit', 'iron-plate', 'iron-ore']
    assert row['raw_prerequisite']['direct_recipe'] == 'iron-plate'
    assert row['gather_start_evidence']['resource_in_current_observation'] is True
    assert row['gather_start_evidence']['fair_target_identity_observed'] is True
    assert row['unknowns'] == row['reasons'] == []
    cue = 'supplies a useful recipe input (level 1)'

    def benefit(rows=support, plans=(plan,)):
        context, questions, _ = question_batch(
            {'facts': state.for_jev(), **rows}, list(plans))
        assert len(json.dumps({'state': context, 'questions': questions},
                              ensure_ascii=False, allow_nan=False).encode('utf-8')) <= 32000
        return questions[plan.id + '/benefit']['instructions']

    assert cue in benefit()
    assert 'independent current blocker fact' in benefit()
    assert 'later recipe steps still require fresh verification' in benefit()

    def no_cue(changed):
        assert cue not in benefit(changed)

    for mutate in (
        lambda rows: rows['candidate_evidence'][plan.id]['raw_prerequisite']
            .__setitem__('observed_tick', state.tick - 1),
        lambda rows: rows['candidate_evidence'][plan.id]['raw_prerequisite']
            .__setitem__('planner_item_path', ['other', 'iron-plate', 'iron-ore']),
        lambda rows: rows['candidate_evidence'][plan.id]['raw_prerequisite']
            .__setitem__('direct_product', 'copper-plate'),
        lambda rows: rows['candidate_evidence'][plan.id]['gather_start_evidence']
            .__setitem__('fair_target_identity_observed', False),
        lambda rows: rows['candidate_evidence'][plan.id]['gather_start_evidence']
            .__setitem__('target_inventory_after_this_step', 7),
        lambda rows: rows['candidate_evidence'][plan.id]
            .__setitem__('unknowns', ['travel:factory_gather']),
        lambda rows: rows['candidate_evidence'][plan.id]
            .__setitem__('reasons', ['native_target_disputed']),
        lambda rows: rows['candidate_evidence'][plan.id]
            .__setitem__('urgency', 1),
        lambda rows: rows['candidate_evidence'][plan.id]
            .__setitem__('urgency', False),
        lambda rows: rows['candidate_evidence'][plan.id]
            .__setitem__('work_scope', 'lookahead'),
        lambda rows: rows['candidate_evidence'][plan.id]['local_target']
            .__setitem__('item', 'other'),
    ):
        altered = deepcopy(support)
        mutate(altered)
        no_cue(altered)
    malformed = deepcopy(support)
    malformed['candidate_evidence'][plan.id]['gather_start_evidence'] = 'observed'
    no_cue(malformed)
    other = replace(plan, id='unrelated-gather')
    assert cue not in benefit(support, (plan, other))


@pytest.mark.parametrize('planner_type', [ReadyWorkPlanner, OutputBufferPlanner,
                                         MiningOutpostPlanner])
def test_ready_lookahead_gear_craft_exposes_bounded_shared_bill_and_possible_overlap(planner_type):
    """Lab needs two more gears through belts; neither plan has completed output."""
    state = snapshot(inventory={'iron-plate': 10, 'iron-gear-wheel': 10},
                     nearby_resources={'copper-ore': 94, 'coal': 80, 'stone': 38})
    state.factory['craft_jobs_protocol'] = 1
    state.factory['entities']['recipe:copper-plate'] = machine(
        fuel={'coal': 5}, energy=100)
    for key in ('output_buffers', 'input_routes', 'mining_outposts'):
        state.factory[key] = {'protocol': 1, 'session_id': state.session_id,
                              'tick': state.tick, 'sources': {}}
    data = catalog()
    data.recipes.update({
        'lab': recipe('lab', {'electronic-circuit': 10, 'iron-gear-wheel': 10,
                             'transport-belt': 4}),
        'electronic-circuit': recipe('electronic-circuit', {'copper-cable': 3,
                                                          'iron-plate': 1}),
        'copper-cable': recipe('copper-cable', {'copper-plate': 1}),
        'copper-plate': recipe('copper-plate', {'copper-ore': 1}, 'smelting'),
        'iron-gear-wheel': recipe('iron-gear-wheel', {'iron-plate': 2}),
        'transport-belt': recipe('transport-belt', {'iron-plate': 1,
                                                  'iron-gear-wheel': 1}),
    })
    data.recipes['transport-belt']['products'][0]['amount'] = 2
    worker = planner_type(data, state, 'rocket_launch')
    worker._set_focus('lab', 1)
    primary = worker._need('lab', 1)
    worker.plan = lambda: primary
    plans = worker.candidates()
    assert plans[0].steps[0].action == 'factory_gather'
    assert plans[0].steps[0].item == 'copper-ore'
    gear = next(plan for plan in plans if plan.steps[0].item == 'iron-gear-wheel')
    assert gear.materials['shared_bill_craft'] == {
        'observed_tick': state.tick, 'local_target_item': 'lab',
        'local_target_amount': 1, 'craft_item': 'iron-gear-wheel',
        'bill_inventory_target': 12, 'inventory_now': 10,
        'planned_product_units': 2,
        'basis': 'current_catalog_shared_material_bill',
    }
    step = gear.steps[0]
    gear = replace(gear, steps=(replace(step, action='factory_craft_job',
        effect='craft_job_complete', parameters={**step.parameters, 'receipt': 'gear-test'}),))
    plans = [plans[0], gear]
    support = scheduling_context(state, data, plans, 'rocket_launch')
    row = support['candidate_evidence'][gear.id]
    assert row['local_target'] == gear.materials['local_objective']
    assert row['craft_dependency'] is None
    assert row['shared_bill_craft']['unfilled_bill_units'] == 2
    assert row['shared_bill_craft']['forecast_is_not_paid_stock_or_completed_output'] is True
    assert row['shared_bill_craft']['background_overlap_requires_native_admission'] is True
    context, questions, offered = question_batch(
        {'facts': state.for_jev(), **support}, plans)
    assert offered == plans
    assert len(json.dumps({'state': context, 'questions': questions},
                          ensure_ascii=False, allow_nan=False).encode()) <= 32000
    assert 'admitted receipt-tracked job' in questions['candidate']['instructions']
    assert 'bounded catalog bill shortfall' in questions[gear.id + '/benefit']['instructions']
    assert row['delivers_or_crafts'] == []

    for changed in (
            {'observed_tick': state.tick - 1},
            {'bill_inventory_target': 13},
            {'inventory_now': 9},
            {'planned_product_units': 1},
            {'local_target_item': 'unrelated'}):
        bad = replace(gear, materials={**gear.materials, 'shared_bill_craft': {
            **gear.materials['shared_bill_craft'], **changed}})
        assert candidate_evidence(state, data, [bad])[bad.id]['shared_bill_craft'] is None
    no_ready = deepcopy(state)
    no_ready.factory['crafting_queue'] = 1
    assert candidate_evidence(no_ready, data, [gear])[gear.id]['shared_bill_craft'] is None
    no_bill = replace(gear, materials={**gear.materials, 'batches': 'malformed'})
    assert candidate_evidence(state, data, [no_bill])[no_bill.id]['shared_bill_craft'] is None
    alone = scheduling_context(state, data, [gear], 'rocket_launch')
    _, alone_questions, _ = question_batch({'facts': state.for_jev(), **alone}, [gear])
    assert 'admitted receipt-tracked job' not in alone_questions['candidate']['instructions']


def test_composed_planner_keeps_native_shaped_belt_bill_witness():
    state = snapshot(inventory={'iron-gear-wheel': 12, 'iron-plate': 6},
                     nearby_resources={'iron-ore': 50, 'coal': 80, 'stone': 38})
    state.factory['craft_jobs_protocol'] = 1
    state.factory['entities']['recipe:iron-plate'] = machine(fuel={'coal': 5}, energy=100)
    for key in ('output_buffers', 'input_routes', 'mining_outposts'):
        state.factory[key] = {'protocol': 1, 'session_id': state.session_id,
                              'tick': state.tick, 'sources': {}}
    data = catalog()
    data.recipes.update({
        'lab': recipe('lab', {'electronic-circuit': 10, 'iron-gear-wheel': 10,
                             'transport-belt': 4}),
        'electronic-circuit': recipe('electronic-circuit', {'iron-plate': 1}),
        'iron-plate': recipe('iron-plate', {'iron-ore': 1}, 'smelting'),
        'iron-gear-wheel': recipe('iron-gear-wheel', {'iron-plate': 2}),
        'transport-belt': recipe('transport-belt', {'iron-plate': 1,
                                                  'iron-gear-wheel': 1}),
    })
    data.recipes['transport-belt']['products'][0]['amount'] = 2
    worker = MiningOutpostPlanner(data, state, 'rocket_launch')
    worker._set_focus('lab', 1)
    primary = worker._need('lab', 1)
    worker.plan = lambda: primary
    plans = worker.candidates()
    belt = next(plan for plan in plans if plan.steps[0].item == 'transport-belt')
    assert belt.steps[0].action == 'factory_craft'
    assert belt.steps[0].parameters['batches'] == 2
    assert belt.materials['shared_bill_craft']['bill_inventory_target'] == 4
    assert belt.materials['shared_bill_craft']['planned_product_units'] == 4
    step = belt.steps[0]
    belt = replace(belt, steps=(replace(step, action='factory_craft_job',
        effect='craft_job_complete', parameters={**step.parameters, 'receipt': 'belt-test'}),))
    row = candidate_evidence(state, data, [primary, belt])[belt.id]
    assert row['shared_bill_craft']['unfilled_bill_units'] == 4
    assert row['shared_bill_craft']['expected_products_after_native_verification'] == 4
    assert row['craft_dependency'] is None
    for changed in ({'observed_tick': state.tick - 1},
                    {'bill_inventory_target': 5},
                    {'planned_product_units': 2}):
        bad = replace(belt, materials={**belt.materials, 'shared_bill_craft': {
            **belt.materials['shared_bill_craft'], **changed}})
        assert candidate_evidence(state, data, [bad])[bad.id]['shared_bill_craft'] is None
    worker._buffer_service = True
    assert all(plan.steps[0].item != 'transport-belt' for plan in worker.candidates())


def test_stale_or_unrelated_raw_dependency_never_enters_candidate_evidence():
    state, data = snapshot(), catalog()
    plan = FactoryPlanner(data, state, 'iron_smelting')._need('iron-plate', 10)
    assert candidate_evidence(state, data, [plan])[plan.id]['raw_prerequisite'] is not None
    stale = replace(plan, materials={**plan.materials, 'raw_prerequisite': {
        **plan.materials['raw_prerequisite'], 'observed_tick': state.tick - 1}})
    unrelated = replace(plan, materials={**plan.materials, 'raw_prerequisite': {
        **plan.materials['raw_prerequisite'], 'direct_product': 'lab'}})
    assert candidate_evidence(state, data, [stale])[stale.id]['raw_prerequisite'] is None
    assert candidate_evidence(state, data, [unrelated])[unrelated.id]['raw_prerequisite'] is None
    state.factory['fair_resource_targets'].pop('stone')
    missing_site = candidate_evidence(state, data, [plan])[plan.id]
    assert missing_site['gather_start_evidence']['fair_target_identity_observed'] is False
    assert 'travel:factory_gather' in missing_site['unknowns']


def test_tree_named_wood_target_is_valid_gather_start_evidence():
    state, data = snapshot(), catalog()
    data.recipes['wooden-chest'] = recipe('wooden-chest', {'wood': 2})
    state.nearby_resources['wood'] = 3
    state.factory['fair_resource_targets']['wood'] = {
        'name': 'tree-01', 'surface_index': 1,
        'position': {'x': 3, 'y': 0},
    }
    plan = FactoryPlanner(data, state, 'rocket_launch')._need('wooden-chest', 1)
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters == {'resource': 'wood', 'quantity': 1}
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['gather_start_evidence']['fair_target_identity_observed'] is True
    assert row['raw_prerequisite']['direct_product'] == 'wooden-chest'


def test_receipt_tracked_handcraft_has_current_start_facts_without_fake_travel():
    state, data = snapshot(inventory={'stone': 5}), catalog()
    state.factory['craft_jobs_protocol'] = 1
    plan = FactoryPlanner(data, state, 'iron_smelting')._need('stone-furnace', 1)
    step = plan.steps[0]
    plan = replace(plan, steps=(replace(step, action='factory_craft_job',
        effect='craft_job_complete', parameters={**step.parameters, 'receipt': 'stone-test'}),))
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['unknowns'] == []
    assert row['travel_tiles_lower_bound'] == 0
    assert row['delivers_or_crafts'] == []  # A queued craft has not delivered output.
    assert row['craft_start_evidence'] == {
        'observed_tick': state.tick, 'native_recipe': 'stone-furnace',
        'input_costs_match_native_recipe': True, 'inputs_in_inventory_now': True,
        'recipe_unlocked_and_handcraftable': True,
        'player_connected_and_bound': True, 'crafting_queue_empty': True,
        'craft_job_protocol_ready': True,
        'expected_products_after_native_verification': {'stone-furnace': 1},
        'native_receipt_required_for_completion': True,
    }
    context, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'iron_smelting')},
        [plan])
    assert context['candidate_evidence'][plan.id]['craft_start_evidence'] == row['craft_start_evidence']
    assert 'expected output still needs native verification' in str(questions)
    assert plan.steps[0].allowed(state)

    state.factory['craft_jobs_protocol'] = True  # Bool must not impersonate protocol version 1.
    bad = candidate_evidence(state, data, [plan])[plan.id]['craft_start_evidence']
    assert bad['craft_job_protocol_ready'] is False
    assert not plan.steps[0].allowed(state)
    state.factory['craft_jobs_protocol'] = 1
    state.inventory['stone'] = 0
    bad = candidate_evidence(state, data, [plan])[plan.id]['craft_start_evidence']
    assert bad['inputs_in_inventory_now'] is False
    assert not plan.steps[0].allowed(state)
    stale = replace(plan, materials={'work_intent': {'observed_tick': state.tick - 1}})
    assert candidate_evidence(state, data, [stale])[stale.id]['craft_start_evidence'] is None
    changed = replace(plan, steps=(replace(plan.steps[0], costs={'stone': 4}),))
    assert candidate_evidence(state, data, [changed])[changed.id]['craft_start_evidence'] is None


def test_furnace_craft_keeps_current_lab_planner_provenance_without_claiming_lab():
    state, data = snapshot(inventory={'stone': 5}), catalog()
    data.recipes['lab'] = recipe('lab', {'iron-plate': 1})
    state.factory['craft_jobs_protocol'] = 1
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._need('lab', 1)
    assert plan.steps[0].action == 'factory_craft'
    assert plan.materials['local_objective']['item'] == 'lab'
    assert plan.materials['craft_dependency'] == {
        'observed_tick': state.tick, 'recipe': 'stone-furnace',
        'product': 'stone-furnace',
        'planner_item_path': ['lab', 'iron-plate', 'stone-furnace'],
    }
    step = plan.steps[0]
    plan = replace(plan, steps=(replace(step, action='factory_craft_job',
        effect='craft_job_complete', parameters={**step.parameters, 'receipt': 'lab-test'}),))
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['craft_dependency'] == {
        'observed_tick': state.tick,
        'planner_item_path': ['lab', 'iron-plate', 'stone-furnace'],
        'current_craft_product': 'stone-furnace',
        'basis': 'current_recursive_planner_provenance_and_native_recipe',
        'later_steps_require_fresh_native_preconditions': True,
    }
    assert row['craft_start_evidence']['expected_products_after_native_verification'] == {
        'stone-furnace': 1}
    assert row['delivers_or_crafts'] == []
    context, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert context['candidate_evidence'][plan.id]['craft_dependency'] == row['craft_dependency']
    assert 'Prefer this bounded craft over observe' in questions['candidate']['instructions']
    assert 'current planner-linked intermediate craft' in (
        questions[plan.id + '/benefit']['instructions'])
    assert 'does not establish receipt-conditional closure of an observed' in (
        questions[plan.id + '/benefit']['criteria'][1])
    assert 'handcrafts from carried inputs without changing existing entities' in (
        questions[plan.id + '/disruption']['criteria'][0])
    assert 'later production still need fresh native receipt and precondition checks' in str(questions)
    benefit = questions[plan.id + '/benefit']['instructions']
    observation = questions[plan.id + '/needs_observation']['instructions']
    assert 'level-1 partial progress from this current planner-linked intermediate craft' in benefit
    assert 'current `candidate_evidence`' in benefit
    assert 'output still requires native receipt verification' in observation
    assert 'future completion is not a missing start observation' in observation
    stale = replace(plan, materials={**plan.materials, 'craft_dependency': {
        **plan.materials['craft_dependency'], 'observed_tick': state.tick - 1}})
    assert candidate_evidence(state, data, [stale])[stale.id]['craft_dependency'] is None
    stale_context, stale_questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [stale], 'rocket_launch')},
        [stale])
    assert stale_context['candidate_evidence'][stale.id]['craft_dependency'] is None
    assert 'Prefer this bounded craft over observe' not in stale_questions['candidate']['instructions']
    assert 'bounded intermediate product' not in stale_questions[stale.id + '/benefit']['instructions']
    stale_start = replace(plan, materials={**plan.materials,
        'work_intent': {'observed_tick': state.tick - 1}})
    stale_start_context, stale_start_questions, _ = question_batch(
        {'facts': state.for_jev(),
         **scheduling_context(state, data, [stale_start], 'rocket_launch')},
        [stale_start])
    assert stale_start_context['candidate_evidence'][stale_start.id]['craft_start_evidence'] is None
    assert 'Prefer this bounded craft over observe' not in stale_start_questions['candidate']['instructions']
    assert 'future completion is not a missing start observation' not in (
        stale_start_questions[stale_start.id + '/needs_observation']['instructions'])
    unrelated = replace(plan, materials={**plan.materials, 'craft_dependency': {
        **plan.materials['craft_dependency'], 'planner_item_path': ['unrelated', 'stone-furnace']}})
    assert candidate_evidence(state, data, [unrelated])[unrelated.id]['craft_dependency'] is None
    missing_target = replace(plan, materials={key: value for key, value in plan.materials.items()
                                              if key != 'local_objective'})
    assert candidate_evidence(state, data, [missing_target])[missing_target.id]['craft_dependency'] is None
    empty_target = replace(plan, materials={**plan.materials, 'local_objective': {'item': ''}})
    assert candidate_evidence(state, data, [empty_target])[empty_target.id]['craft_dependency'] is None
    data.recipes['stone-furnace']['enabled'] = False
    assert candidate_evidence(state, data, [plan])[plan.id]['craft_dependency'] is None


def _native_lab_craft_plan(target=1):
    state, data = snapshot(inventory={'iron-plate': 5}), catalog()
    data.recipes['lab'] = recipe('lab', {'iron-plate': 1})
    data.stack_sizes['lab'] = 10
    state.world_kind = 'fle'
    state.factory['craft_jobs_protocol'] = 1
    identity = (state.session_id, state.tick)
    state._coherent_observation_verified = identity
    state._atomic_inventory_verified = identity
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', target)
    plan = planner._need('lab', target)
    step = plan.steps[0]
    plan = replace(plan, steps=(replace(step, action='factory_craft_job',
        effect='craft_job_complete', parameters={**step.parameters, 'receipt': 'lab-target'}),))
    return state, data, plan


def _native_target_gather_plan(target=5, quantity=5, *, scope='immediate'):
    state, data = snapshot(inventory={'coal': 0}), catalog()
    state.world_kind = 'fle'
    identity = (state.session_id, state.tick)
    state._coherent_observation_verified = identity
    state._atomic_inventory_verified = identity
    runtime = {
        'schema': 1, 'session_id': state.session_id, 'speed': 1.0,
        'tick_paused': False, 'actor_unit': 2543, 'player_index': 1,
        'surface_index': 1, 'force_index': 1,
    }
    state.factory.update({
        'tick': state.tick,
        'observation_snapshot_schema': 2,
        'acceptance_runtime': runtime,
        'inventory_insertable': {'coal': 3900},
        'inventory_insertable_evidence': {
            'schema': 1, 'tick': state.tick,
            'inventory': 'character_main', 'quality': 'normal',
            'method': 'get_insertable_count', 'items': {'coal': 3900},
            'session_id': state.session_id, 'actor_unit': 2543,
            'surface_index': 1, 'force_index': 1,
            'basis': 'native_insertable_count_estimate',
        },
    })
    state.factory['fair_resource_targets'] = {
        'coal': {'name': 'coal', 'surface_index': 1,
                 'position': {'x': 62.5, 'y': -25.5}},
    }
    state.nearby_resources['coal'] = 70.0
    step = Step(
        action='factory_gather', effect='inventory', item='coal', threshold=target,
        timeout_ticks=18000,
        parameters={'resource': 'coal', 'quantity': quantity},
    )
    plan = Plan(
        id=f'factory:factory_gather:coal:{quantity}', goal='rocket_launch',
        description=f'Gather {quantity} coal for the current target', steps=(step,),
        materials={
            'local_objective': {
                'item': 'coal', 'inventory_target': target,
                'ultimate_goal': 'rocket_launch',
            },
            'work_intent': {'scope': scope, 'observed_tick': state.tick},
        },
    )
    return state, data, plan


def test_native_direct_target_gather_is_only_conditional_inventory_closure():
    state, data, plan = _native_target_gather_plan()
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['local_target_completion_evidence'] == {
        'observed_tick': state.tick,
        'session_id': state.session_id,
        'target_item': 'coal',
        'target_inventory': 5,
        'inventory_now': 0,
        'shortfall_now': 5,
        'requested_gather_quantity': 5,
        'target_inventory_threshold': 5,
        'insertable_headroom_now': 3900,
        'fair_target_name': 'coal',
        'fair_target_surface_index': 1,
        'requested_quantity_equals_current_shortfall': True,
        'would_close_current_shortfall_if_native_inventory_verifies': True,
        'inventory_basis': 'coherent_snapshot_and_atomic_native_inventory',
        'fresh_native_inventory_threshold_required': True,
        'travel_is_lower_bound_not_arrival_proof': True,
        'forecast_is_not_harvested_output': True,
    }
    assert row['delivers_or_crafts'] == []
    context = {'facts': state.for_jev(),
               **scheduling_context(state, data, [plan], 'rocket_launch')}
    _, questions, offered = question_batch(context, [plan])
    assert offered == [plan]
    benefit = questions[plan.id + '/benefit']
    assert 'only if a fresh native inventory observation confirms the target threshold' in benefit['instructions']
    assert 'does not establish arrival, patch yield, harvested quantity' in benefit['instructions']
    assert 'fresh native postcondition verifies' in benefit['criteria'][2]

    class LowBenefitConfidence(MockJevClient):
        def evaluate(self, context, questions):
            answers = super().evaluate(context, questions)
            # Level 0 ("no demonstrated contribution") is the most probable
            # level; a confident report cannot rescue the candidate.
            answers[plan.id + '/benefit'].update(
                probabilities={'0': 0.6, '1': 0.4, '2': 0.0}, score=0.4,
                confidence=0.95)
            return answers

    decision = select_plan(LowBenefitConfidence(), context, [plan])
    assert decision.plan_id is None
    assert decision.diagnostics['candidate_rejections'][plan.id] == [
        'low_benefit_confidence']


def test_lookahead_gather_cannot_claim_direct_target_closure():
    state, data, plan = _native_target_gather_plan(target=5, quantity=50, scope='lookahead')
    plan = replace(plan, steps=(replace(plan.steps[0], threshold=50),))
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['work_scope'] == 'lookahead'
    assert row['local_target_completion_evidence'] is None


@pytest.mark.parametrize('failure', [
    'missing_coherent_identity', 'missing_atomic_inventory', 'stale_atomic_inventory',
    'missing_fair_target', 'resource_not_current', 'capacity_too_small',
    'stale_capacity', 'unbound_actor', 'target_already_met', 'quantity_not_shortfall',
    'stale_intent',
])
def test_direct_gather_target_evidence_fails_closed_on_stale_or_incomplete_native_facts(failure):
    state, data, plan = _native_target_gather_plan()
    if failure == 'missing_coherent_identity':
        del state._coherent_observation_verified
    elif failure == 'missing_atomic_inventory':
        del state._atomic_inventory_verified
    elif failure == 'stale_atomic_inventory':
        state._atomic_inventory_verified = (state.session_id, state.tick - 1)
    elif failure == 'missing_fair_target':
        state.factory['fair_resource_targets'] = {}
    elif failure == 'resource_not_current':
        state.nearby_resources.pop('coal')
    elif failure == 'capacity_too_small':
        state.factory['inventory_insertable']['coal'] = 4
        state.factory['inventory_insertable_evidence']['items']['coal'] = 4
    elif failure == 'stale_capacity':
        state.factory['inventory_insertable_evidence']['tick'] -= 1
    elif failure == 'unbound_actor':
        state.factory['player_bound'] = False
    elif failure == 'target_already_met':
        state.inventory['coal'] = 5
    elif failure == 'quantity_not_shortfall':
        plan = replace(plan, steps=(replace(
            plan.steps[0], parameters={'resource': 'coal', 'quantity': 4}),))
    else:
        plan = replace(plan, materials={**plan.materials, 'work_intent': {
            **plan.materials['work_intent'], 'observed_tick': state.tick - 1}})
    assert candidate_evidence(state, data, [plan])[plan.id][
        'local_target_completion_evidence'] is None


def test_native_direct_target_craft_proves_only_conditional_shortfall_closure():
    state, data, plan = _native_lab_craft_plan()
    # The qualified atomic snapshot is complete, so an absent sparse-map key is zero.
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['local_target'] == plan.materials['local_objective']
    evidence = row['local_target_completion_evidence']
    assert evidence == {
        'observed_tick': state.tick, 'session_id': state.session_id,
        'target_item': 'lab', 'target_inventory': 1, 'inventory_now': 0,
        'shortfall_now': 1, 'expected_output_after_native_receipt': 1,
        'shortfall_after_expected_output': 0,
        'would_close_current_shortfall_if_native_receipt_verifies': True,
        'native_recipe': 'lab', 'native_batches': 1,
        'target_item_stack_size': 10,
        'inventory_basis': 'coherent_snapshot_and_atomic_craft_inventory',
        'native_receipt_required_for_completion': True,
        'forecast_is_not_completed_output': True,
    }
    assert row['delivers_or_crafts'] == []
    _, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    benefit = questions[plan.id + '/benefit']
    assert 'current local-target shortfall would close' in benefit['instructions']
    assert 'only after its native receipt verifies' in benefit['criteria'][2]
    assert 'future research' in benefit['instructions']
    assert 'level-1 partial progress' not in benefit['instructions']
    assert 'local-target shortfall or establish blocker removal' not in benefit['instructions']


def test_native_direct_target_craft_that_leaves_shortfall_open_is_partial():
    state, data, plan = _native_lab_craft_plan()
    plan = replace(plan, materials={**plan.materials, 'local_objective': {
        **plan.materials['local_objective'], 'inventory_target': 2}})
    row = candidate_evidence(state, data, [plan])[plan.id]
    evidence = row['local_target_completion_evidence']
    assert evidence['target_inventory'] == 2
    assert evidence['shortfall_now'] == 2
    assert evidence['expected_output_after_native_receipt'] == 1
    assert evidence['shortfall_after_expected_output'] == 1
    assert evidence['would_close_current_shortfall_if_native_receipt_verifies'] is False
    _, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    benefit = questions[plan.id + '/benefit']
    assert 'leaves the current local-target shortfall open' in benefit['instructions']
    assert 'does not establish receipt-conditional closure of an observed' in benefit['criteria'][1]


@pytest.mark.parametrize('failure', [
    'no_native_inventory_contract', 'stale_tick', 'stale_session',
    'invalid_inventory_entry', 'missing_stack_size', 'fractional_recipe_output',
    'unsupported_multi_product_recipe', 'unqualified_start', 'stale_intent',
])
def test_native_target_completion_evidence_fails_closed_on_unknown_or_stale_facts(failure):
    state, data, plan = _native_lab_craft_plan()
    if failure == 'no_native_inventory_contract':
        del state._atomic_inventory_verified
    elif failure == 'stale_tick':
        state._atomic_inventory_verified = (state.session_id, state.tick - 1)
    elif failure == 'stale_session':
        state._coherent_observation_verified = ('other-session', state.tick)
    elif failure == 'invalid_inventory_entry':
        state.inventory['unknown-item-count'] = True
    elif failure == 'missing_stack_size':
        data.stack_sizes.pop('lab')
    elif failure == 'fractional_recipe_output':
        data.recipes['lab']['products'][0]['amount'] = 0.5
    elif failure == 'unsupported_multi_product_recipe':
        data.recipes['lab']['products'].append(
            {'name': 'iron-gear-wheel', 'amount': 1, 'type': 'item', 'probability': 1})
    elif failure == 'unqualified_start':
        state.factory['player_bound'] = False
    else:
        plan = replace(plan, materials={**plan.materials, 'work_intent': {
            **plan.materials['work_intent'], 'observed_tick': state.tick - 1}})
    assert candidate_evidence(state, data, [plan])[plan.id][
        'local_target_completion_evidence'] is None


def test_native_target_already_met_is_not_scored_as_target_closure():
    state, data, plan = _native_lab_craft_plan()
    state.inventory['lab'] = 1
    row = candidate_evidence(state, data, [plan])[plan.id]
    evidence = row['local_target_completion_evidence']
    assert evidence['inventory_now'] == evidence['target_inventory'] == 1
    assert evidence['shortfall_now'] == 0
    assert evidence['would_close_current_shortfall_if_native_receipt_verifies'] is False
    _, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert 'target is already met before this craft' in (
        questions[plan.id + '/benefit']['instructions'])


def test_native_integer_valued_float_product_is_counted_as_exact_item_quantity():
    state, data, plan = _native_lab_craft_plan()
    data.recipes['lab']['products'][0]['amount'] = 1.0
    evidence = candidate_evidence(state, data, [plan])[plan.id][
        'local_target_completion_evidence']
    assert evidence['expected_output_after_native_receipt'] == 1
    assert evidence['would_close_current_shortfall_if_native_receipt_verifies'] is True


def test_local_benefit_score_levels_exclude_blocker_removal_from_partial_level():
    state, data, plan = _native_lab_craft_plan()
    context = {'facts': state.for_jev(),
               **scheduling_context(state, data, [plan], 'rocket_launch')}
    _, questions, _ = question_batch(context, [plan])
    criteria = questions[plan.id + '/benefit']['criteria']
    assert 'does not remove a separately evidenced current blocker or due starvation' in criteria[1]
    assert 'separate same-tick evidence shows it directly removes a specific observed blocker' in criteria[2]

    legacy_context, legacy_questions, _ = question_batch(
        {'facts': state.for_jev(), 'active_goal': 'rocket_launch'}, [plan])
    assert legacy_questions[plan.id + '/benefit']['criteria'] == [
        "The steps do not improve the active goal's required state",
        'The steps make partial progress but leave a required action unplanned',
        'The steps supply all actions needed to satisfy the active goal',
    ]


def test_qualified_target_evidence_does_not_bypass_benefit_confidence_floor():
    state, data, plan = _native_lab_craft_plan()
    support = scheduling_context(state, data, [plan], 'rocket_launch')

    class LowBenefitConfidence(MockJevClient):
        def evaluate(self, context, questions):
            answers = super().evaluate(context, questions)
            # Level 0 ("no demonstrated contribution") is the most probable
            # level; a confident report cannot rescue the candidate.
            answers[plan.id + '/benefit'].update(
                probabilities={'0': 0.6, '1': 0.4, '2': 0.0}, score=0.4,
                confidence=0.95)
            return answers

    decision = select_plan(LowBenefitConfidence(), support, [plan])
    assert decision.plan_id is None
    assert decision.diagnostics['candidate_rejections'][plan.id] == [
        'low_benefit_confidence']


@pytest.mark.parametrize('change', [
    ('start', 'inputs_in_inventory_now', False),
    ('start', 'crafting_queue_empty', False),
    ('start', 'player_connected_and_bound', False),
    ('start', 'recipe_unlocked_and_handcraftable', False),
    ('start', 'native_recipe', 'wrong-recipe'),
    ('start', 'observed_tick', -1),
    ('start', 'native_receipt_required_for_completion', False),
    ('dependency', 'observed_tick', -1),
    ('dependency', 'planner_item_path', ['unrelated', 'stone-furnace']),
    ('row', 'unknowns', ['missing native actor']),
])
def test_handcraft_choice_hint_requires_current_complete_start_and_path(change):
    state, data = snapshot(inventory={'stone': 5}), catalog()
    data.recipes['lab'] = recipe('lab', {'iron-plate': 1})
    state.factory['craft_jobs_protocol'] = 1
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._need('lab', 1)
    step = plan.steps[0]
    plan = replace(plan, steps=(replace(step, action='factory_craft_job',
        effect='craft_job_complete', parameters={**step.parameters, 'receipt': 'lab-test'}),))
    context = {'facts': state.for_jev(),
               **scheduling_context(state, data, [plan], 'rocket_launch')}
    _, questions, _ = question_batch(context, [plan])
    assert 'Prefer this bounded craft over observe' in questions['candidate']['instructions']
    altered = deepcopy(context)
    section, key, value = change
    row = altered['candidate_evidence'][plan.id]
    nested = {'start': 'craft_start_evidence',
              'dependency': 'craft_dependency', 'row': None}[section]
    (row[nested] if nested else row)[key] = value
    _, questions, _ = question_batch(altered, [plan])
    assert 'Prefer this bounded craft over observe' not in questions['candidate']['instructions']


def test_paid_joint_furnace_placement_has_observed_site_and_lab_dependency():
    state, data = snapshot(inventory={'stone-furnace': 1}, player_position=(0, 0)), catalog()
    data.recipes['copper-plate'] = recipe('copper-plate', {'copper-ore': 1}, 'smelting')
    role, anchor = 'recipe:copper-plate', 'cell-site:copper-ore:-42:-109:0:2'
    state.factory['production_sites'] = {
        'protocol': 1, 'session_id': state.session_id, 'tick': state.tick,
        'sources': {role: {
            'state': 'proposed', 'reason': 'joint_layout_available', 'anchor': anchor,
            'position': {'x': -42, 'y': -109}, 'belt_count': 5,
            'bill': {'stone-furnace': 1, 'burner-mining-drill': 1,
                     'burner-inserter': 2, 'wooden-chest': 1, 'transport-belt': 5},
        }},
    }
    planner = InputRoutePlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._machine(role, 'stone-furnace', ('item:lab', 'item:copper-plate'))
    assert plan.steps[0].allowed(state)
    assert plan.materials['local_objective']['item'] == 'lab'
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['unknowns'] == []
    assert row['travel_tiles_lower_bound'] == round((42**2 + 109**2) ** .5, 3)
    assert row['placement_start_evidence']['site_position'] == {'x': -42, 'y': -109}
    assert row['placement_start_evidence']['paid_furnace_in_inventory_now'] is True
    assert row['placement_start_evidence']['native_offer_checked_current_site_clearance'] is True
    assert row['placement_start_evidence']['player_connected_and_bound_now'] is True
    assert row['placement_start_evidence']['crafting_queue_empty_now'] is True
    assert row['placement_dependency'] == {
        'observed_tick': state.tick, 'planner_item_path': ['lab', 'copper-plate'],
        'machine_for_recipe': role,
        'basis': 'current_recursive_planner_and_validated_native_site',
        'later_flow_and_output_require_fresh_native_preconditions': True,
    }
    assert row['delivers_or_crafts'] == []
    context, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert context['candidate_evidence'][plan.id]['placement_dependency'] == row['placement_dependency']
    benefit = questions[plan.id + '/benefit']['instructions']
    assert 'evidenced bounded capacity (score level 1)' in benefit
    assert 'production-blocker removal (level 2)' in benefit
    assert 'transport and output remain unverified' in benefit
    assert 'unverified walking path or future build receipt' in str(questions)
    for section, key, value in (
            ('placement_start_evidence', 'observed_tick', state.tick - 1),
            ('placement_start_evidence', 'paid_furnace_in_inventory_now', False),
            ('placement_start_evidence', 'no_source_owned_at_role_now', False),
            ('placement_start_evidence', 'native_offer_checked_current_site_clearance', False),
            ('placement_start_evidence', 'site_anchor', 'wrong-site'),
            ('placement_dependency', 'planner_item_path', ['unrelated', 'copper-plate']),
            ('placement_dependency', 'machine_for_recipe', 'recipe:iron-plate')):
        altered = deepcopy(context)
        altered['candidate_evidence'][plan.id][section][key] = value
        _, bad_questions, _ = question_batch(altered, [plan])
        assert 'evidenced bounded capacity (score level 1)' not in (
            bad_questions[plan.id + '/benefit']['instructions'])
    missing = replace(plan, materials={key: value for key, value in plan.materials.items()
                                       if key != 'local_objective'})
    assert candidate_evidence(state, data, [missing])[missing.id]['placement_dependency'] is None
    state.inventory['stone-furnace'] = 0
    unfunded = candidate_evidence(state, data, [plan])[plan.id]
    assert unfunded['placement_start_evidence']['paid_furnace_in_inventory_now'] is False
    assert unfunded['placement_dependency'] is None
    assert not plan.steps[0].allowed(state)
    state.inventory['stone-furnace'] = 1
    state.factory['crafting_queue'] = 1
    busy = candidate_evidence(state, data, [plan])[plan.id]
    assert busy['placement_start_evidence']['crafting_queue_empty_now'] is False
    assert busy['placement_dependency'] is None
    state.factory['crafting_queue'] = 0
    state.factory['player_bound'] = False
    unbound = candidate_evidence(state, data, [plan])[plan.id]
    assert unbound['placement_start_evidence']['player_connected_and_bound_now'] is False
    assert unbound['placement_dependency'] is None
    state.factory['player_bound'] = True
    state.factory['entities'][role] = machine(position={'x': -42, 'y': -109})
    occupied = candidate_evidence(state, data, [plan])[plan.id]
    assert occupied['placement_start_evidence']['no_source_owned_at_role_now'] is False
    assert occupied['placement_dependency'] is None
    assert not plan.steps[0].allowed(state)
    state.factory['entities'].pop(role)
    state.factory['production_sites']['tick'] -= 1
    stale = candidate_evidence(state, data, [plan])[plan.id]
    assert stale['placement_start_evidence'] is None and stale['placement_dependency'] is None
    assert 'travel:factory_place' in stale['unknowns']
    assert not plan.steps[0].allowed(state)
    state.factory['production_sites']['tick'] = state.tick
    data.recipes['copper-plate']['enabled'] = False
    assert candidate_evidence(state, data, [plan])[plan.id]['placement_dependency'] is None
    data.recipes['copper-plate']['enabled'] = True
    state.factory['production_sites']['sources'][role]['reason'] = 'survey_not_due'
    assert candidate_evidence(state, data, [plan])[plan.id]['placement_start_evidence'] is None


def test_new_owned_copper_furnace_requests_bounded_startup_coal_with_current_evidence():
    state, data = snapshot(inventory={}, player_position=(0, 0)), catalog()
    data.recipes['copper-plate'] = recipe('copper-plate', {'copper-ore': 1}, 'smelting')
    role = 'recipe:copper-plate'
    state.factory['entities'][role] = machine(unit_number=2546, fuel={},
                                               products_finished=0)
    state.factory['output_buffers'] = {'protocol': 1, 'session_id': state.session_id,
                                       'tick': state.tick, 'sources': {}}
    state.factory['input_routes'] = {'protocol': 1, 'session_id': state.session_id,
                                     'tick': state.tick, 'sources': {}}
    planner = InputRoutePlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    path = ('item:lab', 'item:copper-plate')
    plan = planner._fuel(role, path)
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters == {'resource': 'coal', 'quantity': 5}
    assert plan.steps[0].threshold == 5
    assert plan.materials['fuel_prerequisite']['source_unit'] == 2546
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['unknowns'] == []
    assert row['raw_prerequisite'] is None
    assert row['gather_start_evidence']['resource_in_current_observation'] is True
    assert row['gather_start_evidence']['fair_target_identity_observed'] is True
    assert row['fuel_prerequisite'] == {
        'observed_tick': state.tick, 'planner_item_path': ['lab', 'copper-plate'],
        'burner_role': role, 'burner_unit': 2546, 'fuel_now': 0,
        'coal_in_inventory_now': 0, 'planned_gather_units': 5,
        'established_service_target': None,
        'startup_target': 5, 'current_required_units': 5,
        'current_unfunded_units': 5, 'gather_units_beyond_current_need': 0,
        'basis': 'current_planner_fuel_need_and_owned_native_burner',
        'later_fuel_transfer_and_output_require_fresh_native_preconditions': True,
    }
    assert row['delivers_or_crafts'] == []
    context, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert context['candidate_evidence'][plan.id]['fuel_prerequisite'] == row['fuel_prerequisite']
    assert "owned burner's startup need" in questions[plan.id + '/benefit']['instructions']

    stale = replace(plan, materials={**plan.materials, 'fuel_prerequisite': {
        **plan.materials['fuel_prerequisite'], 'observed_tick': state.tick - 1}})
    assert candidate_evidence(state, data, [stale])[stale.id]['fuel_prerequisite'] is None
    missing = replace(plan, materials={key: value for key, value in plan.materials.items()
                                       if key != 'local_objective'})
    assert candidate_evidence(state, data, [missing])[missing.id]['fuel_prerequisite'] is None
    oversized = replace(plan, steps=(replace(plan.steps[0], threshold=50,
        parameters={'resource': 'coal', 'quantity': 50}),))
    assert candidate_evidence(state, data, [oversized])[oversized.id]['fuel_prerequisite'] is None
    state.factory['entities'][role].pop('fuel')
    assert candidate_evidence(state, data, [plan])[plan.id]['fuel_prerequisite'] is None
    state.factory['entities'][role]['fuel'] = {}
    state.factory['fair_resource_targets'].pop('coal')
    absent_site = candidate_evidence(state, data, [plan])[plan.id]
    assert absent_site['fuel_prerequisite'] is None
    assert absent_site['gather_start_evidence']['fair_target_identity_observed'] is False


def test_established_burner_retains_bulk_service_target():
    state, data = snapshot(inventory={}), catalog()
    state.factory['entities']['recipe:iron-plate'] = machine(
        unit_number=81, fuel={'coal': 0}, products_finished=20)
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('iron-plate', 1)
    plan = planner._fuel('recipe:iron-plate', ('item:iron-plate',))
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].threshold == 50
    assert plan.materials['fuel_prerequisite']['startup'] is False


def test_established_burner_bulk_gather_separates_current_need_from_refill():
    state, data = snapshot(inventory={'coal': 0}, player_position=(62, -27)), catalog()
    role = 'recipe:iron-plate'
    state.factory['entities'][role] = machine(unit_number=2547,
        position={'x': 41, 'y': -111}, fuel={'coal': 3}, products_finished=20)
    state.factory['production_sites'] = {'sources': {
        role: {'state': 'owned', 'source_unit': 2547}}}
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._fuel(role, ('item:lab', 'item:electronic-circuit',
                                'item:iron-plate'))
    step = plan.steps[0]
    assert step.action == 'factory_gather'
    assert step.parameters['quantity'] == 47  # Keep paid grouped service.
    support = scheduling_context(state, data, [plan], 'rocket_launch')
    row = support['candidate_evidence'][plan.id]
    assert row['fuel_prerequisite'] == {
        'observed_tick': state.tick,
        'planner_item_path': ['lab', 'electronic-circuit', 'iron-plate'],
        'burner_role': role, 'burner_unit': 2547, 'fuel_now': 3,
        'coal_in_inventory_now': 0, 'planned_gather_units': 47,
        'established_service_target': 50, 'startup_target': None,
        'current_required_units': 2, 'current_unfunded_units': 2,
        'gather_units_beyond_current_need': 45,
        'basis': 'current_planner_fuel_need_and_owned_native_burner',
        'later_fuel_transfer_and_output_require_fresh_native_preconditions': True,
    }
    context, questions, offered = question_batch(
        {'facts': state.for_jev(), **support}, [plan])
    assert offered == [plan]
    benefit = questions[plan.id + '/benefit']['instructions']
    assert '2 coal still needed' in benefit
    assert 'other 45 support' in benefit
    assert 'not an urgent blocker' in benefit
    assert 'later output' in benefit
    import json
    assert len(json.dumps({'state': context, 'questions': questions},
                          ensure_ascii=False, allow_nan=False).encode('utf-8')) <= 32000

    def no_hint(changed):
        _, questions, _ = question_batch(
            {'facts': state.for_jev(), **changed}, [plan])
        return 'other 45 support' not in questions[plan.id + '/benefit']['instructions']

    stale = deepcopy(support)
    stale['candidate_evidence'][plan.id]['fuel_prerequisite']['observed_tick'] -= 1
    assert no_hint(stale)
    wrong_quantity = deepcopy(support)
    wrong_quantity['candidate_evidence'][plan.id]['fuel_prerequisite'][
        'planned_gather_units'] = 46
    assert no_hint(wrong_quantity)
    malformed_start = deepcopy(support)
    malformed_start['candidate_evidence'][plan.id]['gather_start_evidence'] = 'observed'
    assert no_hint(malformed_start)
    wrong_plan = replace(plan, steps=(replace(step, parameters={
        **step.parameters, 'quantity': 46}, threshold=46),))
    assert candidate_evidence(state, data, [wrong_plan])[wrong_plan.id][
        'fuel_prerequisite'] is None
    no_target = deepcopy(support)
    no_target['local_objective']['primary_target']['item'] = ''
    assert not no_hint(no_target)  # Current binding rebuilds the retained candidate target.
    no_target['selection_contract'].pop('candidate_objective_binding')
    assert no_hint(no_target)  # Unmarked saved requests retain their old global semantics.
    no_target = deepcopy(support)
    no_target['candidate_evidence'][plan.id]['local_target']['item'] = ''
    assert no_hint(no_target)
    state.factory['production_sites']['sources'][role]['state'] = 'proposed'
    assert candidate_evidence(state, data, [plan])[plan.id]['fuel_prerequisite'] is None
    state.factory['production_sites']['sources'][role]['state'] = 'owned'
    state.factory['production_sites']['sources'][role]['source_unit'] = 999
    assert candidate_evidence(state, data, [plan])[plan.id]['fuel_prerequisite'] is None
    state.factory['production_sites']['sources'][role]['source_unit'] = 2547
    state.factory['entities'][role]['fuel']['coal'] = 5
    assert candidate_evidence(state, data, [plan])[plan.id]['fuel_prerequisite'] is None
    state.factory['entities'][role]['fuel']['coal'] = 3
    state.factory['entities'][role].pop('fuel')
    assert candidate_evidence(state, data, [plan])[plan.id]['fuel_prerequisite'] is None


def test_native_shaped_startup_fuel_transfer_has_current_paid_start_evidence():
    state, data = snapshot(inventory={'coal': 5}, player_position=(0, 0)), catalog()
    data.recipes['copper-plate'] = recipe('copper-plate', {'copper-ore': 1}, 'smelting')
    role = 'recipe:copper-plate'
    state.factory['entities'][role] = machine(unit_number=2546, fuel={},
                                               products_finished=0)
    state.factory['player_connected'] = True
    state.factory['player_bound'] = True
    planner = InputRoutePlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._fuel(role, ('item:lab', 'item:copper-plate'))
    step = plan.steps[0]
    assert step.action == 'factory_insert'
    assert step.parameters == {'role': role, 'item': 'coal', 'quantity': 5,
                               'receipt': f'{state.tick}:factory_insert:{role}:coal'}
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['fuel_prerequisite'] is None
    assert row['fuel_transfer_start_evidence'] == {
        'observed_tick': state.tick, 'planner_item_path': ['lab', 'copper-plate'],
        'burner_role': role, 'burner_unit': 2546, 'fuel_now': 0,
        'coal_in_inventory_now': 5, 'coal_to_transfer': 5,
        'native_receipt': step.parameters['receipt'],
        'basis': 'current_planner_need_owned_burner_and_paid_inventory',
        'native_transfer_and_later_output_require_verification': True,
    }
    context, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert context['candidate_evidence'][plan.id]['fuel_transfer_start_evidence'] == row['fuel_transfer_start_evidence']
    assert 'paid coal transfer' in questions[plan.id + '/benefit']['instructions']
    assert 'future transfer outcome' in questions[plan.id + '/needs_observation']['instructions']

    def missing(changed_plan=plan):
        return candidate_evidence(state, data, [changed_plan])[changed_plan.id][
            'fuel_transfer_start_evidence'] is None

    stale = replace(plan, materials={**plan.materials, 'fuel_prerequisite': {
        **plan.materials['fuel_prerequisite'], 'observed_tick': state.tick - 1}})
    assert missing(stale)
    wrong_role = replace(plan, steps=(replace(step, parameters={**step.parameters,
        'role': 'recipe:iron-plate'}),))
    assert missing(wrong_role)
    wrong_quantity = replace(plan, steps=(replace(step, parameters={**step.parameters,
        'quantity': 6}),))
    assert missing(wrong_quantity)
    wrong_receipt = replace(plan, steps=(replace(step, parameters={**step.parameters,
        'receipt': 'stale'}),))
    assert missing(wrong_receipt)
    state.inventory['coal'] = 4
    assert missing()
    state.inventory['coal'] = 5
    state.factory['entities'][role].pop('fuel')
    assert missing()
    state.factory['entities'][role]['fuel'] = {}
    state.factory['player_bound'] = False
    assert missing()


def test_native_shaped_recipe_input_transfer_binds_current_recipe_and_owned_furnace():
    state, data = snapshot(inventory={'copper-ore': 10}, player_position=(0, 0)), catalog()
    data.recipes['copper-plate'] = recipe('copper-plate', {'copper-ore': 1}, 'smelting')
    role = 'recipe:copper-plate'
    state.factory['entities'][role] = machine(unit_number=2546, fuel={'coal': 5},
                                               input={}, crafting=False)
    state.factory['production_sites'] = {'sources': {
        role: {'state': 'owned', 'source_unit': 2546}}}
    state.factory['input_routes'] = {'protocol': 1, 'session_id': state.session_id,
                                     'tick': state.tick, 'sources': {}}
    state.factory['output_buffers'] = {'protocol': 1, 'session_id': state.session_id,
                                       'tick': state.tick, 'sources': {}}
    planner = InputRoutePlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._production(data.recipes['copper-plate'], role, 10,
                               ('item:lab', 'item:copper-plate'))
    step = plan.steps[0]
    assert step.action == 'factory_insert'
    assert step.parameters == {'role': role, 'item': 'copper-ore', 'quantity': 10,
                               'receipt': f'{state.tick}:factory_insert:{role}:copper-ore'}
    assert plan.materials['recipe_input_transfer'] == {
        'observed_tick': state.tick, 'planner_item_path': ['lab', 'copper-plate', 'copper-ore'],
        'recipe': 'copper-plate', 'ingredient': 'copper-ore', 'source_role': role,
        'source_unit': 2546, 'planned_batches': 10, 'observed_input': 0,
        'observed_crafting': False,
    }
    row = candidate_evidence(state, data, [plan])[plan.id]
    assert row['recipe_input_transfer_start_evidence'] == {
        'observed_tick': state.tick, 'planner_item_path': ['lab', 'copper-plate', 'copper-ore'],
        'direct_native_recipe': 'copper-plate', 'owned_source_role': role,
        'owned_source_unit': 2546, 'ingredient': 'copper-ore',
        'ingredient_in_machine_now': 0, 'ingredient_in_inventory_now': 10,
        'burner_fuel_coal_now': 5, 'paid_quantity_to_transfer': 10,
        'planned_native_receipt_id': step.parameters['receipt'],
        'basis': 'current_planner_recipe_input_and_owned_native_machine',
        'native_transfer_and_later_output_require_verification': True,
    }
    context, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert context['candidate_evidence'][plan.id]['recipe_input_transfer_start_evidence'] == row['recipe_input_transfer_start_evidence']
    assert 'paid ingredient transfer' in questions[plan.id + '/benefit']['instructions']
    assert 'transfer and output' in questions[plan.id + '/needs_observation']['instructions']

    def missing(changed_plan=plan):
        return candidate_evidence(state, data, [changed_plan])[changed_plan.id][
            'recipe_input_transfer_start_evidence'] is None

    provenance = plan.materials['recipe_input_transfer']
    assert missing(replace(plan, materials={**plan.materials, 'recipe_input_transfer': {
        **provenance, 'observed_tick': state.tick - 1}}))
    assert missing(replace(plan, materials={**plan.materials, 'recipe_input_transfer': {
        **provenance, 'recipe': 'iron-plate'}}))
    assert missing(replace(plan, materials={key: value for key, value in plan.materials.items()
        if key != 'recipe_input_transfer'}))
    assert missing(replace(plan, steps=(replace(step, parameters={**step.parameters,
        'role': 'recipe:iron-plate'}),)))
    assert missing(replace(plan, steps=(replace(step, parameters={**step.parameters,
        'quantity': 9}),)))
    assert missing(replace(plan, steps=(replace(step, parameters={**step.parameters,
        'receipt': 'stale'}),)))
    state.inventory['copper-ore'] = 9
    assert missing()
    state.inventory['copper-ore'] = 10
    state.factory['entities'][role].pop('input')
    assert missing()
    state.factory['entities'][role]['input'] = {}
    state.factory['production_sites']['sources'][role].pop('source_unit')
    assert missing()
    state.factory['production_sites']['sources'][role]['source_unit'] = 2546
    state.factory['entities'][role].pop('fuel')
    assert missing()


def test_sole_current_iron_ore_input_transfer_has_bounded_level_one_benefit_cue():
    state, data = snapshot(inventory={'iron-ore': 6}, player_position=(0, 0)), catalog()
    role = 'recipe:iron-plate'
    state.factory['entities'][role] = machine(unit_number=2547, fuel={'coal': 2},
                                               input={}, output={}, crafting=False)
    state.factory['production_sites'] = {
        'protocol': 1, 'session_id': state.session_id, 'tick': state.tick,
        'sources': {role: {'state': 'owned', 'source_unit': 2547}}}
    state.factory['input_routes'] = {'protocol': 1, 'session_id': state.session_id,
                                     'tick': state.tick, 'sources': {}}
    state.factory['output_buffers'] = {'protocol': 1, 'session_id': state.session_id,
                                       'tick': state.tick, 'sources': {}}
    planner = InputRoutePlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._production(data.recipes['iron-plate'], role, 6,
                               ('item:lab', 'item:electronic-circuit', 'item:iron-plate'))
    assert plan.steps[0].action == 'factory_insert', plan
    assert plan.steps[0].parameters == {
        'role': role, 'item': 'iron-ore', 'quantity': 6,
        'receipt': f'{state.tick}:factory_insert:{role}:iron-ore'}
    support = scheduling_context(state, data, [plan], 'rocket_launch')
    evidence = support['candidate_evidence'][plan.id]
    start = evidence['recipe_input_transfer_start_evidence']
    assert start['planner_item_path'] == [
        'lab', 'electronic-circuit', 'iron-plate', 'iron-ore']
    assert start['owned_source_unit'] == 2547
    assert start['ingredient_in_inventory_now'] == start['paid_quantity_to_transfer'] == 6
    assert start['burner_fuel_coal_now'] == 2
    cue = ('This sole same-tick, bounded transfer would supply a useful recipe '
           'input if its paid receipt verifies (level 1)')

    def instructions(rows=support, offered=(plan,)):
        context, questions, selected = question_batch(
            {'facts': state.for_jev(), **rows}, list(offered))
        assert selected == list(offered)
        assert len(json.dumps({'state': context, 'questions': questions},
                              ensure_ascii=False, allow_nan=False).encode('utf-8')) <= 32000
        return questions[plan.id + '/benefit']['instructions']

    assert cue in instructions()
    assert 'independent current blocker fact' in instructions()
    assert 'native transfer receipt and later output still require verification' in instructions()
    direct_planner = InputRoutePlanner(data, state, 'rocket_launch')
    direct_planner._set_focus('iron-plate', 6)
    direct = direct_planner._production(data.recipes['iron-plate'], role, 6,
                                        ('item:iron-plate',))
    direct_support = scheduling_context(state, data, [direct], 'rocket_launch')
    assert direct_support['candidate_evidence'][direct.id][
        'recipe_input_transfer_start_evidence']['planner_item_path'] == [
            'iron-plate', 'iron-ore']
    _, direct_questions, _ = question_batch(
        {'facts': state.for_jev(), **direct_support}, [direct])
    assert cue in direct_questions[direct.id + '/benefit']['instructions']

    def absent(mutator):
        changed = deepcopy(support)
        mutator(changed['candidate_evidence'][plan.id])
        assert cue not in instructions(changed)

    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        observed_tick=state.tick - 1))
    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        planner_item_path=['unrelated', 'iron-plate', 'iron-ore']))
    absent(lambda row: row.update(local_target={'item': 'unrelated'}))
    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        owned_source_role='recipe:copper-plate'))
    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        owned_source_unit=0))
    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        direct_native_recipe='copper-plate'))
    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        paid_quantity_to_transfer=7))
    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        planned_native_receipt_id='stale'))
    absent(lambda row: row['recipe_input_transfer_start_evidence'].update(
        native_transfer_and_later_output_require_verification=False))
    absent(lambda row: row.update(recipe_input_transfer_start_evidence='malformed'))
    absent(lambda row: row.update(unknowns=['furnace ownership']))
    absent(lambda row: row.update(reasons=['conflicting queue']))
    absent(lambda row: row.update(urgency=False))
    absent(lambda row: row.update(work_scope='lookahead'))
    absent(lambda row: row.update(research_deadline_tick=state.tick + 1))
    absent(lambda row: row.update(requires_investment=True))
    unsupported = deepcopy(support)
    unsupported['local_objective']['primary_target']['item'] = 'unrelated'
    assert cue in instructions(unsupported)  # Rebind to the retained candidate's proof.
    unsupported['selection_contract'].pop('candidate_objective_binding')
    assert cue not in instructions(unsupported)  # Preserve historical unmarked requests.
    other = replace(plan, id=plan.id + ':other')
    assert cue not in instructions(support, (plan, other))
    altered = replace(plan, steps=(replace(plan.steps[0], parameters={
        **plan.steps[0].parameters, 'quantity': 5}),))
    assert cue not in instructions(support, (altered,))
    altered = replace(plan, steps=(replace(plan.steps[0], costs={'iron-ore': 5}),))
    assert cue not in instructions(support, (altered,))
    altered = replace(plan, steps=(replace(plan.steps[0], item='copper-ore'),))
    assert cue not in instructions(support, (altered,))
    with pytest.raises(ValueError, match='Factory batch must be an integer'):
        replace(plan.steps[0], parameters={
            **plan.steps[0].parameters, 'quantity': True})


def test_ready_owned_output_pickup_has_current_start_facts_without_claiming_transfer():
    state, data = snapshot(inventory={'iron-plate': 0}, player_position=(62, -27)), catalog()
    role = 'recipe:iron-plate'
    state.factory['entities'][role] = machine(unit_number=2547,
        position={'x': 41, 'y': -111}, output={'iron-plate': 20},
        input={}, fuel={'coal': 3}, crafting=False)
    state.factory['production_sites'] = {'sources': {
        role: {'state': 'owned', 'source_unit': 2547}}}
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    plan = planner._need('iron-plate', 20, ('item:lab',))
    step = plan.steps[0]
    assert step.action == 'factory_extract'
    assert plan.materials['output_pickup']['planner_item_path'] == ['lab', 'iron-plate']
    support = scheduling_context(state, data, [plan], 'rocket_launch')
    row = support['candidate_evidence'][plan.id]
    start = row['output_pickup_start_evidence']
    assert start == {
        'observed_tick': state.tick, 'planner_item_path': ['lab', 'iron-plate'],
        'owned_source_role': role, 'owned_source_unit': 2547,
        'ready_output_item': 'iron-plate', 'ready_output_quantity_now': 20,
        'planned_pickup_quantity': 20,
        'planned_native_receipt_id': step.parameters['receipt'],
        'player_connected_and_bound_now': True,
        'basis': 'current_planner_output_and_owned_native_machine',
        'native_pickup_and_inventory_delta_require_verification': True,
    }
    assert state.inventory['iron-plate'] == 0
    assert state.factory.get('receipts', {}) == {}
    context, questions, offered = question_batch(
        {'facts': state.for_jev(), **support}, [plan])
    assert offered == [plan]
    import json
    assert len(json.dumps({'state': context, 'questions': questions},
                          ensure_ascii=False, allow_nan=False).encode('utf-8')) <= 32000
    assert 'bounded useful intermediate' in questions[plan.id + '/benefit']['instructions']
    assert 'bounded useful intermediate' in questions[plan.id + '/useful_progress']['instructions']
    assert 'future pickup and inventory delta' in questions[
        plan.id + '/needs_observation']['instructions']

    stale_support = deepcopy(support)
    stale_support['candidate_evidence'][plan.id][
        'output_pickup_start_evidence']['observed_tick'] -= 1
    _, stale_questions, _ = question_batch(
        {'facts': state.for_jev(), **stale_support}, [plan])
    assert 'bounded useful intermediate' not in stale_questions[
        plan.id + '/benefit']['instructions']
    assert 'bounded useful intermediate' not in stale_questions[
        plan.id + '/useful_progress']['instructions']
    assert 'future pickup and inventory delta' not in stale_questions[
        plan.id + '/needs_observation']['instructions']

    def evidence(changed_plan=plan):
        return candidate_evidence(state, data, [changed_plan])[changed_plan.id][
            'output_pickup_start_evidence']

    provenance = plan.materials['output_pickup']
    assert evidence(replace(plan, materials={**plan.materials, 'output_pickup': {
        **provenance, 'observed_tick': state.tick - 1}})) is None
    assert evidence(replace(plan, materials={**plan.materials, 'output_pickup': {
        **provenance, 'planner_item_path': ['copper-plate', 'iron-plate']}})) is None
    assert evidence(replace(plan, materials={**plan.materials, 'work_intent': {
        'scope': 'lookahead', 'observed_tick': state.tick}})) is None
    assert evidence(replace(plan, steps=(replace(step, parameters={**step.parameters,
        'quantity': 21}),))) is None
    assert evidence(replace(plan, steps=(replace(step, parameters={**step.parameters,
        'receipt': 'stale'}),))) is None
    state.factory['entities'][role]['output']['iron-plate'] = 19
    assert evidence() is None
    state.factory['entities'][role]['output']['iron-plate'] = 20
    state.factory['production_sites']['sources'][role]['state'] = 'proposed'
    assert evidence() is None
    state.factory['production_sites']['sources'][role]['state'] = 'owned'
    state.factory['entities'][role].pop('output')
    assert evidence() is None
    state.factory['entities'][role]['output'] = {'iron-plate': 20}
    state.factory['player_bound'] = False
    assert evidence() is None


def test_local_rubric_does_not_require_one_pickup_to_launch_a_rocket():
    state, data, plans = transfers()
    support = scheduling_context(state, data, plans, 'rocket_launch')
    context, questions, offered = question_batch({'facts': state.for_jev(), **support}, plans)
    assert offered == plans
    assert 'local_objective' in questions['candidate']['instructions']
    assert 'all actions needed' not in str(questions[plans[0].id + '/benefit'])
    assert context['candidate_evidence'][plans[1].id]['travel_tiles_lower_bound'] == 5
    assert support['local_objective']['success_authority'].startswith('unchanged native')


def test_rank_prefers_near_collection_without_mutation_or_claiming_a_path():
    state, data, plans = transfers()
    before = deepcopy((state, plans))
    support = scheduling_context(state, data, plans, 'rocket_launch')
    assert support['deterministic_ranking'] == [plans[1].id, plans[0].id]
    assert support['candidate_evidence'][plans[1].id]['actor_ticks_estimate'] == 400
    assert (state, plans) == before
    assert support['selection_contract']['heuristics_are_not_native_timing_measurements']


@pytest.mark.parametrize('position', [None, {}, {'x': float('nan'), 'y': 0}, {'x': 1}])
def test_missing_geometry_is_unknown_not_zero_cost(position):
    state, data, plans = transfers()
    state.factory['entities']['near']['position'] = position
    support = scheduling_context(state, data, plans, 'rocket_launch')
    row = support['candidate_evidence'][plans[1].id]
    assert row['travel_tiles_lower_bound'] is None and row['actor_ticks_estimate'] is None
    assert row['unknowns']
    assert support['deterministic_ranking'][0] == plans[0].id


def test_urgent_fuel_wins_over_nearby_collection():
    state, data, plans = transfers()
    state.inventory['coal'] = 5
    state.factory['entities']['burner'] = machine(position={'x': 200, 'y': 0}, fuel={'coal': 0})
    fuel = ReadyWorkPlanner(data, state, 'rocket_launch')._transfer('burner', 'coal', 5)
    support = scheduling_context(state, data, [*plans, fuel], 'rocket_launch')
    assert support['deterministic_ranking'][0] == fuel.id
    assert support['candidate_evidence'][fuel.id]['urgency'] == 3


def test_due_science_delivery_precedes_speculative_stock_collection():
    state, data = scenario(available=0)
    state.inventory['red'] = 20
    state, data, plans = transfers(state, data)
    lab = ReadyWorkPlanner(data, state, 'rocket_launch')._transfer('utility:lab', 'red', 20)
    support = scheduling_context(state, data, [*plans, lab], 'rocket_launch')
    assert support['deterministic_ranking'][0] == lab.id
    assert 'due_research_delivery:red' in support['candidate_evidence'][lab.id]['reasons']


@pytest.mark.parametrize('fuel,expected', [(0, 0), (5, 2)])
def test_last_ingredient_only_unblocks_a_supplied_machine(fuel, expected):
    state, data = snapshot(inventory={'iron-ore': 10}), catalog()
    state.factory['entities']['furnace'] = machine(recipe='iron-plate', fuel={'coal': fuel})
    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._transfer('furnace', 'iron-ore', 10)
    assert candidate_evidence(state, data, [plan])[plan.id]['urgency'] == expected


def test_passive_wait_cannot_outrank_ready_work_due_to_zero_actor_cost():
    state, data, plans = transfers()
    wait = ReadyWorkPlanner(data, state, 'rocket_launch')._wait('machine_output', 'iron-plate', 20, 'far')
    support = scheduling_context(state, data, [wait, *plans], 'rocket_launch')
    assert support['deterministic_ranking'][-1] == wait.id


def test_identical_options_collapse_but_different_receipts_remain_distinct():
    _, _, plans = transfers()
    duplicate = replace(plans[0], id='duplicate')
    assert distinct_candidates([plans[0], duplicate]) == [plans[0]]
    changed = replace(duplicate, steps=(replace(duplicate.steps[0], parameters={
        **duplicate.steps[0].parameters, 'receipt': 'new-receipt'}),))
    assert len(distinct_candidates([plans[0], changed])) == 2


@pytest.mark.parametrize('policy,scheduling,source,calls', [
    ('hybrid', 'ready-work', 'deterministic-singleton', 0),
    ('jev', 'ready-work', 'mock', 1),
    ('hybrid', 'serial', 'mock', 1),
    ('deterministic', 'ready-work', 'deterministic', 0),
])
def test_singleton_elision_preserves_explicit_policy_and_model_attribution(policy, scheduling, source, calls):
    backend, client, sink = FactorySimulation(), CountingModel(), Sink()
    backend.state.inventory['stone-furnace'] = 1
    client.last_model, client.last_usage = 'previous-request', {'input_tokens': 999}
    loop = HierarchicalLoop(backend, client, policy=policy, target='iron_smelting',
                            factory_scheduling=scheduling, research_log=sink, tick_seconds=0)
    record = loop.step()
    assert client.calls == calls and record['decision']['source'] == source
    assert record['model_call'] is bool(calls)
    assert len(events(sink, 'model_request')) == calls
    assert events(sink, 'decision')[0]['model_called'] is bool(calls)
    if not calls:
        assert record['resolved_model'] is None and record['usage'] is None
        assert events(sink, 'decision')[0]['diagnostics']['model_skipped']
    assert backend.actions  # Real controller dispatched the admitted synthetic action.


def test_hybrid_fallback_uses_recorded_ranking_and_retains_abstention_evidence():
    state, data, plans = transfers()
    class Client(CountingModel):
        def evaluate(self, state, questions):
            result = super().evaluate(state, questions)
            result['candidate']['confidence'] = 0.1
            return result
    backend, client = FactorySimulation(), Client()
    backend.state.factory['entities'] = deepcopy(state.factory['entities'])
    loop = HierarchicalLoop(backend, client, policy='hybrid', target='iron_smelting',
                            factory_scheduling='ready-work', tick_seconds=0)
    loop._compile_candidates = lambda observation: (plans, '')
    record = loop.step()
    assert client.calls == 1 and backend.actions[0][1]['role'] == 'near'
    assert record['decision']['source'] == 'deterministic-fallback'
    assert record['decision']['diagnostics']['outcome'] == 'low_choice_confidence'
    assert record['decision']['answers']['candidate']['confidence'] == 0.1


def test_fresh_observation_still_prevents_spending_changed_inventory():
    backend, client = FactorySimulation(), CountingModel()
    backend.state.inventory['stone-furnace'] = 1
    original = backend.observe
    observations = 0
    def observe():
        nonlocal observations
        observations += 1
        if observations == 2:
            backend.state.inventory['stone-furnace'] = 0
        return original()
    backend.observe = observe
    loop = HierarchicalLoop(backend, client, policy='hybrid', target='iron_smelting',
                            factory_scheduling='ready-work', tick_seconds=0)
    record = loop.step()
    assert client.calls == 0 and not backend.actions
    assert record['action'] == 'observe' and 'precondition' in record['outcome']


@pytest.mark.parametrize('cause,outcome', [
    ('observe', 'model_abstention'), ('confidence', 'low_choice_confidence'),
    ('missing', 'all_candidates_rejected'), ('benefit', 'all_candidates_rejected'),
    ('disruption', 'all_candidates_rejected'), ('malformed', 'invalid_answer'),
    ('json', 'invalid_provider_payload'),
])
def test_distinct_failure_causes_are_auditable_without_lowering_confidence(cause, outcome):
    state, data, plans = transfers()
    class Client(MockJevClient):
        def evaluate(self, state, questions):
            if cause == 'json':
                raise ValueError('invalid JSON payload')
            answers = super().evaluate(state, questions)
            if cause == 'observe':
                answers['candidate']['choice'] = 'observe'
                answers['candidate']['probabilities'] = {key: float(key == 'observe')
                                                         for key in questions['candidate']['criteria']}
            elif cause == 'confidence':
                answers['candidate']['confidence'] = 0.1
            elif cause == 'malformed':
                answers.pop('candidate')
            else:
                for plan in plans:
                    if cause == 'missing':
                        answers[plan.id + '/needs_observation']['noul'] = 0.8
                    elif cause == 'benefit':
                        # The benefit gate judges the distribution: level 0
                        # ("no demonstrated contribution") dominant rejects.
                        answers[plan.id + '/benefit'].update(
                            probabilities={'0': 0.6, '1': 0.4, '2': 0.0},
                            score=0.4, confidence=0.95)
                    else:
                        answers[plan.id + '/' + cause]['confidence'] = 0.1
            return answers
    result = select_plan(Client(), scheduling_context(state, data, plans, 'rocket_launch'), plans)
    assert result.model_called and result.plan_id is None
    assert result.diagnostics['outcome'] == outcome
    if outcome == 'all_candidates_rejected':
        assert set(result.diagnostics['candidate_rejections']) == {p.id for p in plans}


def test_request_pruning_removes_unoffered_feature_rows_and_reports_ids():
    state, data, plans = transfers()
    plans = [replace(plans[0], id=f'candidate-{i}') for i in range(20)]
    support = scheduling_context(state, data, plans, 'rocket_launch')
    context, _, offered = question_batch(support, plans)
    assert set(context['candidate_evidence']) == {p.id for p in offered}
    assert set(context['deterministic_ranking']) == {p.id for p in offered}
    result = select_plan(MockJevClient(), support, plans)
    assert result.diagnostics['offered_candidates'] < 20
    assert len(result.diagnostics['pruned_candidate_ids']) + result.diagnostics['offered_candidates'] == 20


def test_canonical_replay_keeps_singleton_non_model_decision(tmp_path):
    from jev_factorio.research_log import ResearchLog, RunConfiguration, verify_run
    from jev_factorio.replay import replay_log
    path = tmp_path / 'research'
    backend, client = FactorySimulation(), CountingModel()
    backend.state.inventory['stone-furnace'] = 1
    with ResearchLog(path, RunConfiguration('mock', 'hierarchical', 'hybrid'), environ={}) as sink:
        loop = HierarchicalLoop(backend, client, policy='hybrid', target='iron_smelting',
                                factory_scheduling='ready-work', research_log=sink, tick_seconds=0)
        loop.step()
    verify_run(path)
    report = replay_log(path, format='research-v1')
    assert report.status != 'invalid', report.to_dict()
    assert not any('model' in finding.code for finding in report.findings)
    assert client.calls == 0
