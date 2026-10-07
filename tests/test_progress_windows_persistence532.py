"""Cross-platform persistence contract for the public progress monitor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jev_factorio import progress_watch


def test_atomic_json_writes_replaced_file_and_removes_temp(tmp_path):
    target = tmp_path / "state" / "status.json"
    target.parent.mkdir()
    target.write_text('{"old":true}\n', encoding="utf-8")
    payload = {"status": "completed", "session_id": "campaign"}

    progress_watch.atomic_json(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert not target.with_name(target.name + ".tmp").exists()


def test_run_once_persists_fresh_and_historical_completion_samples(tmp_path):
    fresh_probe = [sys.executable, "-c", (
        "import json,time; n=time.time(); "
        "print(json.dumps({'session_id':'campaign','at':n,'progress_tick':7,"
        "'last_progress_at':n,'checkpoint_status':'running','owner_phase':'running',"
        "'owner_alive':True,'child_alive':True,'pending':False}))"
    )]
    first = progress_watch.run_once(fresh_probe, None, tmp_path, "campaign")
    saved_first = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert first["status"] == "progressing"
    assert saved_first == first
    assert len((tmp_path / "transitions.jsonl").read_text(encoding="utf-8").splitlines()) == 1

    historical_completion_probe = [sys.executable, "-c", (
        "import json,time; n=time.time(); "
        "print(json.dumps({'session_id':'campaign','at':n,'progress_tick':8,"
        "'last_progress_at':n-300,'checkpoint_status':'completed','owner_phase':'completed',"
        "'owner_alive':True,'child_alive':False,'pending':False}))"
    )]
    second = progress_watch.run_once(
        historical_completion_probe, None, tmp_path, "campaign"
    )
    saved_second = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    transitions = (tmp_path / "transitions.jsonl").read_text(encoding="utf-8").splitlines()
    assert second["status"] == "completed"
    assert second["progress_age_seconds"] >= 299
    assert saved_second == second
    assert len(transitions) == 2
    assert json.loads(transitions[-1])["status"] == "completed"


def test_json_write_error_propagates_without_replacing_existing_status(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    target.write_text('{"status":"old"}\n', encoding="utf-8")

    def fail_dump(*_args, **_kwargs):
        raise OSError("injected JSON write failure")

    monkeypatch.setattr(progress_watch.json, "dump", fail_dump)
    with pytest.raises(OSError, match="injected JSON write failure"):
        progress_watch.atomic_json(target, {"status": "new"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "old"}


def test_stream_flush_error_propagates_without_replacing_existing_status(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    target.write_text('{"status":"old"}\n', encoding="utf-8")
    original_fdopen = progress_watch.os.fdopen

    class FlushFailure:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self.stream.__exit__(exc_type, exc, traceback)

        def write(self, value):
            return self.stream.write(value)

        def flush(self):
            raise OSError("injected stream flush failure")

        def fileno(self):
            return self.stream.fileno()

    monkeypatch.setattr(
        progress_watch.os, "fdopen",
        lambda fd, *args, **kwargs: FlushFailure(original_fdopen(fd, *args, **kwargs)),
    )
    with pytest.raises(OSError, match="injected stream flush failure"):
        progress_watch.atomic_json(target, {"status": "new"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "old"}


def test_file_fsync_error_propagates_without_replacing_existing_status(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    target.write_text('{"status":"old"}\n', encoding="utf-8")

    def fail_fsync(_fd):
        raise OSError("injected file fsync failure")

    monkeypatch.setattr(progress_watch.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="injected file fsync failure"):
        progress_watch.atomic_json(target, {"status": "new"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "old"}


def test_replace_error_propagates_without_replacing_existing_status(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    target.write_text('{"status":"old"}\n', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(progress_watch.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected atomic replace failure"):
        progress_watch.atomic_json(target, {"status": "new"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "old"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync is not exposed on Windows")
def test_posix_directory_fsync_error_propagates_after_replacement(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    target.write_text('{"status":"old"}\n', encoding="utf-8")
    original_fsync = progress_watch.os.fsync
    calls = []

    def fail_directory_fsync(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(progress_watch.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        progress_watch.atomic_json(target, {"status": "new"})

    assert len(calls) == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "new"}


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific persistence contract")
def test_windows_atomic_json_flushes_file_and_skips_unavailable_directory_sync(
    tmp_path, monkeypatch
):
    target = tmp_path / "status.json"
    opened = []
    fsynced = []
    original_open = progress_watch.os.open
    original_fsync = progress_watch.os.fsync

    def record_open(path, flags, *args, **kwargs):
        opened.append(Path(path))
        return original_open(path, flags, *args, **kwargs)

    def record_fsync(fd):
        fsynced.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(progress_watch.os, "open", record_open)
    monkeypatch.setattr(progress_watch.os, "fsync", record_fsync)
    progress_watch.atomic_json(target, {"status": "progressing"})

    assert opened == [target.with_name(target.name + ".tmp")]
    assert len(fsynced) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "progressing"}
