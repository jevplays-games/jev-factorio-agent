"""Retain observed completion across the real 64-entry checkpoint window."""
from copy import deepcopy

import pytest

from jev_factorio.memory import retain_latest_craft
from jev_factorio.progress_watch import completed_progress_sample, classify


def evidence():
    rows = [
        {'kind': 'background_job_completed', 'tick': 17709882},
        {'kind': 'step_verified', 'action': 'factory_insert', 'tick': 17711589},
    ]
    cp = {'session_id': 'campaign', 'last_tick': 17712000, 'history': rows}
    owner = {'session_id': 'campaign', 'useful_tick': 17711589,
             'last_useful_action_at': 900}
    return cp, owner


def sample(cp, owner, now, progress):
    return {'session_id': cp['session_id'], 'at': now,
            'owner_alive': True, 'child_alive': True,
            'checkpoint_status': 'blocked', 'owner_phase': 'blocked',
            'reason': 'Candidate evidence insufficient',
            'native_research': {'technology': 'concrete', 'force_index': 1,
                                'progress': progress, 'observed_tick': cp['last_tick']},
            **completed_progress_sample(cp, owner, owner_qualified=True)}


def test_actual_history_retention_does_not_erase_later_verified_transfer():
    cp, owner = evidence()
    first = classify(sample(cp, owner, 1000, .10), {}, 1000, 'campaign')
    rows = cp['history'] + [{'kind': 'blocked_recovery_attempt', 'tick': 17712001 + n}
                            for n in range(80)]
    cp.update(last_tick=17712100, history=retain_latest_craft(rows, events=True))
    assert len(cp['history']) == 64
    assert not any(row['kind'] == 'step_verified' for row in cp['history'])
    assert cp['history'][0]['kind'] == 'background_job_completed'
    before = deepcopy(cp), deepcopy(owner)
    second = classify(sample(cp, owner, 1015, .11), first, 1015, 'campaign')
    assert second['status'] == 'researching' and not second['attention']
    assert second['progress_tick'] == 17711589
    assert second['last_progress_at'] == 900
    assert second['progress_age_seconds'] == 115
    assert not second['automatic_recovery_allowed']
    assert before == (cp, owner)
    # Without the independently qualified owner, keep the genuine regression
    # alert; do not silently take max(previous, current) in the classifier.
    unqualified = completed_progress_sample(cp, owner)
    invalid = classify({**sample(cp, owner, 1015, .11), **unqualified}, first, 1015, 'campaign')
    assert invalid['reason'] == 'verified progress tick regressed'


@pytest.mark.parametrize('change', ['session', 'future', 'negative', 'boolean', 'missing'])
def test_owner_watermark_requires_same_session_and_checkpoint_bounds(change):
    cp, owner = evidence()
    if change == 'session': owner['session_id'] = 'other'
    elif change == 'future': owner['useful_tick'] = cp['last_tick'] + 1
    elif change == 'negative': owner['useful_tick'] = -1
    elif change == 'boolean': owner['useful_tick'] = True
    elif change == 'missing': owner.pop('useful_tick')
    with pytest.raises(ValueError, match='owner progress watermark'):
        completed_progress_sample(cp, owner, owner_qualified=True)


def test_later_retained_background_receipt_still_supplies_completion():
    cp, owner = evidence()
    owner['useful_tick'] = 17700000
    cp['history'] = cp['history'][:1]
    result = completed_progress_sample(cp, owner, owner_qualified=True)
    assert result['progress_tick'] == 17709882
    assert result['last_progress_at'] == 900


def test_watermark_cannot_refresh_progress_time_or_hide_stalled_research():
    cp, owner = evidence()
    first = classify(sample(cp, owner, 1000, .10), {}, 1000, 'campaign')
    owner['last_useful_action_at'] = 1015
    second = classify(sample(cp, owner, 1015, .10), first, 1015, 'campaign')
    assert second['status'] == 'blocked' and second['attention']
    assert second['last_progress_at'] == 900
