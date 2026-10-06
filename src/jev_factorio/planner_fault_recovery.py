"""Reconcile one proven pre-selection planner exit, without granting a retry.

This offline operation returns a proposed checkpoint; it never installs it,
starts a controller, queries a provider or attaches a game backend. The signed
owner must retain the original capture and install the reviewed proposal under
its writer lock, then use normal changed-contract source admission.
"""
from copy import copy, deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from .provenance import digest_json

REASON = "Native buffer component dependency cycle"
EVENT = "native_planner_failure_reconciled"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CYCLE = ("ValueError: Native production cycle: technology:rocket-silo -> "
          "technology:concrete -> item:automation-science-pack -> "
          "item:iron-gear-wheel -> item:burner-inserter -> item:iron-gear-wheel")
_QUIESCENT = ("active_plan", "pending", "attempt", "native_pending", "native_attempt",
              "background_job", "background_attempt", "background_step", "transfer_recovery",
              "async_decision", "two_stage_decision", "capital_investment",
              "solid_funding", "coal_funding")


def _read(path, expected, limit):
    path = Path(path)
    if (type(expected) is not str or not _HASH.fullmatch(expected)
            or path.is_symlink() or not path.is_file()):
        raise ValueError("Planner fault evidence requires pinned regular files")
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Planner fault evidence differs from its pinned capture")
    return data


def validate_record(memory, *, boundary=True):
    """Require the specific retained fault proof for this source-admission reason."""
    row = memory.planner_fault_recovery
    if not isinstance(row, dict):
        raise ValueError("Planner fault boundary lacks its unique reconciliation record")
    expected = {'kind', 'schema', 'reason', 'execution_id', 'owner_run_id', 'source_revision',
                'owner_sha256', 'checkpoint_sha256', 'terminal_sha256', 'console_sha256',
                'final_event_hash', 'prior_state_sha256', 'failures_sha256', 'tick'}
    if (set(row) != expected or type(row['schema']) is not int or row['schema'] != 1 or row['reason'] != REASON
            or row['kind'] != EVENT
            or (boundary and (memory.blocked_recovery is None
                or row['source_revision'] != memory.blocked_recovery['source_revision']
                or row['failures_sha256'] != digest_json(memory.failures)))
            or type(row['tick']) is not int or row['tick'] < 0
            or type(memory.last_tick) is not int or row['tick'] > memory.last_tick
            or type(row['execution_id']) is not str or not re.fullmatch(r'[0-9a-f]{32}', row['execution_id'])
            or type(row['owner_run_id']) is not str or not row['owner_run_id']
            or any(type(row[key]) is not str or not _HASH.fullmatch(row[key]) for key in (
                'owner_sha256', 'checkpoint_sha256', 'terminal_sha256', 'console_sha256',
                'prior_state_sha256', 'failures_sha256'))
            or type(row['final_event_hash']) is not str
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', row['final_event_hash'])):
        raise ValueError("Invalid retained native planner fault proof")
    from .blocked_persistence import _source
    _source(row['source_revision'])


def prepare(checkpoint, research_dir, terminal_path, console_path, *, pins, source_revision,
            owner_sha256, execution_id, owner_run_id, owner_pid, child_pid, lock_fd, lock_path):
    """Propose only the stopped-status reconciliation of an exact sealed fault.

    pins contains checkpoint/terminal/console SHA256 values and the trusted
    sealed final_event_hash. The caller supplies them from reviewed owner
    evidence. Process absence is an additional refusal check, never the proof
    of nonmutation: the sealed final decision must contain no request/dispatch.
    """
    from .compatible_recovery import require_writer_lock
    from .memory import load_checkpoint_bytes
    from .research_log import verify_run
    from .blocked_persistence import _source

    require_writer_lock(lock_fd, lock_path)
    _source(source_revision)
    if (set(pins) != {'checkpoint_sha256', 'terminal_sha256', 'console_sha256', 'final_event_hash'}
            or type(owner_sha256) is not str or not _HASH.fullmatch(owner_sha256)
            or type(execution_id) is not str or not re.fullmatch(r'[0-9a-f]{32}', execution_id)
            or type(owner_run_id) is not str or not owner_run_id
            or any(type(pid) is not int or pid <= 1 or Path('/proc', str(pid)).exists()
                   for pid in (owner_pid, child_pid))):
        raise ValueError("Planner reconciliation requires stopped, pinned owner and child")
    raw = _read(checkpoint, pins['checkpoint_sha256'], 16 * 1024 * 1024)
    terminal = json.loads(_read(terminal_path, pins['terminal_sha256'], 64000))
    console = _read(console_path, pins['console_sha256'], 2 * 1024 * 1024).decode('utf-8')
    if (type(terminal.get('exit_code')) is not int or terminal['exit_code'] != 1
            or terminal.get('script_sha256') != owner_sha256
            or terminal.get('checkpoint_sha256') != pins['checkpoint_sha256']
            or console.rstrip().splitlines()[-1:] != [_CYCLE]):
        raise ValueError("Terminal evidence is not the reviewed native buffer planner fault")
    audit = verify_run(Path(research_dir), expected_final_hash=pins['final_event_hash'],
                       max_events=100000)
    if not audit['complete'] or audit['outcome'] != 'error':
        raise ValueError("Planner fault requires a sealed failed research run")
    tail = []
    with (Path(research_dir) / 'events.jsonl').open() as stream:
        for line in stream:
            event = json.loads(line)
            if event['event_type'] == 'step_started':
                tail = []
            tail.append(event)
    allowed = {'step_started', 'controller_state', 'observation', 'observation_validated',
               'checkpoint_written', 'goal_checked', 'candidate_set_created', 'step_failed', 'run_finished'}
    if (not tail or any(event['event_type'] not in allowed for event in tail)
            or [event['event_type'] for event in tail[-3:]] != [
                'candidate_set_created', 'step_failed', 'run_finished']
            or tail[-1]['payload'] != {'outcome': 'error', 'error_type': 'ValueError'}):
        raise ValueError("Final planner decision is not a proven pre-selection failure")
    failure = tail[-3]['payload']
    provenance = failure.get('supervisor_provenance', {})
    if (failure.get('status') != 'error' or failure.get('error', {}).get('category') != 'invalid_data'
            or provenance.get('code_revision') != source_revision
            or provenance.get('execution_id') != execution_id or provenance.get('run_id') != owner_run_id
            or any(event['payload'].get('action_id') is not None
                   or event['payload'].get('model_call_id') is not None for event in tail)
            or not any(event['event_type'] == 'observation_validated'
                       and event['payload'].get('accepted') is True for event in tail)):
        raise ValueError("Planner fault lineage or nonmutation evidence disagrees")
    data = json.loads(raw)
    memory = load_checkpoint_bytes(raw, data['session_id'], data['target'], checkpoint_path=Path(checkpoint))
    try:
        if (memory.status != 'running' or memory.reason or memory.step_index != 0
                or memory.reservations or any(getattr(memory, key, None) is not None for key in _QUIESCENT)
                or memory.blocked_recovery is None
                or memory.blocked_recovery['source_revision'] != source_revision
                or memory.session_id != failure.get('session_id')
                or memory.last_tick != failure.get('factorio_tick')
                or memory.planner_fault_recovery is not None):
            raise ValueError("Planner failure checkpoint is not the exact quiescent running boundary")
        current_rows = [row for row in memory.blocked_recovery['attempts']
                        if row['source_revision'] == source_revision]
        if not current_rows or any(row.get('outcome', 'pending') in {'pending', 'provider_blocked'}
                                   for row in current_rows):
            raise ValueError("Planner boundary retains an unresolved current-source selection")
        proposal = copy(memory)
        proposal.planner_fault_recovery = {
            'kind': EVENT, 'schema': 1, 'reason': REASON,
            'execution_id': execution_id, 'owner_run_id': owner_run_id,
            'source_revision': deepcopy(source_revision), 'owner_sha256': owner_sha256,
            **pins, 'prior_state_sha256': digest_json(asdict(memory)),
            'failures_sha256': digest_json(memory.failures), 'tick': memory.last_tick}
        proposal.status, proposal.reason = 'blocked', REASON
        validate_record(proposal)
        if Path(checkpoint).read_bytes() != raw:
            raise ValueError("Planner checkpoint changed during reconciliation preparation")
        if verify_run(Path(research_dir), expected_final_hash=pins['final_event_hash'],
                      max_events=100000) != audit:
            raise ValueError("Planner fault research evidence changed during preparation")
        require_writer_lock(lock_fd, lock_path)
        return proposal
    except BaseException:
        index = getattr(memory, '_blocked_recovery_archive_index', None)
        if index is not None:
            index.close()
        raise
