"""Persistent observation of useful progress. Never restart or actuate a game.

The probe is an operator-pinned, read-only adapter returning small JSON. An
optional display adapter receives only the classified status on stdin. Neither
adapter is a gameplay controller, model client or recovery authority.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import time


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def classify(sample, previous, now, session_id, *, heartbeat_seconds=30,
             stall_seconds=120):
    """Do not turn process liveness, tick movement or a restart into progress."""
    previous = previous if isinstance(previous, dict) else {}
    prior = previous if previous.get('session_id') == session_id else {}
    result = {'schema': 1, 'at': now, 'session_id': session_id,
              'status': 'unknown', 'reason': 'probe unavailable', 'attention': True,
              'last_progress_at': prior.get('last_progress_at'),
              'progress_tick': prior.get('progress_tick'),
              'progress_age_seconds': None, 'blocked_since': None,
              'blocked_age_seconds': None, 'pending': False,
              'automatic_recovery_allowed': False}
    if number(prior.get('at')) and now + 2 < prior['at']:
        result['reason'] = 'monitor clock regressed'
        return result
    if not isinstance(sample, dict) or sample.get('session_id') != session_id:
        return result
    at = sample.get('at')
    if not number(at) or at > now + 2 or now - at > heartbeat_seconds:
        result['reason'] = 'stale or invalid controller heartbeat'
        return result
    tick, stamp = sample.get('progress_tick'), sample.get('last_progress_at')
    if type(tick) is not int or tick < 0 or (stamp is not None and (
            not number(stamp) or stamp < 0 or stamp > now + 2)):
        result['reason'] = 'invalid progress evidence'
        return result
    prior_tick = prior.get('progress_tick')
    if type(prior_tick) is int and tick < prior_tick:
        result['reason'] = 'verified progress tick regressed'
        return result
    # A repeated historical tick cannot be made fresh by a rewritten timestamp.
    if prior_tick == tick and number(prior.get('last_progress_at')):
        stamp = prior['last_progress_at']
    elif type(prior_tick) is int and tick > prior_tick:
        stamp = max(stamp or 0, now)
    result.update(progress_tick=tick, last_progress_at=stamp,
                  progress_age_seconds=max(0, now - stamp) if stamp is not None else None,
                  pending=sample.get('pending') is True)
    status, phase = sample.get('checkpoint_status'), sample.get('owner_phase')
    if phase == 'stopped_by_service_owner':
        result.update(status='stopped', reason='stopped by service owner', attention=False)
    elif status == 'completed':
        result.update(status='completed', reason='campaign completed', attention=False)
    elif sample.get('owner_alive') is not True or sample.get('child_alive') is not True:
        result.update(status='stopped', reason='controller process unavailable')
    elif status in {'blocked', 'uncertain'}:
        since = prior.get('blocked_since')
        if prior.get('status') not in {'blocked', 'uncertain'} or not number(since) or since > now:
            since = now
        result.update(status=status, reason=str(sample.get('reason') or status)[:180],
                      blocked_since=since, blocked_age_seconds=now - since)
    elif status != 'running':
        result['reason'] = 'unrecognized controller state'
    elif stamp is None or now - stamp >= stall_seconds:
        result.update(status='no_progress', reason=('awaiting verified progress' if stamp is None
                      else 'no recent verified useful action'))
    else:
        result.update(status='progressing', reason='verified useful progress', attention=False)
    return result


def banner(state):
    status = state['status']
    if status == 'progressing':
        return ''
    age = state.get('progress_age_seconds')
    suffix = f' | {int(age // 60)}m since progress' if number(age) else ''
    return ('JEV ' + status.replace('_', ' ') + ': ' + state['reason'] + suffix)[:240]


def read_json(path, limit=65536):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('monitor input exceeds bound')
    return json.loads(raw)


def command(path):
    value = read_json(path)
    if (not isinstance(value, list) or not value or len(value) > 32
            or any(not isinstance(s, str) or not s or '\0' in s for s in value)
            or not Path(value[0]).is_absolute()):
        raise ValueError('adapter requires an absolute executable and argument list')
    return value


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, sort_keys=True, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def append_event(path, data, max_bytes=1024 * 1024, backups=3):
    path = Path(path)
    if path.exists() and path.stat().st_size >= max_bytes:
        for index in range(backups, 0, -1):
            source = path if index == 1 else path.with_name(path.name + f'.{index-1}')
            if source.exists():
                os.replace(source, path.with_name(path.name + f'.{index}'))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(data, sort_keys=True, allow_nan=False) + '\n')
        stream.flush(); os.fsync(stream.fileno())


def run_once(probe, display, state_dir, session_id, *, timeout=10,
             heartbeat_seconds=30, stall_seconds=120):
    root = Path(state_dir)
    try: previous = read_json(root / 'status.json')
    except (OSError, ValueError): previous = {}
    error = None
    try:
        result = subprocess.run(probe, capture_output=True, timeout=timeout, check=True)
        if len(result.stdout) > 65536:
            raise ValueError('probe output exceeds bound')
        sample = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        sample = None; error = type(exc).__name__
    state = classify(sample, previous, time.time(), session_id,
                     heartbeat_seconds=heartbeat_seconds, stall_seconds=stall_seconds)
    state['probe_error'] = error
    state['banner'] = banner(state)
    state['display_error'] = None
    if display:
        try:
            subprocess.run(display, input=json.dumps(state).encode(), capture_output=True,
                           timeout=timeout, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            state['display_error'] = type(exc).__name__
    keys = ('status', 'reason', 'attention', 'probe_error', 'display_error')
    if any(state.get(k) != previous.get(k) for k in keys):
        append_event(root / 'transitions.jsonl', state)
        print(json.dumps(state, sort_keys=True), flush=True)
    atomic_json(root / 'status.json', state)
    return state


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe-command-file', required=True)
    parser.add_argument('--display-command-file')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--interval', type=float, default=15)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--heartbeat-seconds', type=float, default=30)
    parser.add_argument('--stall-seconds', type=float, default=120)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if any(not number(v) or v <= 0 for v in (
            args.interval, args.timeout, args.heartbeat_seconds, args.stall_seconds)):
        parser.error('monitor intervals must be finite and positive')
    root = Path(args.state_dir); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / 'monitor.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    probe = command(args.probe_command_file)
    display = command(args.display_command_file) if args.display_command_file else None
    while True:
        started = time.monotonic()
        run_once(probe, display, root, args.session_id, timeout=args.timeout,
                 heartbeat_seconds=args.heartbeat_seconds, stall_seconds=args.stall_seconds)
        if args.once: return
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
