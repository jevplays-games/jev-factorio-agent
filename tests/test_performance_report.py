"""Incremental timing/report contracts; no latency targets tied to host speed."""
import gzip
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from jev_factorio.causal_trace import CausalTrace
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.performance import PerformanceCounters, cli, summarize
from jev_factorio.telemetry import phase
from jev_factorio.wait_record_codec import Encoder, encode_line
from test_causal_trace import Backend, Client, Sink


def test_calls_are_incremental_fixed_vocabulary_and_include_failures():
    counters = PerformanceCounters()
    counters.call('observation', 20)
    counters.call('observation', 40, failed=True)
    counters.call('unbounded-user-string', 1)
    captured = counters.snapshot()
    assert captured['calls'] == {'observation': {'count': 2, 'failed': 1, 'total_ns': 60, 'max_ns': 40}}
    captured['calls']['observation']['count'] = 50
    assert counters.snapshot()['calls']['observation']['count'] == 2
    assert captured['durations_are_inclusive']


@pytest.mark.parametrize('enabled', [False, True])
def test_measured_call_does_not_run_operation_twice_or_capture_without_tracing(enabled):
    trace = CausalTrace(Sink() if enabled else None, 'hierarchical')
    trace.metrics = PerformanceCounters()
    calls = []
    def operation(): calls.append('call'); return 3
    def capture(value):
        calls.append('capture')
        return {'value': value}
    assert trace.call('observation', operation, result=capture) == 3
    assert calls == (['call', 'capture'] if enabled else ['call'])
    assert trace.metrics.snapshot()['calls']['observation']['count'] == 1
    with pytest.raises(TimeoutError):
        trace.call('model_response', lambda: (_ for _ in ()).throw(TimeoutError('secret')))
    assert trace.metrics.snapshot()['calls']['model_response']['failed'] == 1


def test_model_boundary_is_measured_even_without_research_log():
    trace = CausalTrace(None, 'hierarchical')
    trace.metrics = PerformanceCounters()
    class Model:
        def evaluate(self, state, questions): return {'answer': 1}
    assert trace.client(Model()).evaluate({}, {}) == {'answer': 1}
    assert trace.metrics.snapshot()['calls']['model_response']['count'] == 1


def test_controller_counters_reset_per_iteration_and_do_not_add_observations(tmp_path):
    backend = Backend()
    loop = HierarchicalLoop(backend, Client(), checkpoint=str(tmp_path/'state.json'),
                            log_file=str(tmp_path/'log.jsonl'), factory_scheduling='ready-work')
    records = [loop.step(), loop.step()]
    assert len([call for call in backend.calls if call[0] == 'observe']) == sum(
        record['performance']['calls']['observation']['count'] for record in records)
    assert all(record['performance']['calls']['observation']['count'] == 3 for record in records)
    result = summarize(tmp_path/'log.jsonl')
    assert result['records'] == result['instrumented_records'] == 2
    assert result['calls']['observation']['count'] == 6
    assert result['checkpoints']['written'] > 0
    assert all(name in records[0]['performance'] for name in (
        'clock', 'durations_are_inclusive', 'scope', 'calls', 'checkpoints',
        'checkpoint_ns'))
    assert isinstance(records[0]['phases'], list)
    assert result['calls']['checkpoint_written']['count'] == sum(
        result['checkpoints'].get(k, 0) for k in ('written', 'unchanged', 'failed', 'disabled'))
    assert not result['wall_time_or_speedup_inferred']


def make_record():
    counters = PerformanceCounters()
    counters.call('observation', 20)
    counters.call('model_response', 70)
    counters.checkpoint({'status': 'written', 'bytes': 10, 'file_sync_ns': 7})
    events = []
    with phase('observe', events.append): pass
    return {'action': 'observe', 'performance': counters.snapshot(), 'phases': events}


@pytest.mark.parametrize('compressed', [False, True])
def test_plain_and_gzip_legacy_records_remain_explicit(tmp_path, compressed):
    path = tmp_path/('log.jsonl.gz' if compressed else 'log.jsonl')
    opener = gzip.open if compressed else open
    with opener(path, 'wt', encoding='utf-8') as stream:
        stream.write(json.dumps({'action': 'factory_wait'})+'\n')
        stream.write(json.dumps(make_record())+'\n')
    data = summarize(path)
    assert data['records'] == 2 and data['legacy_records'] == 1
    assert data['instrumented_records'] == 1
    assert data['calls']['observation']['total_ns'] == 20
    assert data['calls']['observation']['mean_ns'] == 20
    assert data['checkpoints']['bytes_written'] == 10


@pytest.mark.parametrize('corruption', ['bad_schema', 'negative', 'nan', 'unknown_call', 'unknown_status',
                                        'bad_phase', 'truncated', 'not_object'])
def test_malformed_records_do_not_silently_disappear(tmp_path, corruption):
    path = tmp_path/'log.jsonl'
    record = make_record()
    if corruption == 'bad_schema': record['performance']['schema'] = 9
    elif corruption == 'negative': record['performance']['calls']['observation']['total_ns'] = -1
    elif corruption == 'nan': record['performance']['calls']['observation']['total_ns'] = float('nan')
    elif corruption == 'unknown_call': record['performance']['calls']['private'] = {}
    elif corruption == 'unknown_status': record['performance']['checkpoints']['private'] = 1
    elif corruption == 'bad_phase': record['phases'] = [{'stage': 'bad'}]
    elif corruption == 'not_object': record = []
    text = json.dumps(record)
    if corruption == 'truncated': text = text[:-2]
    path.write_text(text)
    expected = 'line 1' if corruption in {'nan', 'truncated', 'not_object'} else 'logical record'
    with pytest.raises(ValueError, match=expected): summarize(path)


def test_report_does_not_add_nested_durations_as_wall_time(tmp_path):
    record = make_record()
    record['performance']['calls']['observation']['total_ns'] = 100
    record['performance']['calls']['model_response'] = {
        'count': 1, 'failed': 0, 'total_ns': 70, 'max_ns': 70}
    path=tmp_path/'log.jsonl';path.write_text(json.dumps(record)+'\n')
    result = summarize(path)
    assert 'total_wall_ns' not in result
    assert result['durations_are_inclusive'] is True


def test_benchmark_preserves_all_changed_writes_and_restores_sync_function():
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location('checkpoint_benchmark',
        Path(__file__).parents[1] / 'scripts/benchmark_checkpoint_io.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = os.fsync
    result = module.benchmark(5)
    assert result['cases'][0]['fsync_calls'] == {'file': 1, 'directory': 1}
    assert result['cases'][1]['fsync_calls'] == {'file': 5, 'directory': 5}
    assert os.fsync is original
    with pytest.raises(ValueError): module.benchmark(0)


def test_report_preserves_failed_call_count(tmp_path):
    record = make_record()
    record['performance']['calls']['observation']['failed'] = 1
    path = tmp_path/'log.jsonl'; path.write_text(json.dumps(record)+'\n')
    assert summarize(path)['calls']['observation']['failed'] == 1


@pytest.mark.parametrize('missing', ['calls', 'checkpoints', 'checkpoint_ns', 'phases', 'schema_only'])
def test_declared_instrumentation_requires_complete_producer_payload(tmp_path, missing):
    record = make_record()
    if missing == 'schema_only':
        record['performance'] = {'schema': 1}
    elif missing == 'phases':
        del record['phases']
    else:
        del record['performance'][missing]
    path = tmp_path/'incomplete.jsonl'
    path.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError, match=r'logical record \(index=1'):
        summarize(path)


@pytest.mark.parametrize('mutation', [
    'null_performance', 'calls_null', 'calls_list', 'checkpoints_null',
    'checkpoint_ns_list', 'phases_null', 'phases_dict', 'cpu_calls_null',
    'cpu_clock_only', 'cpu_calls_without_clock', 'checkpoint_operations_null', 'bad_clock',
    'false_inclusive', 'bad_scope',
])
def test_wrong_typed_or_declared_performance_structures_fail_closed(tmp_path, mutation):
    record = make_record()
    metrics = record['performance']
    if mutation == 'null_performance':
        record['performance'] = None
    elif mutation == 'calls_null':
        metrics['calls'] = None
    elif mutation == 'calls_list':
        metrics['calls'] = []
    elif mutation == 'checkpoints_null':
        metrics['checkpoints'] = None
    elif mutation == 'checkpoint_ns_list':
        metrics['checkpoint_ns'] = []
    elif mutation == 'phases_null':
        record['phases'] = None
    elif mutation == 'phases_dict':
        record['phases'] = {}
    elif mutation == 'cpu_calls_null':
        metrics['cpu_calls'] = None
    elif mutation == 'cpu_clock_only':
        del metrics['cpu_calls']
    elif mutation == 'cpu_calls_without_clock':
        del metrics['cpu_clock']
    elif mutation == 'checkpoint_operations_null':
        metrics['checkpoint_operations'] = None
    elif mutation == 'bad_clock':
        metrics['clock'] = 'wall_time'
    elif mutation == 'false_inclusive':
        metrics['durations_are_inclusive'] = 1
    else:
        metrics['scope'] = 'unknown'
    path = tmp_path/'wrong-type.jsonl'
    path.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError, match=r'logical record \(index=1'):
        summarize(path)


@pytest.mark.parametrize('field', ['count', 'failed', 'total_ns', 'max_ns'])
def test_call_metrics_require_every_producer_field(tmp_path, field):
    record = make_record()
    del record['performance']['calls']['observation'][field]
    path = tmp_path/'missing-counter.jsonl'
    path.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError, match=r'logical record \(index=1'):
        summarize(path)


@pytest.mark.parametrize('field,value', [
    ('count', True), ('count', 0), ('failed', True), ('failed', 2),
    ('total_ns', True), ('total_ns', -1), ('total_ns', 1.5),
    ('max_ns', True), ('max_ns', -1), ('max_ns', 21),
])
def test_invalid_call_counts_durations_and_failure_semantics_fail(tmp_path, field, value):
    record = make_record()
    record['performance']['calls']['observation'][field] = value
    path = tmp_path/'bad-counter.jsonl'
    path.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError, match=r'logical record \(index=1'):
        summarize(path)


@pytest.mark.parametrize('field,value', [
    ('checkpoints', {'written': True}), ('checkpoints', {'written': 1.5}),
    ('checkpoints', {'private': 1}), ('checkpoint_ns', {'serialize_ns': False}),
    ('checkpoint_ns', {'serialize_ns': -1}), ('checkpoint_ns', {'private_ns': 1}),
    ('checkpoint_operations', {'capture_calls': True}),
    ('checkpoint_operations', {'private_metric': 1}),
])
def test_invalid_checkpoint_metric_types_and_labels_fail(tmp_path, field, value):
    record = make_record()
    record['performance'][field] = value
    path = tmp_path/'bad-checkpoint.jsonl'
    path.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError, match=r'logical record \(index=1'):
        summarize(path)


@pytest.mark.parametrize('seconds', [True, -0.01, '1.5'])
def test_phase_durations_reject_booleans_negative_and_wrong_types(tmp_path, seconds):
    record = make_record()
    record['phases'][0]['seconds'] = seconds
    path = tmp_path/'bad-phase.jsonl'
    path.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError, match=r'logical record \(index=1'):
        summarize(path)


def test_historical_complete_schema_one_without_later_extensions_remains_supported(tmp_path):
    record = make_record()
    for field in ('cpu_clock', 'cpu_calls', 'checkpoint_operations'):
        record['performance'].pop(field)
    path = tmp_path/'historical-schema-one.jsonl'
    path.write_text(json.dumps(record)+'\n')
    result = summarize(path)
    assert result['instrumented_records'] == 1
    assert result['legacy_records'] == 0
    assert result['calls']['observation']['count'] == 1
    assert result['cpu_timed_records'] == 0
    assert result['checkpoint_operations'] == {}


def test_complete_empty_maps_and_zero_duration_measurements_are_not_missing(tmp_path):
    empty = make_record()
    empty['performance']['calls'] = {}
    empty['performance']['checkpoints'] = {}
    empty['performance']['checkpoint_ns'] = {}
    empty['phases'] = []
    empty_path = tmp_path/'empty-measured.jsonl'
    empty_path.write_text(json.dumps(empty)+'\n')
    empty_result = summarize(empty_path)
    assert empty_result['instrumented_records'] == 1
    assert empty_result['legacy_records'] == 0
    assert empty_result['calls'] == {}
    assert empty_result['checkpoints'] == {}
    assert empty_result['checkpoint_ns'] == {}

    zero = make_record()
    zero['performance']['calls'] = {
        'observation': {'count': 1, 'failed': 0, 'total_ns': 0, 'max_ns': 0}}
    zero['performance']['checkpoints'] = {}
    zero['performance']['checkpoint_ns'] = {}
    next(event for event in zero['phases'] if event['status'] != 'started')['seconds'] = 0
    zero_path = tmp_path/'zero-duration.jsonl'
    zero_path.write_text(json.dumps(zero)+'\n')
    zero_result = summarize(zero_path)
    assert zero_result['instrumented_records'] == 1
    assert zero_result['calls']['observation'] == {
        'count': 1, 'failed': 0, 'total_ns': 0, 'max_ns': 0, 'mean_ns': 0}
    assert zero_result['phases']['observe']['total_ns'] == 0


def _identity_record(tick):
    record = make_record()
    record.update(
        session_id='session-performance351', process_id='process-17',
        run_id='run-performance351', segment_id='segment-4',
        execution_id='execution-9', code_revision={
            'commit': 'a' * 40, 'source_sha256': 'b' * 64},
        world_kind='fle', target='rocket_launch', policy='jev',
        tick=tick, stable_context={'unchanged': ['value'] * 256})
    record['state'] = {'tick': tick}
    record['after_state'] = {'tick': tick}
    return record


def _write_wait_stream(path, rows, *, compressed=False):
    encoder = Encoder('gameplay')
    lines = []
    deltas = []
    for index, row in enumerate(rows):
        prepared = encoder.prepare(row, wait=index > 0)
        line = encode_line(prepared)
        encoder.commit(prepared, len(line))
        lines.append(line)
        deltas.append(prepared.is_delta)
    payload = b''.join(lines)
    if compressed:
        with gzip.open(path, 'wb') as stream:
            stream.write(payload)
    else:
        path.write_bytes(payload)
    return deltas


@pytest.mark.parametrize('compressed', [False, True])
def test_summarize_reads_lossless_wait_codec_logical_rows(tmp_path, compressed):
    path = tmp_path/('wait.jsonl.gz' if compressed else 'wait.jsonl')
    rows = [_identity_record(10), _identity_record(11)]
    deltas = _write_wait_stream(path, rows, compressed=compressed)
    assert deltas == [False, True]
    result = summarize(path)
    assert result['records'] == result['instrumented_records'] == 2
    assert result['calls']['observation']['count'] == 2


def test_incomplete_delta_reports_reconstructed_logical_record_identity(tmp_path):
    first, second = _identity_record(20), _identity_record(21)
    del second['performance']['calls']
    path = tmp_path/'identity.jsonl'
    deltas = _write_wait_stream(path, [first, second])
    assert deltas == [False, True]
    with pytest.raises(ValueError) as error:
        summarize(path)
    message = str(error.value)
    assert 'logical record (index=2' in message
    assert 'session_id=session-performance351' in message
    assert 'process_id=process-17' in message
    assert 'run_id=run-performance351' in message
    assert 'segment_id=segment-4' in message
    assert 'execution_id=execution-9' in message
    assert 'tick=21' in message


def test_error_identity_is_bounded_for_malformed_row_and_large_tick(tmp_path):
    row = _identity_record(10**1000)
    del row['performance']['calls']
    path = tmp_path/'bounded-identity.jsonl'
    path.write_text(json.dumps(row)+'\n')
    with pytest.raises(ValueError) as error:
        summarize(path)
    message = str(error.value)
    assert 'logical record (index=1' in message
    assert 'tick=' not in message
    assert len(message) < 256


def test_oversized_integer_phase_duration_is_rejected_with_record_identity(tmp_path):
    row = make_record()
    row['phases'][1]['seconds'] = 10**1000
    path = tmp_path/'huge-phase.jsonl'
    path.write_text(json.dumps(row)+'\n')
    with pytest.raises(ValueError, match=r'logical record \(index=1'):
        summarize(path)


@pytest.mark.parametrize('table,cpu', [('calls', False), ('cpu_calls', True)])
def test_unrepresentable_integer_mean_is_rejected_before_aggregation(tmp_path, table, cpu):
    path = tmp_path/f'unrepresentable-{table}.jsonl'
    rows = [make_record(), _identity_record(44)]
    huge = 10**1000
    metrics = rows[1]['performance']
    metrics[table]['observation'] = {
        'count': 1, 'total_ns': huge, 'max_ns': huge,
    }
    if not cpu:
        metrics[table]['observation']['failed'] = 0
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    with pytest.raises(ValueError) as error:
        summarize(path)
    message = str(error.value)
    assert 'logical record (index=2' in message
    assert 'session_id=session-performance351' in message
    assert 'tick=44' in message
    assert 'OverflowError' not in message


def test_large_integer_counters_with_finite_mean_are_not_arbitrarily_capped(tmp_path):
    row = make_record()
    large = 10**1000
    row['performance']['calls']['observation'] = {
        'count': large, 'failed': 0, 'total_ns': large, 'max_ns': large,
    }
    path = tmp_path/'large-but-representable.jsonl'
    path.write_text(json.dumps(row)+'\n')
    result = summarize(path)
    assert result['calls']['observation']['count'] == large
    assert result['calls']['observation']['total_ns'] == large
    assert result['calls']['observation']['max_ns'] == large
    assert result['calls']['observation']['mean_ns'] == 1.0


def test_finite_seconds_with_unrepresentable_nanosecond_mean_is_rejected(tmp_path):
    row = _identity_record(45)
    next(event for event in row['phases'] if event['status'] == 'returned')['seconds'] = 1.8e299
    path = tmp_path/'unrepresentable-phase-ns.jsonl'
    path.write_text(json.dumps(row)+'\n')
    with pytest.raises(ValueError) as error:
        summarize(path)
    assert 'logical record (index=1' in str(error.value)
    assert 'session_id=session-performance351' in str(error.value)


def test_cli_returns_redacted_error_for_unrepresentable_call_counter(
        tmp_path, monkeypatch, capsys):
    row = make_record()
    row.update(session_id='private-session-marker', process_id='private-process-marker')
    huge = 10**1000
    row['performance']['calls']['observation'].update(total_ns=huge, max_ns=huge)
    path = tmp_path/'unrepresentable.jsonl'
    path.write_text(json.dumps(row)+'\n')
    monkeypatch.setattr(sys, 'argv', ['jev-factorio.performance', str(path)])
    with pytest.raises(SystemExit) as exit_info:
        cli()
    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert 'complete, valid performance stream' in captured.err
    assert 'OverflowError' not in captured.err
    assert 'private-session-marker' not in captured.err
    assert 'private-process-marker' not in captured.err


def test_cli_accepts_complete_payload_and_rejects_incomplete_without_echoing_identity(
        tmp_path, monkeypatch, capsys):
    valid = tmp_path/'valid.jsonl'
    valid.write_text(json.dumps(make_record())+'\n')
    monkeypatch.setattr(sys, 'argv', ['jev-factorio.performance', str(valid)])
    cli()
    parsed = json.loads(capsys.readouterr().out)
    assert parsed['instrumented_records'] == 1

    invalid_row = make_record()
    invalid_row.update(session_id='private-session-marker', process_id='private-process-marker')
    del invalid_row['performance']['calls']
    invalid = tmp_path/'invalid.jsonl'
    invalid.write_text(json.dumps(invalid_row)+'\n')
    monkeypatch.setattr(sys, 'argv', ['jev-factorio.performance', str(invalid)])
    with pytest.raises(SystemExit) as exit_info:
        cli()
    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert 'complete, valid performance stream' in captured.err
    assert 'private-session-marker' not in captured.err
    assert 'private-process-marker' not in captured.err
