"""Integration against real repository controllers, with explicit offline backends."""
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
import requests

from jev_factorio import main
from jev_factorio import research_log as rl
from jev_factorio.backends.mock import MockBackend
from jev_factorio.memory import CampaignMemory


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                "JEV_RUN_DIR", "JEV_LOG_FILE", "JEV_DASHBOARD_EVENTS",
                "JEV_TICK_SECONDS", "JEV_CONFIDENCE_FLOOR", "JEV_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("Unexpected provider call"))


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["jev-factorio", *map(str, arguments)])
    main.cli()


class CountingBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.session_id = "mock:fixed-test-world"
        self.observations = 0
        self.actions = []

    def observe(self):
        self.observations += 1
        return super().observe()

    def act(self, action):
        self.actions.append(action)
        return super().act(action)


@pytest.mark.parametrize("controller", ["flat", "hierarchical"])
def test_research_logging_preserves_gameplay_and_legacy_records(tmp_path, monkeypatch, controller):
    worlds = []
    logs = []
    for enabled in (False, True):
        backend = CountingBackend()
        worlds.append(backend)
        monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
        legacy = tmp_path / f"legacy-{enabled}.jsonl"
        logs.append(legacy)
        arguments = ["--backend", "mock", "--controller", controller,
                     "--steps", "40", "--tick-seconds", "0", "--log-file", str(legacy)]
        if controller == "hierarchical":
            arguments += ["--mock-model", "--target", "bootstrap_mining"]
        if enabled:
            arguments += ["--run-dir", str(tmp_path / "research")]
        invoke(monkeypatch, *arguments)
    assert worlds[0].actions == worlds[1].actions
    assert worlds[0].observations == worlds[1].observations
    assert worlds[0].observe() == worlds[1].observe()
    old, new = [[json.loads(line) for line in path.read_text().splitlines()] for path in logs]
    assert len(old) == len(new)
    for first, second in zip(old, new):
        assert set(first) == set(second)
        for key in ("action", "outcome", "state", "after_state", "status", "verified", "completed_goals"):
            assert first.get(key) == second.get(key)
    if controller == "flat":
        assert logs[0].read_bytes() == logs[1].read_bytes()
    else:
        from jev_factorio.evaluation import summarize
        assert summarize(logs[1])["evidence_class"] == "synthetic"
    evidence = [json.loads(line) for line in (tmp_path / "research/events.jsonl").read_text().splitlines()]
    assert rl.verify_run(tmp_path / "research")["event_count"] == len(evidence)
    assert sum(event["event_type"] == "step_finished" for event in evidence) == len(new)


@pytest.mark.parametrize("with_legacy", [False, True])
def test_new_run_directory_and_optional_legacy_file(tmp_path, monkeypatch, with_legacy):
    run = tmp_path / "run with spaces"
    arguments = ["--backend", "mock", "--steps", "2", "--tick-seconds", "0", "--run-dir", run]
    if with_legacy:
        arguments += ["--log-file", run / "decisions.jsonl"]
    invoke(monkeypatch, *arguments)
    assert (run / "decisions.jsonl").exists() is with_legacy
    result = rl.verify_run(run)
    assert result["complete"] is True and result["outcome"] == "returned"
    event_types = [json.loads(line)["event_type"] for line in (run / "events.jsonl").read_text().splitlines()]
    assert event_types[:2] == ["run_started", "controller_initialized"]
    assert event_types[-2:] == ["controller_stopped", "run_finished"]
    assert event_types.count("step_started") == event_types.count("step_finished") == 2
    manifest = json.loads((run / "manifest.json").read_bytes())
    assert manifest["configuration"]["legacy_log_enabled"] is with_legacy
    assert manifest["configuration"]["steps"] == 2
    assert manifest["configuration"]["persistent_idle_observations"] is None
    assert str(tmp_path) not in json.dumps(manifest)


def test_logging_remains_opt_in(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Research logger initialized when disabled")
    monkeypatch.setattr(main, "ResearchLog", unexpected)
    invoke(monkeypatch, "--backend", "mock", "--steps", "0")
    assert list(tmp_path.iterdir()) == []


def test_environment_and_explicit_cli_precedence(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"JEV_RUN_DIR={tmp_path / 'dotenv-run'}\n")
    monkeypatch.setenv("JEV_RUN_DIR", str(tmp_path / "exported-run"))
    invoke(monkeypatch, "--backend", "mock", "--steps", "0", "--run-dir", tmp_path / "cli-run")
    assert rl.verify_run(tmp_path / "cli-run")["complete"] is True
    assert not (tmp_path / "exported-run").exists()
    invoke(monkeypatch, "--backend", "mock", "--steps", "0")
    assert rl.verify_run(tmp_path / "exported-run")["complete"] is True
    monkeypatch.delenv("JEV_RUN_DIR")
    invoke(monkeypatch, "--backend", "mock", "--steps", "0")
    assert rl.verify_run(tmp_path / "dotenv-run")["complete"] is True


@pytest.mark.parametrize("failure", ["existing", "fsync"])
def test_logging_startup_failure_precedes_backend_initialization(tmp_path, monkeypatch, failure):
    run = tmp_path / "run"
    if failure == "existing":
        run.mkdir()
    else:
        monkeypatch.setattr(rl.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "mock", "--steps", "0", "--run-dir", run)
    assert error.value.code == 2


@pytest.mark.parametrize("reserved", ["manifest.json", "events.jsonl", "integrity.json",
                                      "events.jsonl/child", "MANIFEST.JSON", "EVENTS.JSONL",
                                      "INTEGRITY.JSON", "EVENTS.JSONL/child", "."])
@pytest.mark.parametrize("argument", ["--log-file", "--checkpoint", "--dashboard-events"])
def test_artifact_aliases_rejected_before_backend(tmp_path, monkeypatch, reserved, argument):
    run = tmp_path / "run"
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend started"))
    arguments = ["--backend", "mock", "--steps", "0", "--run-dir", run, argument, run / reserved]
    if argument in {"--checkpoint", "--dashboard-events"}:
        arguments += ["--controller", "hierarchical", "--mock-model"]
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *arguments)
    assert error.value.code == 2
    assert not run.exists()


def test_symlink_alias_to_internal_artifact_is_rejected(tmp_path, monkeypatch):
    run = tmp_path / "run"
    alias = tmp_path / "alias"
    alias.symlink_to(run / "events.jsonl")
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "mock", "--steps", "0", "--run-dir", run, "--log-file", alias)
    assert error.value.code == 2
    assert not run.exists()


def test_invalid_cli_creates_no_research_artifacts(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "--run-dir", tmp_path / "run", "--steps", "-1")
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("invalid", [
    ["--backend", "unknown"],
    ["--resume-controller"],
    ["--mock-model", "--policy", "deterministic"],
    ["--background-work"],
    ["--furnace-output-buffers"],
    ["--furnace-input-belts"],
    ["--max-request-bytes", "100"],
    ["--max-request-bytes", "99999999"],
    ["--persistent-idle-observations", "-1"],
    ["--persistent-idle-observations", "1001"],
])
def test_invalid_cli_creates_no_dashboard_or_research(tmp_path, monkeypatch, invalid):
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--controller", "hierarchical", "--run-dir", tmp_path / "run",
               "--dashboard-events", tmp_path / "dashboard.jsonl", *invalid)
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_dashboard_can_live_in_research_directory(tmp_path, monkeypatch):
    run = tmp_path / "run"
    invoke(monkeypatch, "--controller", "hierarchical", "--mock-model", "--steps", "1",
           "--run-dir", run, "--dashboard-events", run / "dashboard.jsonl")
    assert (run / "dashboard.jsonl").is_file()
    assert rl.verify_run(run)["complete"] is True


def test_opt_in_modes_are_recorded_in_manifest(tmp_path, monkeypatch):
    from jev_factorio.background import BackgroundWorkLoop

    captured = {}

    def initialize(self, backend, jev=None, **options):
        captured.update(options)

    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: MockBackend())
    monkeypatch.setattr(BackgroundWorkLoop, "__init__", initialize)
    monkeypatch.setattr(BackgroundWorkLoop, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr("jev_factorio.buffer_controller.buffered_loop_type", lambda base: base)
    monkeypatch.setattr("jev_factorio.input_controller.input_loop_type", lambda base: base)
    invoke(monkeypatch, "--backend", "fle", "--controller", "hierarchical",
           "--policy", "deterministic", "--checkpoint", tmp_path / "checkpoint.json",
           "--tick-seconds", "1", "--steps", "0", "--run-dir", tmp_path / "run",
           "--factory-scheduling", "ready-work", "--background-work",
           "--furnace-output-buffers", "--furnace-input-belts")
    configuration = json.loads((tmp_path / "run" / "manifest.json").read_text())["configuration"]
    assert configuration["factory_scheduling"] == captured["factory_scheduling"] == "ready-work"
    assert configuration["background_work"] is True
    assert configuration["furnace_output_buffers"] is True
    assert configuration["furnace_input_belts"] is True


def test_backend_failure_closes_dashboard_and_research(tmp_path, monkeypatch):
    from jev_factorio.dashboard import EventWriter

    writers = []
    original = EventWriter.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        writers.append(self)

    def fail_backend(*args, **kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(EventWriter, "__init__", capture)
    monkeypatch.setattr(main, "make_backend", fail_backend)
    with pytest.raises(RuntimeError):
        invoke(monkeypatch, "--controller", "hierarchical", "--mock-model", "--steps", "1",
               "--run-dir", tmp_path / "run", "--dashboard-events", tmp_path / "dashboard.jsonl")
    assert rl.verify_run(tmp_path / "run")["outcome"] == "error"
    import os
    with pytest.raises((OSError, TypeError)):
        os.fstat(writers[0].fd)


def test_duration_configuration_without_waiting(tmp_path, monkeypatch):
    captured = {}

    class Loop:
        def __init__(self, backend, **options):
            pass
        def run(self, **limits):
            captured.update(limits)

    monkeypatch.setattr(main, "AgentLoop", Loop)
    invoke(monkeypatch, "--backend", "mock", "--duration-hours", "0.01", "--run-dir", tmp_path / "run")
    assert captured == {"steps": None, "duration_seconds": 36.0}
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert manifest["configuration"]["steps"] is None
    assert manifest["configuration"]["duration_seconds"] == 36.0


def test_until_complete_cli_records_unbounded_hierarchical_mode(tmp_path, monkeypatch):
    captured = {}

    class Loop:
        def __init__(self, backend, **options):
            pass

        def run(self, **limits):
            captured.update(limits)

    monkeypatch.setattr("jev_factorio.controller.HierarchicalLoop", Loop)
    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: MockBackend())
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--policy", "deterministic", "--until-complete", "--run-dir", tmp_path / "run")
    assert captured == {"until_complete": True}
    configuration = json.loads((tmp_path / "run" / "manifest.json").read_text())["configuration"]
    assert configuration["until_complete"] is True
    assert configuration["reconcile_only"] is False
    assert configuration["steps"] is None and configuration["duration_seconds"] is None


@pytest.mark.parametrize(("limit_args", "expected"), [
    ([], 0),
    (["--persistent-idle-observations", "3"], 3),
    (["--persistent-idle-observations", "0"], 0),
])
def test_persistent_idle_bound_effective_value_is_forwarded_and_recorded(
        tmp_path, monkeypatch, limit_args, expected):
    from jev_factorio import operational_safety, provenance
    from jev_factorio.jev_client import MockJevClient

    checkpoint = tmp_path / "checkpoint.json"
    checkpoint_memory = CampaignMemory(
        "fle:idle-bound-config-test", "rocket_launch", status="running")
    checkpoint_memory.save(checkpoint)
    checkpoint_capture = checkpoint.read_bytes()
    restored = CampaignMemory.load(
        checkpoint, checkpoint_memory.session_id, checkpoint_memory.target)
    assert restored == checkpoint_memory
    monkeypatch.setattr(provenance, "gameplay_context", lambda: {
        "code_revision": {"commit": "a" * 40, "source_sha256": "b" * 64}})
    monkeypatch.setattr(operational_safety, "storage_ready", lambda _roots: True)
    monkeypatch.setattr(main, "make_backend", lambda *_args, **_kwargs: MockBackend())
    monkeypatch.setattr("jev_factorio.jev_client.make_client",
                        lambda **_kwargs: MockJevClient())
    captured = {}

    class Loop:
        memory_type = CampaignMemory

        def __init__(self, _backend, **options):
            captured["controller_idle_limit"] = options.get("persistent_idle_observations")
            self.jev = options.get("jev")

        def run(self, **limits):
            captured["run"] = limits

    monkeypatch.setattr("jev_factorio.controller.HierarchicalLoop", Loop)
    run_dir = tmp_path / "run"
    invoke(monkeypatch, "--backend", "fle", "--controller", "hierarchical",
           "--policy", "jev", "--resume", "--resume-controller",
           "--checkpoint", checkpoint, "--tick-seconds", "1", "--until-complete",
           "--persist-recoverable-blocks", "--run-dir", run_dir, *limit_args)

    assert captured["run"] == {"until_complete": True}
    assert captured["controller_idle_limit"] == expected
    assert checkpoint.read_bytes() == checkpoint_capture
    manifest = json.loads((run_dir / "manifest.json").read_bytes())
    assert manifest["configuration"]["persist_recoverable_blocks"] is True
    assert manifest["configuration"]["persistent_idle_observations"] == expected
    assert rl.verify_run(run_dir)["complete"] is True


def test_idle_bound_cli_option_requires_persistent_mode_before_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: pytest.fail("backend started"))
    run_dir = tmp_path / "run"
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "mock", "--steps", "0", "--run-dir", run_dir,
               "--persistent-idle-observations", "0")
    assert error.value.code == 2
    assert not run_dir.exists()


def test_blocked_re_evaluation_cli_continues_in_requested_until_complete_mode(
        tmp_path, monkeypatch):
    from jev_factorio import blocked_reevaluation, operational_safety, provenance

    checkpoint = tmp_path / "checkpoint.json"
    blocked = CampaignMemory(
        "fle:blocked-test", "rocket_launch", active_goal="stockpile_fuel", last_tick=0,
        status="blocked", reason="Candidate evidence insufficient", stalled_decisions=4)
    checkpoint.write_text(json.dumps(asdict(blocked), sort_keys=True), encoding="utf-8")
    checkpoint_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    old_source = "1" * 40
    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision", lambda _revision: {
        "blocked_source_revision": old_source, "source_head": "2" * 40,
        "previous_contract_sha256": "a" * 64, "decision_contract_sha256": "b" * 64,
    })
    monkeypatch.setattr(provenance, "gameplay_context", lambda: {})
    monkeypatch.setattr(operational_safety, "storage_ready", lambda _roots: True)
    monkeypatch.setattr("jev_factorio.jev_client.make_client", lambda **_kwargs: object())
    monkeypatch.setattr(main, "make_backend", lambda *_args, **_kwargs: MockBackend())
    calls = {"constructor": None, "run": None}

    class Loop:
        memory_type = CampaignMemory

        def __init__(self, _backend, **options):
            calls["constructor"] = options

        def run(self, **limits):
            calls["run"] = limits

    monkeypatch.setattr("jev_factorio.controller.HierarchicalLoop", Loop)
    invoke(monkeypatch, "--backend", "fle", "--controller", "hierarchical", "--policy", "jev",
           "--resume", "--resume-controller", "--checkpoint", checkpoint,
           "--tick-seconds", "1", "--until-complete", "--reevaluate-blocked-once",
           "--exact-checkpoint-sha256", checkpoint_sha,
           "--blocked-source-revision", old_source, "--run-dir", tmp_path / "run")

    assert calls["constructor"]["reevaluate_blocked_once"] is True
    assert calls["constructor"]["exact_checkpoint_sha256"] == checkpoint_sha
    assert calls["run"] == {"until_complete": True}
    configuration = json.loads((tmp_path / "run" / "manifest.json").read_bytes())["configuration"]
    assert configuration["reevaluate_blocked_once"] is True
    assert configuration["until_complete"] is True
    assert configuration["steps"] is None and configuration["duration_seconds"] is None


def test_blocked_re_evaluation_cannot_route_through_reconcile_only(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "make_backend", lambda *_args, **_kwargs: pytest.fail("backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "fle", "--controller", "hierarchical", "--policy", "jev",
               "--resume", "--resume-controller", "--checkpoint", checkpoint,
               "--tick-seconds", "1", "--target", "rocket_launch", "--factory-scheduling",
               "ready-work", "--background-work", "--reconcile-only",
               "--reevaluate-blocked-once", "--exact-checkpoint-sha256", "a" * 64,
               "--blocked-source-revision", "b" * 40)
    assert error.value.code == 2


@pytest.mark.parametrize("arguments", [
    ["--until-complete", "--steps", "1"],
    ["--until-complete", "--duration-hours", "1"],
    ["--reconcile-only", "--steps", "1"],
    ["--reconcile-only", "--duration-hours", "1"],
    ["--until-complete", "--reconcile-only"],
])
def test_unbounded_modes_reject_conflicting_cli_limits_before_backend(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: pytest.fail("backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
               "--policy", "deterministic", "--run-dir", tmp_path / "run", *arguments)
    assert error.value.code == 2
    assert not (tmp_path / "run").exists()


def test_until_complete_rejects_flat_controller_before_backend(monkeypatch):
    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: pytest.fail("backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "mock", "--until-complete")
    assert error.value.code == 2


@pytest.mark.parametrize("arguments", [
    ["--reconcile-only"],
    ["--reconcile-only", "--backend", "fle"],
    ["--reconcile-only", "--backend", "fle", "--resume"],
])
def test_reconcile_only_requires_full_native_resume_preflight(monkeypatch, arguments):
    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: pytest.fail("backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--controller", "hierarchical", "--policy", "deterministic", *arguments)
    assert error.value.code == 2


def test_reconcile_only_cli_observes_without_running_controller(tmp_path, monkeypatch, capsys):
    from jev_factorio.background import BackgroundMemory, BackgroundWorkLoop
    from jev_factorio import operational_safety

    checkpoint = tmp_path / "checkpoint.json"
    checkpoint_memory = BackgroundMemory(
        "fle:reconcile-only-test", "rocket_launch", status="running")
    checkpoint_memory.save(checkpoint)
    checkpoint_capture = checkpoint.read_bytes()
    restored = BackgroundMemory.load(
        checkpoint, checkpoint_memory.session_id, checkpoint_memory.target)
    assert restored == checkpoint_memory
    calls = []

    def initialize(self, backend, jev=None, **options):
        calls.append(("initialize", options))

    def reconcile(self):
        calls.append(("reconcile", None))
        return {"status": "running", "tick": 42, "background_state": "verified_completed",
                "verified_attempt_added": True}

    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: MockBackend())
    monkeypatch.setattr(operational_safety, "storage_ready", lambda roots: True)
    monkeypatch.setattr(BackgroundWorkLoop, "__init__", initialize)
    monkeypatch.setattr(BackgroundWorkLoop, "reconcile_only", reconcile)
    monkeypatch.setattr(BackgroundWorkLoop, "run", lambda *args, **kwargs: pytest.fail("run called"))
    invoke(monkeypatch, "--backend", "fle", "--controller", "hierarchical",
           "--resume", "--resume-controller", "--checkpoint", checkpoint, "--tick-seconds", "1",
           "--factory-scheduling", "ready-work", "--background-work", "--reconcile-only",
           "--run-dir", tmp_path / "run")
    assert [call[0] for call in calls] == ["initialize", "reconcile"]
    assert checkpoint.read_bytes() == checkpoint_capture
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed == {"reconciliation": {
        "status": "running", "tick": 42, "background_state": "verified_completed",
        "verified_attempt_added": True,
    }}
    configuration = json.loads((tmp_path / "run" / "manifest.json").read_text())["configuration"]
    assert configuration["reconcile_only"] is True
    assert configuration["until_complete"] is False
    assert configuration["steps"] is None and configuration["duration_seconds"] is None


@pytest.mark.parametrize("exception,outcome", [(RuntimeError("unlogged-private-detail"), "error"),
                                                (KeyboardInterrupt(), "interrupted")])
def test_controller_exception_is_recorded_without_raw_message(tmp_path, monkeypatch, exception, outcome):
    class Loop:
        def __init__(self, backend, **options):
            pass
        def run(self, **limits):
            raise exception

    monkeypatch.setattr(main, "AgentLoop", Loop)
    with pytest.raises(type(exception)):
        invoke(monkeypatch, "--backend", "mock", "--steps", "1", "--run-dir", tmp_path / "run")
    assert rl.verify_run(tmp_path / "run")["outcome"] == outcome
    assert "unlogged-private-detail" not in (tmp_path / "run" / "events.jsonl").read_text()


def test_event_write_failure_stops_before_gameplay_steps(tmp_path, monkeypatch):
    backend = CountingBackend()
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    original = rl._write_durable

    def fail_initialization_event(stream, data):
        if b'"event_type":"controller_initialized"' in data:
            raise OSError("disk failure")
        original(stream, data)

    monkeypatch.setattr(rl, "_write_durable", fail_initialization_event)
    with pytest.raises(OSError):
        invoke(monkeypatch, "--backend", "mock", "--steps", "2", "--run-dir", tmp_path / "run")
    assert backend.actions == [] and backend.observations == 0
    assert not (tmp_path / "run" / "integrity.json").exists()
    assert rl.verify_run(tmp_path / "run", allow_incomplete=True)["complete"] is False


def test_max_request_bytes_option_reaches_the_hierarchical_controller(tmp_path, monkeypatch):
    seen = []
    from jev_factorio.controller import HierarchicalLoop
    original = HierarchicalLoop.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        seen.append(self.max_request_bytes)

    monkeypatch.setattr(HierarchicalLoop, "__init__", capture)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: CountingBackend())
    invoke(monkeypatch, "--controller", "hierarchical", "--mock-model", "--steps", "1")
    invoke(monkeypatch, "--controller", "hierarchical", "--mock-model", "--steps", "1",
           "--max-request-bytes", "64000")
    from jev_factorio.judgments import DEFAULT_MAX_REQUEST_BYTES
    assert seen == [DEFAULT_MAX_REQUEST_BYTES, 64000]
