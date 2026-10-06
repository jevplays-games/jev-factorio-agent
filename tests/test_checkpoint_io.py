"""Durability, exact-state coalescing and failure injection; no game access."""
import json
import os
import stat
from dataclasses import asdict

import pytest

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.checkpoint_io import checkpoint_data, save_checkpoint
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.input_controller import input_loop_type
from jev_factorio.memory import CampaignMemory
from jev_factorio.outpost_controller import outpost_loop_type
from test_causal_trace import Backend, Client


def test_identical_bytes_skip_but_every_changed_state_is_written(tmp_path, monkeypatch):
    path = tmp_path/'state.json'
    memory = CampaignMemory('session', 'rocket_launch')
    import jev_factorio.checkpoint_io as module
    original = module.os.fsync
    syncs = []
    def sync(fd):
        syncs.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        original(fd)
    monkeypatch.setattr(module.os, 'fsync', sync)
    memory.save(path)
    first = path.read_bytes()
    for _ in range(20): memory.save(path)
    assert syncs == ['file', 'directory']
    assert memory._checkpoint_metrics['status'] == 'unchanged'
    assert path.read_bytes() == first
    memory.status = 'uncertain'
    memory.save(path)
    assert syncs == ['file', 'directory', 'file', 'directory']
    assert json.loads(path.read_bytes())['status'] == 'uncertain'
    assert memory._checkpoint_metrics['status'] == 'written'
    saved = json.loads(path.read_bytes())
    expected = asdict(memory)
    expected.pop('capital_investment', None)
    expected.pop('blocked_recovery', None)
    expected.pop('blocked_recovery_archive', None)
    expected.pop('async_decision', None)
    expected.pop('two_stage_decision', None)
    expected.pop('planner_fault_recovery', None)
    assert saved == expected == checkpoint_data(memory)
    expected_payload = json.dumps(expected, sort_keys=True, allow_nan=False).encode('utf-8')
    assert path.read_bytes() == expected_payload
    # Historical checkpoint bytes keep unrelated nullable fields in the schema.
    assert saved['active_goal'] is None


@pytest.mark.parametrize('change', ['delete', 'overwrite', 'replace', 'other_path', 'loaded_instance'])
def test_external_or_process_changes_invalidate_coalescing(tmp_path, change):
    memory = CampaignMemory('session', 'rocket_launch')
    path = tmp_path/'state.json'
    memory.save(path)
    expected = path.read_bytes()
    if change == 'delete': path.unlink()
    elif change == 'overwrite':
        before = path.stat()
        changed = expected.replace(b'"running"', b'"blocked"')
        assert changed != expected
        path.write_bytes(changed)
        # The coalescing contract uses metadata to detect external edits.
        # Make this edit observable even on a coarse or fast filesystem.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        assert path.stat().st_mtime_ns != before.st_mtime_ns
    elif change == 'replace':
        other = tmp_path/'other';other.write_bytes(expected);os.replace(other, path)
    elif change == 'other_path': path = tmp_path/'other.json'
    elif change == 'loaded_instance': memory = CampaignMemory.load(path, 'session', 'rocket_launch')
    memory.save(path)
    assert memory._checkpoint_metrics['status'] == 'written'
    assert path.read_bytes() == expected


def test_composed_memory_extensions_sync_the_same_directory_once(tmp_path, monkeypatch):
    memory_type = outpost_loop_type(input_loop_type(BackgroundWorkLoop)).memory_type
    memory = memory_type('session', 'rocket_launch')
    path = tmp_path/'state.json'
    import jev_factorio.checkpoint_io as module
    calls = []
    monkeypatch.setattr(module.os, 'fsync', lambda fd: calls.append(stat.S_ISDIR(os.fstat(fd).st_mode)))
    memory.save(path); memory.save(path)
    assert calls == [False, True]
    assert json.loads(path.read_text())['background_schema'] == 2
    assert json.loads(path.read_text())['input_routes_schema'] == 1
    assert json.loads(path.read_text())['outposts_schema'] == 1


@pytest.mark.parametrize('failure', ['serialize', 'file_sync', 'replace', 'directory_sync'])
def test_failed_writes_invalidate_cache_and_never_suppress_a_later_write(tmp_path, monkeypatch, failure):
    import jev_factorio.checkpoint_io as module
    path = tmp_path/'state.json'; memory = CampaignMemory('session', 'rocket_launch')
    memory.save(path)
    memory.reason = 'changed'
    with monkeypatch.context() as patch:
        if failure == 'serialize':
            patch.setattr(module.json, 'dumps', lambda *a, **k: (_ for _ in ()).throw(ValueError('serialization')))
        elif failure == 'replace':
            patch.setattr(module.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('replace')))
        else:
            def sync(fd):
                is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                if is_directory == (failure == 'directory_sync'):
                    raise OSError('injected sync failure')
            patch.setattr(module.os, 'fsync', sync)
        with pytest.raises((OSError, ValueError)): memory.save(path)
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['state.json']
    memory.save(path)
    assert memory._checkpoint_metrics['status'] == 'written'
    assert json.loads(path.read_text())['reason'] == 'changed'


def test_no_checkpoint_has_no_clock_or_filesystem_activity(monkeypatch):
    import jev_factorio.checkpoint_io as module
    monkeypatch.setattr(module.time, 'perf_counter_ns', lambda: pytest.fail('clock'))
    memory = CampaignMemory('session', 'rocket_launch')
    save_checkpoint(memory, None)
    assert memory._checkpoint_metrics['status'] == 'disabled'


def test_same_parent_symlink_or_replacement_is_not_an_unchanged_file(tmp_path):
    memory = CampaignMemory('session', 'rocket_launch')
    path = tmp_path/'state.json';memory.save(path)
    target = tmp_path/'other.json';target.write_text('external')
    path.unlink();path.symlink_to(target)
    memory.save(path)
    assert memory._checkpoint_metrics['status'] == 'written'
    assert not path.is_symlink() and target.read_text() == 'external'


def test_pre_dispatch_failure_prevents_mutation_and_poisons_this_controller(tmp_path, monkeypatch):
    import jev_factorio.checkpoint_io as module
    backend = Backend()
    loop = HierarchicalLoop(backend, Client(), checkpoint=str(tmp_path/'state.json'), factory_scheduling='ready-work')
    original = module.os.replace
    def fail_when_prepared(source, target):
        if json.loads(open(source).read())['pending'] is not None:
            raise OSError('injected')
        return original(source, target)
    monkeypatch.setattr(module.os, 'replace', fail_when_prepared)
    with pytest.raises(OSError): loop.step()
    calls = list(backend.calls)
    assert not any(c[0] == 'act' for c in calls)
    with pytest.raises(RuntimeError, match='persistence failed'): loop.step()
    assert calls == backend.calls


def test_phase_changes_and_returned_state_are_not_coalesced(tmp_path):
    path = tmp_path/'state.json'
    memory = CampaignMemory('session', 'rocket_launch')
    for stage in ('prepared', 'approach_started', 'transfer_started', 'returned', 'verified'):
        memory.history.append({'stage': stage})
        memory.save(path)
        assert memory._checkpoint_metrics['status'] == 'written'
        assert json.loads(path.read_bytes())['history'][-1]['stage'] == stage
