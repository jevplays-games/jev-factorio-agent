"""Subprocess regressions for checkpoint admission before run telemetry."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jev_factorio.dashboard import EventWriter, Monitor


REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _environment(*, extra_pythonpath: Path | None = None, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        if (name.startswith(("JEV_", "CF_")) or name.endswith("_API_KEY")
                or name.endswith("_API_TOKEN")):
            env.pop(name, None)
    paths = [str(SRC)]
    if extra_pythonpath is not None:
        paths.insert(0, str(extra_pythonpath))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def _invoke(tmp_path: Path, arguments: list[str], *,
            extra_pythonpath: Path | None = None, extra_env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-m", "jev_factorio", *arguments],
        cwd=tmp_path,
        env=_environment(extra_pythonpath=extra_pythonpath, **(extra_env or {})),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _seed_dashboard(path: Path) -> bytes:
    with EventWriter(path) as writer:
        writer.emit("run_started", 2, target="bootstrap_mining", controller="hierarchical")
    return path.read_bytes()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _gameplay_projection(value):
    """Keep decisions and receipts; omit invocation IDs and measured timings."""
    volatile = {
        "at_utc", "created_at_utc", "finished_at_utc", "started_at_utc",
        "seconds", "latency_seconds", "process_id", "session_id", "trace_id", "id",
    }
    if isinstance(value, dict):
        return {key: _gameplay_projection(child) for key, child in value.items()
                if key not in volatile}
    if isinstance(value, list):
        return [_gameplay_projection(child) for child in value]
    return value


@pytest.mark.parametrize("existing_events", [True, False])
def test_invalid_composed_checkpoint_preserves_existing_outputs_before_admission(
        tmp_path, existing_events):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"session_id": "valid-shaped-session", "status": "running"}),
                          encoding="utf-8")
    checkpoint_before = checkpoint.read_bytes()
    events = tmp_path / "events.jsonl"
    if existing_events:
        events_before = _seed_dashboard(events)
        monitor = Monitor(events)
        monitor.poll()
        prior_view = monitor.snapshot()["view"]
    else:
        events_before = None
        prior_view = None
    run_dir = tmp_path / "research-run"
    setup_timing = tmp_path / "setup-timing.json"

    proc = _invoke(tmp_path, [
        "--controller", "hierarchical", "--backend", "mock", "--mock-model",
        "--resume-controller", "--checkpoint", str(checkpoint),
        "--dashboard-events", str(events), "--run-dir", str(run_dir),
        "--setup-timing-file", str(setup_timing), "--steps", "1",
    ])

    assert proc.returncode == 2
    assert "Composed resume checkpoint preflight failed" in proc.stderr
    assert checkpoint.read_bytes() == checkpoint_before
    if existing_events:
        assert events.read_bytes() == events_before
    else:
        assert not events.exists()
    assert not run_dir.exists()
    assert not setup_timing.exists()
    if existing_events:
        monitor = Monitor(events)
        monitor.poll()
        assert monitor.snapshot()["view"] == prior_view


@pytest.mark.parametrize("existing_events", [True, False])
def test_malformed_checkpoint_is_rejected_before_either_run_writer(tmp_path, existing_events):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(b'{"session_id":')
    checkpoint_before = checkpoint.read_bytes()
    events = tmp_path / "events.jsonl"
    events_before = _seed_dashboard(events) if existing_events else None
    prior_view = None
    if existing_events:
        monitor = Monitor(events)
        monitor.poll()
        prior_view = monitor.snapshot()["view"]
    run_dir = tmp_path / "research-run"

    proc = _invoke(tmp_path, [
        "--controller", "hierarchical", "--backend", "mock", "--mock-model",
        "--resume-controller", "--checkpoint", str(checkpoint),
        "--dashboard-events", str(events), "--run-dir", str(run_dir),
    ])

    assert proc.returncode == 2
    assert checkpoint.read_bytes() == checkpoint_before
    if existing_events:
        assert events.read_bytes() == events_before
        monitor = Monitor(events)
        monitor.poll()
        assert monitor.snapshot()["view"] == prior_view
    else:
        assert not events.exists()
    assert not run_dir.exists()


@pytest.mark.parametrize("kind", ["arguments", "credentials", "treatment", "source", "archive"])
@pytest.mark.parametrize("existing_events", [True, False])
def test_other_preflight_rejections_do_not_create_telemetry_outputs(
        tmp_path, kind, existing_events):
    checkpoint = tmp_path / "checkpoint.json"
    events = tmp_path / "events.jsonl"
    run_dir = tmp_path / "research-run"
    treatment = tmp_path / "invalid-treatment.json"
    treatment.write_text("{", encoding="utf-8")
    env = {}

    if kind == "arguments":
        checkpoint_arg = []
        args = ["--controller", "hierarchical", "--backend", "mock", "--mock-model",
                "--steps", "-1"]
    elif kind == "credentials":
        checkpoint_arg = ["--checkpoint", str(checkpoint)]
        args = ["--controller", "hierarchical", "--backend", "mock", "--async-decisions",
                *checkpoint_arg]
    elif kind == "treatment":
        checkpoint_arg = ["--checkpoint", str(checkpoint), "--tick-seconds", "0.1"]
        args = ["--controller", "hierarchical", "--backend", "fle", "--factory-scheduling",
                "ready-work", "--production-treatment", str(treatment), *checkpoint_arg]
    elif kind == "source":
        checkpoint.write_text(json.dumps({"session_id": "source-session", "status": "blocked"}),
                              encoding="utf-8")
        checkpoint_arg = ["--checkpoint", str(checkpoint), "--tick-seconds", "0.1"]
        args = ["--controller", "hierarchical", "--backend", "fle", "--target", "rocket_launch",
                "--resume", "--resume-controller", "--until-complete", "--persist-recoverable-blocks",
                *checkpoint_arg]
        # A syntactically present fake credential lets source/checkpoint validation
        # be the first failing gate; the CLI never issues a provider request.
        env["TYPESAFE_API_KEY"] = "459-offline-test-credential"
    else:
        checkpoint.write_text(json.dumps({
            "session_id": "archive-session", "status": "blocked",
            "blocked_recovery_archive": {"schema": 999},
        }), encoding="utf-8")
        checkpoint_arg = ["--checkpoint", str(checkpoint)]
        args = ["--controller", "hierarchical", "--backend", "mock", "--mock-model",
                "--resume-controller", *checkpoint_arg]

    events_before = _seed_dashboard(events) if existing_events else None
    prior_view = None
    if existing_events:
        monitor = Monitor(events)
        monitor.poll()
        prior_view = monitor.snapshot()["view"]
    checkpoint_before = checkpoint.read_bytes() if checkpoint.exists() else None
    proc = _invoke(tmp_path, [*args, "--dashboard-events", str(events), "--run-dir", str(run_dir)],
                   extra_env=env)

    assert proc.returncode == 2, (kind, proc.stderr)
    if existing_events:
        assert events.read_bytes() == events_before
        monitor = Monitor(events)
        monitor.poll()
        assert monitor.snapshot()["view"] == prior_view
    else:
        assert not events.exists()
    assert not run_dir.exists()
    if checkpoint_before is not None:
        assert checkpoint.read_bytes() == checkpoint_before
    if kind == "credentials":
        assert "Async decisions require live provider credentials" in proc.stderr
    elif kind == "treatment":
        assert "Invalid production treatment" in proc.stderr
    elif kind == "source":
        assert "Persistent recovery source/checkpoint preflight failed" in proc.stderr
    elif kind == "archive":
        assert "Blocked-recovery archive preflight failed" in proc.stderr


@pytest.mark.parametrize("existing_events", [True, False])
def test_flat_controller_rejects_dashboard_output_before_writer_entry(tmp_path, existing_events):
    events = tmp_path / "events.jsonl"
    events_before = _seed_dashboard(events) if existing_events else None
    prior_view = None
    if existing_events:
        monitor = Monitor(events)
        monitor.poll()
        prior_view = monitor.snapshot()["view"]
    run_dir = tmp_path / "research-run"

    proc = _invoke(tmp_path, [
        "--controller", "flat", "--backend", "mock", "--steps", "1",
        "--dashboard-events", str(events), "--run-dir", str(run_dir),
    ])

    assert proc.returncode == 2
    assert "--dashboard-events requires --controller hierarchical" in proc.stderr
    assert not run_dir.exists()
    if existing_events:
        assert events.read_bytes() == events_before
        monitor = Monitor(events)
        monitor.poll()
        assert monitor.snapshot()["view"] == prior_view
    else:
        assert not events.exists()


def test_admitted_run_keeps_dashboard_and_research_lifecycle(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    events = tmp_path / "events.jsonl"
    run_dir = tmp_path / "research-run"
    proc = _invoke(tmp_path, [
        "--controller", "hierarchical", "--backend", "mock", "--policy", "deterministic",
        "--checkpoint", str(checkpoint), "--dashboard-events", str(events),
        "--run-dir", str(run_dir), "--steps", "1", "--tick-seconds", "0",
    ])

    assert proc.returncode == 0, proc.stderr
    dashboard = _jsonl(events)
    research = _jsonl(run_dir / "events.jsonl")
    assert dashboard[0]["kind"] == "run_started"
    assert dashboard[-1]["kind"] == "run_finished"
    assert {row["run_id"] for row in dashboard} == {dashboard[0]["run_id"]}
    assert research[0]["event_type"] == "run_started"
    assert research[-1]["event_type"] == "run_finished"
    assert "controller_initialized" in [row["event_type"] for row in research]
    assert "controller_stopped" in [row["event_type"] for row in research]
    monitor = Monitor(events)
    monitor.poll()
    assert monitor.snapshot()["view"]["lifecycle"] == "returned"


def test_started_runtime_error_remains_visible_after_admission(tmp_path):
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "from jev_factorio.controller import HierarchicalLoop\n"
        "def fail_after_admission(self, *args, **kwargs):\n"
        "    raise RuntimeError('controlled admitted-run failure')\n"
        "HierarchicalLoop.run = fail_after_admission\n",
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    run_dir = tmp_path / "research-run"
    proc = _invoke(tmp_path, [
        "--controller", "hierarchical", "--backend", "mock", "--mock-model",
        "--dashboard-events", str(events), "--run-dir", str(run_dir), "--steps", "1",
    ], extra_pythonpath=hook)

    assert proc.returncode != 0
    dashboard = _jsonl(events)
    research = _jsonl(run_dir / "events.jsonl")
    assert dashboard[0]["kind"] == "run_started"
    assert dashboard[-1]["kind"] == "run_finished"
    assert dashboard[-1]["data"]["outcome"] == "error"
    assert research[0]["event_type"] == "run_started"
    assert research[-1]["event_type"] == "run_finished"
    assert research[-1]["payload"]["outcome"] == "error"


@pytest.mark.parametrize("controller", ["flat", "hierarchical"])
def test_admitted_mock_telemetry_keeps_the_same_gameplay_result(tmp_path, controller):
    states = []
    for telemetry in (False, True):
        case = tmp_path / ("telemetry-on" if telemetry else "telemetry-off")
        case.mkdir()
        args = ["--controller", controller, "--backend", "mock", "--steps", "1",
                "--tick-seconds", "0"]
        if controller == "flat":
            gameplay_log = case / "gameplay.jsonl"
            args.extend(["--log-file", str(gameplay_log)])
        else:
            checkpoint = case / "checkpoint.json"
            args.extend(["--policy", "deterministic", "--checkpoint", str(checkpoint)])
        if telemetry:
            args.extend(["--run-dir", str(case / "research-run")])
            if controller == "hierarchical":
                args.extend(["--dashboard-events", str(case / "events.jsonl")])
        proc = _invoke(case, args)
        assert proc.returncode == 0, proc.stderr
        if controller == "flat":
            states.append(_gameplay_projection(_jsonl(gameplay_log)))
        else:
            states.append(_gameplay_projection(json.loads(checkpoint.read_bytes())))
    assert states[0] == states[1]


def _seed_resume_checkpoint(tmp_path: Path) -> Path:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    checkpoint = seed_dir / "checkpoint.json"
    proc = _invoke(seed_dir, [
        "--controller", "hierarchical", "--backend", "mock", "--policy", "deterministic",
        "--checkpoint", str(checkpoint), "--steps", "1", "--tick-seconds", "0",
    ])
    assert proc.returncode == 0, proc.stderr
    assert checkpoint.is_file()
    return checkpoint


def _race_hook(hook: Path) -> None:
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import hashlib, json, os\n"
        "from pathlib import Path\n"
        "def mark(value):\n"
        "    with Path(os.environ['ISSUE459_PHASE_LOG']).open('a', encoding='utf-8') as stream:\n"
        "        stream.write(value + '\\n')\n"
        "selected = json.loads(Path(os.environ['ISSUE459_SELECTED_CHECKPOINT']).read_bytes())\n"
        "replacement = Path(os.environ['ISSUE459_REPLACEMENT_CHECKPOINT']).read_bytes()\n"
        "def replace(where):\n"
        "    Path(os.environ['ISSUE459_RACE_CHECKPOINT']).write_bytes(replacement)\n"
        "    mark('replaced:' + where + ':' + hashlib.sha256(replacement).hexdigest())\n"
        "from jev_factorio.dashboard import EventWriter\n"
        "original_event_init = EventWriter.__init__\n"
        "def event_init(self, *args, **kwargs):\n"
        "    if os.environ['ISSUE459_REPLACE_PHASE'] == 'eventwriter':\n"
        "        replace('eventwriter')\n"
        "    original_event_init(self, *args, **kwargs)\n"
        "EventWriter.__init__ = event_init\n"
        "from jev_factorio.backends.mock import MockBackend\n"
        "original_backend_init = MockBackend.__init__\n"
        "def backend_init(self, *args, **kwargs):\n"
        "    original_backend_init(self, *args, **kwargs)\n"
        "    mark('backend_init')\n"
        "    self.session_id = selected['session_id']\n"
        "    self.tick = selected['last_tick']\n"
        "MockBackend.__init__ = backend_init\n"
        "original_observe = MockBackend.observe\n"
        "observation_count = 0\n"
        "def observe(self, *args, **kwargs):\n"
        "    global observation_count\n"
        "    result = original_observe(self, *args, **kwargs)\n"
        "    observation_count += 1\n"
        "    mark('backend_observe')\n"
        "    if (os.environ['ISSUE459_REPLACE_PHASE'] == 'backend_observation'\n"
        "            and observation_count == 1):\n"
        "        replace('backend_observation')\n"
        "    return result\n"
        "MockBackend.observe = observe\n"
        "original_act = MockBackend.act\n"
        "def act(self, action, *args, **kwargs):\n"
        "    mark('action:' + str(action))\n"
        "    return original_act(self, action, *args, **kwargs)\n"
        "MockBackend.act = act\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("replace_phase", ["eventwriter", "backend_observation"])
@pytest.mark.parametrize("replacement_kind", ["valid", "malformed"])
def test_resume_rejects_checkpoint_replacement_after_selected_capture(
        tmp_path, replace_phase, replacement_kind):
    from jev_factorio.memory import load_checkpoint_bytes

    selected_path = _seed_resume_checkpoint(tmp_path)
    selected = selected_path.read_bytes()
    selected_data = json.loads(selected)
    replacement_path = tmp_path / "replacement.json"
    if replacement_kind == "valid":
        replacement_data = json.loads(selected)
        replacement_data["active_plan"] = None
        replacement_data["pending"] = None
        replacement_data["attempt"] = None
        replacement_data["step_index"] = 0
        replacement_data["history"].append({
            "kind": "review_replacement_marker",
            "marker": "post-preflight-replacement",
            "tick": replacement_data["last_tick"],
        })
        replacement = json.dumps(replacement_data, separators=(",", ":")).encode("utf-8")
        replacement_path.write_bytes(replacement)
        validated = load_checkpoint_bytes(
            replacement, selected_data["session_id"], "rocket_launch",
            checkpoint_path=selected_path,
        )
        archive_index = getattr(validated, "_blocked_recovery_archive_index", None)
        if archive_index is not None:
            archive_index.close()
    else:
        replacement = b'{"session_id":'
        replacement_path.write_bytes(replacement)

    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(selected)
    hook = tmp_path / "hook"
    _race_hook(hook)
    phase_log = tmp_path / "phase.log"
    events = tmp_path / "events.jsonl"
    run_dir = tmp_path / "research"
    proc = _invoke(tmp_path, [
        "--controller", "hierarchical", "--backend", "mock", "--policy", "deterministic",
        "--resume-controller", "--checkpoint", str(checkpoint),
        "--dashboard-events", str(events), "--run-dir", str(run_dir),
        "--steps", "1", "--tick-seconds", "0",
    ], extra_pythonpath=hook, extra_env={
        "ISSUE459_PHASE_LOG": str(phase_log),
        "ISSUE459_SELECTED_CHECKPOINT": str(selected_path),
        "ISSUE459_RACE_CHECKPOINT": str(checkpoint),
        "ISSUE459_REPLACEMENT_CHECKPOINT": str(replacement_path),
        "ISSUE459_REPLACE_PHASE": replace_phase,
    })

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert checkpoint.read_bytes() == replacement
    phases = phase_log.read_text(encoding="utf-8")
    assert f"replaced:{replace_phase}:" in phases
    assert "action:" not in phases, proc.stdout + proc.stderr + phases
    research = _jsonl(run_dir / "events.jsonl")
    assert research[0]["event_type"] == "run_started"
    assert research[-1]["event_type"] == "run_finished"
    assert research[-1]["payload"]["outcome"] == "error"
    if replace_phase == "eventwriter":
        assert "backend_init" not in phases
        assert events.read_bytes() == b""
        assert _jsonl(events) == []
    else:
        assert "backend_init" in phases
        assert phases.count("backend_observe") == 1
        dashboard = _jsonl(events)
        assert dashboard[0]["kind"] == "run_started"
        assert dashboard[-1]["kind"] == "run_finished"
        assert dashboard[-1]["data"]["outcome"] == "error"


def test_unchanged_selected_checkpoint_resumes_normally(tmp_path):
    selected_path = _seed_resume_checkpoint(tmp_path)
    selected = selected_path.read_bytes()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(selected)
    replacement_path = tmp_path / "unused-replacement.json"
    replacement_path.write_bytes(selected)
    hook = tmp_path / "hook"
    _race_hook(hook)
    phase_log = tmp_path / "phase.log"
    events = tmp_path / "events.jsonl"
    run_dir = tmp_path / "research"
    proc = _invoke(tmp_path, [
        "--controller", "hierarchical", "--backend", "mock", "--policy", "deterministic",
        "--resume-controller", "--checkpoint", str(checkpoint),
        "--dashboard-events", str(events), "--run-dir", str(run_dir),
        "--steps", "1", "--tick-seconds", "0",
    ], extra_pythonpath=hook, extra_env={
        "ISSUE459_PHASE_LOG": str(phase_log),
        "ISSUE459_SELECTED_CHECKPOINT": str(selected_path),
        "ISSUE459_RACE_CHECKPOINT": str(checkpoint),
        "ISSUE459_REPLACEMENT_CHECKPOINT": str(replacement_path),
        "ISSUE459_REPLACE_PHASE": "none",
    })

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "replaced:" not in phase_log.read_text(encoding="utf-8")
    dashboard = _jsonl(events)
    research = _jsonl(run_dir / "events.jsonl")
    assert dashboard[0]["kind"] == "run_started"
    assert dashboard[-1]["kind"] == "run_finished"
    assert research[0]["event_type"] == "run_started"
    assert research[-1]["event_type"] == "run_finished"
    phases = phase_log.read_text(encoding="utf-8")
    assert "backend_init" in phases
    assert "backend_observe" in phases
    assert not any(row.get("kind") == "review_replacement_marker"
                   for row in json.loads(checkpoint.read_bytes()).get("history", []))


def test_archived_selected_checkpoint_resumes_through_composed_loader(tmp_path):
    from jev_factorio.blocked_recovery_archive import archive_full_tail
    from jev_factorio.memory import load_checkpoint
    from test_blocked_recovery_archive import _full_memory

    checkpoint = tmp_path / "checkpoint.json"
    memory = _full_memory()
    index = archive_full_tail(checkpoint, memory)
    memory.save(checkpoint)
    index.close()
    selected = checkpoint.read_bytes()
    replacement_path = tmp_path / "unused-replacement.json"
    replacement_path.write_bytes(selected)
    hook = tmp_path / "hook"
    _race_hook(hook)
    phase_log = tmp_path / "phase.log"
    events = tmp_path / "events.jsonl"
    run_dir = tmp_path / "research"

    proc = _invoke(tmp_path, [
        "--controller", "hierarchical", "--backend", "mock", "--policy", "deterministic",
        "--resume-controller",
        "--checkpoint", str(checkpoint), "--dashboard-events", str(events),
        "--run-dir", str(run_dir), "--steps", "1", "--tick-seconds", "0",
    ], extra_pythonpath=hook, extra_env={
        "ISSUE459_PHASE_LOG": str(phase_log),
        "ISSUE459_SELECTED_CHECKPOINT": str(checkpoint),
        "ISSUE459_RACE_CHECKPOINT": str(checkpoint),
        "ISSUE459_REPLACEMENT_CHECKPOINT": str(replacement_path),
        "ISSUE459_REPLACE_PHASE": "none",
    })

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert checkpoint.read_bytes() != selected
    phases = phase_log.read_text(encoding="utf-8").splitlines()
    assert "backend_init" in phases
    assert "backend_observe" in phases
    assert any(row.startswith("action:") for row in phases)
    assert _jsonl(events)[0]["kind"] == "run_started"
    assert _jsonl(events)[-1]["kind"] == "run_finished"
    assert _jsonl(events)[-1]["data"]["outcome"] == "returned"
    research = _jsonl(run_dir / "events.jsonl")
    assert research[0]["event_type"] == "run_started"
    assert research[-1]["event_type"] == "run_finished"
    assert research[-1]["payload"]["outcome"] == "returned"
    restored = load_checkpoint(checkpoint, memory.session_id, memory.target)
    try:
        assert restored.blocked_recovery_archive["entry_count"] == 1024
        assert restored._blocked_recovery_archive_index is not None
    finally:
        restored._blocked_recovery_archive_index.close()
