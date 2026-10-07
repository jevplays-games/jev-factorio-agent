"""Historical completions retain their actual age through the public monitor."""
from copy import deepcopy
import json
import sys

import pytest

from jev_factorio import progress_watch


def inputs(timestamp=900, tick=150):
    checkpoint = {'session_id': 'campaign', 'last_tick': 200,
                  'history': [{'kind': 'background_job_completed', 'tick': 100}]}
    owner = {'session_id': 'campaign', 'useful_tick': tick,
             'last_useful_action_at': timestamp}
    sample = {'session_id': 'campaign', 'at': 2000, 'owner_alive': True,
              'child_alive': True, 'checkpoint_status': 'running',
              'owner_phase': 'gameplay',
              **progress_watch.completed_progress_sample(checkpoint, owner,
                                                        owner_qualified=True)}
    previous = {'session_id': 'campaign', 'at': 1985, 'status': 'no_progress',
                'progress_tick': 100, 'last_progress_at': 900}
    return checkpoint, owner, sample, previous


def test_newly_learned_historical_owner_watermark_cannot_refresh_completion_time():
    checkpoint, owner, sample, previous = inputs()
    before = deepcopy((checkpoint, owner, sample, previous))
    result = progress_watch.classify(sample, previous, 2000, 'campaign')
    assert result['progress_tick'] == 150
    assert result['last_progress_at'] == 900
    assert result['progress_age_seconds'] == 1100
    assert result['status'] == 'no_progress' and result['attention']
    assert not result['automatic_recovery_allowed']
    assert (checkpoint, owner, sample, previous) == before


def test_newly_visible_historical_history_receipt_retains_age():
    checkpoint, owner, sample, previous = inputs()
    checkpoint['history'].append({'kind': 'step_verified',
                                 'action': 'factory_insert', 'tick': 160})
    sample.update(progress_watch.completed_progress_sample(checkpoint, owner,
                                                          owner_qualified=True))
    result = progress_watch.classify(sample, previous, 2000, 'campaign')
    assert result['progress_tick'] == 160
    assert result['last_progress_at'] == 900
    assert result['status'] == 'no_progress' and result['attention']


def test_recent_verified_completion_still_clears_stall_at_its_actual_time():
    _, _, sample, previous = inputs(timestamp=1990)
    result = progress_watch.classify(sample, previous, 2000, 'campaign')
    assert result['status'] == 'progressing' and not result['attention']
    assert result['last_progress_at'] == 1990 and result['progress_age_seconds'] == 10
    # Repeating the receipt with a rewritten owner timestamp cannot extend it.
    sample.update(at=2120, last_progress_at=2120)
    repeated = progress_watch.classify(sample, result, 2120, 'campaign')
    assert repeated['last_progress_at'] == 1990
    assert repeated['status'] == 'no_progress' and repeated['attention']


@pytest.mark.parametrize('timestamp', [None, -1, float('nan'), 2003])
def test_missing_or_invalid_completion_time_cannot_become_current(timestamp):
    _, _, sample, previous = inputs(timestamp=timestamp)
    result = progress_watch.classify(sample, previous, 2000, 'campaign')
    assert result['status'] in {'unknown', 'no_progress'} and result['attention']
    assert not result['automatic_recovery_allowed']


def test_public_monitor_persists_historical_completion_without_inventing_recency(tmp_path, monkeypatch):
    _, _, sample, previous = inputs()
    (tmp_path / 'status.json').write_text(json.dumps(previous), encoding='utf-8')
    monkeypatch.setattr(progress_watch.time, 'time', lambda: 2000)
    # The normal subprocess probe protocol supplies a fresh heartbeat carrying
    # an older completion; it never connects to a native/provider service.
    probe = [sys.executable, '-c', 'import json; print(json.dumps(' + repr(sample) + '))']
    result = progress_watch.run_once(probe, None, tmp_path, 'campaign')
    persisted = json.loads((tmp_path / 'status.json').read_text(encoding='utf-8'))
    assert result['status'] == persisted['status'] == 'no_progress'
    assert result['last_progress_at'] == persisted['last_progress_at'] == 900
    assert result['progress_age_seconds'] == 1100
    assert result['attention'] and result['probe_error'] is None
    assert not result['automatic_recovery_allowed']
