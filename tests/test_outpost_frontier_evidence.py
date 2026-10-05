"""Replay the native two-candidate hold after steam-engine placement."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.skills import Plan


def captured():
    data=json.loads((Path(__file__).parent/'fixtures/native144-outpost-choice-usefulness.json').read_text())
    plans=[Plan.from_dict(doc) for doc in data['state']['candidate_plans'].values()]
    child=next(p for p in plans if p.steps[0].item=='wood')
    direct=next(p for p in plans if p.steps[0].item=='iron-ore')
    return data,plans,child,direct


@pytest.mark.parametrize('reverse',[False,True])
def test_native_frontier_keeps_both_choices_and_candidate_specific_kit_guidance(reverse):
    data,plans,child,direct=captured()
    if reverse:plans.reverse()
    context,questions,offered=question_batch(data['state'],plans,max_bytes=48000)
    assert {p.id for p in offered}=={p.id for p in plans}
    assert context['candidate_evidence']==data['state']['candidate_evidence']
    assert context['facts']==data['state']['facts']
    assert 'outpost_kit_prerequisite_start_evidence' in questions[child.id+'/useful_progress']['instructions']
    assert 'current child request on separate' in questions[child.id+'/benefit']['instructions']
    assert 'separate same-tick parent and child paths' in questions[child.id+'/needs_observation']['instructions']
    assert 'outpost_kit_prerequisite_start_evidence' not in questions[direct.id+'/useful_progress']['instructions']
    assert 'not measured payback' in questions[child.id+'/useful_progress']['instructions']
    assert len(json.dumps({'state':context,'questions':questions}).encode())<=48000
    # The captured failure had a confident global choice; usefulness still vetoed it.
    assert data['answers']['candidate']['confidence']==.82
    class Recorded:
        answer_quantum=.01
        def evaluate(self,state,batch):return deepcopy(data['answers'])
    decision=select_plan(Recorded(),data['state'],plans,max_bytes=48000)
    assert decision.plan_id is None and decision.reason=='Candidate evidence insufficient'
    assert set(decision.diagnostics['candidate_rejections'])=={p.id for p in plans}


@pytest.mark.parametrize('change',['tick','parent','child','resource','quantity','inventory','current_item','scope'])
def test_multi_candidate_guidance_rejects_stale_or_crosswired_child_evidence(change):
    data,plans,child,_=captured();state=data['state']
    row=state['candidate_evidence'][child.id]
    proof=row['outpost_kit_prerequisite_start_evidence'];action=proof['action_start_facts']
    if change=='tick':proof['observed_tick']-=1
    elif change=='parent':proof['parent_planner_item_path'][0]='unrelated'
    elif change=='child':proof['child_planner_item_path'][0]='unrelated'
    elif change=='resource':action['resource']='iron-ore'
    elif change=='quantity':action['quantity']+=1
    elif change=='inventory':action['inventory_now']+=1
    elif change=='current_item':proof['current_action_item']='iron-ore'
    elif change=='scope':row['work_scope']='lookahead'
    _,questions,offered=question_batch(state,plans,max_bytes=48000)
    assert {p.id for p in offered}=={p.id for p in plans}
    assert 'outpost_kit_prerequisite_start_evidence' not in questions[child.id+'/useful_progress']['instructions']


def test_confident_usefulness_does_not_bypass_strict_choice_floor():
    data,plans,child,_=captured()
    class LowChoice:
        answer_quantum=.01
        def evaluate(self,state,questions):
            answer=deepcopy(data['answers']);answer['candidate']['confidence']=.44
            answer[child.id+'/useful_progress'].update(choice='useful',confidence=.9,
                probabilities={'useful':.95,'unsupported':.05})
            return answer
    decision=select_plan(LowChoice(),data['state'],plans,max_bytes=48000)
    assert decision.plan_id is None and decision.reason=='low choice confidence'
