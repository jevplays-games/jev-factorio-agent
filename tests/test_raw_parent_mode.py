"""Captured V40 ordinary-work frontier; never call a model or native backend."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio.judgments import question_batch, select_plan, _qualified_candidate_local_raw_demand
from jev_factorio.planning.decision_support import candidate_evidence
from jev_factorio.planning.input_routes import InputRoutePlanner
from test_buffer_component_demand import captured, context


def frontier():
    snapshot,catalog,loop=captured('native-v40-raw-parent-mode.json')
    plans,state=context(snapshot,catalog,loop)
    coal=next(p for p in plans if p.steps[0].item=='coal')
    iron=next(p for p in plans if p.steps[0].item=='iron-ore' and p.steps[0].threshold==20)
    return snapshot,catalog,loop,plans,state,coal,iron


def test_ordinary_raw_parent_matches_exact_current_plan_after_exhausted_capital():
    snapshot,catalog,loop,plans,state,coal,iron=frontier()
    failures=deepcopy(loop.memory.failures)
    assert iron.id not in [p.id for p in InputRoutePlanner(catalog,snapshot,'rocket_launch').candidates()]
    ordinary=InputRoutePlanner(catalog,snapshot,'rocket_launch');ordinary._economic_acquiring=True
    matching=[p for p in ordinary.candidates() if p.id==iron.id]
    assert len(matching)==1 and matching[0].steps==iron.steps
    row=state['candidate_evidence'][iron.id]
    assert _qualified_candidate_local_raw_demand(iron,state['facts'],row,state['candidate_evidence'])
    packet,questions,offered=question_batch(state,[coal,iron],max_bytes=48000)
    assert offered==[coal,iron]
    assert "this candidate's evidence row local_target" in questions[iron.id+'/useful_progress']['criteria']['useful']
    assert 'Compare qualified candidate-local parent contributions separately' in questions['candidate']['instructions']
    assert packet['local_objective']['candidate_targets'][coal.id]['item']=='coal'
    assert packet['local_objective']['candidate_targets'][iron.id]['item']=='logistic-science-pack'
    assert loop.memory.failures==failures and loop.backend.calls==[]
    assert len(json.dumps({'state':packet,'questions':questions}).encode())<=48000


@pytest.mark.parametrize('change', ['quantity','raw_path','work_scope','shortages','batches',
                                   'missing_parent','stale','satisfied','unverified'])
def test_ordinary_mode_does_not_relax_exact_plan_or_native_binding(change):
    snapshot,catalog,_,plans,_,coal,iron=frontier()
    materials=deepcopy(iron.materials)
    if change=='quantity':
        step=replace(iron.steps[0],threshold=19,parameters={'resource':'iron-ore','quantity':19})
        iron=replace(iron,steps=(step,))
    elif change=='raw_path':materials['raw_prerequisite']['planner_item_path']=['logistic-science-pack','iron-ore']
    elif change=='work_scope':materials['work_intent']['scope']='lookahead'
    elif change=='shortages':materials['shortages']['iron-ore']+=1
    elif change=='batches':materials['batches']['iron-plate']+=1
    elif change=='stale':materials['raw_prerequisite']['observed_tick']-=1
    elif change=='satisfied':snapshot.inventory['logistic-science-pack']=20
    elif change=='unverified':del snapshot._coherent_observation_verified
    iron=replace(iron,materials=materials)
    offered=[iron] if change=='missing_parent' else [coal,iron]
    assert candidate_evidence(snapshot,catalog,offered)[iron.id].get('candidate_local_raw_demand') is None


def test_recorded_low_confidence_stays_rejected_with_repaired_evidence():
    _,_,_,_,state,coal,iron=frontier()
    answers=json.loads((Path(__file__).parent/'fixtures/native-v40-low-choice-answers.json').read_bytes())
    class Recorded:
        def evaluate(self,*args,**kwargs):return deepcopy(answers)
    result=select_plan(Recorded(),state,[coal,iron],confidence_floor=.45,max_bytes=48000)
    assert result.plan_id is None and result.reason=='low choice confidence'
