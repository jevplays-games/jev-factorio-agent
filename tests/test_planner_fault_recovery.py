"""Synthetic sealed-fault admission: no source migration or native execution."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

import pytest

from jev_factorio import planner_fault_recovery as recovery
from jev_factorio import blocked_persistence as ledger
from jev_factorio.blocked_reevaluation import validate_blocked_memory
from jev_factorio.memory import CampaignMemory
from jev_factorio.research_log import ResearchLog, RunConfiguration, verify_run

SOURCE = {'commit': 'a' * 40, 'source_sha256': 'b' * 64}
OWNER = 'c' * 64
EXECUTION = 'd' * 32
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux owner flock and process checks')


@pytest.fixture
def incident(tmp_path, request):
    import fcntl
    checkpoint = tmp_path / 'controller.json'
    memory = CampaignMemory('campaign', 'rocket_launch', status='running', last_tick=10,
                            active_goal='rocket_launch', failures={'old-capital': 2},
                            history=[{'kind': 'retained', 'index': i} for i in range(64)])
    ledger.record_attempt(memory, SOURCE, '1' * 64, None, 9)
    ledger.finish_attempt(memory, SOURCE, '1' * 64, 'selected')
    memory.save(checkpoint)
    research = tmp_path / 'research'
    writer = ResearchLog(research, RunConfiguration(backend='mock', controller='hierarchical',
        policy='jev', target='rocket_launch', mock_model=True, steps=1), environ={})
    common = {'action_id': None, 'model_call_id': None, 'decision_id': 'decision:1',
              'session_id': 'campaign', 'factorio_tick': 10,
              'supervisor_provenance': {'code_revision': SOURCE, 'execution_id': EXECUTION,
                                        'run_id': 'owner-run'}}
    writer.emit('step_started', common)
    writer.emit('observation_validated', {**common, 'accepted': True})
    extra_event = getattr(request, 'param', None)
    if extra_event:
        writer.emit(extra_event, common)
    failure = {**common, 'status': 'error', 'error': {'category': 'invalid_data'}}
    writer.emit('candidate_set_created', failure)
    writer.emit('step_failed', failure)
    writer.finish('error', error_type='ValueError')
    writer.close()
    console = tmp_path / 'console.log'
    console.write_text('Traceback (synthetic test)\n' + recovery._CYCLE + '\n')
    terminal = tmp_path / 'result.json'
    terminal.write_text(json.dumps({'exit_code': 1, 'script_sha256': OWNER,
        'checkpoint_sha256': hashlib.sha256(checkpoint.read_bytes()).hexdigest()}))
    lock_path = tmp_path / 'single-writer.lock'
    with lock_path.open('a+') as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pins = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in (
            ('checkpoint_sha256', checkpoint), ('terminal_sha256', terminal), ('console_sha256', console))}
        pins['final_event_hash'] = verify_run(research)['final_event_hash']
        options = dict(pins=pins, source_revision=SOURCE, owner_sha256=OWNER,
                       execution_id=EXECUTION, owner_run_id='owner-run', owner_pid=2_000_000_000,
                       child_pid=2_000_000_001, lock_fd=lock.fileno(), lock_path=str(lock_path))
        yield (checkpoint, research, terminal, console), options, memory


def test_proposal_preserves_history_and_every_field_except_stopped_status_and_proof(incident):
    paths, options, original = incident
    before = paths[0].read_bytes()
    proposal = recovery.prepare(*paths, **options)
    assert paths[0].read_bytes() == before
    assert proposal.status == 'blocked' and proposal.reason == recovery.REASON
    assert proposal.planner_fault_recovery['checkpoint_sha256'] == options['pins']['checkpoint_sha256']
    prior, after = asdict(original), asdict(proposal)
    for key in ('status', 'reason', 'planner_fault_recovery'):
        prior.pop(key)
        after.pop(key)
    assert prior == after
    assert len(proposal.history) == len(original.history) == 64
    validate_blocked_memory(proposal, 4)
    restored = CampaignMemory.from_bytes(json.dumps(asdict(proposal)).encode(), 'campaign', 'rocket_launch')
    validate_blocked_memory(restored, 4)


def test_reconciled_fault_consumes_normal_source_admission_and_reloads(incident):
    from types import SimpleNamespace
    from dataclasses import replace
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.memory import load_checkpoint
    from jev_factorio.backends.mock import MockBackend
    from jev_factorio import two_stage_controller
    from jev_factorio.judgments import question_batch
    from jev_factorio.skills import Plan, Step

    paths, options, _ = incident
    proposal = recovery.prepare(*paths, **options)
    loop = object.__new__(HierarchicalLoop)
    loop.memory = proposal
    loop.policy = 'jev'
    loop.jev = SimpleNamespace(is_mock=False)
    loop.max_stalled_decisions = 4
    loop._blocked_recovery_archive_index = None
    loop._archive_full_recovery_tail = lambda: None
    loop.provenance = {'code_revision': {'commit':'e'*40, 'source_sha256':'f'*64}}
    loop._blocked_reevaluation_source = {
        'blocked_source_revision':SOURCE['commit'], 'source_head':'e'*40,
        'decision_contract_sha256':'9'*64, 'previous_contract_sha256':'8'*64}
    loop._blocked_reevaluation_checkpoint_sha256 = options['pins']['checkpoint_sha256']
    destination = paths[0].with_name('admitted.json')
    loop._save = lambda: proposal.save(destination)
    loop.target, loop.confidence_floor, loop.max_request_bytes = 'rocket_launch', .45, 48000
    snapshot = replace(MockBackend().observe(), session_id='campaign', tick=10, world_kind='fle')
    plans = [Plan('coal', 'stockpile_fuel', 'Gather five coal',
                  (Step('mine_coal', 'inventory', 'coal', 5),))]
    state, questions, offered = question_batch({'facts':snapshot.for_jev()}, plans, max_bytes=48000)
    metadata = ledger.selection_batch_metadata(state, questions, offered,
        state_sha256='3'*64, frontier_sha256='4'*64, current_tick=10)
    input_sha = ledger.decision_input_sha256(state, [p.to_dict() for p in offered],
        session_id='campaign', source_revision=loop.provenance['code_revision'],
        target='rocket_launch', policy='jev', confidence_floor=.45, current_tick=10,
        questions=questions, selection_batch=metadata)
    two_stage_controller.prepare(loop, snapshot, state, plans, state, questions, offered,
        metadata, input_sha, source_authorized=True, authorization_reason=recovery.REASON)
    restored = load_checkpoint(destination, 'campaign', 'rocket_launch')
    assert restored.status == 'running' and restored.reason == ''
    assert restored.two_stage_decision['phase'] == 'assessment_ready'
    ledger.validate_checkpoint_metadata(json.loads(destination.read_bytes()),
                                       loop.provenance['code_revision'])
    loop.memory = restored
    assert two_stage_controller.pending(loop)
    assert restored.blocked_reevaluations[-1]['reason'] == recovery.REASON
    assert restored.blocked_recovery['source_revision'] == loop.provenance['code_revision']
    assert restored.planner_fault_recovery == proposal.planner_fault_recovery
    assert restored.failures == {'old-capital':2}
    assert loop._reevaluate_blocked_once is False


@pytest.mark.parametrize('change', ['checkpoint', 'terminal', 'console', 'trace', 'owner', 'source', 'execution', 'live_child'])
def test_mismatched_evidence_or_live_child_never_produces_a_reconciliation(incident, change):
    paths, options, _ = incident
    options = deepcopy(options)
    if change in ('checkpoint', 'terminal', 'console'):
        options['pins'][change + '_sha256'] = '0' * 64
    elif change == 'trace':
        options['pins']['final_event_hash'] = 'sha256:' + '0' * 64
    elif change == 'owner':
        options['owner_sha256'] = '0' * 64
    elif change == 'source':
        options['source_revision']['commit'] = '0' * 40
    elif change == 'execution':
        options['execution_id'] = '0' * 32
    else:
        options['child_pid'] = os.getpid()
    before = paths[0].read_bytes()
    with pytest.raises(ValueError):
        recovery.prepare(*paths, **options)
    assert paths[0].read_bytes() == before


def test_reason_without_fault_proof_cannot_open_changed_source_admission(incident):
    _, _, memory = incident
    memory.status, memory.reason = 'blocked', recovery.REASON
    with pytest.raises(ValueError, match='unique reconciliation'):
        validate_blocked_memory(memory, 4)


def test_failure_history_change_invalidates_reconciled_boundary(incident):
    paths, options, _ = incident
    proposal = recovery.prepare(*paths, **options)
    proposal.failures.clear()
    with pytest.raises(ValueError, match='fault proof'):
        validate_blocked_memory(proposal, 4)


def test_unlocked_descriptor_is_not_acquired_as_a_side_effect(incident):
    import fcntl
    paths, options, _ = incident
    fcntl.flock(options['lock_fd'], fcntl.LOCK_UN)
    with pytest.raises(ValueError, match='already-held'):
        recovery.prepare(*paths, **options)


@pytest.mark.parametrize('incident', ['model_request', 'dispatch', 'action_completed'], indirect=True)
def test_sealed_final_decision_with_provider_or_native_activity_is_rejected(incident):
    paths, options, _ = incident
    with pytest.raises(ValueError, match='pre-selection failure'):
        recovery.prepare(*paths, **options)


@pytest.mark.parametrize('change', ['completed', 'selected_plan', 'pending_selection', 'wrong_tick'])
def test_even_pinned_checkpoint_cannot_discard_work_or_relabel_completion(incident, change):
    paths, options, _ = incident
    options = deepcopy(options)
    data = json.loads(paths[0].read_bytes())
    if change == 'completed':
        data['status'] = 'completed'
    elif change == 'selected_plan':
        from jev_factorio.skills import Plan, Step
        data['active_plan'] = Plan('retained', 'rocket_launch', 'Keep selected work',
                                   (Step('walk_to_coal', 'near', 'coal'),)).to_dict()
    elif change == 'pending_selection':
        data['blocked_recovery']['attempts'][-1]['outcome'] = 'pending'
    else:
        data['last_tick'] = 11
    paths[0].write_text(json.dumps(data))
    options['pins']['checkpoint_sha256'] = hashlib.sha256(paths[0].read_bytes()).hexdigest()
    terminal = json.loads(paths[2].read_bytes())
    terminal['checkpoint_sha256'] = options['pins']['checkpoint_sha256']
    paths[2].write_text(json.dumps(terminal))
    options['pins']['terminal_sha256'] = hashlib.sha256(paths[2].read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        recovery.prepare(*paths, **options)
