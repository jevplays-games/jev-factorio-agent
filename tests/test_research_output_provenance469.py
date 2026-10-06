"""Research-output path safety and provenance regressions for issue #469."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from jev_factorio import research_log as research_log_module
from jev_factorio.memory import CampaignMemory
from jev_factorio.provenance import source_revision
from jev_factorio.research_log import (
    ResearchLog, RunConfiguration, open_research_output_parent, verify_run,
)
from jev_factorio.supervisor import Supervisor, SupervisorConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = PROJECT_ROOT / "src"
SELECTION = {"provider": "typesafe", "model": "jev-latest", "explicit": False}


def git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    completed = subprocess.run(
        ["git", *args], cwd=root, env=env, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )
    return completed.stdout.strip()


def make_repo(tmp_path: Path, *, ignored: str = "runs/\n") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text(ignored, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "agent.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Issue 469 test")
    git(root, "config", "user.email", "issue469@example.invalid")
    git(root, "add", ".gitignore", "src/agent.py")
    git(root, "commit", "-qm", "isolated source baseline")
    return root


def make_config(tmp_path: Path, repo: Path, research_dir: Path | None) -> SupervisorConfig:
    runtime = tmp_path / "runtime"
    checkpoint = runtime / "campaign.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(asdict(CampaignMemory("issue469-session", "rocket_launch"))),
                          encoding="utf-8")
    return SupervisorConfig(
        state_dir=runtime / "supervisor",
        checkpoint=checkpoint,
        session_id="issue469-session",
        started_at=1000.0,
        duration_hours=12,
        repair_command=["repair"],
        cwd=repo,
        python=sys.executable,
        research_dir=research_dir,
    )


def make_supervisor(config: SupervisorConfig, monkeypatch, popen=None) -> Supervisor:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    instance = Supervisor(config, clock=lambda: 1000.0,
                          popen=popen or (lambda *args, **kwargs: pytest.fail("unexpected child launch")))
    monkeypatch.setattr(instance, "model_selection", lambda environment=None: dict(SELECTION))
    return instance


def selected_run_dir(command: list[str]) -> Path:
    return Path(command[command.index("--run-dir") + 1])


def run_actual_mock_cli(run_dir: Path, repo: Path, log_file: Path) -> dict:
    """Exercise the real mock CLI and ResearchLog without provider/native calls."""
    env = os.environ.copy()
    for key in (
        "TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
        "FACTORIO_RCON_PASSWORD", "JEV_FACTORIO_PROVENANCE",
    ):
        env.pop(key, None)
    env["PYTHONPATH"] = str(PROJECT_SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable, "-m", "jev_factorio", "--backend", "mock",
        "--controller", "hierarchical", "--policy", "deterministic",
        "--target", "rocket_launch", "--steps", "1", "--tick-seconds", "0",
        "--run-dir", str(run_dir), "--log-file", str(log_file),
    ]
    result = subprocess.run(command, cwd=repo, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stdout
    verified = verify_run(run_dir)
    assert verified["complete"] is True
    assert {path.name for path in run_dir.iterdir() if path.is_file()} == {
        "manifest.json", "events.jsonl", "integrity.json"
    }
    return {"command": command, "stdout": result.stdout, "verify_run": verified}


def test_nonignored_in_repo_research_output_is_rejected_or_reproduces_false_boundary(
        tmp_path, monkeypatch):
    """Red baseline runs the actual mock CLI; fixed behavior rejects before output."""
    repo = make_repo(tmp_path)
    research = repo / "evidence"
    config = make_config(tmp_path, repo, research)
    state_path = config.state_dir / "supervisor.json"

    try:
        config.validate()
    except ValueError as error:
        assert "research" in str(error).lower()
        assert not state_path.exists()
        assert not research.exists()
        return

    # Baseline path: current production accepts an unignored in-checkout target,
    # starts a persisted supervisor record, and selects the real invocation path.
    instance = make_supervisor(config, monkeypatch)
    instance.initialize(record_only=True)
    before = instance.snapshot_revision(manual=True)
    assert before is not None
    assert instance.record_revision(before, "gameplay_start", actor_type="unknown",
                                    intervention_type="unattributed_change")
    before_event_count = len((config.state_dir / "events.jsonl").read_text().splitlines())
    run_dir = selected_run_dir(instance.gameplay_command())
    assert run_dir.parent == research.resolve()
    cli = run_actual_mock_cli(run_dir, repo, config.state_dir / "mock-child.log")
    after = instance.snapshot_revision(manual=True)
    assert after == before, {
        "baseline_reproduction": "nonignored research output changed the source fingerprint without code changes",
        "before": before,
        "after": after,
        "actual_mock_cli": cli,
    }
    assert len((config.state_dir / "events.jsonl").read_text().splitlines()) == before_event_count


def test_external_research_directory_remains_supported(tmp_path):
    repo = make_repo(tmp_path)
    external = tmp_path / "external-campaign-evidence"
    config = make_config(tmp_path, repo, external)
    config.validate()


def test_external_research_directory_runs_real_mock_capture_without_revision_change(
        tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    external = tmp_path / "external-campaign-evidence"
    config = make_config(tmp_path, repo, external)
    instance = make_supervisor(config, monkeypatch)
    instance.initialize(record_only=True)
    before = instance.snapshot_revision(manual=True)
    assert before is not None

    run_dir = selected_run_dir(instance.gameplay_command())
    assert run_dir.parent == external.resolve()
    captured = run_actual_mock_cli(run_dir, repo, config.state_dir / "mock-child.log")

    assert captured["verify_run"]["complete"] is True
    assert instance.snapshot_revision(manual=True) == before


def test_documented_ignored_research_output_runs_real_mock_capture_without_revision_change(
        tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    research = repo / "runs" / "campaign-evidence"
    config = make_config(tmp_path, repo, research)
    instance = make_supervisor(config, monkeypatch)
    instance.initialize(record_only=True)

    initial_identity = {
        key: instance.state[key]
        for key in ("run_id", "session_id", "cutoff", "segment", "segment_id")
    }
    before = instance.snapshot_revision(manual=True)
    assert before is not None
    assert instance.record_revision(before, "gameplay_start", actor_type="unknown",
                                    intervention_type="unattributed_change")
    events_path = config.state_dir / "events.jsonl"
    prior_events = events_path.read_text(encoding="utf-8").splitlines()

    first_command = instance.gameplay_command()
    second_command = instance.gameplay_command()
    first_run = selected_run_dir(first_command)
    second_run = selected_run_dir(second_command)
    assert first_run != second_run
    assert first_run.parent == research.resolve() == second_run.parent
    assert all("--resume" in command and "--resume-controller" in command
               for command in (first_command, second_command))
    assert not first_run.exists() and not second_run.exists()

    result = run_actual_mock_cli(first_run, repo, config.state_dir / "mock-child.log")
    after_output = instance.snapshot_revision(manual=True)
    assert after_output == before
    assert instance.record_revision(after_output, "gameplay_start", actor_type="unknown",
                                    intervention_type="unattributed_change")
    assert events_path.read_text(encoding="utf-8").splitlines() == prior_events
    assert {key: instance.state[key] for key in initial_identity} == initial_identity
    assert result["verify_run"]["complete"] is True

    # A genuine tracked-code change remains visible to the supervisor boundary.
    (repo / "src" / "agent.py").write_text("value = 2\n", encoding="utf-8")
    changed = instance.snapshot_revision(manual=True)
    assert changed is not None and changed != before
    assert instance.record_revision(changed, "gameplay_start", actor_type="unknown",
                                    intervention_type="unattributed_change")
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["event"] == "code_revision_changed"


@pytest.mark.parametrize("destination", ["checkout", "ancestor"])
def test_checkout_root_and_ancestor_are_never_research_destinations(tmp_path, destination):
    repo = make_repo(tmp_path)
    research = repo if destination == "checkout" else repo.parent
    config = make_config(tmp_path, repo, research)
    with pytest.raises(ValueError, match="research.*(checkout|ancestor|outside)|checkout.*research"):
        config.validate()


def test_tracked_source_inside_ignored_research_path_is_not_hidden(tmp_path):
    repo = make_repo(tmp_path)
    research = repo / "runs" / "campaign-evidence"
    research.mkdir(parents=True)
    tracked = research / "source.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", "-f", "runs/campaign-evidence/source.py")
    git(repo, "commit", "-qm", "tracked source under ignored output root")
    config = make_config(tmp_path, repo, research)

    before = source_revision(repo, exclude_untracked=(config.state_dir, config.checkpoint))
    assert before is not None
    tracked.write_text("value = 2\n", encoding="utf-8")
    after = source_revision(repo, exclude_untracked=(config.state_dir, config.checkpoint))
    assert after is not None and after != before
    with pytest.raises(ValueError, match="research.*tracked|tracked.*research"):
        config.validate()


def test_git_pathspec_magic_research_name_is_rejected_as_ambiguous(tmp_path):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo, repo / ":(exclude)src")
    with pytest.raises(ValueError, match="research output path uses ambiguous Git path syntax"):
        config.validate()


def test_nonignored_untracked_source_and_symlink_are_still_fingerprinted_under_research_root(
        tmp_path):
    repo = make_repo(tmp_path)
    research = repo / "evidence"
    config = make_config(tmp_path, repo, research)
    before = source_revision(repo, exclude_untracked=(config.state_dir, config.checkpoint))
    assert before is not None

    research.mkdir()
    source = research / "added.py"
    source.write_text("untracked = True\n", encoding="utf-8")
    with_source = source_revision(repo, exclude_untracked=(config.state_dir, config.checkpoint))
    assert with_source is not None and with_source != before

    link = research / "alias.py"
    try:
        link.symlink_to(Path("..") / "src" / "agent.py")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation is unavailable on this platform: {error}")
    with_link = source_revision(repo, exclude_untracked=(config.state_dir, config.checkpoint))
    assert with_link is not None and with_link != with_source

    # The path is rejected for capture, but source fingerprinting still sees
    # files under and adjacent to it; no research-root exclusion is applied.
    with pytest.raises(ValueError, match="ignore|outside"):
        config.validate()
    adjacent = repo / "source_neighbor.py"
    adjacent.write_text("neighbor = True\n", encoding="utf-8")
    with_neighbor = source_revision(repo, exclude_untracked=(config.state_dir, config.checkpoint))
    assert with_neighbor is not None and with_neighbor != with_link


@pytest.mark.parametrize("failure", ["missing_git", "timeout", "ignore_error"])
def test_ambiguous_in_repo_ignore_status_fails_closed(tmp_path, monkeypatch, failure):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo, repo / "runs" / "campaign-evidence")
    from jev_factorio import supervisor as supervisor_module

    real_run = supervisor_module.subprocess.run

    def failing_run(args, *positional, **kwargs):
        command = [str(item) for item in args]
        if failure == "missing_git":
            raise FileNotFoundError("git unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))
        if len(command) > 1 and command[1] == "check-ignore":
            return subprocess.CompletedProcess(args, 128, stdout=b"", stderr=b"permission denied")
        return real_run(args, *positional, **kwargs)

    monkeypatch.setattr(supervisor_module.subprocess, "run", failing_run)
    with pytest.raises(ValueError, match="research.*(Git|ignore|verify|validate)|Git.*research"):
        config.validate()


def test_gitless_nonrepository_allows_external_output_but_source_remains_unknown(
        tmp_path, monkeypatch):
    nonrepo = tmp_path / "not-a-repository"
    nonrepo.mkdir()
    local_output = nonrepo / "research-output"
    local_config = make_config(tmp_path, nonrepo, local_output)
    local_config.validate()
    assert source_revision(nonrepo) is None

    external = tmp_path / "external-output"
    config = make_config(tmp_path, nonrepo, external)
    config.validate()
    assert source_revision(nonrepo) is None


def test_symlink_aliases_into_checkout_are_rejected(tmp_path):
    repo = make_repo(tmp_path)
    aliases = tmp_path / "aliases"
    aliases.mkdir()
    for name, target in (("root", repo), ("unignored", repo / "src")):
        link = aliases / name
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"symlink creation is unavailable on this platform: {error}")
        config = make_config(tmp_path / name, repo, link)
        with pytest.raises(ValueError, match="research|checkout|ignored"):
            config.validate()


def test_symlink_replacement_between_command_selection_and_launch_is_rejected(
        tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    first = repo / "runs" / "first"
    second = repo / "runs" / "second"
    first.parent.mkdir(parents=True)
    alias = tmp_path / "research-alias"
    try:
        alias.symlink_to(first, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation is unavailable on this platform: {error}")
    config = make_config(tmp_path, repo, alias)
    launches = []
    instance = make_supervisor(config, monkeypatch,
                               popen=lambda *a, **kw: launches.append((a, kw)))
    instance.state = {
        "cutoff": 2000.0,
        "gameplay_configuration": {"model_selection": dict(SELECTION)},
    }
    command = instance.gameplay_command()
    before_state = json.dumps(instance.state, sort_keys=True)
    before_events = (config.state_dir / "events.jsonl")

    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    with pytest.raises(ValueError, match="research|output path|changed"):
        instance.launch(command, "gameplay")

    assert launches == []
    assert json.dumps(instance.state, sort_keys=True) == before_state
    assert not before_events.exists()
    assert not selected_run_dir(command).exists()


@pytest.mark.parametrize("marker_change", ["removed", "substituted"])
def test_known_checkout_marker_change_cannot_admit_local_research_output(
        tmp_path, monkeypatch, marker_change):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo, None)
    instance = make_supervisor(config, monkeypatch)
    config.research_dir = repo / "runs" / "campaign-evidence"
    instance.state = {
        "cutoff": 2000.0,
        "gameplay_configuration": {"model_selection": dict(SELECTION)},
    }
    command = instance.gameplay_command()
    launches = []
    instance.popen = lambda *args, **kwargs: launches.append((args, kwargs))

    git_marker = repo / ".git"
    original_marker = repo / ".git-original"
    git_marker.rename(original_marker)
    if marker_change == "substituted":
        git_marker.write_text("not the validated repository marker\n", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="checkout marker changed"):
            instance.launch(command, "gameplay")
    finally:
        if git_marker.exists():
            git_marker.unlink()
        original_marker.rename(git_marker)

    assert launches == []
    assert not (config.state_dir / "events.jsonl").exists()


def test_symlink_loop_is_rejected_as_ambiguous_research_path(tmp_path):
    repo = make_repo(tmp_path)
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation is unavailable on this platform: {error}")
    config = make_config(tmp_path, repo, loop)
    with pytest.raises(ValueError, match="research.*resolve|research.*safe|path"):
        config.validate()


def test_path_invalidated_after_initialize_stops_without_repair_or_obligation_mutation(
        tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    external = tmp_path / "external-evidence"
    config = make_config(tmp_path, repo, external)
    instance = make_supervisor(config, monkeypatch)
    instance.initialize(record_only=True)
    before_state = dict(instance.state)
    before_checkpoint = config.checkpoint.read_bytes()
    before_events = (config.state_dir / "events.jsonl").read_bytes()
    repair_calls = []
    monkeypatch.setattr(instance, "initialize", lambda **_kwargs: None)
    monkeypatch.setattr(instance, "repair", lambda *args: repair_calls.append(args))
    monkeypatch.setattr(Supervisor, "lock_path", staticmethod(lambda: tmp_path / "supervisor.lock"))
    config.research_dir = repo / "unignored-output"

    assert instance.run() == 2

    assert repair_calls == []
    assert instance.state == before_state
    assert config.checkpoint.read_bytes() == before_checkpoint
    assert (config.state_dir / "events.jsonl").read_bytes() == before_events
    assert "research output path rejected" in capsys.readouterr().err.lower()


def _make_launchable_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    (repo / "src" / "jev_factorio").symlink_to(
        PROJECT_SRC / "jev_factorio", target_is_directory=True)
    return repo


def _make_launchable_supervisor(config: SupervisorConfig, monkeypatch) -> Supervisor:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    instance = Supervisor(config, clock=lambda: 1000.0, popen=subprocess.Popen)
    monkeypatch.setattr(instance, "model_selection", lambda environment=None: dict(SELECTION))
    instance.initialize(record_only=True)
    return instance


def _mock_gameplay_command(run_dir: Path, repo: Path, log_file: Path) -> list[str]:
    return [
        sys.executable, "-m", "jev_factorio", "--backend", "mock",
        "--controller", "hierarchical", "--policy", "deterministic",
        "--target", "rocket_launch", "--steps", "1", "--tick-seconds", "0",
        "--run-dir", str(run_dir), "--log-file", str(log_file),
    ]


def _wait_for_child(instance: Supervisor) -> tuple[int, str]:
    assert instance.process is not None
    returncode = instance.process.wait(timeout=60)
    if instance.output is not None:
        instance.output.close()
        instance.output = None
    return returncode, (instance.config.state_dir / "gameplay.log").read_text(
        encoding="utf-8", errors="replace")


@pytest.mark.parametrize("destination", ["ignored", "external"])
@pytest.mark.skipif(os.name == "nt", reason="supervised directory binding currently requires POSIX")
def test_supervised_mock_capture_preserves_source_revision(tmp_path, monkeypatch, destination):
    repo = _make_launchable_repo(tmp_path)
    if destination == "ignored":
        research = repo / "runs" / "campaign-evidence"
        research.mkdir(parents=True)
    else:
        research = tmp_path / "external-campaign-evidence"
    config = make_config(tmp_path, repo, research)
    instance = _make_launchable_supervisor(config, monkeypatch)
    before = instance.snapshot_revision(manual=True)
    assert before is not None

    selected = selected_run_dir(instance.gameplay_command())
    command = _mock_gameplay_command(selected, repo, config.state_dir / "mock-child.log")
    instance.launch(command, "gameplay")
    returncode, output = _wait_for_child(instance)
    assert returncode == 0, output
    assert verify_run(selected)["complete"] is True
    assert instance.snapshot_revision(manual=True) == before
    assert b"JEV_RESEARCH_ROOT_FD" not in (selected / "manifest.json").read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="controlled symlink replacement requires POSIX")
def test_launch_rejects_research_parent_replaced_during_environment_resolution(
        tmp_path, monkeypatch):
    """The original PR35 race occurs after config/path validation, before Popen."""
    repo = _make_launchable_repo(tmp_path)
    research = repo / "runs" / "campaign-evidence"
    research.mkdir(parents=True)
    moved = repo / "runs" / "campaign-evidence-original"
    unsafe = repo / "evidence"
    config = make_config(tmp_path, repo, research)
    instance = _make_launchable_supervisor(config, monkeypatch)
    selected = selected_run_dir(instance.gameplay_command())
    command = _mock_gameplay_command(selected, repo, config.state_dir / "mock-child.log")
    before_state = instance.state_path.read_bytes()
    before_checkpoint = config.checkpoint.read_bytes()
    before_events = (config.state_dir / "events.jsonl").read_bytes()
    before_revision = instance.snapshot_revision(manual=True)
    original_environment = instance.gameplay_environment

    def replace_after_validation():
        environment = original_environment()
        research.rename(moved)
        unsafe.mkdir()
        research.symlink_to(unsafe, target_is_directory=True)
        return environment

    monkeypatch.setattr(instance, "gameplay_environment", replace_after_validation)
    try:
        with pytest.raises(ValueError, match="research.*(changed|destination|binding)|destination.*changed"):
            instance.launch(command, "gameplay")
    finally:
        if instance.process is not None:
            instance.process.wait(timeout=60)
            if instance.output is not None:
                instance.output.close()
                instance.output = None

    assert instance.process is None
    assert not unsafe.exists() or list(unsafe.iterdir()) == []
    assert list(moved.iterdir()) == []
    assert instance.state_path.read_bytes() == before_state
    assert config.checkpoint.read_bytes() == before_checkpoint
    assert (config.state_dir / "events.jsonl").read_bytes() == before_events
    assert instance.snapshot_revision(manual=True) == before_revision


@pytest.mark.skipif(os.name == "nt", reason="controlled symlink replacement requires POSIX")
def test_child_binding_rejects_parent_replacement_after_popen_boundary(
        tmp_path, monkeypatch):
    """Child pre-exec replacement exercises the last-check-to-ResearchLog window."""
    repo = _make_launchable_repo(tmp_path)
    research = repo / "runs" / "campaign-evidence"
    research.mkdir(parents=True)
    moved = repo / "runs" / "campaign-evidence-original"
    unsafe = repo / "evidence"
    config = make_config(tmp_path, repo, research)
    instance = _make_launchable_supervisor(config, monkeypatch)
    selected = selected_run_dir(instance.gameplay_command())
    command = _mock_gameplay_command(selected, repo, config.state_dir / "mock-child.log")
    before_checkpoint = config.checkpoint.read_bytes()
    before_revision = instance.snapshot_revision(manual=True)
    real_popen = subprocess.Popen

    def replace_before_child_exec(*args, **kwargs):
        def replace_path():
            research.rename(moved)
            unsafe.mkdir()
            research.symlink_to(unsafe, target_is_directory=True)

        kwargs["preexec_fn"] = replace_path
        return real_popen(*args, **kwargs)

    instance.popen = replace_before_child_exec
    instance.launch(command, "gameplay")
    returncode, output = _wait_for_child(instance)

    assert returncode != 0, output
    assert "Cannot initialize research evidence" in output
    assert unsafe.exists() and list(unsafe.iterdir()) == []
    assert list(moved.iterdir()) == []
    assert config.checkpoint.read_bytes() == before_checkpoint
    assert instance.snapshot_revision(manual=True) == before_revision


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative output requires POSIX")
def test_research_log_keeps_full_artifact_lifecycle_on_pinned_directory(
        tmp_path, monkeypatch):
    """Replacing the visible parent after open cannot redirect later log writes."""
    repo = make_repo(tmp_path)
    research = repo / "runs" / "campaign-evidence"
    research.mkdir(parents=True)
    moved = repo / "runs" / "campaign-evidence-original"
    unsafe = repo / "evidence"
    run_dir = research / "invocation-bound"
    root_fd = os.open(research, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    child_fd = os.dup(root_fd)
    before_revision = source_revision(repo)
    assert before_revision is not None
    monkeypatch.setenv("JEV_RESEARCH_ROOT_FD", str(child_fd))
    configuration = RunConfiguration(
        backend="mock", controller="hierarchical", policy="deterministic",
        target="rocket_launch", steps=1, tick_seconds=0,
    )

    try:
        logger = ResearchLog(run_dir, configuration, repo_dir=repo)
        research.rename(moved)
        unsafe.mkdir()
        research.symlink_to(unsafe, target_is_directory=True)
        logger.emit("controller_initialized", {"probe": "pinned"})
        logger.finish("returned")
    finally:
        try:
            os.close(child_fd)
        except OSError:
            pass
        os.close(root_fd)

    retained = moved / run_dir.name
    assert verify_run(retained)["complete"] is True
    assert {path.name for path in retained.iterdir()} == {
        "manifest.json", "events.jsonl", "integrity.json"
    }
    assert unsafe.exists() and list(unsafe.iterdir()) == []
    assert source_revision(repo) == before_revision


@pytest.mark.skipif(os.name == "nt", reason="directory fsync requires POSIX descriptors")
def test_bound_run_directory_syncs_pinned_parent_before_capture(tmp_path, monkeypatch):
    parent = tmp_path / "research"
    parent.mkdir()
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    parent_stat = os.fstat(parent_fd)
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
    syncs = []
    real_fsync = os.fsync

    def trace_fsync(fd):
        descriptor_stat = os.fstat(fd)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == parent_identity:
            syncs.append((descriptor_stat.st_dev, descriptor_stat.st_ino))
        return real_fsync(fd)

    monkeypatch.setattr(research_log_module.os, "fsync", trace_fsync)
    configuration = RunConfiguration(
        backend="mock", controller="hierarchical", policy="deterministic",
        target="rocket_launch", steps=1, tick_seconds=0,
    )
    run_dir = parent / "invocation"
    logger = ResearchLog(
        run_dir, configuration, repo_dir=tmp_path,
        environ={research_log_module.RESEARCH_ROOT_FD_ENV: str(parent_fd)},
    )
    assert len(syncs) == 1
    logger.finish("returned")
    assert verify_run(run_dir)["complete"] is True
    with pytest.raises(OSError):
        os.fstat(parent_fd)


@pytest.mark.skipif(os.name == "nt", reason="directory fsync requires POSIX descriptors")
def test_bound_run_parent_fsync_failure_aborts_before_capture_and_closes_owned_fd(
        tmp_path, monkeypatch):
    parent = tmp_path / "research"
    parent.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    unrelated_fd = os.open(unrelated, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    parent_stat = os.fstat(parent_fd)
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
    real_fsync = os.fsync
    real_close = os.close
    owned_close_calls = []

    def fail_parent_fsync(fd):
        descriptor_stat = os.fstat(fd)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == parent_identity:
            raise OSError("injected parent directory fsync failure")
        return real_fsync(fd)

    def count_owned_close(fd):
        if fd == parent_fd:
            owned_close_calls.append(fd)
        return real_close(fd)

    monkeypatch.setattr(research_log_module.os, "fsync", fail_parent_fsync)
    monkeypatch.setattr(research_log_module.os, "close", count_owned_close)
    configuration = RunConfiguration(
        backend="mock", controller="hierarchical", policy="deterministic",
        target="rocket_launch", steps=1, tick_seconds=0,
    )
    run_dir = parent / "must-not-be-captured"
    with pytest.raises(OSError, match="injected parent directory fsync failure"):
        ResearchLog(
            run_dir, configuration, repo_dir=tmp_path,
            environ={research_log_module.RESEARCH_ROOT_FD_ENV: str(parent_fd)},
        )

    assert not run_dir.exists()
    assert owned_close_calls == [parent_fd]
    with pytest.raises(OSError):
        os.fstat(parent_fd)
    unrelated_stat = os.fstat(unrelated_fd)
    assert (unrelated_stat.st_dev, unrelated_stat.st_ino) == (
        unrelated.stat().st_dev, unrelated.stat().st_ino,
    )
    real_close(unrelated_fd)


@pytest.mark.skipif(os.name == "nt", reason="directory fsync requires POSIX descriptors")
def test_open_research_parent_syncs_each_new_component_before_advancing(
        tmp_path, monkeypatch):
    first = tmp_path / "new-a"
    destination = first / "new-b"
    events = []
    real_mkdir = os.mkdir
    real_fsync = os.fsync

    def trace_mkdir(name, mode=0o777, *, dir_fd=None):
        parent_stat = os.fstat(dir_fd)
        identity = (parent_stat.st_dev, parent_stat.st_ino)
        result = real_mkdir(name, mode, dir_fd=dir_fd)
        events.append(("mkdir", str(name), identity))
        return result

    def trace_fsync(fd):
        descriptor_stat = os.fstat(fd)
        identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        events.append(("fsync", identity))
        return real_fsync(fd)

    monkeypatch.setattr(research_log_module.os, "mkdir", trace_mkdir)
    monkeypatch.setattr(research_log_module.os, "fsync", trace_fsync)
    parent_fd = open_research_output_parent(destination)
    os.close(parent_fd)

    mkdirs = [(index, event) for index, event in enumerate(events) if event[0] == "mkdir"]
    assert [event[1] for _, event in mkdirs] == ["new-a", "new-b"]
    for position, (mkdir_index, (_, _name, parent_identity)) in enumerate(mkdirs):
        next_mkdir_index = (mkdirs[position + 1][0]
                            if position + 1 < len(mkdirs) else len(events))
        assert any(
            mkdir_index < index < next_mkdir_index
            and event == ("fsync", parent_identity)
            for index, event in enumerate(events)
        )
    assert destination.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="directory fsync requires POSIX descriptors")
def test_open_research_parent_stops_after_component_fsync_failure(tmp_path, monkeypatch):
    destination = tmp_path / "new-a" / "new-b"
    real_fsync = os.fsync
    tmp_stat = tmp_path.stat()
    parent_identity = (tmp_stat.st_dev, tmp_stat.st_ino)

    def fail_first_created_parent(fd):
        descriptor_stat = os.fstat(fd)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == parent_identity:
            raise OSError("injected component parent fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(research_log_module.os, "fsync", fail_first_created_parent)
    with pytest.raises(ValueError, match="cannot be opened safely"):
        open_research_output_parent(destination)
    assert (tmp_path / "new-a").is_dir()
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="inherited descriptors require POSIX")
def test_bound_constructor_closes_inherited_parent_once_on_early_provenance_error(
        tmp_path, monkeypatch):
    parent = tmp_path / "research"
    parent.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    unrelated_fd = os.open(unrelated, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    real_close = os.close
    owned_close_calls = []

    def count_owned_close(fd):
        if fd == parent_fd:
            owned_close_calls.append(fd)
        return real_close(fd)

    def fail_provenance(*_args, **_kwargs):
        raise OSError("injected provenance failure")

    monkeypatch.setattr(research_log_module.os, "close", count_owned_close)
    monkeypatch.setattr(research_log_module, "collect_provenance", fail_provenance)
    configuration = RunConfiguration(
        backend="mock", controller="hierarchical", policy="deterministic",
        target="rocket_launch", steps=1, tick_seconds=0,
    )
    with pytest.raises(OSError, match="injected provenance failure"):
        ResearchLog(
            parent / "never-created", configuration, repo_dir=tmp_path,
            environ={research_log_module.RESEARCH_ROOT_FD_ENV: str(parent_fd)},
        )

    assert owned_close_calls == [parent_fd]
    with pytest.raises(OSError):
        os.fstat(parent_fd)
    unrelated_stat = os.fstat(unrelated_fd)
    assert (unrelated_stat.st_dev, unrelated_stat.st_ino) == (
        unrelated.stat().st_dev, unrelated.stat().st_ino,
    )
    real_close(unrelated_fd)


@pytest.mark.skipif(os.name == "nt", reason="inherited descriptors require POSIX")
def test_bound_constructor_closes_inherited_parent_once_on_success(tmp_path, monkeypatch):
    parent = tmp_path / "research"
    parent.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    unrelated_fd = os.open(unrelated, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    real_close = os.close
    owned_close_calls = []

    def count_owned_close(fd):
        if fd == parent_fd:
            owned_close_calls.append(fd)
        return real_close(fd)

    monkeypatch.setattr(research_log_module.os, "close", count_owned_close)
    configuration = RunConfiguration(
        backend="mock", controller="hierarchical", policy="deterministic",
        target="rocket_launch", steps=1, tick_seconds=0,
    )
    run_dir = parent / "invocation"
    logger = ResearchLog(
        run_dir, configuration, repo_dir=tmp_path,
        environ={research_log_module.RESEARCH_ROOT_FD_ENV: str(parent_fd)},
    )
    assert owned_close_calls == [parent_fd]
    with pytest.raises(OSError):
        os.fstat(parent_fd)
    assert os.path.samefile(unrelated, f"/proc/self/fd/{unrelated_fd}")
    monkeypatch.setattr(research_log_module.os, "close", real_close)
    logger.finish("returned")
    assert verify_run(run_dir)["complete"] is True
    real_close(unrelated_fd)
