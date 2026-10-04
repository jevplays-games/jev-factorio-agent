"""Immediate raw deficits survive the optional collection heuristic."""
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.planning.output_buffers import OutputBufferPlanner
from test_factory import catalog, machine, recipe, snapshot


def case(kind=ReadyWorkPlanner, **options):
    data = catalog()
    data.recipes['copper-plate'] = recipe('copper-plate', {'copper-ore': 1}, 'smelting')
    data.recipes['iron-gear-wheel'] = recipe('iron-gear-wheel', {'iron-plate': 2})
    data.recipes['lab'] = recipe('lab', {'iron-gear-wheel': 1, 'copper-plate': 15})
    state = snapshot(inventory={'stone': 5, 'iron-ore': 2, 'copper-plate': 10},
                     nearby_resources={'stone': 1, 'iron-ore': 2, 'copper-ore': 3})
    state.factory['output_buffers'] = {'protocol': 1, 'session_id': state.session_id,
                                       'tick': state.tick, 'sources': {}}
    planner = kind(data, state, 'rocket_launch', **options)
    planner.plan = lambda: planner._need('lab', 1)
    return state, data, planner


@pytest.mark.parametrize('kind', [ReadyWorkPlanner, OutputBufferPlanner])
def test_small_current_deficit_is_independent_and_preserves_craft_inputs(kind):
    state, _, planner = case(kind)
    before = deepcopy(state)
    plans = planner.candidates()
    assert len(plans) == 2
    assert plans[0].steps[0].action == 'factory_craft'
    assert plans[0].steps[0].costs == {'stone': 5}
    gather = plans[1]
    assert gather.steps[0].parameters == {'resource': 'copper-ore', 'quantity': 5}
    assert not gather.steps[0].costs
    assert gather.materials['work_intent']['scope'] == 'immediate'
    assert gather.materials['raw_prerequisite']['planner_item_path'] == ['lab', 'copper-plate', 'copper-ore']
    assert gather.materials['current_target_raw_alternative']['preserved_primary_costs'] == {'stone': 5}
    assert state == before


@pytest.mark.parametrize('change', ['queue', 'disconnected', 'unbound', 'unpaid',
                                  'service', 'speculative', 'budget', 'native_unqualified',
                                  'stale_intent', 'wrong_target'])
def test_no_extra_work_while_craft_or_owner_preconditions_are_missing(change):
    state, _, planner = case()
    primary = planner.plan()
    if change == 'queue': state.factory['crafting_queue'] = 1
    elif change == 'disconnected': state.factory['player_connected'] = False
    elif change == 'unbound': state.factory['player_bound'] = False
    elif change == 'unpaid': state.inventory['stone'] = 0
    elif change == 'service': planner._buffer_service = True
    elif change == 'speculative': planner.speculative = True
    elif change == 'budget': planner.max_candidates = 1
    elif change == 'native_unqualified': state.world_kind = 'fle'
    elif change == 'stale_intent': primary.materials['work_intent']['observed_tick'] -= 1
    elif change == 'wrong_target': primary.materials['local_objective']['item'] = 'unrelated'
    assert planner._current_raw_craft_alternatives(primary) == [primary]


@pytest.mark.parametrize('paid', ['carried_raw', 'finished_product', 'queued_input'])
def test_already_paid_stock_is_not_gathered_twice(paid):
    state, data, _ = case()
    if paid == 'carried_raw': state.inventory['copper-ore'] = 5
    elif paid == 'finished_product': state.inventory['copper-plate'] = 15
    else:
        state.factory['entities']['recipe:copper-plate'] = machine(
            recipe='copper-plate', input={'copper-ore': 5}, fuel={'coal': 4})
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner.plan = lambda: planner._need('lab', 1)
    assert len(planner.candidates()) == 1


def test_optional_horizon_shortage_does_not_create_current_target_work():
    state, _, planner = case()
    primary = planner.plan()
    planner.targets['coal'] = 50
    planner.raw_targets['coal'] = 50
    planner.demands['unrelated-future-product'] = 100
    plans = planner._current_raw_craft_alternatives(primary)
    assert [p.steps[0].item for p in plans] == ['stone-furnace', 'copper-ore']


def test_placement_remains_serial():
    _, _, planner = case()
    primary = planner.plan()
    primary = replace(primary, steps=(replace(primary.steps[0], action='factory_place',
        effect='machine', parameters={'role':'recipe:iron-plate', 'name':'stone-furnace',
                                      'anchor':'factory'}),))
    assert planner._current_raw_craft_alternatives(primary) == [primary]


def test_missing_resource_does_not_offer_exploration_as_independent_work():
    state, _, planner = case()
    state.nearby_resources.pop('copper-ore')
    assert len(planner.candidates()) == 1


def test_combined_construction_and_gather_frontier_keeps_the_choice_gate():
    from jev_factorio.judgments import DEFAULT_MAX_REQUEST_BYTES, question_batch, select_plan
    from jev_factorio.jev_client import MockJevClient
    from jev_factorio.planning.decision_support import scheduling_context
    state, data, planner = case()
    state.factory['craft_jobs_protocol'] = 1
    plans = planner.candidates()
    plans[0] = replace(plans[0], steps=(replace(plans[0].steps[0],
        action='factory_craft_job', effect='craft_job_complete',
        parameters={**plans[0].steps[0].parameters, 'receipt':'test-construction'}),))
    context = {'active_goal':'rocket_launch', 'facts':state.for_jev(),
               **scheduling_context(state, data, plans, 'rocket_launch')}
    _, questions, offered = question_batch(context, plans, max_bytes=DEFAULT_MAX_REQUEST_BYTES)
    assert offered == plans
    assert context['candidate_evidence'][plans[0].id]['machine_construction_prerequisite']
    assert context['candidate_evidence'][plans[1].id]['raw_prerequisite']

    class LowChoice(MockJevClient):
        def evaluate(self, state, questions):
            answers = super().evaluate(state, questions)
            answers['candidate']['confidence'] = 0.26
            return answers

    decision = select_plan(LowChoice(), context, plans)
    assert decision.plan_id is None
    assert decision.reason == 'low choice confidence'
    assert all(row['passed'] for row in decision.diagnostics['usefulness_gate'].values())
