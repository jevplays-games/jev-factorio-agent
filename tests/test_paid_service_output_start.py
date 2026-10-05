from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jev_factorio.planning.factory import FactoryPlanner
from jev_factorio.planning.service_visits import service_visit
from jev_factorio.planning.decision_support import _paid_service_output_start_evidence, candidate_evidence
from jev_factorio.judgments import question_batch, select_plan, _qualified_paid_service_output
from jev_factorio.jev_client import MockJevClient
from test_paid_service_input_start import setup as input_case


def case():
    catalog, state, old, _, _, _ = input_case()
    state.inventory = {'coal': 5, 'copper-plate': 19, 'iron-plate': 40}
    role = 'recipe:copper-plate'
    machine = state.factory['entities'][role]
    machine.update(output={'copper-plate': 1},fuel={},input={},recipe='',crafting=False)
    params = {'role':role,'item':'copper-plate','quantity':1,
              'receipt':f'{state.tick}:factory_extract:{role}:copper-plate'}
    step = replace(old.steps[0],action='factory_extract',costs={},parameters=params)
    materials = {k:deepcopy(v) for k,v in old.materials.items() if k not in ('service_visit','recipe_input_transfer')}
    materials['output_pickup'] = {'item':'copper-plate','observed_output':1,'observed_tick':state.tick,
        'planner_item_path':['automation-science-pack','copper-plate'],
        'source_role':role,'source_unit':machine['unit_number']}
    atomic = replace(old,id='factory:factory_extract:'+role,steps=(step,),materials=materials)
    planner = FactoryPlanner(catalog,state,'rocket_launch')
    planner.ledger = SimpleNamespace(carried=dict(state.inventory));planner.targets={}
    plan = service_visit(planner,atomic)
    assert [s.action for s in plan.steps] == ['factory_extract','factory_insert']
    return state,catalog,plan


def test_output_pickup_proof_survives_same_cell_fuel_service_wrapping():
    state,catalog,plan = case()
    before = deepcopy((state,plan))
    proof = _paid_service_output_start_evidence(state,catalog,plan)
    assert proof['first_output_pickup']['planned_pickup_quantity'] == 1
    assert proof['combined_paid_costs'] == {'coal':5}
    assert proof['first_output_pickup']['native_pickup_and_inventory_delta_require_verification']
    assert proof['later_fuel_output_and_target_completion_unverified']
    assert state.inventory == before[0].inventory and plan == before[1]
    row = candidate_evidence(state,catalog,[plan])[plan.id]
    assert row['output_pickup_start_evidence'] is None  # It is not a single-step plan.
    assert row['paid_service_output_start_evidence'] == proof
    facts = deepcopy(input_case()[3])  # Controller-compacted model facts.
    facts['inventory'] = dict(state.inventory)
    role = 'recipe:copper-plate'
    for key in ('output','input','fuel','recipe','crafting'):
        facts['factory']['entities'][role][key] = deepcopy(state.factory['entities'][role][key])
    context = {'facts':facts,'candidate_evidence':{plan.id:row}}
    assert _qualified_paid_service_output(plan,facts,row)
    _,questions,offered = question_batch(context,[plan],max_bytes=48000)
    for suffix in ("useful_progress","benefit","needs_observation"):
        assert "paid_service_output_start_evidence" in questions[plan.id+"/"+suffix]["instructions"]
    altered=deepcopy(row);altered["paid_service_output_start_evidence"]["native_receipt_queries"][1]["present"]=True
    assert not _qualified_paid_service_output(plan,facts,altered)
    assert offered == [plan]
    class Rejected(MockJevClient):
        def evaluate(self,state,questions):
            answers=super().evaluate(state,questions)
            answers['candidate']['confidence']=.44
            return answers
    assert select_plan(Rejected(),context,[plan]).plan_id is None


@pytest.mark.parametrize('change',['coherence','atomic','admission','output','coal','owner','receipt','tick','reverse','marker','paid_stock','path'])
def test_stale_or_crosswired_service_does_not_receive_output_proof(change):
    state,catalog,plan = case()
    if change == 'coherence': state._coherent_observation_verified=None
    elif change == 'atomic': state._atomic_inventory_verified=None
    elif change == 'admission': state._paid_service_admissions={}
    elif change == 'output': state.factory['entities']['recipe:copper-plate']['output']={}
    elif change == 'coal': state.inventory['coal']=4
    elif change == 'owner': state.factory['production_sites']['sources']['recipe:copper-plate']['source_unit']+=1
    elif change == 'receipt': state.factory['receipts'][plan.steps[0].parameters['receipt']]={}
    elif change == 'tick': state.factory['tick']-=1
    elif change == 'reverse': plan=replace(plan,steps=tuple(reversed(plan.steps)))
    elif change == 'marker': plan.materials['service_visit']['steps']=3
    elif change == 'paid_stock': plan.materials['service_visit']['paid_stock_now']['coal']=6
    elif change == 'path': plan.materials['output_pickup']['planner_item_path'][0]='unrelated'
    assert _paid_service_output_start_evidence(state,catalog,plan) is None


@pytest.mark.parametrize('change',['receipt_count','output','coal','unit','native_recipe','parent_recipe','job','costs','marker','later_claim'])
def test_consumer_rejects_contrary_native_facts_or_crosswired_proof(change):
    state,catalog,plan=case()
    row=candidate_evidence(state,catalog,[plan])[plan.id]
    facts=deepcopy(input_case()[3]);facts['inventory']=dict(state.inventory)
    role='recipe:copper-plate'
    for key in ('output','input','fuel','recipe','crafting'):
        facts['factory']['entities'][role][key]=deepcopy(state.factory['entities'][role][key])
    proof=row['paid_service_output_start_evidence']
    assert _qualified_paid_service_output(plan,facts,row)
    if change=='receipt_count': facts['factory']['native_transfer_receipt_count']+=1
    elif change=='output': facts['factory']['entities'][role]['output']={}
    elif change=='coal': facts['inventory']['coal']=4
    elif change=='unit': facts['factory']['entities'][role]['unit_number']+=1
    elif change=='native_recipe': proof['native_recipe']['products']=[]
    elif change=='parent_recipe': proof['native_parent_recipes']['automation-science-pack']['ingredients']=[]
    elif change=='job': facts['factory']['craft_job']={'status':'running'}
    elif change=='costs': row['material_costs']={}
    elif change=='marker': proof['service_visit']['unit_numbers'][1]+=1
    elif change=='later_claim': proof['later_fuel_output_and_target_completion_unverified']=False
    assert not _qualified_paid_service_output(plan,facts,row)
