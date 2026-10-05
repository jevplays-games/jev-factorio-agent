"""Real local-file fault boundaries; not native Factorio or power-loss proof."""
from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

import jev_factorio.checkpoint_io as checkpoint
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import CampaignMemory
from test_causal_trace import Backend, Client


def interfere(path: Path, change: str) -> bytes | None:
    before = path.stat()
    content = path.read_bytes()
    if change == 'replace':
        other = path.with_name('external-replacement')
        other.write_bytes(content)
        os.replace(other, path)
    elif change == 'delete':
        path.unlink()
        return None
    elif change == 'symlink':
        other = path.with_name('external-target')
        other.write_bytes(b'external')
        path.unlink()
        path.symlink_to(other)
    elif change == 'truncate':
        path.write_bytes(b'{}')
    elif change == 'same_size_restored_mtime':
        assert b'running' in content
        path.write_bytes(content.replace(b'running', b'blocked'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        raise AssertionError(change)
    return path.read_bytes()


CHANGES = ['replace', 'delete', 'symlink', 'truncate', 'same_size_restored_mtime']


@pytest.mark.parametrize('boundary', ['replace', 'directory_sync'])
@pytest.mark.parametrize('change', CHANGES)
def test_changed_installation_never_becomes_a_successful_cached_save(tmp_path, monkeypatch, boundary, change):
    path = tmp_path / 'state.json'
    memory = CampaignMemory('fixture', 'rocket_launch')
    memory.save(path)
    memory.reason = 'new authoritative state'
    real_replace, real_sync = os.replace, os.fsync
    altered = []

    def replace(source, target):
        real_replace(source, target)
        if boundary == 'replace' and Path(target) == path and not altered:
            altered.append(True)
            altered.append(interfere(path, change))

    def sync(fd):
        real_sync(fd)
        if boundary == 'directory_sync' and stat.S_ISDIR(os.fstat(fd).st_mode) and not altered:
            altered.append(True)
            altered.append(interfere(path, change))

    monkeypatch.setattr(checkpoint.os, 'replace', replace)
    monkeypatch.setattr(checkpoint.os, 'fsync', sync)
    with pytest.raises(OSError, match='Checkpoint installation'):
        memory.save(path)
    assert altered
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'
    # Never retry over or delete the conflicting owner's file.
    assert (path.read_bytes() if path.exists() else None) == altered[1]
    if change == 'symlink':
        assert path.is_symlink()
        assert path.with_name('external-target').read_bytes() == b'external'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX directory durability')
@pytest.mark.parametrize('depth', [0, 1, 3])
def test_new_parent_entries_are_synced_before_checkpoint_preparation(tmp_path, monkeypatch, depth):
    directory = tmp_path
    for index in range(depth):
        directory /= f'level{index}'
    path = directory / 'state.json'
    real_sync, real_temp = os.fsync, checkpoint.tempfile.mkstemp
    events = []

    def sync(fd):
        info = os.fstat(fd)
        events.append(('directory' if stat.S_ISDIR(info.st_mode) else 'file', info.st_ino))
        real_sync(fd)

    def temporary(*args, **kwargs):
        events.append(('temporary', None))
        return real_temp(*args, **kwargs)

    monkeypatch.setattr(checkpoint.os, 'fsync', sync)
    monkeypatch.setattr(checkpoint.tempfile, 'mkstemp', temporary)
    memory = CampaignMemory('fixture', 'rocket_launch')
    memory.save(path)
    expected_parents = [tmp_path]
    for index in range(max(depth - 1, 0)):
        expected_parents.append(expected_parents[-1] / f'level{index}')
    assert events[:depth] == [('directory', p.stat().st_ino) for p in expected_parents[:depth]]
    assert [kind for kind, _ in events[depth:]] == ['temporary', 'file', 'directory']
    metrics = memory._checkpoint_metrics
    assert metrics['directory_sync_calls'] == depth + 1
    assert metrics['parent_directory_sync_calls'] == depth
    assert metrics['file_sync_calls'] == 1
    assert metrics['verification_read_calls'] == 1
    assert metrics['verification_read_bytes'] == len(path.read_bytes())
    events.clear()
    memory.save(path)
    assert events == []
    assert memory._checkpoint_metrics['status'] == 'unchanged'
    assert memory._checkpoint_metrics['verification_read_calls'] == 0


@pytest.mark.skipif(os.name != 'posix', reason='POSIX directory durability')
@pytest.mark.parametrize('fail_at', [1, 2, 3])
def test_parent_sync_failure_precedes_any_checkpoint_or_backend_mutation(tmp_path, monkeypatch, fail_at):
    path = tmp_path / 'one' / 'two' / 'three' / 'state.json'
    real_sync = os.fsync
    error = OSError(errno.EIO, 'injected parent sync failure')
    calls = []

    def sync(fd):
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        calls.append(fd)
        if len(calls) == fail_at:
            raise error
        real_sync(fd)

    monkeypatch.setattr(checkpoint.os, 'fsync', sync)
    backend = Backend()
    loop = HierarchicalLoop(backend, Client(), checkpoint=str(path), factory_scheduling='ready-work')
    with pytest.raises(OSError):
        loop.step()
    assert not path.exists()
    assert not any(p.is_file() for p in tmp_path.rglob('*'))
    assert not any(call[0] == 'act' for call in backend.calls)
    before = list(backend.calls)
    with pytest.raises(RuntimeError, match='persistence failed'):
        loop.step()
    assert backend.calls == before
    assert loop.memory._checkpoint_cache is None


@pytest.mark.parametrize('stage,expected_actions', [('prepared', 0), ('returned', 1)])
@pytest.mark.parametrize('boundary', ['replace', 'directory_sync'])
@pytest.mark.parametrize('change', ['replace', 'delete', 'same_size_restored_mtime'])
def test_interference_stops_controller_without_losing_previous_prepared_state(
        tmp_path, monkeypatch, stage, expected_actions, boundary, change):
    path = tmp_path / 'state.json'
    backend = Backend()
    loop = HierarchicalLoop(backend, Client(), checkpoint=str(path), factory_scheduling='ready-work')
    real_replace, real_sync = os.replace, os.fsync
    captured = {}

    def damage_when_stage_matches():
        if captured or not path.exists():
            return
        payload = path.read_bytes()
        pending = json.loads(payload).get('pending') or {}
        if pending.get('dispatch') == stage:
            captured['payload'] = payload
            captured['identity'] = dict(pending)
            captured['after'] = interfere(path, change)

    def replace(source, target):
        real_replace(source, target)
        if boundary == 'replace' and Path(target) == path:
            damage_when_stage_matches()

    def sync(fd):
        real_sync(fd)
        if boundary == 'directory_sync' and stat.S_ISDIR(os.fstat(fd).st_mode):
            damage_when_stage_matches()

    monkeypatch.setattr(checkpoint.os, 'replace', replace)
    monkeypatch.setattr(checkpoint.os, 'fsync', sync)
    with pytest.raises(OSError):
        loop.step()
    assert captured
    assert sum(call[0] == 'act' for call in backend.calls) == expected_actions
    assert loop.memory.pending == captured['identity']
    assert loop.memory._checkpoint_cache is None
    before = list(backend.calls)
    loop._trace.metrics = None
    with pytest.raises(RuntimeError, match='persistence failed'):
        loop.step()
    assert backend.calls == before
    assert (path.read_bytes() if path.exists() else None) == captured['after']


def test_cleanup_failure_does_not_mask_primary_sync_failure(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    memory = CampaignMemory('fixture', 'rocket_launch')
    primary = OSError(errno.ENOSPC, 'primary storage failure')
    monkeypatch.setattr(checkpoint.os, 'fsync', lambda fd: (_ for _ in ()).throw(primary))
    monkeypatch.setattr(checkpoint.os, 'unlink', lambda path, *a, **k: (_ for _ in ()).throw(PermissionError('cleanup failure')))
    with pytest.raises(OSError) as caught:
        memory.save(path)
    assert caught.value is primary
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'


@pytest.mark.parametrize('existing', [False, True])
def test_partial_write_is_detected_before_checkpoint_acceptance(tmp_path, monkeypatch, existing):
    memory = CampaignMemory('fixture', 'rocket_launch')
    path = tmp_path / 'state.json'
    previous = b'previous durable checkpoint'
    if existing:
        path.write_bytes(previous)
    real_open = os.fdopen

    class PartialWriter:
        def __init__(self, stream): self.stream = stream
        def __getattr__(self, name): return getattr(self.stream, name)
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def write(self, value): return self.stream.write(value[:len(value) // 2])

    monkeypatch.setattr(checkpoint.os, 'fdopen', lambda *a, **k: PartialWriter(real_open(*a, **k)))
    with pytest.raises(OSError, match='Checkpoint write length mismatch'):
        memory.save(path)
    assert path.read_bytes() == previous if existing else not path.exists()
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX directory identity')
def test_parent_replacement_during_sync_is_not_accepted(tmp_path, monkeypatch):
    directory = tmp_path / 'parent'
    directory.mkdir()
    path = directory / 'state.json'
    retained = tmp_path / 'retained-parent'
    real_sync = os.fsync

    def sync(fd):
        real_sync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not retained.exists():
            directory.rename(retained)
            directory.mkdir()
            path.write_bytes((retained / path.name).read_bytes())

    monkeypatch.setattr(checkpoint.os, 'fsync', sync)
    memory = CampaignMemory('fixture', 'rocket_launch')
    with pytest.raises(OSError, match='Checkpoint installation changed'):
        memory.save(path)
    assert memory._checkpoint_cache is None
    assert path.exists() and (retained / path.name).exists()


def test_io_counters_roundtrip_without_inventing_legacy_measurement(tmp_path):
    from jev_factorio.performance import PerformanceCounters, summarize
    memory = CampaignMemory('fixture', 'rocket_launch')
    path = tmp_path / 'state.json'
    counters = PerformanceCounters()
    for _ in range(3):
        memory.save(path)
        counters.checkpoint(memory._checkpoint_metrics)
    snapshot = counters.snapshot()
    assert snapshot['checkpoint_operations']['io_measured_calls'] == 3
    assert snapshot['checkpoint_operations']['verification_read_calls'] == 1
    assert snapshot['checkpoint_operations']['directory_sync_calls'] == 1
    log = tmp_path / 'metrics.jsonl'
    # summarize() consumes gameplay rows; match the controller's declared
    # record shape while preserving this isolated checkpoint-counter roundtrip.
    log.write_text(json.dumps({'action': 'observe', 'phases': [], 'performance': snapshot}) + '\n')
    assert summarize(log)['checkpoint_operations'] == snapshot['checkpoint_operations']
    legacy = PerformanceCounters()
    legacy.checkpoint({'status': 'written', 'bytes': 5, 'capture_calls': 1, 'serialization_calls': 1})
    assert 'io_measured_calls' not in legacy.snapshot()['checkpoint_operations']


def test_snapshot_excludes_transient_verification_counters(tmp_path):
    from dataclasses import asdict
    from jev_factorio.checkpoint_io import checkpoint_data
    memory = CampaignMemory('fixture', 'rocket_launch')
    path = tmp_path / 'state.json'
    memory.save(path)
    persisted = json.loads(path.read_bytes())
    expected = asdict(memory)
    expected.pop('capital_investment', None)
    expected.pop('blocked_recovery', None)
    expected.pop('blocked_recovery_archive', None)
    assert persisted == expected == checkpoint_data(memory)
    assert not any('verification' in key or 'sync_calls' in key for key in persisted)


def test_snapshot_preserves_nonempty_blocked_recovery_ledger(tmp_path):
    from jev_factorio.blocked_persistence import record_attempt
    from jev_factorio.checkpoint_io import checkpoint_data
    memory = CampaignMemory('fixture', 'rocket_launch', status='blocked',
                            reason='low choice confidence', last_tick=0)
    source = {'commit': 'a' * 40, 'source_sha256': 'b' * 64}
    record_attempt(memory, source, 'c' * 64, memory.reason, 0)
    path = tmp_path / 'state.json'
    memory.save(path)
    persisted = json.loads(path.read_bytes())
    assert persisted == checkpoint_data(memory)
    assert persisted['blocked_recovery'] == memory.blocked_recovery
    restored = CampaignMemory.load(path, 'fixture', 'rocket_launch')
    assert restored.blocked_recovery == memory.blocked_recovery

@pytest.mark.parametrize('changed', [False, True])
def test_windows_sharing_branch_closes_temp_before_replace_and_checks_reopened_identity(
        tmp_path, monkeypatch, changed):
    """Modeled Windows sharing semantics, not actual Windows qualification."""
    from types import SimpleNamespace
    path = tmp_path / 'state.json'
    streams = []
    real_fdopen, real_replace = os.fdopen, os.replace

    def fdopen(*args, **kwargs):
        stream = real_fdopen(*args, **kwargs)
        streams.append(stream)
        return stream

    def replace(source, target):
        assert streams and all(stream.closed for stream in streams)
        real_replace(source, target)
        if changed:
            # Preserve size and timestamps but not the captured descriptor identity.
            interfered = Path(target)
            before = interfered.stat()
            replacement = interfered.with_name('other-file')
            replacement.write_bytes(interfered.read_bytes())
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            real_replace(replacement, interfered)

    proxy = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    proxy.name, proxy.fdopen, proxy.replace = 'nt', fdopen, replace
    monkeypatch.setattr(checkpoint, 'os', proxy)
    memory = CampaignMemory('fixture', 'rocket_launch')
    if changed:
        with pytest.raises(OSError, match='Checkpoint installation'):
            memory.save(path)
        assert memory._checkpoint_cache is None
    else:
        memory.save(path)
        assert memory._checkpoint_metrics['status'] == 'written'
        assert memory._checkpoint_metrics['directory_sync_calls'] == 0
        assert memory._checkpoint_metrics['verification_read_calls'] == 1
        memory.save(path)
        assert memory._checkpoint_metrics['status'] == 'unchanged'

@pytest.mark.skipif(os.name != 'posix', reason='POSIX directory durability')
@pytest.mark.parametrize('primary', [OSError(errno.ENOSPC, 'injected full device'), KeyboardInterrupt('injected interruption')])
@pytest.mark.parametrize('secondary', [OSError(errno.EIO, 'injected close'), KeyboardInterrupt('injected close interruption')])
def test_directory_close_preserves_original_sync_failure(tmp_path, monkeypatch, primary, secondary):
    real_sync, real_close = os.fsync, os.close
    directory_fds = set()
    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fds.add(fd)
            raise primary
        return real_sync(fd)
    def close(fd):
        real_close(fd)
        if fd in directory_fds:
            raise secondary
    monkeypatch.setattr(checkpoint.os, 'fsync', sync)
    monkeypatch.setattr(checkpoint.os, 'close', close)
    memory = CampaignMemory('fixture', 'rocket_launch')
    with pytest.raises(BaseException) as raised:
        memory.save(tmp_path / 'state.json')
    assert raised.value is primary
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX directory durability')
def test_directory_close_only_failure_is_not_hidden(tmp_path, monkeypatch):
    real_close = os.close
    failure = OSError(errno.EIO, 'injected directory close')
    def close(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_directory:
            raise failure
    monkeypatch.setattr(checkpoint.os, 'close', close)
    memory = CampaignMemory('fixture', 'rocket_launch')
    with pytest.raises(OSError) as raised:
        memory.save(tmp_path / 'state.json')
    assert raised.value is failure
    assert memory._checkpoint_cache is None


@pytest.mark.parametrize('boundary', ['replace', 'directory_sync'])
def test_byte_verification_rejects_edit_even_when_metadata_timestamps_collide(
        tmp_path, monkeypatch, boundary):
    path = tmp_path / 'state.json'
    memory = CampaignMemory('fixture', 'rocket_launch')
    real_identity = checkpoint._identity
    real_replace, real_sync = checkpoint.os.replace, checkpoint.os.fsync
    edited = []

    def coarse_identity(info):
        # Model equal timestamp values deterministically; preserve inode and size.
        return (*real_identity(info)[:3], 0, 0)

    def corrupt():
        payload = path.read_bytes()
        assert b'running' in payload
        path.write_bytes(payload.replace(b'running', b'blocked', 1))
        edited.append(path.read_bytes())

    def replace(source, target):
        real_replace(source, target)
        if boundary == 'replace':
            corrupt()

    def sync(fd):
        real_sync(fd)
        if boundary == 'directory_sync' and stat.S_ISDIR(os.fstat(fd).st_mode):
            corrupt()

    monkeypatch.setattr(checkpoint, '_identity', coarse_identity)
    monkeypatch.setattr(checkpoint.os, 'replace', replace)
    monkeypatch.setattr(checkpoint.os, 'fsync', sync)
    with pytest.raises(OSError, match='Checkpoint installation bytes'):
        memory.save(path)
    assert edited == [path.read_bytes()]
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'
