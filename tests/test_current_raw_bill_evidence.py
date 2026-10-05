from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.jev_client import MockJevClient
from jev_factorio.planning.decision_support import add_current_raw_bill_evidence, candidate_evidence
from test_bill_overlap_evidence import bill_case
from test_factory import machine, recipe


def case():
    state, data, plans, _ = bill_case()
    # Full native lab chain, with enough carried iron ore for all but one plate.
    data.recipes['copper-cable'] = recipe('copper-cable', {'copper-plate': 1})
    data.recipes['copper-cable']['products'][0]['amount'] = 2
    data.recipes['electronic-circuit'] = recipe('electronic-circuit', {'iron-plate': 1, 'copper-cable': 3})
    data.recipes['lab'] = recipe('lab', {'electronic-circuit': 10, 'iron-gear-wheel': 10, 'transport-belt': 4})
    state.inventory['iron-ore'] = 5
    state.factory['entities']['recipe:iron-plate'] = machine(unit_number=18, output={'iron-plate': 1})
    rows = candidate_evidence(state, data, plans)
    gather = next(p for p in plans if p.steps[0].action == 'factory_gather')
    rows[gather.id]['raw_prerequisite']['planner_item_path'] = ['lab','electronic-circuit','copper-cable','copper-plate','copper-ore']
    state.world_kind = 'fle'; state.game_version = data.version
    return state, data, plans, rows, gather


def test_recomputed_bill_separates_carried_from_collectible_supply():
    state, data, plans, rows, gather = case()
    before = deepcopy((state, plans, rows))
    add_current_raw_bill_evidence(state, data, plans, rows)
    proof = rows[gather.id].pop('current_raw_material_bill')
    assert (state, plans, rows) == before
    assert proof['carried_only_shortages'] == {'copper-ore': 5, 'iron-ore': 1}
    assert proof['collectible_outputs_not_carried_or_paid'] == {'iron-plate': 1}
    assert proof['shortages_if_observed_outputs_are_collected'] == {'copper-ore': 5}
    assert proof['remaining_resource_shortfall_if_gather_and_collection_verify'] == 0
    assert proof['remaining_recipe_batches_after_supply_credit']['lab'] == 1
    assert proof['remaining_recipe_batches_after_supply_credit']['iron-plate'] == 5


@pytest.mark.parametrize('change', ['stale','wrong_target','wrong_session','unknown','quantity','already_supplied','active_job','queue','fluid','probability','queued','reserved','version'])
def test_missing_or_unsupported_witness_suppresses_proof(change):
    state, data, plans, rows, gather = case()
    if change == 'stale': rows[gather.id]['raw_prerequisite']['observed_tick'] -= 1
    elif change == 'wrong_target': rows[gather.id]['local_target']['item'] = 'unrelated'
    elif change == 'wrong_session': rows[gather.id]['gather_start_evidence']['session_id'] = 'other'
    elif change == 'unknown': rows[gather.id]['unknowns'] = ['missing']
    elif change == 'quantity': gather.steps[0].parameters['quantity'] += 1
    elif change == 'already_supplied': state.inventory['copper-plate'] = 15
    elif change == 'active_job': state.factory['craft_job'] = {'status': 'running'}
    elif change == 'queue': state.factory['crafting_queue'] = 1
    elif change == 'fluid': data.recipes['lab']['ingredients'][0]['type'] = 'fluid'
    elif change == 'probability': data.recipes['lab']['products'][0]['probability'] = .5
    elif change == 'queued': state.factory['entities']['recipe:iron-plate'].update(recipe='iron-plate',input={'iron-ore':1})
    elif change == 'reserved': state.factory['launch_readiness'] = {}; state.inventory['raw-fish'] = 1
    elif change == 'version': state.game_version = 'different'
    add_current_raw_bill_evidence(state, data, plans, rows)
    assert all('current_raw_material_bill' not in row for row in rows.values())


def test_only_named_target_is_expanded_and_collectible_alias_is_not_double_counted():
    state, data, plans, rows, gather = case()
    state.factory['research'] = 'unrelated-research'
    state.factory['entities']['alias'] = deepcopy(state.factory['entities']['recipe:iron-plate'])
    add_current_raw_bill_evidence(state, data, plans, rows)
    proof = rows[gather.id]['current_raw_material_bill']
    assert proof['collectible_outputs_not_carried_or_paid'] == {'iron-plate': 1}
    assert proof['shortages_if_observed_outputs_are_collected'] == {'copper-ore': 5}


def test_added_evidence_keeps_all_choices_and_strict_global_gate():
    state, data, plans, rows, gather = case()
    base = {'facts':state.for_jev(),'candidate_evidence':deepcopy(rows)}
    add_current_raw_bill_evidence(state, data, plans, rows)
    context = {**base,'candidate_evidence': rows}
    _, old_questions, old_plans = question_batch(base, plans)
    _, questions, offered = question_batch(context, plans)
    assert offered == old_plans == plans
    assert questions == old_questions
    class LowConfidence(MockJevClient):
        def evaluate(self,state,questions):
            result = super().evaluate(state,questions)
            result['candidate']['confidence'] = .44
            return result
    assert select_plan(LowConfidence(),context,plans).plan_id is None
