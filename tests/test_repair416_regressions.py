"""Issue #416 regressions through real local Git and supervisor gates.

All GitHub/review/child responses are deterministic local doubles.  No hosted,
provider, game, or native process is contacted.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from jev_factorio import supervisor as supervisor_module
from jev_factorio import provenance as provenance_module
from jev_factorio.memory import CampaignMemory
from jev_factorio.provenance import source_revision
from jev_factorio.supervisor import Supervisor, SupervisorConfig, atomic_json
from test_supervisor import FakeClock, FakeProcess


PR_URL = "https://github.com/jevplays-games/jev-factorio-agent/pull/41"
OWNER_URL = "https://github.com/jevplays-games/jev-factorio-agent.git"


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, input=input_text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return completed.stdout.strip()


def _repo(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "repair416@example.invalid")
    _git(root, "config", "user.name", "Repair 416 Test")
    (root / "src.py").write_text("value = 1\n", encoding="utf-8")
    (root / "ordinary.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "src.py", "ordinary.txt")
    _git(root, "commit", "-qm", "baseline")
    return root, _git(root, "rev-parse", "HEAD")


def _instance(root: Path, *, checkpoint: Path | None = None,
              background_work: bool = False) -> Supervisor:
    runtime = root / ".supervisor"
    runtime.mkdir(exist_ok=True)
    checkpoint = checkpoint or (root / "campaign.json")
    if not checkpoint.exists():
        atomic_json(checkpoint, asdict(CampaignMemory(
            "repair-session", "rocket_launch", status="running")))
    clock = FakeClock()
    config = SupervisorConfig(
        state_dir=runtime, checkpoint=checkpoint, session_id="repair-session",
        started_at=clock.now, repair_command=["offline-repair"], cwd=root,
        duration_hours=1, poll_seconds=1, repair_seconds=10,
        factory_scheduling="ready-work" if background_work else "serial",
        background_work=background_work,
    )
    instance = Supervisor(config, clock=clock, sleep=clock.sleep,
                          popen=lambda *args, **kwargs: FakeProcess(0))
    instance.kill_group = lambda pid: None
    instance.lock_path = lambda: runtime / "supervisor.lock"
    instance.source_identity = lambda: ("incident-source", "incident-diff")
    instance.initialize(record_only=True)
    return instance


def _pull(commit: str) -> dict:
    return {
        "url": PR_URL,
        "baseRefName": "main",
        "state": "MERGED",
        "mergeCommit": {"oid": commit},
        "headRefOid": "c" * 40,
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }


def _review_payload(commit: str) -> str:
    pull = _pull(commit)
    current = {
        "number": 41,
        "url": PR_URL,
        "baseRefName": "main",
        "state": "MERGED",
        "headRefOid": pull["headRefOid"],
        "reviewDecision": "APPROVED",
        "mergeCommit": {"oid": commit},
        "latestOpinionatedReviews": {
            "nodes": [{"state": "APPROVED", "author": {"login": "reviewer"},
                       "commit": {"oid": pull["headRefOid"]}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }
    return json.dumps({"data": {"repository": {"pullRequest": current}}})


def _install_gate_capture(instance: Supervisor, root: Path, commit: str,
                          during_tests=None) -> list[list[str]]:
    calls: list[list[str]] = []
    pull = _pull(commit)

    def capture(command: list[str]) -> tuple[int, str]:
        calls.append(command)
        if command == ["git", "rev-parse", "HEAD"]:
            return 0, commit
        if command == ["git", "status", "--porcelain"]:
            return 0, ""
        if command == ["git", "remote"]:
            return 0, "origin"
        if command == ["git", "remote", "get-url", "--all", "origin"]:
            return 0, OWNER_URL
        if command == ["git", "remote", "get-url", "--push", "--all", "origin"]:
            return 0, OWNER_URL
        if command == ["git", "ls-remote", "origin", "refs/heads/main"]:
            return 0, f"{commit}\trefs/heads/main"
        if command[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps(pull)
        if command[:3] == ["gh", "api", "graphql"]:
            return 0, _review_payload(commit)
        if command[1:] == ["-m", "pytest", "tests/"]:
            if during_tests is not None:
                during_tests()
            return 0, "offline test control passed"
        pytest.fail(f"unexpected verification command: {command}")

    instance.capture = capture
    return calls


def _code_report(instance: Supervisor, commit: str) -> dict:
    return {
        "status": "repaired", "kind": "code", "session_id": instance.config.session_id,
        "checkpoint": str(instance.config.checkpoint.resolve()),
        "run_id": instance.state["run_id"],
        "incident_id": instance.state["incident"]["incident_id"],
        "attempt": instance.state["attempt"], "commit": commit, "pr_url": PR_URL,
        "tests_passed": True, "checks_passed": True,
        "exact_head_reviewed": True, "merged": True, "remotes_synced": True,
        "evidence": ["deterministic local Git and gate controls"],
    }


def _run_code_repair(tmp_path: Path, monkeypatch, *, mutate_after_validation=None,
                     during_tests=None, fault=None, background_owner=False,
                     after_final_checkpoint_read=None,
                     during_final_source_read=None):
    root, commit = _repo(tmp_path / "checkout")
    instance = _instance(root, background_work=background_owner)
    if background_owner:
        from test_supervisor import background_checkpoint
        retained_checkpoint = background_checkpoint(instance)
        atomic_json(instance.config.checkpoint, retained_checkpoint)
    else:
        retained_checkpoint = instance.checkpoint()
    _install_gate_capture(instance, root, commit, during_tests)
    real_validate = instance.validate_repair

    def launch(command, phase, prompt=None):
        assert phase == "repair"
        result = instance.config.state_dir / "repair-1.json"
        atomic_json(result, _code_report(instance, commit))
        instance.process = FakeProcess(0)

    instance.launch = launch
    if mutate_after_validation is not None:
        def validate_then_mutate(*args, **kwargs):
            valid = real_validate(*args, **kwargs)
            if valid:
                mutate_after_validation(root)
            return valid
        instance.validate_repair = validate_then_mutate
    if after_final_checkpoint_read is not None or during_final_source_read is not None:
        real_checkpoint = instance.checkpoint
        after_final_checkpoint = False
        changed_checkpoint_once = False

        def checkpoint_with_final_boundary_hook():
            nonlocal after_final_checkpoint
            value = real_checkpoint()
            if (instance._validated_repair_source is not None
                    and not after_final_checkpoint):
                after_final_checkpoint = True
                if after_final_checkpoint_read is not None:
                    after_final_checkpoint_read(root, instance)
            return value

        instance.checkpoint = checkpoint_with_final_boundary_hook
        if during_final_source_read is not None:
            real_path_open = Path.open

            class CheckpointChangingReader:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def read(self, *args, **kwargs):
                    nonlocal changed_checkpoint_once
                    data = self.stream.read(*args, **kwargs)
                    if data and after_final_checkpoint and not changed_checkpoint_once:
                        changed_checkpoint_once = True
                        during_final_source_read(root, instance)
                    return data

            def path_open_with_checkpoint_race(path, *args, **kwargs):
                stream = real_path_open(path, *args, **kwargs)
                mode = args[0] if args else kwargs.get("mode", "r")
                if (after_final_checkpoint and not changed_checkpoint_once
                        and path == root / "src.py" and mode == "rb"):
                    return CheckpointChangingReader(stream)
                return stream

            monkeypatch.setattr(Path, "open", path_open_with_checkpoint_race)
    if fault is not None:
        real_atomic_json = supervisor_module.atomic_json
        real_append_audit = supervisor_module.append_audit
        real_fsync = supervisor_module.os.fsync
        acceptance = {"event_id": None, "sequence": None, "failed": False}

        def event_is_acceptance(record):
            return isinstance(record, dict) and record.get("repair_finished") is True \
                and record.get("accepted") is True

        def atomic_json_with_fault(path, value):
            pending = value.get("audit_pending") if isinstance(value, dict) else None
            if path == instance.state_path and event_is_acceptance(pending):
                acceptance["event_id"] = pending["event_id"]
                acceptance["sequence"] = pending["sequence"]
                if fault == "state_save" and not acceptance["failed"]:
                    acceptance["failed"] = True
                    raise OSError("injected accepted-state save failure")
            if (path == instance.state_path and fault == "outbox_clear"
                    and acceptance["event_id"] is not None and not value.get("audit_pending")
                    and value.get("audit_sequence") == acceptance["sequence"]
                    and not acceptance["failed"]):
                acceptance["failed"] = True
                raise OSError("injected outbox clear save failure")
            return real_atomic_json(path, value)

        def append_with_fault(path, record):
            if event_is_acceptance(record) and fault == "append_before" and not acceptance["failed"]:
                acceptance["failed"] = True
                raise OSError("injected pre-append failure")
            result = real_append_audit(path, record)
            if event_is_acceptance(record) and fault == "append_after" and not acceptance["failed"]:
                acceptance["failed"] = True
                raise OSError("injected post-fsync append failure")
            return result

        def fsync_with_fault(fd):
            if fault in {"state_fsync", "audit_fsync", "outbox_clear_fsync"} \
                    and not acceptance["failed"] and Path("/proc/self/fd").exists():
                try:
                    fd_path = os.readlink(f"/proc/self/fd/{fd}")
                except OSError:
                    fd_path = ""
                pending = instance.state.get("audit_pending")
                if (fault == "state_fsync" and fd_path.endswith("supervisor.json.tmp")
                        and event_is_acceptance(pending)):
                    acceptance["failed"] = True
                    raise OSError("injected state-file fsync failure")
                if (fault == "audit_fsync" and fd_path.endswith("events.jsonl")
                        and event_is_acceptance(pending)):
                    acceptance["failed"] = True
                    raise OSError("injected audit-file fsync failure")
                if (fault == "outbox_clear_fsync" and fd_path.endswith("supervisor.json.tmp")
                        and acceptance["event_id"] is not None and pending is None
                        and instance.state.get("audit_sequence") == acceptance["sequence"]):
                    acceptance["failed"] = True
                    raise OSError("injected outbox-clear state fsync failure")
            return real_fsync(fd)

        monkeypatch.setattr(supervisor_module, "atomic_json", atomic_json_with_fault)
        monkeypatch.setattr(supervisor_module, "append_audit", append_with_fault)
        if fault in {"state_fsync", "audit_fsync", "outbox_clear_fsync"}:
            monkeypatch.setattr(supervisor_module.os, "fsync", fsync_with_fault)
    accepted = instance.repair("blocked")
    return instance, root, commit, accepted, fault, retained_checkpoint


def test_real_code_repair_accepts_unchanged_validated_checkout(tmp_path, monkeypatch):
    instance, _, commit, accepted, _, retained = _run_code_repair(
        tmp_path, monkeypatch, background_owner=True)
    assert accepted
    assert instance.state["repair_required"] is False
    assert instance.state["incident"] is None
    assert instance.state["code_revision"]["commit"] == commit
    assert instance.state["last_valid_checkpoint"]["background_job"] \
        == retained["background_job"]
    records = [json.loads(row) for row in
               (instance.config.state_dir / "events.jsonl").read_text().splitlines()]
    acceptance_rows = [row for row in records if row.get("repair_finished") is True]
    assert len(acceptance_rows) == 1
    event = acceptance_rows[0]
    assert event["schema"] == "jev-factorio.supervisor-event.v1"
    assert event["accepted"] is True and event["repair_finished"] is True
    assert event["declared_kind"] == "code"
    assert event["intervention_type"] == "code_repair"
    assert event["event"] in {"segment_started", "code_revision_changed"}
    assert event["segment_id"] == instance.state["segment_id"]
    assert event["sequence"] == instance.state["audit_sequence"]
    assert event["source_after"] == instance.state["code_revision"]
    assert event["source_after"] == instance._validated_repair_source
    assert event["source_after"]["commit"] == commit
    assert event["incident_id"] and event["attempt"] == instance.state["attempt"]
    assert event["result_sha256"] and event["correlation_complete"] is True
    assert instance.state["audit_pending"] is None
    assert instance.state["repair_required"] is False and instance.state["incident"] is None


@pytest.mark.parametrize("race", ["tracked", "untracked", "index", "head"])
def test_real_code_repair_rejects_post_verification_source_races(
        tmp_path, monkeypatch, race):
    def mutate(root: Path):
        if race == "tracked":
            (root / "src.py").write_text("value = 2\n", encoding="utf-8")
        elif race == "untracked":
            (root / "unexpected.py").write_text("untracked = True\n", encoding="utf-8")
        elif race == "index":
            blob = _git(root, "rev-parse", "HEAD:ordinary.txt")
            _git(root, "update-index", "--index-info",
                 input_text=f"100644 {blob} 1\tordinary.txt\n")
        else:
            (root / "head-only.txt").write_text("new head\n", encoding="utf-8")
            _git(root, "add", "head-only.txt")
            _git(root, "commit", "-qm", "post-validation head race")

    instance, _, _, accepted, _, retained = _run_code_repair(
        tmp_path, monkeypatch, mutate_after_validation=mutate, background_owner=True)
    assert not accepted, race
    assert instance.state["repair_required"] is True
    assert instance.state["incident"] is not None
    assert instance.state["incident"]["checkpoint"]["background_job"] \
        == retained["background_job"]
    assert instance.state["repair_attempt_open"] is False
    records = [json.loads(row) for row in
               (instance.config.state_dir / "events.jsonl").read_text().splitlines()]
    assert not any(row.get("accepted") is True and row.get("intervention_type") == "code_repair"
                   for row in records)


def test_real_verifier_rechecks_source_after_its_test_gate(tmp_path):
    root, commit = _repo(tmp_path / "checkout")
    instance = _instance(root)

    def mutate_during_tests():
        (root / "generated.py").write_text("arrived during tests\n", encoding="utf-8")

    _install_gate_capture(instance, root, commit, mutate_during_tests)
    assert not instance.verify_code({"commit": commit, "pr_url": PR_URL})
    assert getattr(instance, "_verified_code_revision", None) is None


def test_final_checkpoint_replacement_prevents_accepted_attribution(tmp_path, monkeypatch):
    def replace_with_invalid(root: Path):
        (root / "campaign.json").write_text("{replacement", encoding="utf-8")

    instance, _, _, accepted, _, retained = _run_code_repair(
        tmp_path, monkeypatch, mutate_after_validation=replace_with_invalid,
        background_owner=True)
    assert not accepted
    assert instance.state["repair_required"] is True
    assert instance.state["incident"]["checkpoint"]["background_job"] \
        == retained["background_job"]
    records = [json.loads(row) for row in
               (instance.config.state_dir / "events.jsonl").read_text().splitlines()]
    assert not any(row.get("accepted") is True and row.get("intervention_type") == "code_repair"
                   for row in records)


def test_source_change_during_final_checkpoint_read_prevents_acceptance(tmp_path, monkeypatch):
    def change_source_after_read(root: Path, _instance: Supervisor):
        (root / "src.py").write_text("value = 9\n", encoding="utf-8")

    instance, _, _, accepted, _, retained = _run_code_repair(
        tmp_path, monkeypatch, after_final_checkpoint_read=change_source_after_read,
        background_owner=True)
    assert not accepted
    assert instance.state["repair_required"] is True
    assert instance.state["incident"] is not None
    assert instance.state["incident"]["checkpoint"]["background_job"] \
        == retained["background_job"]
    assert instance.state["code_revision"] == instance.snapshot_revision(manual=True)
    assert instance.state["code_revision"] != instance._validated_repair_source
    records = [json.loads(row) for row in
               (instance.config.state_dir / "events.jsonl").read_text().splitlines()]
    assert not any(row.get("accepted") is True and row.get("intervention_type") == "code_repair"
                   for row in records)


def test_checkpoint_change_during_final_source_read_prevents_acceptance(tmp_path, monkeypatch):
    def change_checkpoint_during_source_read(_root: Path, instance: Supervisor):
        path = instance.config.checkpoint
        path.write_bytes(path.read_bytes() + b" ")

    instance, _, _, accepted, _, retained = _run_code_repair(
        tmp_path, monkeypatch, during_final_source_read=change_checkpoint_during_source_read,
        background_owner=True)
    assert not accepted
    assert instance.state["repair_required"] is True
    assert instance.state["incident"] is not None
    assert instance.state["incident"]["checkpoint"]["background_job"] \
        == retained["background_job"]
    records = [json.loads(row) for row in
               (instance.config.state_dir / "events.jsonl").read_text().splitlines()]
    assert not any(row.get("accepted") is True and row.get("intervention_type") == "code_repair"
                   for row in records)


def test_checkpoint_reader_rejects_atomic_path_replacement_during_read(tmp_path, monkeypatch):
    root, _ = _repo(tmp_path / "checkout")
    instance = _instance(root)
    path = instance.config.checkpoint
    original_open = Path.open
    replaced = False

    def open_then_replace(candidate, *args, **kwargs):
        nonlocal replaced
        stream = original_open(candidate, *args, **kwargs)
        if candidate == path and args and args[0] == "rb" and not replaced:
            replacement = root / "replacement.json"
            replacement.write_bytes(path.read_bytes() + b" ")
            replacement.replace(path)
            replaced = True
        return stream

    monkeypatch.setattr(Path, "open", open_then_replace)
    with pytest.raises(ValueError, match="changed during read"):
        instance.checkpoint()


@pytest.mark.parametrize("fault", [
    "state_save", "state_fsync", "append_before", "audit_fsync", "append_after",
    "outbox_clear", "outbox_clear_fsync",
])
def test_acceptance_outbox_faults_reload_to_one_truthful_state(tmp_path, monkeypatch, fault):
    real_atomic_json = supervisor_module.atomic_json
    real_append_audit = supervisor_module.append_audit
    real_fsync = supervisor_module.os.fsync
    instance, _, commit, accepted, _, retained = _run_code_repair(
        tmp_path, monkeypatch, fault=fault, background_owner=True)
    assert not accepted

    monkeypatch.setattr(supervisor_module, "atomic_json", real_atomic_json)
    monkeypatch.setattr(supervisor_module, "append_audit", real_append_audit)
    monkeypatch.setattr(supervisor_module.os, "fsync", real_fsync)
    persisted = json.loads(instance.state_path.read_text(encoding="utf-8"))
    cutoff = persisted["cutoff"]
    attempt_count = persisted.get("incident_repair_attempts", 0)
    pending_before_reload = persisted.get("audit_pending")
    original_incident_id = ((persisted.get("incident") or {}).get("incident_id")
                            or (pending_before_reload or {}).get("incident_id"))
    assert original_incident_id
    if fault in {"state_save", "state_fsync"}:
        assert persisted["repair_required"] is True
        assert persisted["incident"] is not None
        assert persisted["incident"]["checkpoint"]["background_job"] \
            == retained["background_job"]
        assert persisted.get("audit_pending") is None
        before_rows = (instance.config.state_dir / "events.jsonl").read_text().splitlines()
        assert not any(json.loads(row).get("repair_finished") is True for row in before_rows)
    else:
        pending = persisted.get("audit_pending")
        assert pending is not None and pending["repair_finished"] is True
        assert pending["accepted"] is True
        if fault in {"append_after", "outbox_clear", "outbox_clear_fsync"}:
            before_rows = (instance.config.state_dir / "events.jsonl").read_text().splitlines()
            assert any(json.loads(row).get("event_id") == pending["event_id"]
                       for row in before_rows)

    restarted = Supervisor(instance.config, clock=FakeClock(), sleep=lambda seconds: None)
    restarted.initialize(record_only=True)
    assert restarted.state["cutoff"] == cutoff
    assert restarted.state.get("incident_repair_attempts", 0) == attempt_count == 1
    rows = [json.loads(row) for row in
            (instance.config.state_dir / "events.jsonl").read_text().splitlines()]
    accepted_rows = [row for row in rows
                     if row.get("repair_finished") is True and row.get("accepted") is True]
    if fault in {"state_save", "state_fsync"}:
        assert restarted.state["repair_required"] is True
        assert restarted.state["incident"] is not None
        assert restarted.state["incident"]["incident_id"] == original_incident_id
        assert restarted.state["incident"]["checkpoint"]["background_job"] \
            == retained["background_job"]
        assert accepted_rows == []
    else:
        assert restarted.state["repair_required"] is False
        assert restarted.state["incident"] is None
        assert restarted.state["last_valid_checkpoint"]["background_job"] \
            == retained["background_job"]
        assert restarted.state.get("audit_pending") is None
        assert len(accepted_rows) == 1
        event = accepted_rows[0]
        assert event["schema"] == "jev-factorio.supervisor-event.v1"
        assert event["event"] in {
            "segment_started", "code_revision_changed", "source_provenance_changed"
        }
        assert event["repair_finished"] is True and event["accepted"] is True
        assert event["intervention_type"] == "code_repair"
        assert event["run_id"] == restarted.state["run_id"]
        assert event["incident_id"] == original_incident_id
        assert event["attempt"] == 1 and event["correlation_complete"] is True
        assert event["segment_id"] == restarted.state["segment_id"]
        assert event["sequence"] == restarted.state["audit_sequence"]
        assert event["source_after"] == restarted.state["code_revision"]
        assert event["source_after"]["commit"] == commit
        assert event["result_sha256"]
        if pending_before_reload is not None:
            assert event["event_id"] == pending_before_reload["event_id"]
            assert event["sequence"] == pending_before_reload["sequence"]


@pytest.mark.parametrize("field", ["state_dir", "checkpoint"])
@pytest.mark.parametrize("exclusion", ["root", "ancestor", "normalized", "symlink"])
def test_config_rejects_runtime_exclusions_that_cover_checkout(tmp_path, field, exclusion):
    root, _ = _repo(tmp_path / "checkout")
    runtime = root / ".supervisor"
    checkpoint = root / "campaign.json"
    if exclusion == "root":
        value = root
    elif exclusion == "ancestor":
        value = root.parent
    elif exclusion == "normalized":
        value = root / "subdir" / ".." / ".." / root.name
    else:
        alias = root / "checkout-alias"
        alias.symlink_to(root, target_is_directory=True)
        value = alias
    config = SupervisorConfig(
        state_dir=value if field == "state_dir" else runtime,
        checkpoint=value if field == "checkpoint" else checkpoint,
        session_id="s", started_at=1,
        repair_command=["repair"], cwd=root,
    )
    with pytest.raises(ValueError, match="runtime exclusion"):
        config.validate()


def test_checkpoint_siblings_are_excluded_but_tracked_files_are_fingerprinted(tmp_path):
    root, _ = _repo(tmp_path / "checkout")
    runtime = root / ".supervisor"
    runtime.mkdir()
    checkpoint = root / "campaign.json"
    atomic_json(checkpoint, asdict(CampaignMemory("repair-session", "rocket_launch")))
    instance = _instance(root, checkpoint=checkpoint)
    before = instance.snapshot_revision(manual=True)
    (runtime / "events.jsonl").write_text("runtime output\n", encoding="utf-8")
    (root / "campaign.json.tmp").write_text("temporary checkpoint\n", encoding="utf-8")
    assert instance.snapshot_revision(manual=True) == before
    (runtime / "ordinary.txt").write_text("changed tracked source\n", encoding="utf-8")
    _git(root, "add", "-f", ".supervisor/ordinary.txt")
    assert instance.snapshot_revision(manual=True) != before


def test_snapshot_rejects_runtime_symlink_retargeted_to_checkout_ancestor(tmp_path):
    root, _ = _repo(tmp_path / "checkout")
    target = root / "runtime-target"
    target.mkdir()
    link = root / "runtime-link"
    link.symlink_to(target, target_is_directory=True)
    instance = _instance(root)
    instance.config.state_dir = link
    assert instance.snapshot_revision(manual=True) is not None
    link.unlink()
    link.symlink_to(root.parent, target_is_directory=True)
    assert instance.snapshot_revision(manual=True) is None


def test_absent_and_materialized_gitlinks_are_unknown_but_missing_files_are_known(tmp_path):
    root, _ = _repo(tmp_path / "checkout")
    before = source_revision(root)
    assert before is not None
    (root / "ordinary.txt").unlink()
    missing_ordinary = source_revision(root)
    assert missing_ordinary is not None and missing_ordinary != before
    blob = "1" * 40
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{blob},vendor/submodule")
    assert source_revision(root) is None
    (root / "vendor" / "submodule").mkdir(parents=True)
    assert source_revision(root) is None


def test_external_checkpoint_prefix_that_covers_checkout_fails_closed(tmp_path):
    root, _ = _repo(tmp_path / "campaign.runtime")
    checkpoint = root.parent / "campaign"
    state_dir = root / ".supervisor"
    state_dir.mkdir()
    prefix = checkpoint.with_name(checkpoint.name + ".")
    config = SupervisorConfig(
        state_dir=state_dir, checkpoint=checkpoint, session_id="repair-session",
        started_at=1000.0, repair_command=["offline-repair"], cwd=root,
    )
    config.validate()
    instance = Supervisor(config, clock=lambda: 1000.0)
    unexcluded_before = source_revision(root)
    assert unexcluded_before is not None
    before = source_revision(root, exclude_untracked=(checkpoint,),
                             exclude_untracked_prefixes=(prefix,))
    assert before is None
    assert instance.snapshot_revision(manual=True) is None

    (root / "new_executable.py").write_text("new_source = True\n", encoding="utf-8")
    direct_after = source_revision(root, exclude_untracked=(checkpoint,),
                                   exclude_untracked_prefixes=(prefix,))
    snapshot_after = instance.snapshot_revision(manual=True)
    unexcluded_after = source_revision(root)

    assert unexcluded_after is not None and unexcluded_after != unexcluded_before
    assert direct_after is None
    assert snapshot_after is None


def test_safe_external_checkpoint_does_not_disable_source_snapshot(tmp_path):
    root, _ = _repo(tmp_path / "checkout")
    checkpoint = tmp_path / "campaign.json"
    config = SupervisorConfig(
        state_dir=root / ".supervisor", checkpoint=checkpoint,
        session_id="repair-session", started_at=1000.0,
        repair_command=["offline-repair"], cwd=root,
    )
    config.validate()
    instance = Supervisor(config, clock=lambda: 1000.0)
    before = instance.snapshot_revision(manual=True)
    assert before is not None

    (root / "new_executable.py").write_text("new_source = True\n", encoding="utf-8")
    after = instance.snapshot_revision(manual=True)
    assert after is not None and after != before


def test_prefix_cover_check_uses_platform_case_normalization(tmp_path, monkeypatch):
    root, _ = _repo(tmp_path / "Campaign.runtime")
    prefix = tmp_path / "campaign."
    monkeypatch.setattr(provenance_module.os.path, "normcase",
                        lambda path: os.fspath(path).casefold())
    assert source_revision(root, exclude_untracked_prefixes=(prefix,)) is None
