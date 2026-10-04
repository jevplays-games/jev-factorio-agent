"""Actual Windows checkpoint identity, replacement, metadata, and content guards."""
import json
import os
import stat
from pathlib import Path

import pytest

import jev_factorio.checkpoint_io as checkpoint
from jev_factorio.memory import CampaignMemory

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows path/fstat metadata semantics')


def test_windows_memory_save_accepts_stable_path_and_descriptor_stamps(tmp_path):
    memory = CampaignMemory('windows-save', 'rocket_launch')
    path = tmp_path / 'state.json'
    memory.save(path)
    first = path.read_bytes()
    assert json.loads(first)['session_id'] == memory.session_id
    assert memory._checkpoint_metrics['status'] == 'written'
    assert memory._checkpoint_metrics['file_sync_calls'] == 1
    assert memory._checkpoint_metrics['directory_sync_calls'] == 0
    assert memory._checkpoint_metrics['verification_read_bytes'] == len(first)
    assert memory._checkpoint_cache[2] == checkpoint._stamp(path)

    memory.save(path)
    assert memory._checkpoint_metrics['status'] == 'unchanged'
    assert memory._checkpoint_metrics['file_sync_calls'] == 0
    assert path.read_bytes() == first

    memory.reason = 'changed authoritative state'
    memory.save(path)
    assert memory._checkpoint_metrics['status'] == 'written'
    assert memory._checkpoint_metrics['file_sync_calls'] == 1
    assert json.loads(path.read_bytes())['reason'] == memory.reason


def test_windows_replacement_with_matching_bytes_and_mtime_is_rejected(tmp_path, monkeypatch):
    memory = CampaignMemory('windows-replacement', 'rocket_launch')
    path = tmp_path / 'state.json'
    real_replace = os.replace
    replacement = {}

    def replace(source, target):
        real_replace(source, target)
        if Path(target) != path or replacement:
            return
        installed = path.stat()
        before = checkpoint._stamp(path)
        external = path.with_name('external-replacement')
        external.write_bytes(path.read_bytes())
        os.utime(external, ns=(installed.st_atime_ns, installed.st_mtime_ns))
        external_before = checkpoint._stamp(external)
        real_replace(external, path)
        replacement.update(before=before, after=checkpoint._stamp(path), bytes=path.read_bytes())
        assert replacement['before'][:2] != external_before[:2]

    monkeypatch.setattr(checkpoint.os, 'replace', replace)
    with pytest.raises(OSError, match='Checkpoint installation changed before synchronization'):
        memory.save(path)
    assert replacement
    assert replacement['after'][:2] != replacement['before'][:2]
    assert path.read_bytes() == replacement['bytes']
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'


def test_windows_same_size_byte_edit_with_colliding_metadata_is_rejected(tmp_path, monkeypatch):
    memory = CampaignMemory('windows-byte-edit', 'rocket_launch')
    path = tmp_path / 'state.json'
    real_identity = checkpoint._identity
    real_sync = checkpoint._directory_sync
    changed = []

    def coarse_identity(info):
        # Force all reported timestamps and non-file identity metadata to collide.
        return (*real_identity(info)[:3], 0, 0)

    def edit_after_sync(directory, metrics, *, parent_entry=False):
        real_sync(directory, metrics, parent_entry=parent_entry)
        if parent_entry or changed:
            return
        before = path.stat()
        payload = path.read_bytes()
        altered = payload.replace(b'"running"', b'"blocked"', 1)
        assert len(altered) == len(payload) and altered != payload
        path.write_bytes(altered)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed.append((payload, path.read_bytes()))

    monkeypatch.setattr(checkpoint, '_identity', coarse_identity)
    monkeypatch.setattr(checkpoint, '_directory_sync', edit_after_sync)
    with pytest.raises(OSError, match='Checkpoint installation bytes changed across synchronization'):
        memory.save(path)
    assert changed and changed[0][0] != changed[0][1]
    assert path.read_bytes() == changed[0][1]
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'


def test_windows_metadata_change_after_installation_is_rejected(tmp_path, monkeypatch):
    memory = CampaignMemory('windows-metadata-edit', 'rocket_launch')
    path = tmp_path / 'state.json'
    real_sync = checkpoint._directory_sync
    changed = []

    def make_read_only_after_sync(directory, metrics, *, parent_entry=False):
        real_sync(directory, metrics, parent_entry=parent_entry)
        if parent_entry or changed:
            return
        before = checkpoint._stamp(path)
        path.chmod(stat.S_IREAD)
        after = checkpoint._stamp(path)
        assert after != before
        changed.append((before, after))

    monkeypatch.setattr(checkpoint, '_directory_sync', make_read_only_after_sync)
    try:
        with pytest.raises(OSError, match='Checkpoint installation changed across synchronization'):
            memory.save(path)
        assert changed and changed[0][0] != changed[0][1]
        assert memory._checkpoint_cache is None
        assert memory._checkpoint_metrics['status'] == 'failed'
    finally:
        if path.exists():
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_windows_deleted_installation_after_replace_is_reported_as_eio(tmp_path, monkeypatch):
    memory = CampaignMemory('windows-deleted-installation', 'rocket_launch')
    path = tmp_path / 'state.json'
    real_replace = os.replace
    removed = []

    def replace(source, target):
        real_replace(source, target)
        if Path(target) == path and not removed:
            path.unlink()
            removed.append(True)

    monkeypatch.setattr(checkpoint.os, 'replace', replace)
    with pytest.raises(OSError, match='Checkpoint installation changed before synchronization'):
        memory.save(path)
    assert removed and not path.exists()
    assert memory._checkpoint_cache is None
    assert memory._checkpoint_metrics['status'] == 'failed'
