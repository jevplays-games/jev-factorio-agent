"""Public repair controls for unsupported source provenance."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from jev_factorio.memory import CampaignMemory
from jev_factorio.skills import Plan, Step
from jev_factorio.supervisor import Supervisor, SupervisorConfig, atomic_json
from jev_factorio.telemetry import make_attempt


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeRepairProcess:
    pid = 987654321

    def poll(self):
        return 0

    def wait(self):
        return 0


class OfflineSupervisor(Supervisor):
    """Production watcher/validator/repair with only child execution faked."""

    def __init__(self, *args, launches=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.launches = launches if launches is not None else []
        self.validator_results = []
        self.report_operational_verified = True

    def model_selection(self, environment=None):
        return {"provider": None, "model": None, "explicit": False}

    def capture(self, command):
        allowed = {
            ("git", "rev-parse", "HEAD"),
            ("git", "diff", "HEAD", "--"),
            ("git", "status", "--porcelain"),
            ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
        }
        if tuple(command) not in allowed:
            raise AssertionError(f"Unexpected capture command: {command!r}")
        completed = subprocess.run(
            command, cwd=self.config.cwd, env=git_environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=5, check=False,
        )
        return completed.returncode, completed.stdout.strip()

    def kill_group(self, pid):
        if pid != FakeRepairProcess.pid:
            raise AssertionError(f"Refusing to touch non-fixture process {pid}")

    def validate_repair(self, path, previous, source_before=None):
        accepted = super().validate_repair(path, previous, source_before)
        self.validator_results.append(accepted)
        return accepted

    def launch(self, command, phase, prompt=None):
        if phase != "repair" or command != self.config.repair_command:
            raise AssertionError(f"Only fake repair child is allowed, got {phase!r}")
        if self.process is not None:
            raise AssertionError("Unexpected concurrent child launch")
        self.launches.append(phase)
        report = {
            "status": "repaired",
            "kind": "operational",
            "session_id": self.config.session_id,
            "checkpoint": str(self.config.checkpoint.resolve()),
            "run_id": self.state["run_id"],
            "incident_id": self.state["incident"]["incident_id"],
            "attempt": self.state["attempt"],
            "operational_verified": self.report_operational_verified,
            "evidence": ["Fixture retains the exact validated campaign checkpoint"],
        }
        atomic_json(self.config.state_dir / f"repair-{self.state['attempt']}.json", report)
        before_validate = getattr(self, "before_validate", None)
        if before_validate is not None:
            before_validate()
        self.process = FakeRepairProcess()


def git_environment():
    environment = dict(os.environ)
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "TYPESAFE_API_KEY",
        "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "FACTORIO_RCON_PASSWORD",
    ):
        environment.pop(key, None)
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, env=git_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=5, check=True,
    )
    return completed.stdout.strip()


def make_fixture(tmp_path, *, name="run", max_attempts=2, status="running",
                 background=False):
    workspace = tmp_path / "repository"
    runtime = tmp_path / "runtime"
    state_dir = runtime / "supervisor"
    checkpoint = runtime / "campaign.json"
    workspace.mkdir()
    state_dir.mkdir(parents=True)
    (workspace / "source.py").write_text("fixture_value = 1\n", encoding="utf-8")
    git(workspace, "init", "-q")
    git(workspace, "config", "user.name", "Issue471 offline fixture")
    git(workspace, "config", "user.email", "issue471@example.invalid")
    git(workspace, "config", "commit.gpgsign", "false")
    git(workspace, "add", "source.py")
    git(workspace, "commit", "-qm", "Offline source baseline")

    session = f"issue471-{name}"
    plan = Plan("retained-plan", "rocket_launch", "Retained fixture action", (
        Step("mine_iron", "inventory", "iron-ore", 1),
    ))
    memory = CampaignMemory(
        session, "rocket_launch", status=status, active_goal="rocket_launch",
        active_plan=asdict(plan), last_tick=1,
    )
    memory.pending = {
        "started_tick": 1, "polls": 0, "action": "mine_iron", "dispatch": "ambiguous",
    }
    memory.attempt = make_attempt(
        memory.session_id, memory.target, memory.active_plan, 0, memory.pending,
    )
    atomic_json(checkpoint, asdict(memory))

    clock = FakeClock()
    config = SupervisorConfig(
        state_dir=state_dir, checkpoint=checkpoint, session_id=session,
        started_at=1000.0, repair_command=["offline-fake-repair"], cwd=workspace,
        python=os.sys.executable, duration_hours=1, max_repair_attempts=max_attempts,
        poll_seconds=0.01, backoff_seconds=0.01, model=None,
        factory_scheduling="ready-work" if background else "serial",
        background_work=background,
    )
    supervisor = OfflineSupervisor(config, clock=clock, sleep=clock.sleep)
    if background:
        # Use the existing production-shaped receipt fixture to create a real
        # paid background craft and its admitted history in the checkpoint.
        from test_supervisor import background_checkpoint

        atomic_json(checkpoint, background_checkpoint(supervisor))
    supervisor.initialize()
    baseline_revision = supervisor.snapshot_revision(manual=True)
    assert baseline_revision is not None
    assert supervisor.record_revision(baseline_revision, "offline_fixture_baseline")
    return {
        "workspace": workspace, "checkpoint": checkpoint, "state_dir": state_dir,
        "config": config, "clock": clock, "supervisor": supervisor,
        "baseline_revision": baseline_revision,
        "checkpoint_bytes": checkpoint.read_bytes(),
        "run_id": supervisor.state["run_id"], "cutoff": supervisor.state["cutoff"],
    }


def add_unsupported_gitlink(fixture):
    head = git(fixture["workspace"], "rev-parse", "HEAD")
    git(fixture["workspace"], "update-index", "--add", "--cacheinfo",
        f"160000,{head},missing-submodule")
    assert fixture["supervisor"].snapshot_revision(manual=True) is None
    assert fixture["supervisor"].source_identity() is not None


def start_reconciliation(fixture):
    supervisor = fixture["supervisor"]
    reason = supervisor.watch_game()
    assert reason == "checkpoint_reconciliation"
    incident_id = supervisor.state["incident"]["incident_id"]
    return reason, incident_id


def test_unknown_source_repair_keeps_same_incident_and_attempt_budget_after_reload(tmp_path):
    fixture = make_fixture(tmp_path, name="unknown-budget", max_attempts=2)
    supervisor = fixture["supervisor"]
    add_unsupported_gitlink(fixture)
    reason, incident_id = start_reconciliation(fixture)
    baseline = fixture["baseline_revision"]

    assert supervisor.repair(reason) is False
    assert supervisor.validator_results == [False]
    assert supervisor.state["repair_required"] is True
    assert supervisor.state["incident"]["incident_id"] == incident_id
    assert supervisor.state["incident"]["code_revision"] == baseline
    assert supervisor.state["code_revision"] == baseline
    assert supervisor.state["incident_repair_attempts"] == 1
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]

    launches = supervisor.launches
    restored = OfflineSupervisor(
        fixture["config"], clock=fixture["clock"], sleep=fixture["clock"].sleep,
        launches=launches,
    )
    restored.initialize()
    fixture["supervisor"] = restored
    assert restored.state["incident"]["incident_id"] == incident_id
    assert restored.state["incident_repair_attempts"] == 1
    assert restored.state["code_revision"] == baseline

    assert restored.watch_game() == "checkpoint_reconciliation"
    assert restored.repair(reason) is False
    assert restored.state["incident"]["incident_id"] == incident_id
    assert restored.state["incident_repair_attempts"] == 2
    launches_before_quota = len(restored.launches)
    assert restored.repair(reason) is False
    assert len(restored.launches) == launches_before_quota
    assert restored.state["repair_required"] is True
    assert restored.state["incident"]["incident_id"] == incident_id
    assert restored.state["incident_repair_attempts"] == 2
    assert restored.state["phase"] == "blocked"
    assert restored.state["run_id"] == fixture["run_id"]
    assert restored.state["cutoff"] == fixture["cutoff"]
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]


def test_unknown_then_known_unchanged_source_closes_same_incident(tmp_path):
    fixture = make_fixture(tmp_path, name="unknown-to-known")
    supervisor = fixture["supervisor"]
    add_unsupported_gitlink(fixture)
    reason, incident_id = start_reconciliation(fixture)

    assert supervisor.repair(reason) is False
    assert supervisor.state["incident_repair_attempts"] == 1
    assert supervisor.state["code_revision"] == fixture["baseline_revision"]
    git(fixture["workspace"], "update-index", "--force-remove", "missing-submodule")
    assert supervisor.snapshot_revision(manual=True) == fixture["baseline_revision"]

    assert supervisor.repair(reason) is True
    assert supervisor.state["repair_required"] is False
    assert supervisor.state["incident"] is None
    assert supervisor.state["code_revision"] == fixture["baseline_revision"]
    assert supervisor.state["incident_repair_attempts"] == 2
    assert supervisor.state["run_id"] == fixture["run_id"]
    assert supervisor.state["cutoff"] == fixture["cutoff"]
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]
    repairs = [row for row in audit_events(supervisor)
               if row.get("event") == "repair_finished"]
    assert [row["attempt"] for row in repairs] == [1, 2]
    assert {row["incident_id"] for row in repairs} == {incident_id}
    assert repairs[0]["accepted"] is False and repairs[1]["accepted"] is True


def test_unknown_source_becoming_known_mid_attempt_requires_fresh_attempt(tmp_path):
    fixture = make_fixture(tmp_path, name="unknown-to-known-mid-attempt")
    supervisor = fixture["supervisor"]
    baseline = fixture["baseline_revision"]
    add_unsupported_gitlink(fixture)
    reason, incident_id = start_reconciliation(fixture)
    supervisor.before_validate = lambda: git(
        fixture["workspace"], "update-index", "--force-remove", "missing-submodule")

    # The provenance becomes known during this repair attempt. It cannot use
    # the identity captured while provenance was unknown; a fresh attempt may.
    assert supervisor.repair(reason) is False
    assert supervisor.validator_results == [False]
    assert supervisor.snapshot_revision(manual=True) == baseline
    assert supervisor.state["repair_required"] is True
    assert supervisor.state["incident"]["incident_id"] == incident_id
    assert supervisor.state["incident"]["code_revision"] == baseline
    assert supervisor.state["code_revision"] == baseline
    assert supervisor.state["incident_repair_attempts"] == 1
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]

    del supervisor.before_validate
    assert supervisor.repair(reason) is True
    assert supervisor.validator_results == [False, True]
    assert supervisor.state["repair_required"] is False
    assert supervisor.state["incident"] is None
    assert supervisor.state["code_revision"] == baseline
    assert supervisor.state["incident_repair_attempts"] == 2


def test_operational_repair_rejects_source_change_during_final_checkpoint_read(tmp_path):
    fixture = make_fixture(tmp_path, name="source-changes-during-final-checkpoint")
    supervisor = fixture["supervisor"]
    baseline = fixture["baseline_revision"]
    supervisor.begin_repair("checkpoint_reconciliation")
    incident_id = supervisor.state["incident"]["incident_id"]
    original_checkpoint = supervisor.checkpoint
    mutations = []

    def checkpoint_then_change_tracked_source():
        snapshot = original_checkpoint()
        if (supervisor._validated_repair_checkpoint_digest is not None
                and not mutations):
            (fixture["workspace"] / "source.py").write_text(
                "fixture_value = 2\n", encoding="utf-8")
            mutations.append("tracked_source_changed_during_final_checkpoint_read")
        return snapshot

    supervisor.checkpoint = checkpoint_then_change_tracked_source
    accepted = supervisor.repair("checkpoint_reconciliation")
    current_revision = supervisor.snapshot_revision(manual=True)

    assert mutations == ["tracked_source_changed_during_final_checkpoint_read"]
    assert supervisor.validator_results == [True]
    assert current_revision is not None and current_revision != baseline
    assert accepted is False
    assert supervisor.state["repair_required"] is True
    assert supervisor.state["incident"]["incident_id"] == incident_id
    assert supervisor.state["incident"]["code_revision"] == baseline
    assert supervisor.state["code_revision"] == current_revision
    assert supervisor.state["incident_repair_attempts"] == 1
    assert supervisor.state["run_id"] == fixture["run_id"]
    assert supervisor.state["cutoff"] == fixture["cutoff"]
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]
    assert CampaignMemory.load(
        fixture["checkpoint"], fixture["config"].session_id, "rocket_launch"
    ).pending == json.loads(fixture["checkpoint_bytes"])["pending"]


def test_operational_repair_rejects_checkpoint_change_during_final_source_read(tmp_path):
    fixture = make_fixture(tmp_path, name="checkpoint-changes-during-final-source")
    supervisor = fixture["supervisor"]
    baseline = fixture["baseline_revision"]
    supervisor.begin_repair("checkpoint_reconciliation")
    incident_id = supervisor.state["incident"]["incident_id"]
    original_checkpoint = supervisor.checkpoint
    original_snapshot_revision = supervisor.snapshot_revision
    first_final_checkpoint_read = []
    mutations = []

    def mark_first_final_checkpoint_read():
        snapshot = original_checkpoint()
        if (supervisor._validated_repair_checkpoint_digest is not None
                and not first_final_checkpoint_read):
            first_final_checkpoint_read.append(True)
        return snapshot

    def snapshot_then_replace_checkpoint(manual=False):
        revision = original_snapshot_revision(manual=manual)
        if first_final_checkpoint_read and not mutations:
            changed = json.loads(fixture["checkpoint"].read_text(encoding="utf-8"))
            changed["last_tick"] = changed["last_tick"] + 1
            atomic_json(fixture["checkpoint"], changed)
            mutations.append("checkpoint_replaced_during_final_source_read")
        return revision

    supervisor.checkpoint = mark_first_final_checkpoint_read
    supervisor.snapshot_revision = snapshot_then_replace_checkpoint
    accepted = supervisor.repair("checkpoint_reconciliation")
    current_revision = original_snapshot_revision(manual=True)
    current_checkpoint = CampaignMemory.load(
        fixture["checkpoint"], fixture["config"].session_id, "rocket_launch")

    assert first_final_checkpoint_read == [True]
    assert mutations == ["checkpoint_replaced_during_final_source_read"]
    assert supervisor.validator_results == [True]
    assert current_revision == baseline
    assert accepted is False
    assert supervisor.state["repair_required"] is True
    assert supervisor.state["incident"]["incident_id"] == incident_id
    assert supervisor.state["incident"]["code_revision"] == baseline
    assert supervisor.state["code_revision"] == baseline
    assert supervisor.state["incident_repair_attempts"] == 1
    assert supervisor.state["run_id"] == fixture["run_id"]
    assert supervisor.state["cutoff"] == fixture["cutoff"]
    assert current_checkpoint.last_tick == 2
    assert current_checkpoint.pending == json.loads(fixture["checkpoint_bytes"])["pending"]


def test_unknown_then_changed_known_source_remains_open(tmp_path):
    fixture = make_fixture(tmp_path, name="unknown-to-changed")
    supervisor = fixture["supervisor"]
    add_unsupported_gitlink(fixture)
    reason, incident_id = start_reconciliation(fixture)
    assert supervisor.repair(reason) is False
    git(fixture["workspace"], "update-index", "--force-remove", "missing-submodule")
    (fixture["workspace"] / "changed.py").write_text("changed = True\n", encoding="utf-8")
    git(fixture["workspace"], "add", "changed.py")
    assert supervisor.snapshot_revision(manual=True) != fixture["baseline_revision"]

    assert supervisor.repair(reason) is False
    assert supervisor.state["repair_required"] is True
    assert supervisor.state["incident"]["incident_id"] == incident_id
    assert supervisor.state["incident"]["code_revision"] == fixture["baseline_revision"]
    assert supervisor.state["code_revision"] == supervisor.snapshot_revision(manual=True)
    assert supervisor.state["incident_repair_attempts"] == 2
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]


def test_unknown_source_preserves_paid_background_receipt_history_on_reload(tmp_path):
    fixture = make_fixture(tmp_path, name="unknown-background", background=True)
    supervisor = fixture["supervisor"]
    original = json.loads(fixture["checkpoint_bytes"])
    assert original["background_job"] is not None
    assert original["background_attempt"] is not None
    assert any(row.get("kind") == "background_job_admitted" for row in original["history"])
    add_unsupported_gitlink(fixture)
    reason, incident_id = start_reconciliation(fixture)

    assert supervisor.repair(reason) is False
    assert supervisor.state["incident"]["incident_id"] == incident_id
    assert supervisor.state["incident_repair_attempts"] == 1
    assert supervisor.state["code_revision"] == fixture["baseline_revision"]
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]

    restored = OfflineSupervisor(fixture["config"], clock=fixture["clock"],
                                 sleep=fixture["clock"].sleep,
                                 launches=supervisor.launches)
    restored.initialize()
    from jev_factorio.memory import checkpoint_memory_type, load_checkpoint_data

    loaded = load_checkpoint_data(
        original, fixture["config"].session_id, "rocket_launch",
        memory_type=checkpoint_memory_type(original))
    assert loaded.background_job == original["background_job"]
    assert loaded.background_attempt == original["background_attempt"]
    assert loaded.history == original["history"]
    assert restored.state["incident"]["incident_id"] == incident_id
    assert restored.state["incident_repair_attempts"] == 1
    assert restored.state["code_revision"] == fixture["baseline_revision"]
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]


def test_unknown_interrupted_attempt_preserves_trusted_revision_and_quota_after_reload(tmp_path):
    fixture = make_fixture(tmp_path, name="unknown-interrupted", max_attempts=2)
    supervisor = fixture["supervisor"]
    baseline = fixture["baseline_revision"]
    add_unsupported_gitlink(fixture)
    reason, incident_id = start_reconciliation(fixture)

    attempt = supervisor.state["attempt"] + 1
    source_before = supervisor.snapshot_revision()
    identity = supervisor.source_identity()
    identity_digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert supervisor.transition("repair_started", {
        "attempt": attempt,
        "repair_attempt_open": True,
        "repair_budget_incident_id": incident_id,
        "incident_repair_attempts": 1,
        "attempt_incident_id": incident_id,
        "attempt_source_before": source_before,
        "attempt_source_identity_sha256": identity_digest,
    }, attempt=attempt, actor_type="repair_agent", source_before=source_before,
        attempt_source_identity_sha256=identity_digest)
    assert supervisor.state["repair_attempt_open"] is True

    # Restart through the public initializer. It closes the durable open
    # attempt, but an unknown source must not erase the last trusted revision.
    restored = OfflineSupervisor(fixture["config"], clock=fixture["clock"],
                                 sleep=fixture["clock"].sleep,
                                 launches=supervisor.launches)
    restored.initialize()
    fixture["supervisor"] = restored

    assert restored.state["repair_attempt_open"] is False
    assert restored.state["repair_required"] is True
    assert restored.state["incident"]["incident_id"] == incident_id
    assert restored.state["incident"]["code_revision"] == baseline
    assert restored.state["code_revision"] == baseline
    assert restored.state["attempt_source_identity_sha256"] is None
    assert restored.state["incident_repair_attempts"] == 1
    assert restored.state["run_id"] == fixture["run_id"]
    assert restored.state["cutoff"] == fixture["cutoff"]
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]
    interrupted = [row for row in audit_events(restored)
                   if row.get("event") == "repair_interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["incident_id"] == incident_id
    assert interrupted[0]["source_after"] is None
    assert interrupted[0]["attempt_source_identity_sha256"] == identity_digest

    assert restored.watch_game() == reason
    assert restored.repair(reason) is False
    assert restored.state["incident"]["incident_id"] == incident_id
    assert restored.state["incident"]["code_revision"] == baseline
    assert restored.state["code_revision"] == baseline
    assert restored.state["incident_repair_attempts"] == 2
    launches_before_quota = len(restored.launches)
    assert restored.repair(reason) is False
    assert len(restored.launches) == launches_before_quota
    assert restored.state["incident"]["incident_id"] == incident_id
    assert restored.state["incident_repair_attempts"] == 2
    assert fixture["checkpoint"].read_bytes() == fixture["checkpoint_bytes"]


def test_known_unchanged_operational_recovery_remains_supported(tmp_path):
    fixture = make_fixture(tmp_path, name="known-positive")
    supervisor = fixture["supervisor"]
    supervisor.begin_repair("checkpoint_reconciliation")
    incident_id = supervisor.state["incident"]["incident_id"]

    assert supervisor.repair("checkpoint_reconciliation") is True
    assert supervisor.state["repair_required"] is False
    assert supervisor.state["incident"] is None
    assert supervisor.state["code_revision"] == fixture["baseline_revision"]
    assert supervisor.state["run_id"] == fixture["run_id"]
    assert supervisor.state["cutoff"] == fixture["cutoff"]
    rows = [row for row in audit_events(supervisor)
            if row.get("event") == "repair_finished"]
    assert rows[-1]["accepted"] is True and rows[-1]["incident_id"] == incident_id


def audit_events(supervisor):
    return [json.loads(line) for line in
            (supervisor.config.state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
