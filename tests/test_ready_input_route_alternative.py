"""Native V38 ready-kit rejection must not monopolize ordinary production."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from jev_factorio.judgments import question_batch, _qualified_recipe_transfer_chain
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from test_buffer_component_demand import captured, context


def capture():
    return captured('native-v38-ready-input-kit.json')


def test_composed_ready_build_retains_current_manual_science_input():
    state, catalog, loop = capture()
    before = deepcopy(asdict(state)), deepcopy(asdict(loop.memory))
    plans, source = context(state, catalog, loop)
    build, manual = plans[:2]
    assert build.steps[0].action == 'factory_input_build'
    assert build.steps[0].costs == {
        'burner-inserter': 1, 'burner-mining-drill': 1, 'transport-belt': 25}
    assert manual.steps[0].action == 'factory_insert'
    assert manual.steps[0].costs == {'iron-ore': 20}
    assert manual.materials['local_objective'] == {
        'item': 'logistic-science-pack', 'inventory_target': 20, 'ultimate_goal': 'rocket_launch'}
    row = source['candidate_evidence'][manual.id]
    assert _qualified_recipe_transfer_chain(manual, source['facts'], row)
    wire, questions, offered = question_batch(source, plans, max_bytes=48000)
    assert manual in offered and build in offered
    assert 'recipe_input_transfer_start_evidence' in questions[manual.id + '/useful_progress']['instructions']
    assert wire['candidate_evidence'][manual.id]['recipe_input_transfer_start_evidence']
    assert not any(p.id.startswith('capital:') for p in plans)
    assert before == (asdict(state), asdict(loop.memory))
    assert loop.backend.calls == []


@pytest.mark.parametrize('limit', [1, 2, 3, 8])
def test_manual_primary_precedes_lookahead_within_existing_budget(limit):
    state, catalog, _ = capture()
    planner = MiningOutpostPlanner(catalog, state, 'rocket_launch', max_candidates=limit)
    planner._economic_acquiring = True
    plans = planner.candidates()
    assert plans[0].steps[0].action == 'factory_input_build'
    assert len(plans) <= limit
    assert len({p.id for p in plans}) == len(plans)
    assert planner.expansions <= 512
    if limit >= 2:
        assert plans[1].steps[0].action == 'factory_insert'
        assert plans[1].steps[0].parameters['quantity'] == 20


def test_building_route_stays_serial_without_manual_alternative():
    state, catalog, _ = capture()
    state.factory['input_routes']['sources']['recipe:iron-plate']['state'] = 'building'
    planner = MiningOutpostPlanner(catalog, state, 'rocket_launch')
    planner._economic_acquiring = True
    plans = planner.candidates()
    assert len(plans) == 1
    assert plans[0].steps[0].action == 'factory_input_build'


@pytest.mark.parametrize('change', ['paid_proposal', 'stale', 'unit', 'session'])
def test_invalid_or_paid_proposal_cannot_enable_manual_bypass(change):
    state, catalog, _ = capture()
    rows = state.factory['input_routes']
    row = rows['sources']['recipe:iron-plate']
    if change == 'paid_proposal':
        row['parts']['inserter'] = {'role': 'input:test', 'receipt': 'test', 'unit_number': 9999, 'paid': 1}
    elif change == 'stale': rows['tick'] -= 1
    elif change == 'unit': state.factory['entities']['recipe:iron-plate']['unit_number'] += 1
    elif change == 'session': rows['session_id'] = 'other'
    with pytest.raises(ValueError):
        MiningOutpostPlanner(catalog, state, 'rocket_launch').candidates()
