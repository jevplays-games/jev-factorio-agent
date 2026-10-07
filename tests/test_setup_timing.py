import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from jev_factorio.setup_timing import (BACKEND_STAGES, LEGACY_SETUP_STAGES,
                                       STAGES, SetupTiming)


def test_setup_boundaries_follow_preflight_before_run_writer_readiness():
    assert STAGES == (
        'setup_start', 'preflight_ready', 'research_ready', 'dashboard_ready',
        'backend_ready', 'controller_ready', 'outputs_ready', 'initialized',
    )
    assert LEGACY_SETUP_STAGES == (
        'setup_start', 'research_ready', 'dashboard_ready', 'preflight_ready',
        'backend_ready', 'controller_ready', 'outputs_ready', 'initialized',
    )

    timing = SetupTiming(None)
    for stage in LEGACY_SETUP_STAGES:
        timing.mark(stage)
    assert timing.result()['status'] == 'partial'


def test_setup_boundaries_are_ordered_nonoverlapping_and_content_free(tmp_path):
    walls = iter(range(100, 100 + 10 * len(STAGES), 10))
    cpus = iter(range(20, 20 + 3 * len(STAGES), 3))
    timing = SetupTiming(tmp_path / 'result.json', wall_clock=lambda: next(walls),
                         cpu_clock=lambda: next(cpus))
    for stage in STAGES:
        timing.mark(stage)
    timing.write()
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['schema'] == 'jev.setup-timing.v1'
    assert result['status'] == 'complete'
    assert [(row['from'], row['to']) for row in result['phases']] == list(zip(STAGES, STAGES[1:]))
    assert all(row['wall_ns'] == 10 and row['process_cpu_ns'] == 3 for row in result['phases'])
    assert not ({'path', 'session_id', 'actor', 'command', 'payload'} & set(result))
    timing.write()
    assert json.loads((tmp_path / 'result.json').read_text()) == result


def test_failed_or_regressing_setup_is_partial(tmp_path):
    timing = SetupTiming(tmp_path / 'partial.json', wall_clock=iter((10, 9)).__next__,
                         cpu_clock=iter((1, 2)).__next__)
    timing.mark('setup_start')
    timing.mark('research_ready')
    timing.write()
    result = json.loads((tmp_path / 'partial.json').read_text())
    assert result['status'] == 'partial'
    assert result['phases'] == []


def test_fle_backend_subphases_are_nested_without_counting_them_twice(tmp_path):
    walls = iter(range(100, 1000, 10))
    cpus = iter(range(20, 200, 2))
    timing = SetupTiming(tmp_path / 'backend.json', backend_expected=True,
                         wall_clock=lambda: next(walls),
                         cpu_clock=lambda: next(cpus))
    for stage in STAGES:
        timing.mark(stage)
        if stage == 'dashboard_ready':
            for detail in BACKEND_STAGES:
                timing.mark_backend(detail)
    data = timing.result()
    assert data['status'] == 'complete'
    assert [row['to'] for row in data['backend_phases']] == list(BACKEND_STAGES[1:])
    outer = next(row for row in data['phases'] if row['from'] == 'dashboard_ready')
    assert sum(row['wall_ns'] for row in data['backend_phases']) <= outer['wall_ns']


def test_bad_final_timing_cannot_mask_existing_step_error(tmp_path):
    class BrokenLoop:
        @property
        def _timing_pending(self):
            raise TypeError('diagnostic failed')

    timing = SetupTiming(tmp_path / 'partial.json')
    with pytest.raises(RuntimeError, match='original step failure'):
        try:
            raise RuntimeError('original step failure')
        finally:
            timing.capture_final_iteration_safely(BrokenLoop())
    assert timing.result()['status'] == 'partial'


def test_one_step_mock_cli_publishes_setup_without_secret_or_identifiers(tmp_path):
    result = tmp_path / 'setup.json'
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'TYPESAFE_API_KEY': 'secret-never-in-setup-result'}
    proc = subprocess.run([
        sys.executable, '-m', 'jev_factorio', '--backend', 'mock',
        '--controller', 'hierarchical', '--mock-model', '--target', 'bootstrap_mining',
        '--factory-scheduling', 'ready-work', '--steps', '1', '--tick-seconds', '0',
        '--setup-timing-file', str(result),
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(result.read_text())
    assert data['status'] == 'complete'
    assert len(data['phases']) == len(STAGES) - 1
    assert data['final_iteration']['partition_complete'] is True
    assert data['final_iteration']['gap']['complete'] is False
    assert data['final_iteration']['phases']['iteration']['calls'] == 1
    assert 'secret-never-in-setup-result' not in result.read_text()
    assert str(tmp_path) not in result.read_text()


def test_failed_research_setup_publishes_partial_without_starting_step(tmp_path):
    occupied = tmp_path / 'occupied'
    occupied.mkdir()
    result = tmp_path / 'partial.json'
    proc = subprocess.run([
        sys.executable, '-m', 'jev_factorio', '--backend', 'mock',
        '--controller', 'hierarchical', '--mock-model', '--target', 'bootstrap_mining',
        '--steps', '1', '--tick-seconds', '0', '--run-dir', str(occupied),
        '--setup-timing-file', str(result),
    ], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    data = json.loads(result.read_text())
    assert data['status'] == 'partial'
    assert data['final_iteration'] is None
    assert [(row['from'], row['to']) for row in data['phases']] == [
        ('setup_start', 'preflight_ready')]
    assert all(row['wall_ns'] >= 0 and row['process_cpu_ns'] >= 0
               for row in data['phases'])
