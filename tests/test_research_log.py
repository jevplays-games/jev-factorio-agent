import hashlib
import itertools
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jev_factorio import research_log as rl


@pytest.fixture
def configuration():
    return rl.RunConfiguration(backend="mock", controller="hierarchical", policy="jev",
                               target="bootstrap_mining", mock_model=True, steps=8)


@pytest.fixture
def make_log(tmp_path, configuration):
    writers = []

    def create(name="run", **kwargs):
        options = {"environ": {}, "monotonic_ns": itertools.count().__next__,
                   "utc_now": lambda: datetime(2026, 9, 21, tzinfo=timezone.utc)}
        options.update(kwargs)
        writer = rl.ResearchLog(tmp_path / name, configuration, **options)
        writers.append(writer)
        return writer

    yield create
    for writer in writers:
        writer.close()


def records(writer):
    return [json.loads(line) for line in (writer.run_dir / "events.jsonl").read_bytes().splitlines()]


def put_records(writer, events):
    (writer.run_dir / "events.jsonl").write_bytes(
        b"".join(rl.canonical_bytes(event) + b"\n" for event in events)
    )


def test_manifest_event_and_seal_are_independently_hashable(make_log):
    with make_log() as writer:
        manifest_bytes = (writer.run_dir / "manifest.json").read_bytes()
        event = writer.emit("observation", {"inventory": {"coal": 5}}, factorio_tick=120,
                            session_id="world-1", correlation={"decision_id": "decision-1"})
        assert records(writer)[-1] == event  # Visible before close, not merely buffered.
        event["payload"]["inventory"]["coal"] = 999  # Does not alter persisted evidence.
    result = rl.verify_run(writer.run_dir)
    assert result["complete"] is True and result["outcome"] == "returned"
    assert result["event_count"] == 3
    previous = "sha256:" + hashlib.sha256(manifest_bytes.rstrip(b"\n")).hexdigest()
    for index, event in enumerate(records(writer), 1):
        assert event["sequence"] == index
        assert event["time"]["monotonic_ns"] == index - 1
        assert event["time"]["utc"] == "2026-09-21T00:00:00.000000Z"
        assert event["prev_hash"] == previous
        claimed = event.pop("event_hash")
        independent_bytes = json.dumps(event, sort_keys=True, ensure_ascii=True,
                                       allow_nan=False, separators=(",", ":")).encode("ascii")
        previous = "sha256:" + hashlib.sha256(independent_bytes).hexdigest()
        assert claimed == previous
    assert result["final_event_hash"] == previous
    assert (writer.run_dir / "manifest.json").read_bytes() == manifest_bytes
    rl.verify_run(writer.run_dir, expected_final_hash=previous)
    with pytest.raises(ValueError, match="Trusted"):
        rl.verify_run(writer.run_dir, expected_final_hash="sha256:" + "0" * 64)


def test_v1_canonical_encoding_is_explicit():
    assert rl.canonical_bytes({"z": "\u00e9", "a": [True, None, 1.25]}) == (
        b'{"a":[true,null,1.25],"z":"\\u00e9"}'
    )


def test_original_v1_configuration_without_treatment_fields_remains_valid(make_log):
    writer = make_log()
    manifest = json.loads((writer.run_dir / "manifest.json").read_bytes())
    for key in rl._TREATMENT_FIELDS:
        del manifest["configuration"][key]
    manifest["configuration"].pop("until_complete", None)
    manifest["configuration"].pop("reconcile_only", None)
    manifest["configuration"].pop("persistent_idle_observations", None)
    rl.validate_manifest(manifest)


def test_legacy_persistent_manifest_without_idle_bound_remains_unknown_and_valid(make_log):
    from dataclasses import asdict

    config = rl.RunConfiguration(
        backend="fle", controller="hierarchical", policy="jev", target="rocket_launch",
        steps=None, resume=True, resume_controller=True, checkpoint_enabled=True,
        until_complete=True, persist_recoverable_blocks=True,
        persistent_idle_observations=6)
    manifest = {
        "schema": rl.MANIFEST_SCHEMA, "schema_version": 1,
        "run_id": "9d28f127-6c53-4518-a8e3-a0b2d97cde15",
        "created_utc": "2026-09-30T00:00:00.000000Z",
        "configuration": asdict(config),
        "provenance": {
            "git": {"commit": "a" * 40, "dirty": False},
            "runtime": {"python": "3.12", "system": "Linux", "machine": "x86_64",
                        "packages": {name: None for name in rl._PACKAGES}},
            "provider_configuration": {
                "typesafe": False, "cloudflare": False, "factorio_rcon": False},
        },
        "durability": "file-and-directory-fsync",
    }
    manifest["configuration"].pop("persistent_idle_observations")

    rl.validate_manifest(manifest)
    assert rl.RunConfiguration(**manifest["configuration"]).persistent_idle_observations is None


@pytest.mark.parametrize("value", [True, -1, 1001, 6.0, "6", None])
def test_persistent_idle_bound_manifest_requires_a_valid_recorded_integer(value):
    from dataclasses import asdict

    config = rl.RunConfiguration(
        backend="fle", controller="hierarchical", policy="jev", target="rocket_launch",
        resume=True, resume_controller=True, checkpoint_enabled=True, until_complete=True,
        persist_recoverable_blocks=True, persistent_idle_observations=value)
    with pytest.raises(ValueError, match="Invalid persistent idle observation bound"):
        rl._configuration(asdict(config))


@pytest.mark.parametrize("value", [0, 1, 6, 1000])
def test_persistent_idle_bound_manifest_accepts_configured_range(value):
    from dataclasses import asdict

    config = rl.RunConfiguration(
        backend="fle", controller="hierarchical", policy="jev", target="rocket_launch",
        resume=True, resume_controller=True, checkpoint_enabled=True, until_complete=True,
        persist_recoverable_blocks=True, persistent_idle_observations=value)
    rl._configuration(asdict(config))


def test_idle_bound_cannot_be_recorded_as_active_when_persistence_is_disabled():
    from dataclasses import asdict

    config = rl.RunConfiguration(
        backend="mock", controller="hierarchical", policy="jev", mock_model=True,
        persistent_idle_observations=6)
    with pytest.raises(ValueError, match="requires persistent blocked recovery"):
        rl._configuration(asdict(config))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"),
                                    {1: "numeric key"}, (1, 2), {1, 2}, object()])
def test_non_json_values_never_become_evidence(make_log, value):
    writer = make_log()
    before = (writer.run_dir / "events.jsonl").read_bytes()
    with pytest.raises(ValueError):
        writer.emit("observation", {"value": value})
    assert (writer.run_dir / "events.jsonl").read_bytes() == before
    assert writer.emit("observation", {"value": None})["sequence"] == 2


@pytest.mark.parametrize("field,value", [
    ("schema", "jev-factorio.event.v99"), ("schema_version", True),
    ("schema_version", 2), ("run_id", "not-a-uuid"), ("sequence", True),
    ("sequence", 0), ("sequence", 1.5), ("event_type", "Invalid Event"),
    ("event_type", "x" * 65), ("payload", []), ("session_id", ""),
    ("correlation", {"unknown_id": "x"}), ("correlation", {"action_id": 1}),
    ("prev_hash", "not-a-hash"), ("event_hash", "not-a-hash"),
])
def test_event_schema_is_strict(make_log, field, value):
    event = records(make_log())[0]
    event[field] = value
    with pytest.raises(ValueError):
        rl.validate_event(event)


@pytest.mark.parametrize("field,value", [("utc", "2026-01-01"),
    ("utc", "2026-13-01T00:00:00.000000Z"), ("monotonic_ns", -1),
    ("monotonic_ns", True), ("factorio_tick", False), ("factorio_tick", -1)])
def test_clock_schema_is_strict(make_log, field, value):
    event = records(make_log())[0]
    event["time"][field] = value
    with pytest.raises(ValueError):
        rl.validate_event(event)


def test_missing_and_extra_fields_are_rejected(make_log):
    event = records(make_log())[0]
    event["extra"] = 1
    with pytest.raises(ValueError):
        rl.validate_event(event)
    del event["extra"]
    del event["payload"]
    with pytest.raises(ValueError):
        rl.validate_event(event)


@pytest.mark.parametrize("field,value", [
    ("steps", True), ("steps", -1), ("tick_seconds", float("nan")),
    ("confidence_floor", 2), ("duration_seconds", 0), ("resume", 1),
    ("backend", ""), ("requested_model", 123),
])
def test_configuration_rejected_before_directory_creation(tmp_path, configuration, field, value):
    path = tmp_path / "run"
    with pytest.raises(ValueError):
        rl.ResearchLog(path, replace(configuration, **{field: value}), environ={})
    assert not path.exists()


@pytest.mark.parametrize("updates", [
    {"until_complete": True, "controller": "flat"},
    {"until_complete": True, "steps": 1},
    {"until_complete": True, "reconcile_only": True},
    {"reconcile_only": True},
])
def test_unbounded_mode_configuration_is_validated_before_creation(tmp_path, configuration, updates):
    path = tmp_path / "run"
    with pytest.raises(ValueError):
        rl.ResearchLog(path, replace(configuration, **updates), environ={})
    assert not path.exists()


@pytest.mark.parametrize("execution", [
    {"until_complete": True, "steps": None, "duration_seconds": None},
    {"until_complete": False, "steps": 3, "duration_seconds": None},
    {"until_complete": False, "steps": None, "duration_seconds": 3600.0},
])
def test_blocked_re_evaluation_authorizes_one_decision_not_a_run_lifetime(
        tmp_path, configuration, execution):
    path = tmp_path / "run"
    selected = replace(
        configuration, backend="fle", controller="hierarchical", target="rocket_launch",
        factory_scheduling="ready-work", background_work=True, policy="jev",
        mock_model=False, resume=True, resume_controller=True, checkpoint_enabled=True,
        reevaluate_blocked_once=True, exact_checkpoint_sha256="a" * 64,
        blocked_source_revision="b" * 40, **execution)
    writer = rl.ResearchLog(path, selected, environ={})
    manifest = json.loads((path / "manifest.json").read_bytes())
    assert manifest["configuration"]["reevaluate_blocked_once"] is True
    assert manifest["configuration"]["until_complete"] is execution["until_complete"]
    assert manifest["configuration"]["steps"] == execution["steps"]
    assert manifest["configuration"]["duration_seconds"] == execution["duration_seconds"]
    writer.close()


def test_blocked_re_evaluation_cannot_be_recorded_as_reconcile_only(
        tmp_path, configuration):
    path = tmp_path / "run"
    selected = replace(
        configuration, backend="fle", controller="hierarchical", target="rocket_launch",
        factory_scheduling="ready-work", background_work=True, policy="jev",
        mock_model=False, resume=True, resume_controller=True, checkpoint_enabled=True,
        reevaluate_blocked_once=True, exact_checkpoint_sha256="a" * 64,
        blocked_source_revision="b" * 40, reconcile_only=True, steps=None)
    with pytest.raises(ValueError, match="Blocked decision re-evaluation"):
        rl.ResearchLog(path, selected, environ={})
    assert not path.exists()


def test_monotonic_regression_rejected_but_wall_clock_adjustment_allowed(make_log):
    clocks = iter([10, 9, 10, 11])
    utc = iter([datetime(2026, 9, 21, tzinfo=timezone.utc) - timedelta(seconds=i)
                for i in range(6)])
    writer = make_log(monotonic_ns=clocks.__next__, utc_now=utc.__next__)
    with pytest.raises(ValueError, match="regressed"):
        writer.emit("observation", {})
    writer.emit("observation", {})
    writer.finish()
    assert rl.verify_run(writer.run_dir)["complete"] is True
    assert records(writer)[1]["time"]["utc"] < records(writer)[0]["time"]["utc"]


def test_naive_clock_rejected_before_directory_creation(tmp_path, configuration):
    with pytest.raises(ValueError, match="timezone"):
        rl.ResearchLog(tmp_path / "run", configuration, environ={}, utc_now=lambda: datetime(2026, 1, 1))
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("existing_kind", ["empty", "file", "symlink"])
def test_never_reuses_existing_directory(tmp_path, configuration, existing_kind):
    path = tmp_path / "run"
    if existing_kind == "empty":
        path.mkdir()
    elif existing_kind == "file":
        path.write_text("preserve me")
    else:
        target = tmp_path / "target"
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        rl.ResearchLog(path, configuration, environ={})
    assert not (path / "manifest.json").exists()


def test_existing_run_is_not_modified(make_log, configuration):
    with make_log() as writer:
        pass
    before = {path.name: path.read_bytes() for path in writer.run_dir.iterdir()}
    with pytest.raises(FileExistsError):
        rl.ResearchLog(writer.run_dir, configuration, environ={})
    assert before == {path.name: path.read_bytes() for path in writer.run_dir.iterdir()}


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_private_file_modes_and_nested_parents(make_log):
    with make_log("nested/parents/run") as writer:
        pass
    assert writer.run_dir.stat().st_mode & 0o777 == 0o700
    for path in writer.run_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    assert rl.verify_run(writer.run_dir)["complete"] is True


def test_flush_happens_before_fsync(monkeypatch):
    calls = []

    class Stream:
        def write(self, value):
            calls.append("write")
            return len(value)

        def flush(self):
            calls.append("flush")

        def fileno(self):
            return 42

    monkeypatch.setattr(rl.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    rl._write_durable(Stream(), b"record\n")
    assert calls == ["write", "flush", ("fsync", 42)]


@pytest.mark.parametrize("failure_point", ["write", "flush", "fsync"])
def test_io_failure_poisons_writer_and_never_seals(make_log, monkeypatch, failure_point):
    writer = make_log()
    original = writer._stream

    def fail(*args):
        raise OSError("simulated disk failure")

    if failure_point == "fsync":
        monkeypatch.setattr(rl.os, "fsync", fail)
    else:
        class FailingStream:
            write = fail if failure_point == "write" else staticmethod(original.write)
            flush = fail if failure_point == "flush" else staticmethod(original.flush)
            fileno = staticmethod(original.fileno)
            close = staticmethod(original.close)
        writer._stream = FailingStream()
    with pytest.raises(OSError):
        writer.emit("observation", {"x": 1})
    with pytest.raises(RuntimeError, match="failed"):
        writer.emit("observation", {"x": 2})
    writer.close()
    assert not (writer.run_dir / "integrity.json").exists()
    with pytest.raises(ValueError):
        rl.verify_run(writer.run_dir)


def test_short_write_is_failure(monkeypatch):
    class ShortWrite:
        def write(self, value):
            return len(value) - 1
    with pytest.raises(OSError, match="Incomplete"):
        rl._write_durable(ShortWrite(), b"data")


def test_initial_fsync_failure_preserves_failed_directory(tmp_path, configuration, monkeypatch):
    monkeypatch.setattr(rl.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        rl.ResearchLog(tmp_path / "run", configuration, environ={})
    assert (tmp_path / "run").exists()
    assert not (tmp_path / "run" / "integrity.json").exists()


def test_seal_failure_is_not_reported_complete(make_log, monkeypatch):
    writer = make_log()
    original = rl._write_document

    def fail_seal(path, value):
        if path.name == "integrity.json":
            raise OSError("seal failed")
        original(path, value)

    monkeypatch.setattr(rl, "_write_document", fail_seal)
    with pytest.raises(OSError):
        writer.finish()
    assert rl.verify_run(writer.run_dir, allow_incomplete=True)["complete"] is False
    with pytest.raises(RuntimeError):
        writer.emit("observation", {})


def test_close_without_finish_leaves_explicit_incomplete_evidence(make_log):
    writer = make_log()
    writer.close()
    with pytest.raises(ValueError, match="incomplete"):
        rl.verify_run(writer.run_dir)
    result = rl.verify_run(writer.run_dir, allow_incomplete=True)
    assert result["complete"] is False and result["outcome"] is None


@pytest.mark.parametrize("corruption", ["edit", "drop_middle", "drop_tail", "reorder", "duplicate",
                                         "partial_tail", "blank_line", "whitespace", "duplicate_key",
                                         "manifest", "seal", "post_terminal", "schema"])
def test_verifier_detects_corruption(make_log, corruption):
    with make_log() as writer:
        writer.emit("observation", {"coal": 5})
        writer.emit("observation", {"coal": 10})
    events = records(writer)
    path = writer.run_dir / "events.jsonl"
    if corruption == "edit":
        events[1]["payload"]["coal"] = 99
    elif corruption == "drop_middle":
        events.pop(1)
    elif corruption == "drop_tail":
        events.pop()
    elif corruption == "reorder":
        events[1], events[2] = events[2], events[1]
    elif corruption == "duplicate":
        events.insert(1, events[1])
    elif corruption == "post_terminal":
        events.append(events[-1])
    elif corruption == "schema":
        events[1]["schema_version"] = 2
        events[1]["event_hash"] = rl.digest({k: v for k, v in events[1].items() if k != "event_hash"})
    elif corruption in {"partial_tail", "blank_line", "whitespace", "duplicate_key"}:
        data = path.read_bytes()
        if corruption == "partial_tail":
            data = data[:-7]
        elif corruption == "blank_line":
            data += b"\n"
        elif corruption == "whitespace":
            data = b" " + data
        else:
            data = data.replace(b'{"correlation":', b'{"sequence":1,"correlation":', 1)
        path.write_bytes(data)
    elif corruption == "manifest":
        manifest_path = writer.run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["configuration"]["steps"] = 999
        manifest_path.write_bytes(rl.canonical_bytes(manifest) + b"\n")
    elif corruption == "seal":
        seal_path = writer.run_dir / "integrity.json"
        seal = json.loads(seal_path.read_bytes())
        seal["event_count"] = 100
        seal_path.write_bytes(rl.canonical_bytes(seal) + b"\n")
    if corruption not in {"partial_tail", "blank_line", "whitespace", "duplicate_key", "manifest", "seal"}:
        put_records(writer, events)
    with pytest.raises(ValueError):
        rl.verify_run(writer.run_dir, allow_incomplete=True)


def test_unsealed_valid_prefix_not_misrepresented_as_complete(make_log):
    with make_log() as writer:
        writer.emit("observation", {})
    original = rl.verify_run(writer.run_dir)["final_event_hash"]
    (writer.run_dir / "integrity.json").unlink()
    put_records(writer, records(writer)[:-1])
    assert rl.verify_run(writer.run_dir, allow_incomplete=True)["complete"] is False
    with pytest.raises(ValueError):
        rl.verify_run(writer.run_dir, allow_incomplete=True, expected_final_hash=original)


def test_mixed_runs_rejected(make_log):
    with make_log("a") as first, make_log("b") as second:
        first.emit("observation", {})
        second.emit("observation", {})
    events = records(first)
    events[1] = records(second)[1]
    put_records(first, events)
    with pytest.raises(ValueError):
        rl.verify_run(first.run_dir)


def test_secrets_removed_before_hashing_and_input_unchanged(make_log):
    secrets = {"TYPESAFE_API_KEY": "typesafe-test-secret-123", "CLOUDFLARE_API_TOKEN": "cf-test-secret-456",
               "FACTORIO_RCON_PASSWORD": "rcon-test-secret-789", "OTHER_ACCESS_TOKEN": "other-secret-abc"}
    original = {"nested": [{"Authorization": "Bearer hidden-token", "password": "not-in-environment"}],
                "error": "provider echoed other-secret-abc and typesafe-test-secret-123",
                "url": "https://user:private@localhost/?key=secret",
                "request": "Bearer arbitrary-secret", "input_tokens": 123,
                "cf-test-secret-456": "rcon-test-secret-789"}
    with make_log(environ=secrets) as writer:
        event = writer.emit("provider_error", original)
    assert event["payload"]["input_tokens"] == 123
    assert original["nested"][0]["password"] == "not-in-environment"
    joined = b"".join(path.read_bytes() for path in writer.run_dir.iterdir()).decode()
    for secret in [*secrets.values(), "hidden-token", "not-in-environment", "arbitrary-secret", "user:private"]:
        assert secret not in joined
    assert rl.verify_run(writer.run_dir)["complete"] is True
    manifest = json.loads((writer.run_dir / "manifest.json").read_bytes())
    assert manifest["provenance"]["provider_configuration"] == {
        "typesafe": True, "cloudflare": True, "factorio_rcon": True,
    }
    assert "OTHER_ACCESS_TOKEN" not in joined


@pytest.mark.parametrize("field,value,secret_name", [
    ("backend", "mock", "PASSWORD"),
    ("controller", "flat", "PASSWORD"),
    ("policy", "jev", "API_KEY"),
])
def test_supported_structural_labels_survive_secret_redaction_and_verification(
        tmp_path, configuration, field, value, secret_name):
    selected = replace(configuration, **{field: value})
    run_dir = tmp_path / f"{field}-label"

    with rl.ResearchLog(run_dir, selected, environ={secret_name: value}) as writer:
        assert getattr(writer.configuration, field) == value
        writer.emit("configuration_probe", {"field": field})

    manifest = json.loads((run_dir / "manifest.json").read_bytes())
    rl.validate_manifest(manifest)
    consumed_configuration = rl.RunConfiguration(**manifest["configuration"])
    assert getattr(consumed_configuration, field) == value
    assert rl.verify_run(run_dir)["complete"] is True


def test_public_structural_label_does_not_exempt_freeform_model_from_redaction(
        tmp_path, configuration):
    selected = replace(configuration, policy="jev", requested_model="jev")
    run_dir = tmp_path / "freeform-model"

    with rl.ResearchLog(run_dir, selected, environ={"PASSWORD": "jev"}) as writer:
        assert writer.configuration.policy == "jev"
        assert writer.configuration.requested_model == rl.REDACTED
        writer.emit("configuration_probe", {"requested_model": "jev"})

    manifest = json.loads((run_dir / "manifest.json").read_bytes())
    rl.validate_manifest(manifest)
    assert manifest["configuration"]["policy"] == "jev"
    assert manifest["configuration"]["requested_model"] == rl.REDACTED
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_bytes().splitlines()]
    assert events[1]["payload"]["requested_model"] == rl.REDACTED
    assert rl.verify_run(run_dir)["complete"] is True


@pytest.mark.parametrize("secret", ["x", "run", "custom-label-secret-123"])
def test_unsupported_structural_label_and_model_remain_redacted(
        tmp_path, configuration, secret):
    selected = replace(configuration, backend=secret, requested_model=secret)
    run_dir = tmp_path / "unsupported-label"

    with rl.ResearchLog(run_dir, selected, environ={"PASSWORD": secret}) as writer:
        assert writer.configuration.backend == rl.REDACTED
        assert writer.configuration.requested_model == rl.REDACTED

    manifest = json.loads((run_dir / "manifest.json").read_bytes())
    rl.validate_manifest(manifest)
    assert manifest["configuration"]["backend"] == rl.REDACTED
    assert manifest["configuration"]["requested_model"] == rl.REDACTED
    assert rl.verify_run(run_dir)["complete"] is True


def test_redacted_key_collisions_are_not_silently_lost(make_log):
    writer = make_log(environ={"API_KEY": "sensitive-value"})
    with pytest.raises(ValueError, match="duplicate"):
        writer.emit("observation", {"sensitive-value": 1, rl.REDACTED: 2})
    assert len(records(writer)) == 1


@pytest.mark.parametrize("exception,outcome", [(RuntimeError("do-not-log-this"), "error"),
    (KeyboardInterrupt("do-not-log-this"), "interrupted"), (SystemExit(2), "interrupted")])
def test_exception_lifecycle_is_sealed_but_not_labeled_success(make_log, exception, outcome):
    writer = make_log()
    with pytest.raises(type(exception)):
        with writer:
            raise exception
    assert rl.verify_run(writer.run_dir)["outcome"] == outcome
    assert "do-not-log-this" not in (writer.run_dir / "events.jsonl").read_text()
    assert records(writer)[-1]["payload"]["error_type"] == type(exception).__name__


def test_threads_get_unique_ordered_sequences(make_log):
    with make_log() as writer:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: writer.emit("observation", {"index": i}), range(24)))
    assert rl.verify_run(writer.run_dir)["event_count"] == 26


def test_forked_process_cannot_share_writer(make_log, monkeypatch):
    writer = make_log()
    monkeypatch.setattr(rl.os, "getpid", lambda: writer._owner_pid + 1)
    with pytest.raises(RuntimeError, match="processes"):
        writer.emit("observation", {})


def test_lifecycle_cannot_be_injected_and_sealed_writer_rejects_append(make_log):
    writer = make_log()
    for kind in ("run_started", "run_finished"):
        with pytest.raises(ValueError):
            writer.emit(kind, {})
    writer.finish()
    with pytest.raises(RuntimeError):
        writer.emit("observation", {})


def test_size_limits_on_writer_and_verifier(make_log, monkeypatch):
    writer = make_log()
    monkeypatch.setattr(rl, "MAX_RECORD_BYTES", 1024)
    with pytest.raises(ValueError, match="size"):
        writer.emit("observation", {"too_big": "x" * 1024})
    writer.close()
    (writer.run_dir / "events.jsonl").write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="size"):
        rl.verify_run(writer.run_dir, allow_incomplete=True)


def test_provenance_allowlist_and_real_git_dirty_flag(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "file.txt").write_text("fixture")
    monkeypatch.setattr(rl, "__file__", str(repo / "file.txt"))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    env = {"TYPESAFE_API_KEY": "should-not-appear", "HOME": "/private/user", "UNLISTED_SETTING": "private"}
    clean = rl.collect_provenance(repo, env)
    assert clean["git"]["dirty"] is False
    assert len(clean["git"]["commit"]) == 40
    (repo / "untracked-secret-name").write_text("private")
    dirty = rl.collect_provenance(repo, env)
    assert dirty["git"]["dirty"] is True
    encoded = json.dumps(dirty)
    assert all(secret not in encoded for secret in ("should-not-appear", "/private/user", "untracked-secret-name"))
    assert set(dirty["runtime"]["packages"]) == set(rl._PACKAGES)


def test_untracked_installed_copy_cannot_claim_enclosing_git_provenance(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "source.py").write_text("source")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    installed = repo / ".venv/lib/python/site-packages/jev_factorio/research_log.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("installed copy")
    monkeypatch.setattr(rl, "__file__", str(installed))
    assert rl.collect_provenance(installed.parent, {})["git"] == {"commit": None, "dirty": None}


@pytest.mark.parametrize("secret", ["a", "0", "run", "run_started", "serial"])
def test_secret_values_do_not_corrupt_structural_evidence(make_log, secret):
    with make_log(environ={"PASSWORD": secret}) as writer:
        writer.emit("observation", {"password": secret, "value": secret})
    assert rl.verify_run(writer.run_dir)["complete"] is True
    manifest = json.loads((writer.run_dir / "manifest.json").read_bytes())
    rl.validate_manifest(manifest)
    assert records(writer)[1]["payload"]["value"] == rl.REDACTED


def test_causal_payload_exports_and_envelope_promotion(make_log):
    original = {"session_id": "world", "factorio_tick": 120, "decision_id": "decision:1",
                "action_id": None, "trace_id": "trace", "observation_id": "observation:1",
                "answers": [{"confidence": float("nan"), "api_token": "private"}]}
    captured = rl.safe_payload(original)
    assert captured["answers"][0] == {"confidence": {"invalid_numeric": "nan"},
                                      "api_token": rl.REDACTED}
    assert original["answers"][0]["api_token"] == "private"
    with make_log() as writer:
        event = writer.emit("observation", captured)
        assert event["time"]["factorio_tick"] == 120
        assert event["session_id"] == "world"
        assert event["correlation"] == {"decision_id": "decision:1"}
        assert event["payload"]["observation_id"] == "observation:1"
    assert rl.verify_run(writer.run_dir)["complete"] is True


@pytest.mark.parametrize("name", ["EVENTS.JSONL", "Manifest.Json", "integrity.JSON/child", "."])
def test_direct_sink_artifact_aliases_rejected(make_log, name):
    writer = make_log()
    with pytest.raises(ValueError, match="Output paths"):
        rl.validate_output_paths(writer, writer.run_dir / name)
    rl.validate_output_paths(writer, writer.run_dir / "decisions.jsonl", None)


def test_direct_sink_hardlink_alias_rejected(make_log, tmp_path):
    writer = make_log()
    alias = tmp_path / "legacy.jsonl"
    os.link(writer.run_dir / "events.jsonl", alias)
    with pytest.raises(ValueError, match="Output paths"):
        rl.validate_output_paths(writer, alias)


def test_missing_git_and_packages_stay_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(rl.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(rl.metadata, "version", lambda name: (_ for _ in ()).throw(rl.metadata.PackageNotFoundError()))
    provenance = rl.collect_provenance(tmp_path, {})
    assert provenance["git"] == {"commit": None, "dirty": None}
    assert all(value is None for value in provenance["runtime"]["packages"].values())


def test_verifier_cli_returns_status_without_private_details(make_log, monkeypatch, capsys):
    with make_log() as writer:
        pass
    monkeypatch.setattr(sys, "argv", ["verify", str(writer.run_dir)])
    rl.cli()
    assert json.loads(capsys.readouterr().out)["complete"] is True
    (writer.run_dir / "events.jsonl").write_bytes(b"private credential junk")
    with pytest.raises(SystemExit) as error:
        rl.cli()
    assert error.value.code == 1
    assert "credential" not in capsys.readouterr().err
