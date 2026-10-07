import json
import sys
from types import SimpleNamespace

import pytest

from jev_factorio import main, research_log
from jev_factorio.causal_trace import CausalTrace
from jev_factorio.iteration_timing import profiled_iteration
from jev_factorio.latency_report import _validate_setup_attribution, analyze
from jev_factorio.research_log import ResearchLog, RunConfiguration, verify_run
from jev_factorio.setup_timing import LEGACY_SETUP_STAGES, STAGES, SetupTiming
from jev_factorio.timing_attribution import elapsed_clocks


def _rewrite_hash_chain(run_dir, events, *, seal=True):
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    previous = research_log.digest(manifest)
    for sequence, event in enumerate(events, 1):
        event['sequence'] = sequence
        event['prev_hash'] = previous
        event.pop('event_hash', None)
        event['event_hash'] = research_log.digest(event)
        previous = event['event_hash']
    (run_dir / 'events.jsonl').write_bytes(
        b''.join(research_log.canonical_bytes(event) + b'\n' for event in events))
    integrity_path = run_dir / 'integrity.json'
    if seal:
        integrity = json.loads(integrity_path.read_text())
        integrity['event_count'] = len(events)
        integrity['final_event_hash'] = previous
        integrity['manifest_hash'] = research_log.digest(manifest)
        integrity_path.write_bytes(research_log.canonical_bytes(integrity) + b'\n')
    else:
        integrity_path.unlink(missing_ok=True)


def test_elapsed_clocks_preserves_unavailable_and_cross_thread_values():
    start = {'wall_ns': 10, 'process_cpu_ns': 4, 'thread_cpu_ns': 3, '_thread_id': 8}
    end = {'wall_ns': 30, 'process_cpu_ns': 12, 'thread_cpu_ns': 9, '_thread_id': 9}
    value = elapsed_clocks(start, end, 'fixture')
    assert value['complete'] is True
    assert (value['wall_ns'], value['process_cpu_ns']) == (20, 8)
    assert value['thread_cpu_ns'] is None
    assert value['clocks']['thread_cpu'] is None

    partial = elapsed_clocks(start, {'wall_ns': 9, 'process_cpu_ns': None,
                                     'thread_cpu_ns': 4, '_thread_id': 8}, 'fixture')
    assert partial['complete'] is False
    assert partial['wall_ns'] is None
    assert partial['process_cpu_ns'] is None
    assert partial['thread_cpu_ns'] == 1


def test_setup_profile_has_thread_cpu_and_nested_backend_scope():
    walls = iter(range(100, 100 + 10 * (len(STAGES) + 4), 10))
    cpus = iter(range(50, 50 + 4 * (len(STAGES) + 4), 4))
    threads = iter(range(20, 20 + (len(STAGES) + 4), 1))
    timing = SetupTiming(None, backend_expected=True,
                         wall_clock=lambda: next(walls),
                         cpu_clock=lambda: next(cpus),
                         thread_clock=lambda: next(threads))
    for stage in STAGES:
        timing.mark(stage)
        if stage == 'dashboard_ready':
            for backend_stage in ('attach_start', 'instance_ready', 'installation_ready', 'fair_ready'):
                timing.mark_backend(backend_stage)

    result = timing.profile_result()
    assert result['status'] == 'complete'
    assert result['clocks']['thread_cpu'] == 'thread_time_ns'
    for row in result['phases']:
        expected = (50, 20, 5) if row['from'] == 'dashboard_ready' else (10, 4, 1)
        assert (row['wall_ns'], row['process_cpu_ns'], row['thread_cpu_ns']) == expected
    assert all((row['wall_ns'], row['process_cpu_ns'], row['thread_cpu_ns']) == (10, 4, 1)
               for row in result['backend_phases'])
    assert result['scope'] == (
        'ordered_setup_boundaries; backend phases are nested in '
        'dashboard_ready_to_backend_ready')


def test_partial_setup_clock_failure_keeps_only_verified_phase_prefix():
    calls = 0

    def wall_clock():
        nonlocal calls
        current = calls
        calls += 1
        if current == 3:
            raise OSError('fixture clock unavailable')
        return 100 + current * 10

    cpu = iter(range(50, 50 + 4 * (len(STAGES) + 2), 4))
    thread = iter(range(20, 20 + len(STAGES) + 2))
    timing = SetupTiming(None, wall_clock=wall_clock,
                         cpu_clock=lambda: next(cpu), thread_clock=lambda: next(thread))
    for stage in STAGES:
        timing.mark(stage)

    result = timing.profile_result()
    assert result['status'] == 'partial'
    assert [(row['from'], row['to']) for row in result['phases']] == [
        ('setup_start', 'preflight_ready'), ('preflight_ready', 'research_ready')]
    assert _validate_setup_attribution(result, backend='mock') == result


def test_setup_attribution_v1_accepts_exact_historical_phase_order():
    phases = [
        {'from': start, 'to': end, 'wall_ns': 10, 'process_cpu_ns': 3,
         'thread_cpu_ns': None}
        for start, end in zip(LEGACY_SETUP_STAGES, LEGACY_SETUP_STAGES[1:])
    ]
    historical = {
        'schema': 'jev.setup-attribution.v1',
        'status': 'complete',
        'clocks': {'wall': 'perf_counter_ns', 'process_cpu': 'process_time_ns',
                   'thread_cpu': None},
        'scope': 'ordered_setup_boundaries; backend phases are nested in preflight_to_backend',
        'phases': phases,
        'backend_phases': [],
    }

    assert _validate_setup_attribution(historical, backend='mock') == historical

    historical_partial = {**historical, 'status': 'partial', 'phases': phases[:2]}
    assert (_validate_setup_attribution(historical_partial, backend='mock')
            == historical_partial)


@pytest.mark.parametrize('mutation', ['mixed', 'reordered', 'wrong_scope'])
def test_setup_attribution_v1_rejects_mixed_reordered_or_mismatched_scope(mutation):
    timing = SetupTiming(None)
    for stage in STAGES:
        timing.mark(stage)
    current = timing.profile_result()

    if mutation == 'mixed':
        current['phases'][1]['from'] = 'research_ready'
        current['phases'][1]['to'] = 'dashboard_ready'
    elif mutation == 'wrong_scope':
        current['scope'] = (
            'ordered_setup_boundaries; backend phases are nested in preflight_to_backend')
    else:
        current['phases'][1], current['phases'][2] = (
            current['phases'][2], current['phases'][1])

    expected_error = ('Initialization scope disagrees with phase order'
                      if mutation == 'wrong_scope'
                      else 'Invalid initialization phase identity')
    with pytest.raises(ValueError, match=expected_error):
        _validate_setup_attribution(current, backend='mock')


def test_opt_in_cli_profile_records_durable_between_call_window_and_verified_report(
        tmp_path, monkeypatch, capsys):
    emitted = []
    clock_calls = iter((
        {'wall_ns': 100, 'process_cpu_ns': 20, 'thread_cpu_ns': 10, '_thread_id': 7},
        {'wall_ns': 160, 'process_cpu_ns': 40, 'thread_cpu_ns': 20, '_thread_id': 7},
        {'wall_ns': 220, 'process_cpu_ns': 50, 'thread_cpu_ns': 30, '_thread_id': 7},
    ))
    expected_last_event = iter(('model_response', 'model_request', 'model_response'))

    class RecordingSink:
        def __init__(self, sink):
            self.sink = sink

        def emit(self, event_type, payload, **kwargs):
            event = self.sink.emit(event_type, payload, **kwargs)
            emitted.append(event_type)
            return event

    def trace_clock():
        assert emitted and emitted[-1] == next(expected_last_event)
        return next(clock_calls)

    class FakeLoop:
        def __init__(self, backend, *, jev, research_log=None, **kwargs):
            self.backend = backend
            self.jev = jev
            self.memory = None
            self._trace = CausalTrace(RecordingSink(research_log), 'hierarchical', jev,
                                      timing_clock=trace_clock)
            self._client = self._trace.client(jev)

        @profiled_iteration
        def step(self):
            return self._client.evaluate(
                {'tick': 60}, {'next_action': {'type': 'choice', 'criteria': ['observe']}})

        def run(self, *, steps, **kwargs):
            for index in range(steps):
                self.step()
                if index + 1 < steps:
                    from jev_factorio.iteration_timing import loop_sleep
                    loop_sleep(self, 0, lambda: None)

    import jev_factorio.controller as controller
    monkeypatch.setattr(controller, 'HierarchicalLoop', FakeLoop)
    run_dir = tmp_path / 'profiled-run'
    monkeypatch.setattr(sys, 'argv', [
        'jev-factorio', '--backend', 'mock', '--controller', 'hierarchical',
        '--mock-model', '--target', 'bootstrap_mining', '--steps', '2',
        '--tick-seconds', '0', '--run-dir', str(run_dir), '--profile-latency',
    ])

    main.cli()
    capsys.readouterr()

    verified = verify_run(run_dir)
    report = analyze(run_dir)
    events = [json.loads(line) for line in (run_dir / 'events.jsonl').read_text().splitlines()]
    initialized = next(event for event in events if event['event_type'] == 'controller_initialized')
    responses = [event for event in events if event['event_type'] == 'model_response']
    requests = [event for event in events if event['event_type'] == 'model_request']

    assert verified['complete'] and report['integrity_verified']
    assert report['latency_profile_enabled'] is True
    assert report['model_requests'] == report['model_responses'] == 2
    assert report['startup_windows'] == report['initialization_profiles'] == 1
    assert initialized['payload']['startup_window_timing']['complete'] is True
    assert initialized['payload']['initialization_timing']['status'] == 'complete'
    assert 'inter_request_timing' not in responses[0]['payload']
    assert responses[1]['payload']['inter_request_timing']['complete'] is True
    assert (responses[1]['payload']['inter_request_timing']['wall_ns'],
            responses[1]['payload']['inter_request_timing']['process_cpu_ns'],
            responses[1]['payload']['inter_request_timing']['thread_cpu_ns']) == (60, 20, 10)
    assert responses[1]['payload']['inter_request_timing']['scope'] == (
        'clock_sample_after_model_response_event_emission_to_clock_sample_after_next_model_request_emission_before_client_evaluate')
    assert all(response['payload']['client_evaluate_timing']['complete'] for response in responses)
    assert requests[1]['payload']['previous_iteration_timing']['partition_complete'] is True
    assert requests[1]['payload']['previous_iteration_timing']['gap']['complete'] is True
    assert report['counts']['model_inter_request:measured'] == 1
    assert report['distributions']['model_response_operation:wall_ns']['count'] == 2
    assert report['prior_iteration_contexts'] == 1
    assert report['distributions']['model_inter_request:wall_ns']['count'] == 1
    assert report['native_acceptance_proven'] is False
    assert report['deployment_authorized'] is False
    with pytest.raises(ValueError, match='budget'):
        analyze(run_dir, max_records=1)

    event_path = run_dir / 'events.jsonl'
    event_bytes = event_path.read_bytes()
    integrity_path = run_dir / 'integrity.json'
    integrity_bytes = integrity_path.read_bytes()
    altered = [json.loads(line) for line in event_bytes.splitlines()]
    response = next(event for event in altered if event['event_type'] == 'model_response')
    response['correlation']['model_call_id'] = 'model:wrong-link'
    response['payload']['model_call_id'] = 'model:wrong-link'
    _rewrite_hash_chain(run_dir, altered)
    assert verify_run(run_dir)['complete'] is True
    with pytest.raises(ValueError, match='^Invalid verified latency event at record '):
        analyze(run_dir)

    altered = [json.loads(line) for line in event_bytes.splitlines()]
    last_request = max(index for index, event in enumerate(altered)
                       if event['event_type'] == 'model_request')
    _rewrite_hash_chain(run_dir, altered[:last_request + 1], seal=False)
    incomplete = verify_run(run_dir, allow_incomplete=True)
    assert incomplete['complete'] is False
    partial_report = analyze(run_dir, allow_incomplete=True)
    assert partial_report['counts']['model_call:request_without_response'] == 1
    assert 'model_response_event_missing' in partial_report['unavailable']

    event_path.write_bytes(event_bytes)
    integrity_path.write_bytes(integrity_bytes)
    changed = event_bytes.replace(
        b'clock_sample_after_model_response_event_emission_to_clock_sample_after_next_model_request_emission_before_client_evaluate',
        b'clock_sample_after_model_response_event_emission_to_clock_sample_after_next_model_request_emission_before_client_evaluateX', 1)
    assert changed != event_bytes
    event_path.write_bytes(changed)
    with pytest.raises(ValueError):
        analyze(run_dir)


def test_profile_disabled_path_does_not_sample_or_add_event_fields(tmp_path, monkeypatch):
    samples = []

    def forbidden_clock():
        samples.append(True)
        raise AssertionError('disabled latency profiling sampled a clock')

    import jev_factorio.causal_trace as causal_trace
    monkeypatch.setattr(causal_trace, 'sample_clocks', forbidden_clock)

    class Client:
        model = 'offline-test'
        is_mock = True
        last_model = None
        last_usage = None

        def evaluate(self, state, questions):
            return {'result': 'ok'}

    run_dir = tmp_path / 'unprofiled-run'
    with ResearchLog(
            run_dir,
            RunConfiguration('mock', 'hierarchical', 'deterministic'),
            repo_dir=tmp_path, environ={}, timing_wall_clock=forbidden_clock,
            timing_process_clock=forbidden_clock, timing_thread_clock=forbidden_clock) as sink:
        client = Client()
        trace = CausalTrace(sink, 'hierarchical', client, timing_clock=forbidden_clock)
        trace.client(client).evaluate(
            {'tick': 1}, {'next_action': {'type': 'choice', 'criteria': ['observe']}})
        sink.emit('controller_initialized', {'model_is_mock': True})
        sink.finish()

    import jev_factorio.iteration_timing as iteration_timing

    def forbidden_ledger(*args, **kwargs):
        raise AssertionError('disabled latency profiling created an iteration ledger')

    monkeypatch.setattr(iteration_timing, 'Ledger', forbidden_ledger)

    class DisabledLoop:
        profile_latency = False
        factory_scheduling = 'serial'
        backend = SimpleNamespace(profile_observations=False)

        @profiled_iteration
        def step(self):
            return 'unchanged'

    assert DisabledLoop().step() == 'unchanged'
    events = [json.loads(line) for line in (run_dir / 'events.jsonl').read_text().splitlines()]
    assert samples == []
    assert all('startup_window_timing' not in event['payload']
               for event in events if event['event_type'] == 'controller_initialized')
    assert all('inter_request_timing' not in event['payload']
               for event in events if event['event_type'] == 'model_response')


def test_profile_flag_requires_hierarchical_research_directory(monkeypatch):
    monkeypatch.setattr(sys, 'argv', [
        'jev-factorio', '--backend', 'mock', '--controller', 'hierarchical',
        '--mock-model', '--profile-latency',
    ])
    with pytest.raises(SystemExit):
        main.cli()
