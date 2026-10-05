from copy import deepcopy

import pytest

from jev_factorio.jev_client import MockJevClient
from jev_factorio.judgments import _scheduling_tie_break_hint, question_batch, select_plan
from jev_factorio.planning.decision_support import scheduling_context
from test_bill_craft_defer import frontier


def case():
    snapshot, catalog, plans = frontier()
    state = {'facts': snapshot.for_jev(),
             **scheduling_context(snapshot, catalog, plans, 'rocket_launch')}
    return state, plans


def test_current_compiler_order_is_explained_without_removing_either_choice():
    state, plans = case()
    before = deepcopy(state)
    context, questions, offered = question_batch(state, plans)
    assert _scheduling_tie_break_hint(context)
    assert 'scheduling tie-breaker' in questions['candidate']['instructions']
    assert 'never proof of usefulness' in questions['candidate']['instructions']
    assert offered == plans and state == before
    assert set(questions['candidate']['criteria']) == {p.id for p in plans} | {'observe'}
    assert all('scheduling tie-breaker' not in question['instructions']
               for key, question in questions.items() if key != 'candidate')


@pytest.mark.parametrize('change', ['stale', 'missing', 'duplicate', 'reversed',
                                   'unknown_cost', 'infinite', 'bool_cost',
                                   'negative_cost', 'null_units', 'scope_type',
                                   'huge_cost', 'wrong_schema'])
def test_stale_incomplete_or_inconsistent_order_adds_no_preference(change):
    state, plans = case()
    first = state['deterministic_ranking'][0]
    row = state['candidate_evidence'][first]
    if change == 'stale': state['selection_contract']['observed_tick'] -= 1
    elif change == 'missing': state['deterministic_ranking'].pop()
    elif change == 'duplicate': state['deterministic_ranking'] = [first, first]
    elif change == 'reversed': state['deterministic_ranking'].reverse()
    elif change == 'unknown_cost': row['actor_ticks_estimate'] = None
    elif change == 'infinite': row['actor_ticks_estimate'] = float('inf')
    elif change == 'bool_cost': row['actor_ticks_estimate'] = True
    elif change == 'negative_cost': row['actor_ticks_estimate'] = -1
    elif change == 'null_units': row['current_prerequisite_units'] = None
    elif change == 'scope_type': row['work_scope'] = []
    elif change == 'huge_cost': row['actor_ticks_estimate'] = 10 ** 1000
    elif change == 'wrong_schema': state['selection_contract']['schema'] = True
    # Inspect before serialization so invalid numeric input is never sent.
    assert not _scheduling_tie_break_hint({**state, 'candidate_plans': {p.id: p.to_dict() for p in plans}})


@pytest.mark.parametrize('veto', ['low_choice', 'observe', 'unsupported', 'observation', 'disruption'])
def test_scheduling_preference_does_not_override_any_existing_gate(veto):
    state, plans = case()

    class VetoModel(MockJevClient):
        def evaluate(self, context, questions):
            assert 'scheduling tie-breaker' in questions['candidate']['instructions']
            answers = super().evaluate(context, questions)
            if veto == 'low_choice': answers['candidate']['confidence'] = .24
            elif veto == 'observe':
                answers['candidate'].update(choice='observe', probabilities={key: float(key == 'observe') for key in questions['candidate']['criteria']})
            else:
                for plan in plans:
                    if veto == 'unsupported':
                        answers[plan.id + '/useful_progress'].update(choice='unsupported', probabilities={'useful': 0., 'unsupported': 1.})
                    elif veto == 'observation': answers[plan.id + '/needs_observation']['noul'] = .8
                    elif veto == 'disruption': answers[plan.id + '/disruption']['confidence'] = .2
            return answers

    result = select_plan(VetoModel(), state, plans)
    assert result.plan_id is None
    assert result.reason == ('low choice confidence' if veto == 'low_choice' else
                             'model abstention' if veto == 'observe' else 'Candidate evidence insufficient')


def test_single_candidate_has_no_comparative_preference():
    state, plans = case()
    _, questions, offered = question_batch(state, plans[:1])
    assert len(offered) == 1
    assert 'scheduling tie-breaker' not in questions['candidate']['instructions']
