"""Read-only integration analyzer: synthetic inputs are never native acceptance."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio import integration_evidence as report
from jev_factorio.solid_routes import commitment
from jev_factorio.acceptance_io import canonical
from integration_evidence_fixtures import evidence


def configure_output_buffers(args, enabled):
    rows, trial, initial, final = args
    trial['configuration']['furnace_output_buffers'] = enabled
    for record in rows:
        record['acceptance_configuration'] = deepcopy(trial['configuration'])
        if enabled:
            record.update(furnace_output_buffers=True)
            for label in ('state', 'after_state'):
                state = record[label]
                state['factory']['output_buffers'] = {
                    'protocol': 1,
                    'session_id': state['session_id'],
                    'tick': state['tick'],
                    'sources': {},
                }
            record['buffer_evidence'] = deepcopy(
                record['after_state']['factory']['output_buffers'])
        else:
            record.pop('furnace_output_buffers', None)
            record.pop('buffer_evidence', None)
            for label in ('state', 'after_state'):
                record[label]['factory'].pop('output_buffers', None)
    for checkpoint in (initial, final):
        if enabled:
            checkpoint.update(output_buffers_schema=1, output_commitments={})
        else:
            checkpoint.pop('output_buffers_schema', None)
            checkpoint.pop('output_commitments', None)


def test_reconciled_fixture_is_still_not_native_acceptance():
    rows, trial, initial, final = evidence()
    value = report.analyze_rows(rows, trial, initial, final)
    assert value['measurement_checks_passed'], value['issues']
    assert value['evidence_kind'] == 'fixture'
    assert value['window']['wall_seconds'] == 1860
    assert value['science']['delivered_by_new_owned_lab_receipts'] == 62
    assert value['science']['force_consumed_with_single_owned_lab'] == 62
    assert value['transport']['coal_consumers_with_new_flow'] == 2
    assert value['transport']['coal_inventory_delivery_lower_bound'] == 124
    assert value['transport']['downstream_delivery_units'] == 62
    assert not value['transport']['mined_coal_provenance_verified']
    assert value['native_acceptance'] == 'not_accepted'
    assert not value['deployment_authorized'] and not value['external_authenticity_proven']
    assert 'native_coal_mining_bootstrap_and_network_fuel_provenance' in value['remaining_gates']
    assert 'private-fixture' not in json.dumps(value)


def test_unrelated_intermediate_route_cannot_qualify_science_delivery():
    rows, trial, initial, final = evidence()
    trial['downstream_recipes'] = ['transport-belt']
    for record in rows:
        for label in ('state', 'after_state'):
            state = record[label]
            for route in state['factory']['solid_routes']['routes'].values():
                if route['target']['inventory'] != 'input':
                    continue
                route['target']['recipe'] = 'transport-belt'
                state['factory']['entities'][route['target']['role']]['recipe'] = 'transport-belt'
    for checkpoint, state in ((initial, rows[0]['state']),
                              (final, rows[-1]['after_state'])):
        checkpoint['solid_commitments'] = {
            key: commitment(route) for key, route in
            state['factory']['solid_routes']['routes'].items()}

    result = report.analyze_rows(rows, trial, initial, final)
    assert result['integrity_checks_passed'], result['issues']
    assert result['science']['force_consumed_with_single_owned_lab'] > 0
    assert result['transport']['downstream_routes_with_flow_and_production'] == 0
    assert 'downstream_flow_and_production_not_measured' in result['outcome_gaps']
    assert not result['measurement_checks_passed']


def test_routed_science_pack_with_lab_delivery_and_consumption_qualifies():
    result = report.analyze_rows(*evidence())
    assert result['integrity_checks_passed'], result['issues']
    assert result['transport']['downstream_routes_with_flow_and_production'] == 1
    assert result['measurement_checks_passed']


@pytest.mark.parametrize('status', ['failed', None, 'unexpected', [], {}])
def test_unrecognized_controller_status_cannot_pass(status):
    args = evidence()
    args[0][4]['status'] = status
    value = report.analyze_rows(*args)
    assert 'controller_or_route_failure' in value['issues']
    assert not value['integrity_checks_passed']


@pytest.mark.parametrize('boundary', ['state', 'after_state'])
def test_downstream_production_reset_cannot_be_hidden_by_later_growth(boundary):
    args = evidence()
    state = args[0][4][boundary]
    downstream = next(route for route in state['factory']['solid_routes']['routes'].values()
                      if route['target']['inventory'] != 'fuel')
    state['factory']['entities'][downstream['target']['role']]['products_finished'] = 0
    value = report.analyze_rows(*args)
    assert 'downstream_production_counter_regressed' in value['issues']
    assert not value['integrity_checks_passed']


def test_nested_timing_not_added_to_iteration():
    value = report.analyze_rows(*evidence())
    measured = value['timing']['distributions']
    assert measured['iteration:wall']['count'] == 30
    assert measured['iteration:wall']['total_ns'] == 30 * 4000000
    assert sum(v['total_ns'] for k, v in measured.items() if k.startswith('exclusive:') and k.endswith(':wall')) == measured['iteration:wall']['total_ns']
    assert value['timing']['counts']['native_io:command_calls'] == 90


@pytest.mark.parametrize('mutate,code', [
    (lambda r,t,i,f: t.update(runtime_cutoff_utc='2026-09-27T23:00:00Z'), 'original_cutoff_changed'),
    (lambda r,t,i,f: t.update(declared_at_utc='2026-09-26T20:05:00Z'), 'experiment_not_predeclared'),
    (lambda r,t,i,f: r[4]['code_revision'].update(dirty=True), 'source_readback_mismatch'),
    (lambda r,t,i,f: r[4].update(process_id='other'), 'mixed_invocation_or_treatment'),
    (lambda r,t,i,f: r[4]['acceptance_configuration'].update(background_work=True), 'configuration_mismatch'),
    (lambda r,t,i,f: r[4].update(status='uncertain'), 'controller_or_route_failure'),
    (lambda r,t,i,f: r[1].update(model_call=False), 'single_actual_jev_model_not_demonstrated'),
    (lambda r,t,i,f: r[2].update(model_call=True, resolved_model='another'), 'resolved_model_missing_or_mismatched'),
    (lambda r,t,i,f: r[4]['after_state']['factory']['acceptance_runtime'].update(speed=10), 'simulation_speed_or_pause_changed'),
    (lambda r,t,i,f: r[4]['state']['factory']['acceptance_runtime'].update(player_index=2), 'native_epoch_drift'),
    (lambda r,t,i,f: r[4]['state']['factory']['solid_routes']['routes'].clear(), 'paid_route_ownership_regressed'),
    (lambda r,t,i,f: r[4]['after_state']['factory']['entities']['utility:lab'].update(unit_number=1234), 'owned_lab_replaced'),
    (lambda r,t,i,f: r[4]['after_state']['factory']['force_entity_counts'].update(lab=2), 'exclusive_owned_lab_not_observed'),
    (lambda r,t,i,f: r[4]['after_state']['factory']['consumed'].update({'automation-science-pack': 0}), 'consumption_counter_regressed'),
    (lambda r,t,i,f: r[4]['state']['factory']['consumed'].update({'automation-science-pack': 0}), 'consumption_counter_regressed'),
    (lambda r,t,i,f: r[4]['state']['factory']['receipts']['science:1'].update(quantity=200), 'receipt_identity_rewritten'),
    (lambda r,t,i,f: r[4]['after_state']['factory']['receipts']['science:1'].update(quantity=200), 'receipt_identity_rewritten'),
    (lambda r,t,i,f: r[4]['failure_budgets'].update({'earlier': 2}), 'failure_history_regressed'),
    (lambda r,t,i,f: f.update(last_tick=0), 'final_checkpoint_window_mismatch'),
    (lambda r,t,i,f: f['solid_commitments'].clear(), 'final_route_checkpoint_mismatch'),
    (lambda r,t,i,f: r[4].update(previous_iteration_timing=deepcopy(r[3]['previous_iteration_timing'])), 'duplicate_or_regressed_iteration_timing'),
    (lambda r,t,i,f: r[4].pop('previous_iteration_timing'), 'complete_iteration_timing_coverage_missing'),
])
def test_fail_closed_on_inconsistent_evidence(mutate, code):
    args = evidence(); mutate(*args)
    value = report.analyze_rows(*args)
    assert not value['measurement_checks_passed']
    assert code in value['issues'], value['issues']
    assert value['native_acceptance'] == 'not_accepted'


def test_short_successful_probe_does_not_replace_thirty_minutes():
    rows, trial, initial, final = evidence()
    rows = rows[:5]
    rows[-1]['after_state']['researched'].append(trial['research_goal'])
    final['last_tick'] = rows[-1]['after_state']['tick']
    value = report.analyze_rows(rows, trial, initial, final)
    assert 'complete_30_minute_window_missing' in value['issues']


def test_preexisting_inventory_flow_is_not_new_flow():
    args = evidence()
    for record in args[0]:
        for label in ('state', 'after_state'):
            for route in record[label]['factory']['solid_routes']['routes'].values():
                route['flow'].update(sent=10000, received=10000, positive_samples=1000)
    value = report.analyze_rows(*args)
    assert value['transport']['coal_inventory_delivery_lower_bound'] == 0
    assert 'two_distinct_fuel_consumers_not_measured' in value['issues']


def test_science_stall_then_last_minute_progress_is_not_sustained():
    args = evidence()
    for record in args[0][1:-1]:
        for label in ('state', 'after_state'):
            record[label]['factory']['consumed']['automation-science-pack'] = 0
            record[label]['factory']['research_progress'] = 0
    assert 'sustained_science_progress_missing' in report.analyze_rows(*args)['issues']


def test_duplicate_records_rejected():
    args = evidence(); args[0].insert(3, deepcopy(args[0][2]))
    with pytest.raises(ValueError, match='Duplicate'): report.analyze_rows(*args)


@pytest.mark.parametrize('key,value', [
    ('minimum_window_seconds', 1799), ('minimum_window_seconds', True),
    ('max_observation_gap_seconds', 121), ('minimum_timing_samples', 1),
    ('expected_commit', 'path/private'), ('expected_source_sha256', 'NaN'),
    ('science_packs', ['made-up-science']), ('downstream_recipes', []),
    ('declared_at_utc', '2026-09-26T19:00:00'),
])
def test_invalid_trial_rejected(key, value):
    args = evidence(); args[1][key] = value
    with pytest.raises(ValueError): report.analyze_rows(*args)


def files(tmp_path, args=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows, trial, initial, final = args or evidence()
    values = {'gameplay': b''.join(canonical(v) for v in rows), 'trial': canonical(trial),
              'initial_checkpoint': canonical(initial), 'final_checkpoint': canonical(final)}
    paths = {name: tmp_path / (name + '.json') for name in values}
    for name, value in values.items(): paths[name].write_bytes(value)
    return paths


def test_files_validate_real_composed_checkpoint_without_loading_game(tmp_path):
    paths = files(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    value = report.analyze(paths['gameplay'], paths['trial'], paths['initial_checkpoint'], paths['final_checkpoint'])
    assert value['measurement_checks_passed'], value['issues']
    assert len(value['inputs_sha256']) == 4
    assert before == {name: path.read_bytes() for name, path in paths.items()}


def test_cli_never_overwrites_and_does_not_echo_private_payload(tmp_path, capsys):
    paths = files(tmp_path); output = tmp_path / 'report.json'
    args = [item for key, path in paths.items() for item in ('--' + key.replace('_', '-'), str(path))] + ['--output', str(output)]
    with pytest.raises(SystemExit) as result: report.main(args)
    assert result.value.code == 0
    saved = output.read_bytes()
    with pytest.raises(SystemExit) as result: report.main(args)
    assert result.value.code == 2 and output.read_bytes() == saved
    assert 'private-fixture' not in capsys.readouterr().err


@pytest.mark.parametrize('boundary', ['initial', 'final'])
@pytest.mark.parametrize(('enabled', 'extension_present'), [(False, True), (True, False)])
def test_analyze_rows_rejects_output_buffer_composition_mismatch(boundary, enabled,
                                                                  extension_present):
    args = evidence()
    configure_output_buffers(args, enabled)
    checkpoint = args[2 if boundary == 'initial' else 3]
    if extension_present:
        checkpoint.update(output_buffers_schema=1, output_commitments={})
    else:
        checkpoint.pop('output_buffers_schema', None)
        checkpoint.pop('output_commitments', None)

    result = report.analyze_rows(*args)
    assert 'checkpoint_composition_mismatch' in result['issues']
    assert not result['integrity_checks_passed']
    assert not result['measurement_checks_passed']
    assert result['native_acceptance'] == 'not_accepted'
    assert result['deployment_authorized'] is False


def test_analyze_rows_accepts_declared_output_buffer_composition():
    args = evidence()
    configure_output_buffers(args, True)
    result = report.analyze_rows(*args)
    assert result['integrity_checks_passed'], result['issues']
    assert result['measurement_checks_passed'], result['issues']
    assert result['native_acceptance'] == 'not_accepted'
    assert result['deployment_authorized'] is False


def test_output_owner_prefix_is_retained_inside_composed_checkpoints():
    args = evidence()
    configure_output_buffers(args, True)
    from test_output_buffer_integration import setup as output_setup

    source_state, _, route = output_setup(True)
    route = deepcopy(route)
    entities = deepcopy(source_state.factory['entities'])
    for paid in route['parts'].values():
        paid['unit_number'] += 18000
        entities[paid['role']]['unit_number'] = paid['unit_number']
    source = route['source']
    owner = {source: {
        'source_unit': route['source_unit'],
        'layout': route['layout'],
        'parts': deepcopy(route['parts']),
    }}
    for record in args[0]:
        for label in ('state', 'after_state'):
            state = record[label]
            state['factory']['entities'].update(deepcopy(entities))
            state['factory']['output_buffers'] = {
                'protocol': 1,
                'session_id': state['session_id'],
                'tick': state['tick'],
                'sources': {source: deepcopy(route)},
            }
        record['buffer_evidence'] = deepcopy(
            record['after_state']['factory']['output_buffers'])
    args[2]['output_commitments'] = deepcopy(owner)
    args[3]['output_commitments'] = deepcopy(owner)
    valid = report.analyze_rows(*args)
    assert valid['integrity_checks_passed'], valid['issues']

    args[3]['output_commitments'] = {}
    regressed = report.analyze_rows(*args)
    assert 'composed_ownership_regressed' in regressed['issues']
    assert not regressed['integrity_checks_passed']
    assert regressed['native_acceptance'] == 'not_accepted'


@pytest.mark.parametrize('boundary', ['initial', 'final'])
@pytest.mark.parametrize(('enabled', 'extension_present'), [(False, True), (True, False)])
def test_file_analyzer_rejects_output_buffer_composition_mismatch_without_mutation(
        tmp_path, boundary, enabled, extension_present):
    args = evidence()
    configure_output_buffers(args, enabled)
    checkpoint = args[2 if boundary == 'initial' else 3]
    if extension_present:
        checkpoint.update(output_buffers_schema=1, output_commitments={})
    else:
        checkpoint.pop('output_buffers_schema', None)
        checkpoint.pop('output_commitments', None)
    paths = files(tmp_path, args)
    before = {name: path.read_bytes() for name, path in paths.items()}

    result = report.analyze(paths['gameplay'], paths['trial'],
                            paths['initial_checkpoint'], paths['final_checkpoint'])
    assert 'checkpoint_composition_mismatch' in result['issues']
    assert not result['measurement_checks_passed']
    assert result['native_acceptance'] == 'not_accepted'
    assert result['deployment_authorized'] is False
    assert before == {name: path.read_bytes() for name, path in paths.items()}


def test_cli_exports_composition_failure_without_acceptance_or_authority(tmp_path):
    args = evidence()
    configure_output_buffers(args, False)
    args[3].update(output_buffers_schema=1, output_commitments={})
    paths = files(tmp_path / 'inputs', args)
    output = tmp_path / 'report.json'
    cli_args = [item for key, path in paths.items()
                for item in ('--' + key.replace('_', '-'), str(path))]
    cli_args += ['--output', str(output)]

    with pytest.raises(SystemExit) as result:
        report.main(cli_args)
    assert result.value.code == 2
    saved = json.loads(output.read_bytes())
    assert 'checkpoint_composition_mismatch' in saved['issues']
    assert saved['native_acceptance'] == 'not_accepted'
    assert saved['deployment_authorized'] is False


def test_symlink_and_incomplete_jsonl_rejected(tmp_path):
    paths = files(tmp_path)
    target = paths['gameplay']; alias = tmp_path / 'alias'; alias.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        report.analyze(alias, paths['trial'], paths['initial_checkpoint'], paths['final_checkpoint'])
    target.write_bytes(target.read_bytes().rstrip(b'\n'))
    with pytest.raises(ValueError):
        report.analyze(target, paths['trial'], paths['initial_checkpoint'], paths['final_checkpoint'])


def paired(*, stalled_baseline=False):
    baseline, treatment = evidence(), evidence()
    baseline[1]['arm'] = 'baseline'
    for record in baseline[0]:
        record['process_id'] = 'baseline-process'
        record['execution_id'] = 'baseline-execution'
        record['run_id'] = 'baseline-run'
        if stalled_baseline:
            for label in ('state', 'after_state'):
                state = record[label]
                state['researched'] = ['automation']
                state['factory'].update(research_progress=0, consumed={}, receipts={})
                for route in state['factory']['solid_routes']['routes'].values():
                    route['flow'].update(sent=10, received=10, positive_samples=3)
                    state['factory']['entities'][route['target']['role']]['products_finished'] = 0
    return baseline, treatment


def comparison(args):
    baseline, treatment = args
    return report.compare(report.analyze_rows(*baseline), report.analyze_rows(*treatment), baseline[1], treatment[1])


def test_matched_pair_and_zero_baseline_remain_distinct_from_acceptance():
    result = comparison(paired())
    assert result['paired_measurement_checks_passed'], result['issues']
    assert result['science_rate_ratio'] == result['iteration_p95_ratio'] == 1
    assert result['evidence_kind'] == 'fixture' and result['native_acceptance'] == 'not_accepted'
    assert not result['causal_improvement_proven']
    result = comparison(paired(stalled_baseline=True))
    assert result['paired_measurement_checks_passed'], result['issues']
    assert result['baseline_science_per_wall_minute'] == 0
    assert result['treatment_science_per_wall_minute'] == 2
    assert result['science_rate_ratio'] is None
    assert result['iteration_p95_ratio'] == 1


def test_pair_rejects_different_observed_mod_sets():
    args = paired()
    for record in args[1][0]:
        for boundary in ('state', 'after_state'):
            record[boundary]['factory']['acceptance_runtime']['mods'] = {'base': 'other-fixture'}
    baseline = report.analyze_rows(*args[0])
    treatment = report.analyze_rows(*args[1])
    assert baseline['measurement_checks_passed'] and treatment['measurement_checks_passed']
    assert baseline['runtime_mods_sha256'] != treatment['runtime_mods_sha256']
    result = report.compare(baseline, treatment, args[0][1], args[1][1])
    assert 'uncontrolled_runtime_mods' in result['issues']
    assert not result['paired_measurement_checks_passed']


@pytest.mark.parametrize('status', ['completed', 'blocked', 'uncertain'])
def test_initial_checkpoint_must_be_running(status):
    args = evidence()
    args[2]['status'] = status
    value = report.analyze_rows(*args)
    assert 'initial_checkpoint_not_running' in value['issues']


def test_checkpoint_extensions_must_match_declared_composition():
    args = evidence()
    for checkpoint in args[2:]:
        checkpoint.update(background_schema=2, background_job=None, background_attempt=None)
    value = report.analyze_rows(*args)
    assert 'checkpoint_composition_mismatch' in value['issues']


def test_valid_successor_composed_checkpoint_can_be_analyzed():
    args = evidence()
    configure_output_buffers(args, True)
    for checkpoint in args[2:]:
        checkpoint.update(background_schema=2, background_job=None, background_attempt=None,
                          output_buffers_schema=1, output_commitments={},
                          input_routes_schema=1, input_commitments={}, successor_schema=1,
                          successor_projects={}, successor_receipts={})
    for flag in ('background_work', 'furnace_output_buffers', 'furnace_input_belts', 'ore_side_successors'):
        args[1]['configuration'][flag] = True
    for record in args[0]:
        record['acceptance_configuration'] = deepcopy(args[1]['configuration'])
        record.update(background_work=True, background_schema=2,
                      background_job=None, background_attempt=None,
                      furnace_output_buffers=True,
                      furnace_input_belts=True, input_route_evidence={},
                      input_validation_failure={}, ore_side_successors=True,
                      successor_evidence={}, successor_projects={})
    value = report.analyze_rows(*args)
    assert value['measurement_checks_passed'], value['issues']


@pytest.mark.parametrize('mods', [{'base': None}, {'base': True}, {'base': {'nested': 'value'}}])
def test_malformed_observed_mods_cannot_be_hashed_as_valid(mods):
    args = evidence()
    for record in args[0]:
        for boundary in ('state', 'after_state'):
            record[boundary]['factory']['acceptance_runtime']['mods'] = deepcopy(mods)
    value = report.analyze_rows(*args)
    assert 'invalid_native_mods' in value['issues']
    assert value['runtime_mods_sha256'] is None


def test_matched_pair_requires_comparable_actual_windows():
    args = paired()
    from datetime import datetime, timedelta
    for index, record in enumerate(args[0][0]):
        instant = datetime.fromisoformat(record['recorded_at_utc'])
        record['recorded_at_utc'] = (instant + timedelta(minutes=index)).isoformat()
    baseline = report.analyze_rows(*args[0])
    treatment = report.analyze_rows(*args[1])
    assert baseline['integrity_checks_passed'] and treatment['measurement_checks_passed']
    assert 'unmatched_measurement_windows' in report.compare(baseline, treatment, args[0][1], args[1][1])['issues']


def test_each_declared_science_pack_needs_new_delivery():
    args = evidence()
    args[1]['science_packs'].append('logistic-science-pack')
    for index, record in enumerate(args[0]):
        record['state']['factory']['consumed']['logistic-science-pack'] = max(0, index - 1) * 2
        record['after_state']['factory']['consumed']['logistic-science-pack'] = index * 2
    value = report.analyze_rows(*args)
    assert 'science_delivery_or_consumption_missing' in value['outcome_gaps']


@pytest.mark.parametrize('fault', [None, 'false', 0])
def test_solid_fault_flag_must_be_explicitly_false(fault):
    args = evidence()
    args[0][4]['solid_route_fault'] = fault
    assert 'controller_or_route_failure' in report.analyze_rows(*args)['issues']


def test_failed_iteration_is_not_a_successful_timing_sample():
    args = evidence()
    args[0][4]['previous_iteration_timing']['status'] = 'error'
    value = report.analyze_rows(*args)
    assert 'failed_iteration_timing' in value['issues']
    assert not value['measurement_checks_passed']


def test_comparison_carries_checkpoint_and_full_input_bindings():
    args = paired()
    original = comparison(args)
    args[0][2]['reason'] = 'different retained reason'
    changed = comparison(args)
    assert original['baseline_evidence_sha256'] == changed['baseline_evidence_sha256']
    assert original['baseline_input_binding_sha256'] != changed['baseline_input_binding_sha256']
    assert original['treatment_input_binding_sha256'] == changed['treatment_input_binding_sha256']


@pytest.mark.parametrize('checkpoint', [2, 3])
def test_science_policy_must_be_explicit_in_both_checkpoints(checkpoint):
    args = evidence()
    args[checkpoint].pop('solid_science_policy')
    assert 'checkpoint_treatment_mismatch' in report.analyze_rows(*args)['issues']


def test_file_pair_binds_exact_input_bytes(tmp_path):
    baseline, treatment = paired()
    first = files(tmp_path / 'baseline', baseline)
    second = files(tmp_path / 'treatment', treatment)
    original = report.compare_files(first, second)
    trial = first['trial']
    trial.write_bytes(json.dumps(json.loads(trial.read_bytes()), indent=2).encode() + b'\n')
    changed = report.compare_files(first, second)
    assert original['paired_measurement_checks_passed']
    assert changed['paired_measurement_checks_passed']
    assert original['baseline_input_binding_sha256'] == changed['baseline_input_binding_sha256']
    assert original['baseline_raw_inputs_binding_sha256'] != changed['baseline_raw_inputs_binding_sha256']
    assert original['treatment_raw_inputs_binding_sha256'] == changed['treatment_raw_inputs_binding_sha256']


def test_file_pair_rejects_formatting_change_between_trial_reads(tmp_path, monkeypatch):
    baseline, treatment = paired()
    first = files(tmp_path / 'baseline', baseline)
    second = files(tmp_path / 'treatment', treatment)
    original_read = report.stable_read
    reads = 0
    def changed_second_read(path, *args, **kwargs):
        nonlocal reads
        value = original_read(path, *args, **kwargs)
        if path == first['trial']:
            reads += 1
            if reads == 2:
                return json.dumps(json.loads(value), indent=2).encode() + b'\n'
        return value
    monkeypatch.setattr(report, 'stable_read', changed_second_read)
    with pytest.raises(ValueError, match='Trial changed during comparison'):
        report.compare_files(first, second)


def test_record_attempt_requires_reconciliation():
    args = evidence()
    args[0][4]['attempt'] = {'action': 'unresolved'}
    value = report.analyze_rows(*args)
    assert 'pending_attempt_requires_native_reconciliation' in value['issues']
    assert not value['measurement_checks_passed']


def test_malformed_initial_pending_checkpoint_fails_closed():
    args = evidence()
    args[2]['pending'] = {'action': 'unresolved'}
    with pytest.raises(ValueError, match='Pending action without an active plan'):
        report.analyze_rows(*args)


def test_completed_status_cannot_return_to_running():
    args = evidence()
    args[0][4]['status'] = 'completed'
    assert 'records_after_terminal_completion' in report.analyze_rows(*args)['issues']


def test_first_boundary_cannot_invent_paid_solid_route():
    args = evidence()
    args[2]['solid_commitments'].clear()
    assert 'uncheckpointed_initial_solid_commitment' in report.analyze_rows(*args)['issues']


@pytest.mark.parametrize('enabled', [('furnace_input_belts',),
                                     ('background_work', 'furnace_input_belts', 'ore_side_successors')])
def test_trial_rejects_transport_without_output_buffers(enabled):
    args = evidence()
    for flag in enabled:
        args[1]['configuration'][flag] = True
    with pytest.raises(ValueError, match='solid-route configuration'):
        report.analyze_rows(*args)


def test_composed_ownership_prefix_cannot_be_replaced():
    assert report._retains_prefix({'source': {'paid': 17, 'parts': {'belt': 'receipt'}}},
                                  {'source': {'paid': 17, 'parts': {'belt': 'receipt', 'next': 'new'}}})
    assert not report._retains_prefix({'source': {'paid': 17}}, {'source': {'paid': 18}})
    assert not report._retains_prefix({'source': {'paid': False}}, {'source': {'paid': True}})


def test_final_checkpoint_status_matches_last_record():
    args = evidence()
    args[0][-1]['status'] = 'completed'
    assert 'final_checkpoint_status_mismatch' in report.analyze_rows(*args)['issues']


@pytest.mark.parametrize('mutate', [
    lambda rows: rows[1].update(decision=None),
    lambda rows: rows[1]['decision'].update(model_called=False),
    lambda rows: rows[2].update(decision={'model_called': True}),
])
def test_model_call_agrees_with_decision_payload(mutate):
    args = evidence()
    mutate(args[0])
    assert 'decision_model_call_mismatch' in report.analyze_rows(*args)['issues']


@pytest.mark.parametrize('boundary', ['record', 'final'])
def test_completed_goals_cannot_disappear_or_change(boundary):
    args = evidence()
    args[2]['completed_goals'] = {'stockpile_fuel': 900}
    for row in args[0]:
        row['completed_goals'] = {'stockpile_fuel': 900}
    args[3]['completed_goals'] = {'stockpile_fuel': 900}
    if boundary == 'record':
        args[0][4]['completed_goals'] = {}
    else:
        args[3]['completed_goals']['stockpile_fuel'] = 901
    assert 'completed_goal_history_regressed' in report.analyze_rows(*args)['issues']


def test_belt_drainage_without_new_sends_is_not_new_flow():
    args = evidence()
    for row in args[0]:
        for label in ('state', 'after_state'):
            for route in row[label]['factory']['solid_routes']['routes'].values():
                route['flow']['sent'] = 100
    value = report.analyze_rows(*args)
    assert value['transport']['coal_consumers_with_new_flow'] == 0
    assert value['transport']['coal_inventory_delivery_lower_bound'] == 0
    assert 'two_distinct_fuel_consumers_not_measured' in value['issues']


def test_final_checkpoint_cannot_invent_goal():
    args = evidence()
    args[3]['completed_goals']['stockpile_fuel'] = args[3]['last_tick']
    assert 'completed_goal_history_regressed' in report.analyze_rows(*args)['issues']


def test_completed_status_requires_target_goal_history():
    args = evidence()
    args[0][-1]['status'] = args[3]['status'] = 'completed'
    assert 'completed_target_history_missing' in report.analyze_rows(*args)['issues']


def test_final_cleared_plan_requires_zero_step_index():
    args = evidence()
    args[3]['step_index'] = 1
    assert 'final_step_index_not_cleared' in report.analyze_rows(*args)['issues']


def test_new_receipt_cannot_be_backdated_before_previous_observation():
    args = evidence()
    args[0][4]['after_state']['factory']['receipts']['science:4']['tick'] = 1001
    assert 'unbound_science_delivery' in report.analyze_rows(*args)['issues']


def test_early_belt_drainage_and_late_sends_do_not_prove_delivery():
    args = evidence()
    for index, row in enumerate(args[0]):
        for label in ('state', 'after_state'):
            for route in row[label]['factory']['solid_routes']['routes'].values():
                route['flow'].update(sent=100 + (10 if index > 30 or index == 30 and label == 'after_state' else 0),
                                     received=90 + min(index, 10), positive_samples=3 + index)
    value = report.analyze_rows(*args)
    assert value['transport']['coal_consumers_with_new_flow'] == 0
    assert value['transport']['coal_inventory_delivery_lower_bound'] == 0
    assert 'two_distinct_fuel_consumers_not_measured' in value['issues']


def test_native_positive_samples_must_advance_with_attributed_receipts():
    args = evidence()
    for row in args[0]:
        for label in ('state', 'after_state'):
            for route in row[label]['factory']['solid_routes']['routes'].values():
                route['flow'].update(positive_samples=3, last_positive_tick=1000)
    value = report.analyze_rows(*args)
    assert 'route_positive_sample_history_mismatch' in value['issues']
    assert value['transport']['coal_consumers_with_new_flow'] == 0


def test_observed_input_commitment_cannot_be_omitted_from_final_checkpoint():
    args = evidence()
    for checkpoint in args[2:]:
        checkpoint.update(output_buffers_schema=1, output_commitments={},
                          input_routes_schema=1, input_commitments={})
    args[1]['configuration'].update(furnace_output_buffers=True, furnace_input_belts=True)
    for row in args[0]:
        row['acceptance_configuration'] = deepcopy(args[1]['configuration'])
        row.update(furnace_output_buffers=True, buffer_evidence={},
                   furnace_input_belts=True, input_route_evidence={},
                   input_validation_failure={})
    args[0][-1]['after_state']['factory']['input_routes'] = {
        'sources': {'recipe:iron-plate': {'state': 'building'}}}
    assert 'observed_composed_commitment_missing' in report.analyze_rows(*args)['issues']


def test_initial_capital_cannot_disappear_without_reconciliation():
    from test_capital_investments import scenario, offer
    from jev_factorio.planning import capital
    args = evidence()
    catalog, state = scenario()
    spec = offer(catalog, state).materials[capital.MARKER]['spec']
    args[2]['active_goal'] = 'rocket_launch'
    args[2]['capital_investment'] = {
        'spec': spec, 'stage': 'kit', 'started_tick': 1000,
        'deadline_tick': 8200, 'unit_number': None, 'products_baseline': None}
    assert 'capital_reconciliation_missing' in report.analyze_rows(*args)['issues']


@pytest.mark.parametrize('field,value', [
    ('capacity_profile_sha256', '1'*64), ('initial_save_sha256', '1'*64),
    ('workload_sha256', '1'*64), ('experiment_sha256', '1'*64),
    ('evidence_kind', 'native_campaign'), ('minimum_timing_samples', 29),
    ('max_observation_gap_seconds', 119), ('max_no_science_progress_seconds', 119),
    ('regression_limits', {'max_iteration_p95_ratio': 1, 'min_science_rate_ratio': 1}),
])
def test_comparison_rejects_changed_controls(field, value):
    args = paired(); args[1][1][field] = value
    assert 'uncontrolled_pair_difference' in comparison(args)['issues']


def test_capacity_comparison_must_hold_source_fixed():
    args = paired()
    for part in args: part[1]['comparison_axis'] = 'capacity'
    args[1][1]['capacity_profile_sha256'] = '1'*64
    assert comparison(args)['paired_measurement_checks_passed']
    args[1][1]['expected_commit'] = '2'*40
    for record in args[1][0]: record['code_revision']['commit'] = '2'*40
    assert 'uncontrolled_pair_difference' in comparison(args)['issues']


def test_algorithm_comparison_allows_different_source_not_different_capacity():
    args = paired(); args[1][1]['expected_commit'] = '2'*40
    for record in args[1][0]: record['code_revision']['commit'] = '2'*40
    assert comparison(args)['paired_measurement_checks_passed']


def test_same_capture_cannot_be_relabelled_as_a_second_arm():
    baseline, treatment = evidence(), evidence(); baseline[1]['arm'] = 'baseline'
    assert 'reused_capture_or_invocation' in comparison((baseline, treatment))['issues']


def test_unmatched_production_trends_are_not_controlled_pairs():
    args = paired()
    for part in args: part[1]['comparison_axis'] = 'unmatched'
    assert 'invalid_or_unmatched_pair' in comparison(args)['issues']


def test_tampered_report_trial_binding_is_rejected():
    args = paired(); first = report.analyze_rows(*args[0]); second = report.analyze_rows(*args[1])
    first['trial_sha256'] = '0'*64
    with pytest.raises(ValueError, match='bound'):
        report.compare(first, second, args[0][1], args[1][1])


def test_current_report_tail_is_not_inferred_and_first_crossing_cycle_excluded():
    value = report.analyze_rows(*evidence())
    assert value['timing']['boundary_iterations_excluded'] == 1
    assert not value['timing']['final_unpublished_tail_inferred']
    assert value['timing']['distributions']['gap:total_ns:wall']['count'] == 30


@pytest.mark.parametrize('kind', ['direction', 'foreign_endpoint', 'wrong_recipe', 'stale_tick',
                                  'lost_prefix', 'counter_reset', 'flow_epoch', 'lost_flow', 'bad_receipt'])
def test_bad_transport_evidence_never_passes(kind):
    args = evidence(); state = args[0][4]['state']; factory = state['factory']
    key, route = next(iter(factory['solid_routes']['routes'].items()))
    if kind == 'direction': route['steps'][0]['direction'] = 4
    elif kind == 'foreign_endpoint': factory['entities'][route['source']['role']]['unit_number'] = 123
    elif kind == 'wrong_recipe': factory['entities'][route['target']['role']]['name'] = 'assembling-machine-2'
    elif kind == 'stale_tick': factory['solid_routes']['tick'] -= 1
    elif kind == 'lost_prefix': route['parts'].pop('send')
    elif kind == 'counter_reset': route['flow']['received'] = 0
    elif kind == 'flow_epoch': route['flow']['first_tick'] += 1
    elif kind == 'lost_flow': route['flow'] = {}
    elif kind == 'bad_receipt': route['parts']['send']['receipt'] = 'other-paid-receipt'
    value = report.analyze_rows(*args)
    assert not value['measurement_checks_passed'] and value['issues']
    assert value['native_acceptance'] == 'not_accepted'


def test_one_fuel_consumer_and_placed_only_downstream_do_not_meet_flow_checks():
    args = evidence()
    for record in args[0]:
        for label in ('state', 'after_state'):
            for route in list(record[label]['factory']['solid_routes']['routes'].values())[1:]:
                route['flow'].update(sent=10, received=10, positive_samples=3)
    value = report.analyze_rows(*args)
    assert value['transport']['coal_consumers_with_new_flow'] == 1
    assert 'two_distinct_fuel_consumers_not_measured' in value['issues']
    assert 'downstream_flow_and_production_not_measured' in value['issues']


def test_old_ore_side_exporter_still_rejects_solid_treatment():
    from jev_factorio.acceptance_capture import project_record
    with pytest.raises(ValueError, match='solid-route'):
        from jev_factorio.research_log import Redactor
        from collections import Counter
        project_record(evidence()[0][0], Redactor({}), Counter())


def test_native_claim_label_never_confers_authenticity():
    args = evidence(); args[1]['evidence_kind'] = 'native_campaign'
    value = report.analyze_rows(*args)
    assert value['measurement_checks_passed']
    assert not value['external_authenticity_proven']
    assert value['native_acceptance'] == 'not_accepted'


def test_baseline_outcome_failure_is_reported_without_discarding_valid_zero_data():
    args = paired(stalled_baseline=True)[0]
    value = report.analyze_rows(*args)
    assert value['integrity_checks_passed'] and value['measurement_checks_passed']
    assert value['science']['force_consumed_with_single_owned_lab'] == 0
    assert 'sustained_science_progress_missing' in value['outcome_gaps']


def test_every_required_science_pack_must_be_consumed():
    args = evidence(); args[1]['science_packs'].append('logistic-science-pack')
    value = report.analyze_rows(*args)
    assert 'science_delivery_or_consumption_missing' in value['issues']


@pytest.mark.parametrize('value', [10**400, True, 1.5, -1, float('nan'), float('inf')])
def test_huge_counter_and_noninteger_budget_do_not_overflow(value):
    args = evidence(); args[0][2]['failure_budgets']['invalid'] = value
    with pytest.raises(ValueError): report.analyze_rows(*args)


def test_malformed_checkpoint_is_not_accepted_as_optional_metadata(tmp_path):
    paths = files(tmp_path)
    value = json.loads(paths['initial_checkpoint'].read_bytes()); value['unrecognized_extension'] = True
    paths['initial_checkpoint'].write_bytes(canonical(value))
    with pytest.raises(ValueError):
        report.analyze(paths['gameplay'], paths['trial'], paths['initial_checkpoint'], paths['final_checkpoint'])


def test_incomplete_pair_cli_never_writes_output(tmp_path, capsys):
    paths = files(tmp_path); output = tmp_path / 'output.json'
    args = [item for key, path in paths.items() for item in ('--'+key.replace('_','-'), str(path))]
    with pytest.raises(SystemExit) as result:
        report.main(args + ['--baseline-gameplay', str(paths['gameplay']), '--output', str(output)])
    assert result.value.code == 2 and not output.exists()
    assert 'private-fixture' not in capsys.readouterr().err


def test_comparison_recomputes_both_raw_captures_and_cli_roundtrip(tmp_path):
    first, second = paired(stalled_baseline=True)
    baseline = files(tmp_path / 'baseline', first); treatment = files(tmp_path / 'treatment', second)
    value = report.compare_files(baseline, treatment)
    assert value['paired_measurement_checks_passed'], value['issues']
    output = tmp_path / 'compared.json'
    args = [item for key,path in treatment.items() for item in ('--'+key.replace('_','-'), str(path))]
    args += [item for key,path in baseline.items() for item in ('--baseline-'+key.replace('_','-'), str(path))]
    with pytest.raises(SystemExit) as result: report.main(args + ['--output', str(output)])
    assert result.value.code == 0
    assert json.loads(output.read_bytes()) == value
    assert output.stat().st_mode & 0o777 == 0o600


def test_comparison_refuses_incomplete_input_maps(tmp_path):
    with pytest.raises(ValueError): report.compare_files({'trial': tmp_path/'trial'}, {})


def test_trial_aba_readback_conflict_is_detected(tmp_path, monkeypatch):
    baseline, treatment = paired()
    first, second = files(tmp_path/'a', baseline), files(tmp_path/'b', treatment)
    original = report.stable_read
    counts = {}
    def changed(path, *args):
        data = original(path, *args)
        counts[path] = counts.get(path,0) + 1
        if path == first['trial'] and counts[path] == 2:
            value = json.loads(data); value['minimum_timing_samples'] = 29
            return canonical(value)
        return data
    monkeypatch.setattr(report, 'stable_read', changed)
    with pytest.raises(ValueError, match='changed'): report.compare_files(first, second)


def test_last_record_and_checkpoint_must_agree_on_failure_history():
    args = evidence(); args[3]['failures'] = {'not-in-observation': 1}
    assert 'final_failure_history_mismatch' in report.analyze_rows(*args)['issues']


def test_native_tick_speedup_is_not_elapsed_window_proof():
    args = evidence()
    for record in args[0]:
        for label in ('state', 'after_state'):
            state=record[label];state['tick'] *= 10;state['factory']['tick']=state['tick']
            state['factory']['solid_routes']['tick']=state['tick']
            for route in state['factory']['solid_routes']['routes'].values():
                for key in ('first_tick','last_tick','last_positive_tick'):route['flow'][key] *= 10
    args[2]['last_tick'] *= 10;args[3]['last_tick'] *= 10
    assert 'native_time_outpaces_wall_time' in report.analyze_rows(*args)['issues']


def test_unowned_new_receipt_and_changed_epoch_fail():
    args = evidence(); args[0][4]['after_state']['factory']['receipts']['science:4']['unit_number'] = 17
    assert 'unbound_science_delivery' in report.analyze_rows(*args)['issues']
    args=evidence();args[2]['solid_epoch']['actor_index']=2
    assert 'checkpoint_epoch_mismatch' in report.analyze_rows(*args)['issues']


def test_cli_sanitizes_malformed_secret_bearing_evidence(tmp_path, capsys):
    paths=files(tmp_path);output=tmp_path/'report.json'
    paths['trial'].write_bytes(b'{"secret":"TOKEN=fixture-secret-value","schema":NaN}')
    args=[item for key,path in paths.items() for item in ('--'+key.replace('_','-'), str(path))]
    with pytest.raises(SystemExit) as result:report.main(args+['--output',str(output)])
    assert result.value.code==2 and not output.exists()
    assert 'TOKEN' not in capsys.readouterr().err


def test_no_backend_or_actor_execution_is_required(tmp_path, monkeypatch):
    from jev_factorio.backends.fle import FleBackend
    def forbidden(*args,**kwargs):raise AssertionError('Must not initialize a native backend')
    monkeypatch.setattr(FleBackend,'__init__',forbidden)
    paths=files(tmp_path)
    result=report.analyze(paths['gameplay'],paths['trial'],paths['initial_checkpoint'],paths['final_checkpoint'])
    assert result['measurement_checks_passed']


def test_existing_supervisor_revision_shape_does_not_invent_clean_tree_evidence():
    args=evidence()
    assert set(args[0][0]['code_revision']) == {'commit','source_sha256'}
    value=report.analyze_rows(*args)
    assert value['measurement_checks_passed'] and value['working_tree_cleanliness']=='not_reported'
    for row in args[0]:row['code_revision']['dirty']=False
    assert report.analyze_rows(*args)['working_tree_cleanliness']=='explicitly_reported_clean'
    assert 'source_clean_tree_and_fingerprint_readback' in value['remaining_gates']


@pytest.mark.parametrize('field,value,code', [
    ('requested_model','wrong-model','requested_model_mismatch'),
    ('segment_id','other-segment','mixed_invocation_or_treatment'),
    ('model_call',1,'invalid_model_call_flag'),
    ('campaign_treatment',{'schema':1},'configuration_mismatch'),
])
def test_additional_treatment_fields_are_bound(field,value,code):
    args=evidence();args[0][4][field]=value
    assert code in report.analyze_rows(*args)['issues']


@pytest.mark.parametrize('checkpoint_index', [2, 3])
def test_in_memory_entrypoint_rejects_invalid_composed_checkpoint(checkpoint_index):
    args = evidence()
    args[checkpoint_index]['solid_schema'] = 999
    with pytest.raises(ValueError):
        report.analyze_rows(*args)


def test_documented_trial_example_is_explicitly_a_fixture():
    example = Path(__file__).resolve().parents[1] / 'examples/integration_trial.fixture.json'
    trial = json.loads(example.read_text())
    report.validate_trial(trial)
    assert trial['evidence_kind'] == 'fixture'
    assert trial == evidence()[1]


@pytest.mark.parametrize('mutate,code', [
    (lambda factory: factory['entities']['utility:lab'].update(unit_number=1234), 'owned_lab_replaced'),
    (lambda factory: factory['force_entity_counts'].update(lab=2), 'exclusive_owned_lab_not_observed'),
])
def test_pre_action_lab_identity_and_exclusivity_cannot_be_erased_by_after_state(mutate, code):
    args = evidence()
    mutate(args[0][4]['state']['factory'])
    value = report.analyze_rows(*args)
    assert not value['measurement_checks_passed']
    assert code in value['issues']
