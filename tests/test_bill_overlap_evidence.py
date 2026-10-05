from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.planning.decision_support import add_craft_overlap_evidence, candidate_evidence
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from test_craft_overlap_evidence import case
from test_factory import recipe, machine


def bill_case():
    state, data, _ = case()
    state.inventory.update({'iron-plate': 10, 'iron-gear-wheel': 10})
    data.recipes['transport-belt'] = recipe('transport-belt', {'iron-plate': 1, 'iron-gear-wheel': 1})
    data.recipes['transport-belt']['products'][0]['amount'] = 2
    data.recipes['lab'] = recipe('lab', {'copper-plate': 15, 'iron-gear-wheel': 10, 'transport-belt': 4})
    state.factory['entities']['recipe:copper-plate'] = machine(fuel={'coal': 5}, energy=100)
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    planner._set_focus('lab', 1)
    planner.plan = lambda: planner._need('lab', 1)
    plans = [replace(p, steps=(replace(p.steps[0], action='factory_craft_job',
        effect='craft_job_complete', parameters={**p.steps[0].parameters, 'receipt': 'test-'+p.steps[0].item}),))
        if p.steps[0].action == 'factory_craft' else p for p in planner.candidates()]
    crafts = [p for p in plans if p.steps[0].action == 'factory_craft_job']
    assert len(crafts) == 2 and len(plans) == 3
    return state, data, plans, crafts


def test_multiple_current_bill_crafts_keep_every_option_and_compiler_scope():
    state, data, plans, crafts = bill_case()
    rows = candidate_evidence(state, data, plans)
    before = deepcopy(rows)
    add_craft_overlap_evidence(state, plans, rows)
    for p in crafts:
        proof = rows[p.id].pop('independent_gather_overlap')
        assert proof['requires_native_admission_then_fresh_gather_observation'] is True
        assert rows[p.id]['work_scope'] == 'lookahead'
        assert rows[p.id]['shared_bill_craft']['unfilled_bill_units'] > 0
    assert rows == before


@pytest.mark.parametrize('key,value', [
    ('observed_tick', -1), ('basis', 'forecast_only'), ('local_target_item', 'unrelated'),
    ('craft_item', 'wrong'), ('inventory_now', -1), ('bounded_bill_inventory_target', 0),
    ('unfilled_bill_units', 100), ('expected_products_after_native_verification', 0),
    ('forecast_is_not_paid_stock_or_completed_output', False),
    ('background_overlap_requires_native_admission', False),
])
def test_each_bill_is_independently_qualified(key, value):
    state, data, plans, crafts = bill_case()
    rows = candidate_evidence(state, data, plans)
    rows[crafts[0].id]['shared_bill_craft'][key] = value
    add_craft_overlap_evidence(state, plans, rows)
    assert 'independent_gather_overlap' not in rows[crafts[0].id]
    assert 'independent_gather_overlap' in rows[crafts[1].id]


def test_discretionary_lookahead_has_no_current_bill_comparison():
    state, data, plans, crafts = bill_case()
    rows = candidate_evidence(state, data, plans)
    for p in crafts:
        rows[p.id]['shared_bill_craft'] = None
    add_craft_overlap_evidence(state, plans, rows)
    assert all('independent_gather_overlap' not in row for row in rows.values())
