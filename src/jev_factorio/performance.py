"""Offline, streaming controller-overhead report; never initializes a backend.

Durations are inclusive: observation/dispatch may contain checkpoint calls;
selection contains model evaluation. They must not be added as disjoint time.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import Counter
from copy import deepcopy
from pathlib import Path

CALLS = {'observation', 'model_response', 'candidate_set_created', 'checkpoint_written',
         'trace_capture', 'trace_emit', 'action_returned', 'verification'}
CHECKPOINT_TIMINGS = {'serialize_ns', 'capture_ns', 'json_encode_ns',
                      'file_sync_ns', 'directory_sync_ns', 'total_ns', 'compare_ns',
                      'installation_check_ns'}
CHECKPOINT_IO_OPERATIONS = {'file_sync_calls', 'directory_sync_calls', 'parent_directory_sync_calls',
                            'verification_read_calls', 'verification_read_bytes'}
CHECKPOINT_STATUSES = {'written', 'unchanged', 'failed', 'disabled'}
CHECKPOINT_OPERATION_COUNTS = {
    'capture_calls', 'serialization_calls', 'measured_calls', 'io_measured_calls',
    'phase_fields_present_calls',
} | CHECKPOINT_IO_OPERATIONS
PERFORMANCE_CLOCK = 'perf_counter_ns'
PERFORMANCE_SCOPE = 'current_iteration_through_checkpoint_before_record'
CPU_CLOCK = 'process_time_ns'


class PerformanceCounters:
    def __init__(self):
        self.calls: dict[str, dict] = {}
        self.checkpoints = Counter()
        self.checkpoint_ns = Counter()
        self.cpu_calls: dict[str, dict] = {}
        self.checkpoint_operations = Counter()

    def call(self, name: str, duration_ns: int, failed: bool = False, *, cpu_ns: int | None = None) -> None:
        if name not in CALLS:
            return
        row = self.calls.setdefault(name, {'count': 0, 'failed': 0, 'total_ns': 0, 'max_ns': 0})
        row['count'] += 1
        row['failed'] += int(failed)
        row['total_ns'] += max(0, duration_ns)
        row['max_ns'] = max(row['max_ns'], duration_ns)
        if cpu_ns is not None:
            cpu = self.cpu_calls.setdefault(name, {'count': 0, 'total_ns': 0, 'max_ns': 0})
            cpu['count'] += 1
            cpu['total_ns'] += max(0, cpu_ns)
            cpu['max_ns'] = max(cpu['max_ns'], cpu_ns)

    def checkpoint(self, metrics: dict) -> None:
        status = metrics.get('status')
        if status not in CHECKPOINT_STATUSES:
            return
        self.checkpoints[status] += 1
        if status == 'written':
            self.checkpoints['bytes_written'] += metrics['bytes']
        for key in CHECKPOINT_TIMINGS:
            if key in metrics:
                self.checkpoint_ns[key] += metrics[key]
        if {'capture_ns', 'json_encode_ns'} <= metrics.keys():
            # Presence marks current-schema metric coverage, including exact
            # repeats that correctly perform neither capture nor encoding.
            self.checkpoint_operations['phase_fields_present_calls'] += 1
        if 'capture_calls' in metrics and 'serialization_calls' in metrics:
            self.checkpoint_operations['measured_calls'] += 1
            for key in ('capture_calls', 'serialization_calls'):
                self.checkpoint_operations[key] += metrics[key]
        if CHECKPOINT_IO_OPERATIONS <= metrics.keys():
            self.checkpoint_operations['io_measured_calls'] += 1
            for key in CHECKPOINT_IO_OPERATIONS:
                self.checkpoint_operations[key] += metrics[key]

    def snapshot(self) -> dict:
        return {'schema': 1, 'clock': PERFORMANCE_CLOCK, 'durations_are_inclusive': True,
                'scope': PERFORMANCE_SCOPE,
                'calls': deepcopy(self.calls), 'checkpoints': dict(self.checkpoints),
                'checkpoint_ns': dict(self.checkpoint_ns), 'cpu_clock': CPU_CLOCK,
                'cpu_calls': deepcopy(self.cpu_calls),
                'checkpoint_operations': dict(self.checkpoint_operations)}


def _nonnegative_int(value) -> bool:
    return type(value) is int and value >= 0


def _mean_ns(total_ns: int, count: int) -> float | None:
    if count == 0:
        return None
    try:
        mean = total_ns / count
    except OverflowError as error:
        raise ValueError('Unrepresentable performance mean') from error
    if not math.isfinite(mean):
        raise ValueError('Nonfinite performance mean')
    return mean


def _validate_call_table(table: dict, *, cpu: bool = False) -> None:
    if type(table) is not dict:
        raise ValueError('Invalid timed-call table')
    required = {'count', 'total_ns', 'max_ns'} if cpu else {
        'count', 'failed', 'total_ns', 'max_ns'}
    for name, value in table.items():
        if type(name) is not str or name not in CALLS:
            raise ValueError('Unknown timed call')
        if type(value) is not dict or not required <= value.keys():
            raise ValueError('Invalid timed-call metrics')
        count, total, maximum = value['count'], value['total_ns'], value['max_ns']
        if (not _nonnegative_int(count) or count < 1
                or not _nonnegative_int(total) or not _nonnegative_int(maximum)
                or maximum > total):
            raise ValueError('Invalid nonnegative performance counter')
        _mean_ns(total, count)
        if not cpu:
            failed = value['failed']
            if not _nonnegative_int(failed) or failed > count:
                raise ValueError('Invalid failed-call count')


def _validate_counter_table(table: dict, permitted: set[str], label: str) -> None:
    if type(table) is not dict:
        raise ValueError(f'Invalid {label} table')
    for key, value in table.items():
        if type(key) is not str or key not in permitted or not _nonnegative_int(value):
            raise ValueError(f'Invalid {label} metric')


def _validate_performance_record(row: dict) -> tuple[dict, list[tuple[str, int]], list[str]]:
    """Validate one declared payload completely before it contributes to a report."""
    metrics = row.get('performance')
    if type(metrics) is not dict:
        raise ValueError('Invalid performance payload')
    if type(metrics.get('schema')) is not int or metrics['schema'] != 1:
        raise ValueError('Unsupported performance schema')
    if (metrics.get('clock') != PERFORMANCE_CLOCK
            or metrics.get('durations_are_inclusive') is not True
            or metrics.get('scope') != PERFORMANCE_SCOPE):
        raise ValueError('Invalid performance timing declaration')

    # These three maps and the top-level phase list are emitted by both the
    # original and current schema-1 controller producers. Empty maps/lists are
    # valid measured zeroes; omitted containers are incomplete instrumentation.
    for field in ('calls', 'checkpoints', 'checkpoint_ns'):
        if field not in metrics:
            raise ValueError(f'Missing required performance field: {field}')
    if 'phases' not in row:
        raise ValueError('Missing required performance field: phases')

    calls = metrics['calls']
    checkpoints = metrics['checkpoints']
    checkpoint_ns = metrics['checkpoint_ns']
    _validate_call_table(calls)
    _validate_counter_table(checkpoints, CHECKPOINT_STATUSES | {'bytes_written'}, 'checkpoint')
    _validate_counter_table(checkpoint_ns, CHECKPOINT_TIMINGS, 'checkpoint timing')
    written = checkpoints.get('written', 0)
    if (written > 0) != ('bytes_written' in checkpoints) or (
            'bytes_written' in checkpoints and written == 0):
        raise ValueError('Checkpoint byte count does not match written calls')

    cpu_fields = {'cpu_clock', 'cpu_calls'}
    if cpu_fields & metrics.keys() and not cpu_fields <= metrics.keys():
        raise ValueError('Incomplete CPU timing extension')
    cpu_calls = metrics.get('cpu_calls')
    if 'cpu_calls' in metrics:
        _validate_call_table(cpu_calls, cpu=True)
    if 'cpu_clock' in metrics and metrics['cpu_clock'] != CPU_CLOCK:
        raise ValueError('Unsupported CPU clock')

    checkpoint_operations = metrics.get('checkpoint_operations')
    if 'checkpoint_operations' in metrics:
        _validate_counter_table(checkpoint_operations, CHECKPOINT_OPERATION_COUNTS,
                                'checkpoint operation')

    events = row['phases']
    if type(events) is not list:
        raise ValueError('Invalid phase list')
    from .telemetry import validate_phase
    phases = []
    for event in events:
        validate_phase(event)
        if event['status'] == 'started':
            continue
        elapsed = event['seconds']
        if (type(elapsed) not in (int, float) or not math.isfinite(elapsed)
                or elapsed < 0):
            raise ValueError('Invalid phase duration')
        elapsed_ns = elapsed * 1_000_000_000
        if not math.isfinite(elapsed_ns):
            raise ValueError('Invalid phase duration')
        ns = round(elapsed_ns)
        _mean_ns(ns, 1)
        phases.append((event['stage'], ns))

    capacity = row.get('capacity_evidence', {})
    if type(capacity) is not dict:
        raise ValueError('Invalid capacity evidence')
    producers = capacity.get('producers', {})
    if type(producers) is not dict:
        raise ValueError('Invalid capacity producer evidence')
    reasons = []
    for value in producers.values():
        if type(value) is not dict:
            raise ValueError('Invalid capacity reason')
        reason = value.get('reason')
        if reason not in {'sustained_supplied_production', 'insufficient_history',
                          'supply_or_activity_constraint', 'output_or_transport_constraint',
                          'rate_outside_supported_band', 'missing_or_unsupported_evidence'}:
            raise ValueError('Invalid capacity reason')
        reasons.append(reason)

    validated = {
        'metrics': metrics,
        'calls': calls,
        'checkpoints': checkpoints,
        'checkpoint_ns': checkpoint_ns,
        'cpu_calls': cpu_calls,
        'checkpoint_operations': checkpoint_operations or {},
    }
    return validated, phases, reasons


def _identity_component(value) -> str | None:
    if type(value) is int and 0 <= value <= (2 ** 63 - 1):
        return str(value)
    if (type(value) is str and 0 < len(value) <= 128 and value.isascii()
            and all(char.isalnum() or char in '._:-' for char in value)):
        return value
    return None


def _logical_record_identity(row: dict, number: int, action: str) -> str:
    parts = [f'index={number}']
    for field in ('session_id', 'process_id', 'run_id', 'segment_id', 'execution_id'):
        value = _identity_component(row.get(field))
        if value is not None:
            parts.append(f'{field}={value}')
    tick = row.get('tick')
    after = row.get('after_state')
    state = row.get('state')
    if type(after) is dict and type(after.get('tick')) is int:
        tick = after['tick']
    elif type(state) is dict and type(state.get('tick')) is int:
        tick = state['tick']
    value = _identity_component(tick)
    if value is not None:
        parts.append(f'tick={value}')
    if action != 'unknown':
        parts.append(f'action={action}')
    return ', '.join(parts)


def summarize(path: Path) -> dict:
    """Aggregate incremental per-record metrics, not repeated attempt histories.

    Legacy records are counted separately, never assigned zero overhead. This
    reports the supplied stream as-is; pass one nonduplicated campaign export.
    Incomplete or malformed records fail explicitly instead of disappearing.
    """
    summary = {'schema': 1, 'records': 0, 'instrumented_records': 0,
               'legacy_records': 0, 'calls': {}, 'phases': {}, 'checkpoints': {},
               'checkpoint_ns': {}, 'checkpoint_operations': {}, 'cpu_calls': {},
               'cpu_timed_records': 0, 'actions': {}, 'capacity_reasons': {},
               'durations_are_inclusive': True,
               'wall_time_or_speedup_inferred': False}
    def merge(destination, key, count, total, maximum):
        if any(type(v) is not int or v < 0 for v in (count, total, maximum)):
            raise ValueError('Invalid nonnegative performance counter')
        previous = destination.get(key, {'count': 0, 'total_ns': 0, 'max_ns': 0})
        merged_count = previous['count'] + count
        merged_total = previous['total_ns'] + total
        _mean_ns(merged_total, merged_count)
        destination[key] = {
            'count': merged_count,
            'total_ns': merged_total,
            'max_ns': max(previous['max_ns'], maximum),
        }
    opener = gzip.open if path.suffix == '.gz' else open
    from .wait_record_codec import iter_stream
    with opener(path, 'rb') as stream:
        for number, row in enumerate(iter_stream(stream, 'gameplay', max_records=None,
                                                  require_final_newline=False, skip_blank=True), 1):
            try:
                if not isinstance(row, dict):
                    raise ValueError('Invalid record')
                action = row.get('action', 'unknown')
                # Fixed vocabulary, not arbitrary native payloads or error messages.
                from .skills import ACTIONS
                from .factory_contract import COMMAND_FIELDS
                allowed_actions = ACTIONS | COMMAND_FIELDS.keys() | {'observe', 'verify', 'reconcile'}
                action = action if type(action) is str and action in allowed_actions else 'unknown'
                if 'performance' not in row:
                    summary['records'] += 1
                    summary['actions'][action] = summary['actions'].get(action, 0) + 1
                    summary['legacy_records'] += 1
                    continue
                validated, phases, reasons = _validate_performance_record(row)
                metrics = validated['metrics']
                summary['records'] += 1
                summary['instrumented_records'] += 1
                summary['actions'][action] = summary['actions'].get(action, 0) + 1
                for name, value in validated['calls'].items():
                    merge(summary['calls'], name, value['count'], value['total_ns'], value['max_ns'])
                    summary['calls'][name]['failed'] = summary['calls'][name].get('failed', 0) + value['failed']
                cpu_calls = validated['cpu_calls']
                if cpu_calls is not None:
                    summary['cpu_timed_records'] += 1
                    for name, value in cpu_calls.items():
                        merge(summary['cpu_calls'], name, value['count'], value['total_ns'], value['max_ns'])
                for category in ('checkpoints', 'checkpoint_ns', 'checkpoint_operations'):
                    values = validated[category] if category in validated else metrics.get(category, {})
                    for key, value in values.items():
                        summary[category][key] = summary[category].get(key, 0) + value
                for stage, ns in phases:
                    merge(summary['phases'], stage, 1, ns, ns)
                for reason in reasons:
                    summary['capacity_reasons'][reason] = summary['capacity_reasons'].get(reason, 0) + 1
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
                identity = _logical_record_identity(row, number, action) if isinstance(row, dict) else f'index={number}'
                raise ValueError(f'Invalid performance record at logical record ({identity})') from error
    for kind in ('calls', 'phases', 'cpu_calls'):
        for row in summary[kind].values():
            row['mean_ns'] = _mean_ns(row['total_ns'], row['count'])
    return summary


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path, help='One nonduplicated JSONL or JSONL.gz gameplay stream')
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(args.log), indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError):
        parser.exit(2, 'Could not read a complete, valid performance stream.\n')


if __name__ == '__main__':
    cli()
