"""Ready crafts carry forecast handling volume, never completed inventory."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.decision_support import candidate_evidence, ranking_key
from jev_factorio.skills import Plan
from jev_factorio.state import GameSnapshot


def captured():
    data = json.loads((Path(__file__).parent / 'fixtures/native-pipe-craft-zero-volume.json').read_text())
    plans = [Plan.from_dict(p) for p in data['state']['candidate_plans'].values()]
    craft = next(p for p in plans if p.steps[0].action == 'factory_craft_job')
    return data, plans, craft, GameSnapshot(**data['state']['facts']), Catalog.from_dict(data['catalog'])


@pytest.mark.parametrize('action', ['factory_craft', 'factory_craft_job'])
def test_native_craft_counts_paid_recipe_volume_without_crediting_inventory(action):
    data, plans, craft, state, catalog = captured()
    parameters = dict(craft.steps[0].parameters)
    if action == 'factory_craft': parameters.pop('receipt')
    step = replace(craft.steps[0], action=action, parameters=parameters)
    craft = replace(craft, steps=(step,))
    before = deepcopy(state.for_jev())
    row = candidate_evidence(state, catalog, [craft])[craft.id]
    assert row['processed_units'] == 3
    assert row['craft_handling_forecasts'] == [{
        'native_recipe': 'pipe', 'batches': 3,
        'expected_products_after_native_verification': {'pipe': 3},
        'forecast_not_completed_output': True}]
    assert state.for_jev() == before
    if action == 'factory_craft_job':
        assert row['delivers_or_crafts'] == []
        assert row['actor_ticks_estimate'] == 300


@pytest.mark.parametrize('bad', ['unpaid', 'wrong_cost', 'probabilistic', 'locked',
                               'queue', 'protocol', 'batches', 'energy'])
def test_invalid_or_unready_craft_does_not_gain_forecast_volume(bad):
    data, plans, craft, state, catalog = captured()
    step = craft.steps[0]
    if bad == 'unpaid': state.inventory['iron-plate'] = 2
    elif bad == 'wrong_cost': step = replace(step, costs={'iron-plate': 2})
    elif bad == 'probabilistic': catalog.recipes['pipe']['products'][0]['probability'] = .5
    elif bad == 'locked':
        catalog.recipes['pipe']['enabled'] = False
        state.researched = []
    elif bad == 'queue': state.factory['crafting_queue'] = 1
    elif bad == 'protocol': state.factory['craft_jobs_protocol'] = 0
    elif bad == 'batches': step.parameters['batches'] = True
    elif bad == 'energy': catalog.recipes['pipe']['energy'] = float('nan')
    craft = replace(craft, steps=(step,))
    row = candidate_evidence(state, catalog, [craft])[craft.id]
    assert row['processed_units'] == 0
    assert 'craft_handling_forecasts' not in row


def test_captured_frontier_corrects_cost_comparison_but_keeps_strict_choice_gate():
    data, plans, craft, state, catalog = captured()
    context = data['state']
    assert context['candidate_evidence'][craft.id]['processed_units'] == 0
    assert context['deterministic_ranking'][0] != craft.id
    computed = candidate_evidence(state, catalog, plans)[craft.id]
    context['candidate_evidence'][craft.id].update(
        processed_units=computed['processed_units'],
        craft_handling_forecasts=computed['craft_handling_forecasts'])
    context['deterministic_ranking'] = sorted(context['candidate_evidence'],
        key=lambda key: ranking_key(context['candidate_evidence'][key]))
    assert context['deterministic_ranking'][0] == craft.id
    _, questions, offered = question_batch(context, plans, max_bytes=48000)
    assert set(p.id for p in offered) == set(p.id for p in plans)
    assert questions == data['questions']
    class Retained:
        answer_quantum = .01
        def evaluate(self, state, questions): return data['answers']
    result = select_plan(Retained(), context, plans, max_bytes=48000)
    assert result.plan_id is None and result.reason == 'low choice confidence'
