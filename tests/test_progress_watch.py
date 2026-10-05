"""Health labels depend on verified progress, not heartbeat or fresh game ticks."""
import json
from pathlib import Path
import sys

import pytest

from jev_factorio.progress_watch import classify, banner, append_event, run_once


def sample(**changes):
    return dict({'session_id': 'campaign', 'at': 1000, 'progress_tick': 80,
                 'last_progress_at': 900, 'checkpoint_status': 'running',
                 'owner_phase': 'running', 'owner_alive': True, 'child_alive': True,
                 'pending': False}, **changes)


def test_live_process_and_advancing_world_tick_cannot_hide_stall():
    state = classify(sample(at=1050, world_tick=999999), {}, 1050, 'campaign')
    assert state['status'] == 'no_progress' and state['attention']
    assert state['progress_age_seconds'] == 150
    assert not state['automatic_recovery_allowed']


def test_blocked_duration_survives_monitor_restart_and_new_verified_progress_clears_it():
    blocked = sample(checkpoint_status='blocked', reason='low choice confidence')
    first = classify(blocked, {}, 1000, 'campaign')
    prior = json.loads(json.dumps(first))
    second = classify({**blocked, 'at': 1060}, prior, 1060, 'campaign')
    assert second['blocked_age_seconds'] == 60 and second['progress_age_seconds'] == 160
    assert 'low choice confidence' in banner(second)
    recovered = classify(sample(at=1061, progress_tick=81), second, 1061, 'campaign')
    assert recovered['status'] == 'progressing' and not recovered['attention']
    assert recovered['blocked_since'] is None and banner(recovered) == ''


def test_restarting_owner_does_not_refresh_same_historical_progress():
    first = classify(sample(), {}, 1000, 'campaign')
    later = classify(sample(at=1100, last_progress_at=1100), first, 1100, 'campaign')
    assert later['status'] == 'no_progress'
    assert later['last_progress_at'] == 900 and later['progress_age_seconds'] == 200


@pytest.mark.parametrize('change', [dict(session_id='other'), dict(at=800),
                                   dict(at=1010), dict(progress_tick=79),
                                   dict(last_progress_at=float('nan'))])
def test_missing_stale_mismatched_or_regressed_evidence_is_unknown(change):
    previous = classify(sample(), {}, 1000, 'campaign')
    current = classify(sample(**change), previous, 1001, 'campaign')
    assert current['status'] == 'unknown' and current['attention']


def test_wall_clock_regression_is_not_fresh_progress():
    previous = classify(sample(), {}, 1000, 'campaign')
    current = classify(sample(at=990), previous, 990, 'campaign')
    assert current['status'] == 'unknown' and current['reason'] == 'monitor clock regressed'


def test_pending_native_work_is_reported_without_authorizing_replay():
    state = classify(sample(at=1100, pending=True), {}, 1100, 'campaign')
    assert state['pending'] and state['status'] == 'no_progress'
    assert state['automatic_recovery_allowed'] is False


@pytest.mark.parametrize('phase,status,expected', [
    ('stopped_by_service_owner', 'running', 'stopped'), ('completed', 'completed', 'completed')])
def test_intentional_stop_and_completion_are_not_recovery_requests(phase, status, expected):
    state = classify(sample(owner_phase=phase, checkpoint_status=status, child_alive=False),
                     {}, 1000, 'campaign')
    assert state['status'] == expected and not state['attention']


def test_read_failures_and_display_failures_remain_visible(tmp_path):
    if sys.platform == 'win32': pytest.skip('durable Linux directory fsync')
    broken = [sys.executable, '-c', 'raise SystemExit(2)']
    state = run_once(broken, broken, tmp_path, 'campaign')
    assert state['status'] == 'unknown'
    assert state['probe_error'] == state['display_error'] == 'CalledProcessError'
    assert json.loads((tmp_path / 'status.json').read_text()) == state
    assert len((tmp_path / 'transitions.jsonl').read_text().splitlines()) == 1
    run_once(broken, broken, tmp_path, 'campaign')
    assert len((tmp_path / 'transitions.jsonl').read_text().splitlines()) == 1


def test_journal_rotation_is_bounded_and_retains_latest_transition(tmp_path):
    path = tmp_path / 'events'
    for i in range(10): append_event(path, {'event': i}, max_bytes=1, backups=2)
    assert {p.name for p in tmp_path.iterdir()} == {'events', 'events.1', 'events.2'}
    assert json.loads(path.read_text()) == {'event': 9}
