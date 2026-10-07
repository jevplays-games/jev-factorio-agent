"""Bounded offline latency distributions with explicit nested/unknown scopes.

Reads a single nonduplicated gameplay stream, never opens a game connection.
Only fixed metric names, numeric counters and validated source digests leave
this report. Session IDs, process IDs, native payloads and paths do not.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import gzip
import json
import math
from pathlib import Path
import re
import statistics

from .observation import LABELS
from .performance import (CALLS, CHECKPOINT_STATUSES, CHECKPOINT_TIMINGS,
                          CHECKPOINT_IO_OPERATIONS)
from .iteration_timing import validate_timing, CLOCKS
from .telemetry import STAGES, validate_phase

MAX_LINE = 8 * 1024 * 1024
MAX_RECORDS = 100_000
PARTS = {'rpc', 'helpers', 'decode', 'unattributed'}
NATIVE_STAGES = {'campaign_snapshot', 'discovery', 'serialize'}
CHECKPOINT_OPERATIONS = ({'capture_calls', 'serialization_calls', 'measured_calls',
                         'io_measured_calls', 'phase_fields_present_calls'}
                        | CHECKPOINT_IO_OPERATIONS)


def distribution(values: list[int]) -> dict:
    ordered = sorted(values)
    return {'count': len(ordered), 'median_ns': statistics.median(ordered) if ordered else None,
            'p95_ns': ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None,
            'total_ns': sum(ordered)}


def nonnegative(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError('Invalid nonnegative integer')
    return value


def _validate_clock_window(value: object, *, scope: str) -> dict:
    fields = {'schema', 'complete', 'clocks', 'wall_ns', 'process_cpu_ns',
              'thread_cpu_ns', 'scope', 'cpu_scope'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['complete']) is not bool or value['scope'] != scope
            or value['cpu_scope'] != 'process_cpu_includes_other_python_threads; thread_cpu_is_current_thread'):
        raise ValueError('Invalid clock attribution')
    clocks = value['clocks']
    if (not isinstance(clocks, dict) or set(clocks) != {'wall', 'process_cpu', 'thread_cpu'}
            or clocks['wall'] != 'perf_counter_ns' or clocks['process_cpu'] != 'process_time_ns'
            or clocks['thread_cpu'] not in (None, 'thread_time_ns')):
        raise ValueError('Invalid clock identities')
    for key in ('wall_ns', 'process_cpu_ns', 'thread_cpu_ns'):
        if value[key] is not None:
            nonnegative(value[key])
    complete = value['wall_ns'] is not None and value['process_cpu_ns'] is not None
    if value['complete'] != complete:
        raise ValueError('Clock availability disagrees with completeness')
    if (clocks['thread_cpu'] == 'thread_time_ns') != (value['thread_cpu_ns'] is not None):
        raise ValueError('Thread clock availability disagrees with its sample')
    return value


def _validate_setup_attribution(value: object, *, backend: str) -> dict:
    from .setup_timing import BACKEND_STAGES as BACKEND_SETUP_STAGES
    from .setup_timing import LEGACY_SETUP_STAGES
    from .setup_timing import STAGES as SETUP_STAGES
    fields = {'schema', 'status', 'clocks', 'scope', 'phases', 'backend_phases'}
    if (not isinstance(value, dict) or set(value) != fields
            or value['schema'] != 'jev.setup-attribution.v1'
            or value['status'] not in {'complete', 'partial'}):
        raise ValueError('Invalid initialization timing')
    clocks = value['clocks']
    if (not isinstance(clocks, dict) or set(clocks) != {'wall', 'process_cpu', 'thread_cpu'}
            or clocks['wall'] != 'perf_counter_ns' or clocks['process_cpu'] != 'process_time_ns'
            or clocks['thread_cpu'] not in (None, 'thread_time_ns')):
        raise ValueError('Invalid initialization clock identities')

    def phases(rows, stage_orders):
        if not isinstance(rows, list):
            raise ValueError('Invalid initialization phase count')
        matching_orders = []
        for stages in stage_orders:
            if len(rows) > len(stages) - 1:
                continue
            if all(isinstance(row, dict)
                   and set(row) == {'from', 'to', 'wall_ns', 'process_cpu_ns', 'thread_cpu_ns'}
                   and row['from'] == stages[index]
                   and row['to'] == stages[index + 1]
                   for index, row in enumerate(rows)):
                matching_orders.append(stages)
        if not matching_orders:
            raise ValueError('Invalid initialization phase identity')
        stages = matching_orders[0]
        for row in rows:
            nonnegative(row['wall_ns'])
            nonnegative(row['process_cpu_ns'])
            if row['thread_cpu_ns'] is not None:
                nonnegative(row['thread_cpu_ns'])
            if (clocks['thread_cpu'] == 'thread_time_ns') != (row['thread_cpu_ns'] is not None):
                raise ValueError('Initialization thread clock availability changed')
        return rows, stages

    main, main_stages = phases(value['phases'],
                               (SETUP_STAGES, LEGACY_SETUP_STAGES))
    nested, nested_stages = phases(value['backend_phases'],
                                   (BACKEND_SETUP_STAGES,))
    legacy_scope = ('ordered_setup_boundaries; backend phases are nested in '
                    'preflight_to_backend')
    current_scope = ('ordered_setup_boundaries; backend phases are nested in '
                     'dashboard_ready_to_backend_ready')
    if main:
        expected_scope = (current_scope if main_stages == SETUP_STAGES else legacy_scope)
        if value['scope'] != expected_scope:
            raise ValueError('Initialization scope disagrees with phase order')
    elif value['scope'] not in {legacy_scope, current_scope}:
        raise ValueError('Invalid initialization scope')
    if value['status'] == 'complete':
        if len(main) != len(main_stages) - 1:
            raise ValueError('Complete initialization timing lacks stages')
        if (backend == 'fle') != (len(nested) == len(nested_stages) - 1):
            raise ValueError('Complete backend timing lacks expected stages')
    return value


def _model_call_identity(event: dict) -> tuple:
    """Bind a request or response to its trace, decision, controller and session."""
    payload = event['payload']
    correlation = event['correlation']
    call_id = correlation.get('model_call_id')
    if (type(call_id) is not str or not call_id
            or payload.get('model_call_id') != call_id):
        raise ValueError('Invalid model call identity')
    trace_id = payload.get('trace_id')
    controller = payload.get('controller')
    decision_id = payload.get('decision_id')
    session_id = event['session_id']
    if (type(trace_id) is not str or not trace_id
            or type(controller) is not str or not controller
            or correlation.get('decision_id') != decision_id
            or payload.get('session_id') != session_id):
        raise ValueError('Invalid model call context')
    return trace_id, call_id, controller, session_id, decision_id


def timestamp(value) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Invalid timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Timestamp requires timezone')
    return result


def analyze(path: Path, *, max_records: int = MAX_RECORDS,
            allow_incomplete: bool = False) -> dict:
    if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS:
        raise ValueError('Invalid record budget')
    path = Path(path)
    if path.is_dir():
        return analyze_research_run(path, max_records=max_records,
                                    allow_incomplete=allow_incomplete)
    samples = defaultdict(list)
    counts = defaultdict(int)
    records = legacy = incomplete = 0
    identity = source = source_digest = source_dirty = previous_record_time = None
    profiles_seen = partitions_seen = native_profiles_seen = 0
    timed_iterations = incomplete_iterations = timed_gaps = missing_prior = 0
    previous_iteration_index = None
    opener = gzip.open if path.suffix == '.gz' else open
    from .wait_record_codec import Decoder as WaitRecordDecoder, parse_json as parse_wait_json
    wait_decoder = WaitRecordDecoder('gameplay')
    with opener(path, 'rb') as stream:
        while raw := stream.readline(MAX_LINE + 1):
            records += 1
            try:
                if records > max_records or not raw.endswith(b'\n') or len(raw) > MAX_LINE:
                    raise ValueError('Capture exceeds budget or is incomplete')
                wire = parse_wait_json(raw)
                row = wait_decoder.decode(wire, len(raw), anchor_candidate=True)
                if not isinstance(row, dict):
                    raise ValueError('Gameplay row must be an object')
                # Enforce one immutable treatment/epoch without publishing its identifiers.
                signature = json.dumps({key: row.get(key) for key in (
                    'session_id', 'world_kind', 'process_id', 'code_revision', 'policy',
                    'target', 'requested_model', 'acceptance_configuration', 'campaign_treatment')},
                    sort_keys=True, allow_nan=False)
                if identity is not None and signature != identity:
                    raise ValueError('Mixed treatment or epoch')
                identity = signature
                revision = row.get('code_revision')
                commit = revision.get('commit') if isinstance(revision, dict) else revision
                source = commit if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit) else None
                digest = revision.get('source_sha256') if isinstance(revision, dict) else None
                source_digest = digest if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest) else None
                dirty = revision.get('dirty') if isinstance(revision, dict) else None
                source_dirty = dirty if type(dirty) is bool else None
                phase_start = None
                phases = row.get('phases', [])
                if not isinstance(phases, list) or len(phases) > 1024:
                    raise ValueError('Invalid phase count')
                for phase in phases:
                    validate_phase(phase)
                    if phase['status'] != 'started':
                        samples['phase_inclusive:' + phase['stage']].append(round(phase['seconds'] * 1e9))
                    if phase['stage'] == 'observe' and phase['status'] == 'started' and phase_start is None:
                        phase_start = timestamp(phase['at_utc'])
                current_time = timestamp(row['recorded_at_utc']) if row.get('recorded_at_utc') else None
                if current_time is not None and previous_record_time is not None and current_time < previous_record_time:
                    raise ValueError('Recorded time regressed')
                if phase_start is not None and previous_record_time is not None:
                    delta = (phase_start - previous_record_time).total_seconds()
                    if delta < 0:
                        raise ValueError('Observation predates previous record')
                    samples['record_utc_to_next_observe_utc_gap'].append(round(delta * 1e9))
                if current_time is not None and phase_start is not None:
                    delta = (current_time - phase_start).total_seconds()
                    if delta < 0:
                        raise ValueError('Record predates observation')
                    samples['observe_utc_to_record_utc'].append(round(delta * 1e9))
                previous_record_time = current_time
                profiles = row.get('observation_profiles', [])
                if not isinstance(profiles, list) or len(profiles) > 4:
                    raise ValueError('Invalid profile count')
                for profile in profiles:
                    if not isinstance(profile, dict) or type(profile.get('schema')) is not int or profile['schema'] != 1:
                        raise ValueError('Unsupported observation profile')
                    profiles_seen += 1
                    samples['observation_wall'].append(nonnegative(profile['total_ns']))
                    native = profile.get('native_ns', {})
                    if not isinstance(native, dict) or set(native) - NATIVE_STAGES:
                        raise ValueError('Invalid native profiler stages')
                    if ('native_timing_available' in profile
                            and (type(profile['native_timing_available']) is not bool
                                 or profile['native_timing_available'] != bool(native))):
                        raise ValueError('Invalid native profiler availability')
                    if native:
                        native_profiles_seen += 1
                    for stage, elapsed in native.items():
                        value = nonnegative(elapsed)
                        if value > 10**15:
                            raise ValueError('Native profiler stage exceeds budget')
                        samples['observation_native_stage:' + stage + ':nested'].append(value)
                        counts['observation_native_stage:' + stage + ':available'] += 1
                    if 'attribution_schema' not in profile:
                        legacy += 1
                    elif type(profile['attribution_schema']) is not int or profile['attribution_schema'] != 1:
                        raise ValueError('Unsupported attribution schema')
                    else:
                        samples['observation_process_cpu'].append(nonnegative(profile['process_cpu_ns']))
                        if profile.get('partition_complete') is True:
                            for key, total_key, label in (
                                    ('wall_partition_ns', 'total_ns', 'observation_exclusive_wall:'),
                                    ('process_cpu_partition_ns', 'process_cpu_ns', 'observation_exclusive_cpu:')):
                                parts = profile[key]
                                if not isinstance(parts, dict) or set(parts) != PARTS:
                                    raise ValueError('Invalid partition')
                                if sum(nonnegative(v) for v in parts.values()) != profile[total_key]:
                                    raise ValueError('Partition does not reconcile')
                                for name, value in parts.items():
                                    samples[label + name].append(value)
                            partitions_seen += 1
                        else:
                            incomplete += 1
                    for group in ('calls', 'subcalls'):
                        table = profile.get(group, {})
                        if not isinstance(table, dict) or set(table) - LABELS:
                            raise ValueError('Unknown profile label')
                        for name, entry in table.items():
                            count = nonnegative(entry['count'])
                            failed = nonnegative(entry.get('failed', 0))
                            if failed > count:
                                raise ValueError('Invalid failure count')
                            prefix = 'observation_' + group + ':' + name
                            samples[prefix + ':inclusive_aggregate'].append(nonnegative(entry['total_ns']))
                            counts[prefix + ':count'] += count
                            counts[prefix + ':failed'] += failed
                            for key in ('request_bytes', 'response_bytes'):
                                if key in entry:
                                    counts[prefix + ':' + key] += nonnegative(entry[key])
                prior = row.get('previous_iteration_timing')
                if prior is None:
                    missing_prior += 1
                else:
                    prior = validate_timing(prior)
                    index = prior['iteration_index']
                    if previous_iteration_index is not None:
                        if index <= previous_iteration_index:
                            raise ValueError('Duplicate or regressed iteration timing')
                        counts['iteration:unpublished_between_records'] += index - previous_iteration_index - 1
                    else:
                        # The input can be a tail of a stream. These indices are
                        # unrepresented here, not proof the runtime lost records.
                        counts['iteration:unobserved_before_first_sample'] += index - 1
                        counts['iteration:unpublished_between_records'] += index - 1
                    previous_iteration_index = index
                    for name, value in prior['native_io'].items():
                        counts['iteration_native_io:' + name] += value
                    if prior['partition_complete']:
                        timed_iterations += 1
                        for clock in CLOCKS:
                            samples['iteration_total:' + clock].append(prior['totals_ns'][clock])
                        for name, values in prior['phases'].items():
                            counts['iteration_phase:' + name + ':calls'] += values['calls']
                            counts['iteration_phase:' + name + ':failed'] += values['failed']
                            for clock in CLOCKS:
                                for scope in ('inclusive', 'exclusive'):
                                    samples['iteration_phase_' + scope + ':' + name + ':' + clock].append(values[clock + '_' + scope + '_ns'])
                    else:
                        incomplete_iterations += 1
                    gap = prior['gap']
                    counts['loop_sleep:calls'] += gap['sleep_calls']
                    counts['loop_sleep:failed'] += gap['sleep_failed']
                    if gap['complete']:
                        timed_gaps += 1
                        for clock in CLOCKS:
                            for name in ('total_ns', 'intentional_sleep_ns', 'other_gap_ns'):
                                samples['iteration_gap:' + name + ':' + clock].append(gap[name][clock])
                            if prior['partition_complete']:
                                samples['iteration_and_following_gap:' + clock].append(prior['totals_ns'][clock] + gap['total_ns'][clock])
                metrics = row.get('performance')
                if metrics is not None:
                    if not isinstance(metrics, dict) or type(metrics.get('schema')) is not int or metrics['schema'] != 1:
                        raise ValueError('Unsupported performance schema')
                    for group in ('calls', 'cpu_calls'):
                        table = metrics.get(group, {})
                        if not isinstance(table, dict) or set(table) - CALLS:
                            raise ValueError('Unknown performance label')
                        for name, entry in table.items():
                            samples['per_record_' + group + ':' + name].append(nonnegative(entry['total_ns']))
                            counts['per_record_' + group + ':' + name + ':count'] += nonnegative(entry['count'])
                    checkpoints = metrics.get('checkpoints', {})
                    if not isinstance(checkpoints, dict) or set(checkpoints) - (CHECKPOINT_STATUSES | {'bytes_written'}):
                        raise ValueError('Unknown checkpoint counter')
                    for key, value in checkpoints.items():
                        counts['checkpoint:' + key] += nonnegative(value)
                    operations = metrics.get('checkpoint_operations', {})
                    if not isinstance(operations, dict) or set(operations) - CHECKPOINT_OPERATIONS:
                        raise ValueError('Unknown checkpoint operation counter')
                    for key, value in operations.items():
                        counts['checkpoint:' + key] += nonnegative(value)
                    timings = metrics.get('checkpoint_ns', {})
                    if not isinstance(timings, dict) or set(timings) - CHECKPOINT_TIMINGS:
                        raise ValueError('Unknown checkpoint timing')
                    for key, value in timings.items():
                        # These are aggregates for this record. Missing fields
                        # in legacy records are unknown, never zero samples.
                        samples['per_record_checkpoint:' + key].append(nonnegative(value))
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
                raise ValueError(f'Invalid latency record at line {records}') from error
    if not records:
        raise ValueError('Empty latency capture')
    for stage in NATIVE_STAGES:
        counts['observation_native_stage:' + stage + ':unavailable'] = (
            profiles_seen - counts['observation_native_stage:' + stage + ':available'])
    return {'schema': 1, 'records': records, 'source_commit': source,
            'source_sha256': source_digest, 'source_dirty': source_dirty,
            'iteration_timing': {'complete_iterations': timed_iterations,
                'incomplete_iterations': incomplete_iterations, 'complete_following_gaps': timed_gaps,
                'records_without_prior_timing': missing_prior,
                'publication': 'one_record_lag; final_tail_not_inferred_or_assigned_zero'},
            'observation_profiles': profiles_seen,
            'profiles_with_native_profiler_stages': native_profiles_seen,
            'profiles_without_native_profiler_stages': profiles_seen - native_profiles_seen,
            'legacy_profiles_without_cpu_partition': legacy,
            'reconciled_partitions': partitions_seen, 'incomplete_partitions': incomplete,
            'distributions': {name: distribution(values) for name, values in sorted(samples.items())},
            'counts': dict(sorted(counts.items())),
            'quantiles': 'median_middle_pair_mean_and_p95_nearest_rank',
            'scopes': {
                'iteration_exclusive': 'nonoverlapping_components_of_completed_decorated_step',
                'iteration_inclusive': 'nested_totals_not_additive_not_individual_call_quantiles',
                'iteration_gap': 'nonoverlapping_intentional_loop_sleep_and_other_gap; not watchdog cadence',
                'missing_iteration_indices': 'not_represented_in_input_since_index_1_including_prefix; not_proof_of_runtime_loss',
                'thread_cpu': 'current_python_thread_not_native_server_cpu',
                'observation_exclusive': 'within_each_single_ordered_observation_only',
                'observation_native_stage': 'native_profiler_elapsed_when_available; nested_within_observation_rpc_not_additive_to_wall_or_cpu',
                'phase_inclusive': 'nested_phase_durations_not_additive',
                'per_record': 'per_record_aggregates_not_individual_call_quantiles',
                'checkpoint': 'current_iteration_through_checkpoint_before_record; '
                              'per_record_inclusive_aggregates_not_additive; '
                              'serialize_ns_includes_capture_and_json_encode; '
                              'absent_fields_unknown_not_zero',
                'gap': 'UTC_record_to_next_observation_includes_unmeasured_emission_and_sleep',
                'process_cpu': 'whole_python_process_including_other_threads_not_native_or_host_cpu'},
            'unavailable': ['opaque_helper_transport_decomposition', 'helper_retry_and_backoff',
                            'native_server_cpu', 'final_iteration_tail_without_following_record']
                + ([] if timed_gaps else ['intentional_sleep_separation'])
                + ([] if timed_iterations else ['full_record_construction_emission_partition']),
            'native_acceptance_proven': False, 'deployment_authorized': False}


def analyze_research_run(run_dir: Path, *, max_records: int = MAX_RECORDS,
                          allow_incomplete: bool = False) -> dict:
    """Report optional timing from a hash-verified research event stream."""
    from .research_log import _read_document, validate_event, verify_run
    run_dir = Path(run_dir)
    if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS:
        raise ValueError('Invalid record budget')
    if type(allow_incomplete) is not bool:
        raise ValueError('Invalid incomplete-run option')
    before = verify_run(run_dir, allow_incomplete=allow_incomplete,
                        max_events=max_records)
    try:
        manifest = _read_document(run_dir / 'manifest.json')
        configuration = manifest['configuration']
        profile_enabled = configuration.get('profile_latency', False) is True
        backend = configuration['backend']
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError('Invalid verified latency manifest') from error

    samples = defaultdict(list)
    counts = defaultdict(int)
    records = requests = responses = 0
    pending_model_call = None
    seen_model_calls: set[tuple[str, str]] = set()
    previous_context = incomplete_context = 0
    startup_windows = setup_profiles = 0
    unavailable: set[str] = set()

    def add_window(prefix: str, value: dict) -> None:
        for field in ('wall_ns', 'process_cpu_ns', 'thread_cpu_ns'):
            if value[field] is not None:
                samples[prefix + ':' + field].append(value[field])

    with (run_dir / 'events.jsonl').open('rb') as stream:
        while raw := stream.readline(MAX_LINE + 1):
            records += 1
            if records > max_records or len(raw) > MAX_LINE or not raw.endswith(b'\n'):
                raise ValueError('Latency event stream exceeds budget or has a partial record')
            try:
                event = json.loads(raw.decode('utf-8'))
                validate_event(event)
                event_type, payload = event['event_type'], event['payload']
                if event_type == 'controller_initialized':
                    if profile_enabled:
                        if 'startup_window_timing' not in payload or 'initialization_timing' not in payload:
                            raise ValueError('Profiled initialization lacks timing')
                        window = _validate_clock_window(
                            payload['startup_window_timing'],
                            scope='before_run_started_event_construction_to_before_controller_initialized_event_construction')
                        setup = _validate_setup_attribution(payload['initialization_timing'], backend=backend)
                        startup_windows += 1
                        setup_profiles += 1
                        add_window('startup_window', window)
                        if not window['complete']:
                            unavailable.add('startup_wall_or_process_cpu')
                        for row in setup['phases']:
                            stage = row['from'] + '_to_' + row['to']
                            for field in ('wall_ns', 'process_cpu_ns', 'thread_cpu_ns'):
                                if row[field] is not None:
                                    samples['initialization_phase:' + stage + ':' + field].append(row[field])
                                else:
                                    unavailable.add('initialization_' + field)
                        for row in setup['backend_phases']:
                            stage = row['from'] + '_to_' + row['to']
                            for field in ('wall_ns', 'process_cpu_ns', 'thread_cpu_ns'):
                                if row[field] is not None:
                                    samples['initialization_backend_phase_nested:' + stage + ':' + field].append(row[field])
                                else:
                                    unavailable.add('backend_initialization_' + field)
                if event_type == 'model_request':
                    requests += 1
                    if profile_enabled:
                        link = _model_call_identity(event)
                        call_key = link[:2]
                        if pending_model_call is not None or call_key in seen_model_calls:
                            raise ValueError('Duplicate or overlapping model request')
                        pending_model_call = link
                        seen_model_calls.add(call_key)
                        timing = payload.get('previous_iteration_timing')
                        if timing is not None:
                            timing = validate_timing(timing)
                            previous_context += 1
                            if not timing['partition_complete']:
                                incomplete_context += 1
                            else:
                                for clock in CLOCKS:
                                    samples['prior_iteration_total:' + clock + '_ns'].append(
                                        timing['totals_ns'][clock])
                                for name, row in timing['phases'].items():
                                    for clock in CLOCKS:
                                        samples['prior_iteration_phase_exclusive:' + name + ':' + clock + '_ns'].append(
                                            row[clock + '_exclusive_ns'])
                                        samples['prior_iteration_phase_inclusive_nested:' + name + ':' + clock + '_ns'].append(
                                            row[clock + '_inclusive_ns'])
                                gap = timing['gap']
                                if gap['complete']:
                                    for clock in CLOCKS:
                                        for key in ('total_ns', 'intentional_sleep_ns', 'other_gap_ns'):
                                            samples['prior_iteration_gap:' + key + ':' + clock + '_ns'].append(
                                                gap[key][clock])
                                else:
                                    unavailable.add('prior_iteration_following_gap')
                if event_type == 'model_response':
                    responses += 1
                    if profile_enabled:
                        link = _model_call_identity(event)
                        if pending_model_call is None or link != pending_model_call:
                            raise ValueError('Orphan or mismatched model response')
                        pending_model_call = None
                        call_timing = payload.get('client_evaluate_timing')
                        if call_timing is None:
                            counts['model_response_operation:missing'] += 1
                            unavailable.add('model_response_operation_clock_sample')
                        else:
                            call_timing = _validate_clock_window(
                                call_timing,
                                scope='clock_samples_around_client_evaluate_call')
                            add_window('model_response_operation', call_timing)
                            if not call_timing['complete']:
                                counts['model_response_operation:incomplete'] += 1
                                unavailable.add('model_response_operation_wall_or_process_cpu')
                            if call_timing['thread_cpu_ns'] is None:
                                unavailable.add('model_response_operation_thread_cpu')
                        gap = payload.get('inter_request_timing')
                        if gap is None:
                            if responses > 1:
                                counts['model_inter_request:missing'] += 1
                                unavailable.add('model_inter_request_clock_sample')
                        else:
                            if responses == 1:
                                raise ValueError('First model response cannot have a preceding-call gap')
                            gap = _validate_clock_window(
                                gap, scope='clock_sample_after_model_response_event_emission_to_clock_sample_after_next_model_request_emission_before_client_evaluate')
                            counts['model_inter_request:measured'] += 1
                            add_window('model_inter_request', gap)
                            if not gap['complete']:
                                counts['model_inter_request:incomplete'] += 1
                                unavailable.add('model_inter_request_wall_or_process_cpu')
                            if gap['thread_cpu_ns'] is None:
                                unavailable.add('model_inter_request_thread_cpu')
            except (KeyError, TypeError, ValueError, AttributeError, UnicodeError, OverflowError) as error:
                raise ValueError(f'Invalid verified latency event at record {records}') from error
    after = verify_run(run_dir, allow_incomplete=allow_incomplete,
                       max_events=max_records)
    if before != after or records != before['event_count']:
        raise ValueError('Research run changed while its latency report was being read')
    if profile_enabled and startup_windows == 0:
        unavailable.add('controller_initialization_timing')
    if profile_enabled and pending_model_call is not None and before['complete']:
        raise ValueError('Completed research run ends with an unmatched model request')
    if profile_enabled and requests > responses:
        counts['model_call:request_without_response'] += requests - responses
        unavailable.add('model_response_event_missing')
    if profile_enabled and responses > requests:
        counts['model_call:response_without_request'] += responses - requests
        unavailable.add('model_request_event_missing')
    if not profile_enabled:
        unavailable.update({'startup_wall_process_thread_cpu', 'model_inter_request_timing',
                            'controller_iteration_phase_timing'})
    revision = manifest.get('provenance', {}).get('git', {}).get('commit')
    source = revision if isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision) else None
    return {
        'schema': 1,
        'source': 'hash_verified_research_event_stream',
        'integrity_verified': True,
        'complete': before['complete'],
        'event_count': records,
        'source_commit': source,
        'latency_profile_enabled': profile_enabled,
        'model_requests': requests,
        'model_responses': responses,
        'startup_windows': startup_windows,
        'initialization_profiles': setup_profiles,
        'prior_iteration_contexts': previous_context,
        'incomplete_prior_iteration_contexts': incomplete_context,
        'distributions': {name: distribution(values) for name, values in sorted(samples.items())},
        'counts': dict(sorted(counts.items())),
        'scopes': {
            'startup_window': 'sample before run_started event construction through sample before controller_initialized event construction; overlaps ordered initialization phase rows',
            'initialization_backend_phase_nested': 'nested within its containing top-level backend phase; do not add to top-level initialization phases',
            'model_inter_request': 'pairwise non-overlapping window from a clock sample after model-response event emission through a clock sample after the next model-request event emission and immediately before client.evaluate; includes event emission and endpoint-sampling overhead',
            'model_response_operation': 'clock samples around the client.evaluate call; the boundaries include small endpoint-sampling overhead',
            'prior_iteration_phase_exclusive': 'nonoverlapping components within one decorated controller step',
            'prior_iteration_phase_inclusive_nested': 'nested phase durations; not additive to one another or step totals',
            'prior_iteration_gap': 'nonoverlapping intentional sleep and other gap between adjacent decorated steps; not watchdog cadence',
            'nested_measurement_examples': 'checkpoint file/directory sync phases are nested within checkpoint writes; research redaction, validation, serialization, hashing, and fsync are nested within event append; intentional sleep is a component of the between-step gap',
            'cpu': 'process/thread CPU are measured usage, not proof of waiting, native-server CPU, network time, or host scheduling cause',
            'relationship': 'model_inter_request windows do not overlap one another, but overlap prior/current iteration phase and gap views; nested inclusive metrics and CPU values are not additional wall time',
        },
        'unavailable': sorted(unavailable),
        'native_acceptance_proven': False,
        'deployment_authorized': False,
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--max-records', type=int, default=MAX_RECORDS)
    parser.add_argument('--allow-incomplete', action='store_true',
                        help='Report a live unsealed research run after verifying its current hash chain')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(analyze(args.log, max_records=args.max_records,
                                 allow_incomplete=args.allow_incomplete),
                          indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError):
        parser.exit(2, 'Cannot report an incomplete, mixed or invalid latency capture.\n')


if __name__ == '__main__':
    main()
