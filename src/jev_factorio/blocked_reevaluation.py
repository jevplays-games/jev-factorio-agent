"""Fail-closed preflight for one explicit blocked-decision re-evaluation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Candidate generation includes the composed planner and its demand, fuel,
# ownership, route and evidence helpers. Keep this explicit and blob-bound: a
# Git commit or unrelated file change must never grant a new evaluation.
_CONTRACT_PATHS = (
    "src/jev_factorio/judgments.py",
    "src/jev_factorio/planning/decision_support.py",
    "src/jev_factorio/planning/mining_outposts.py",
    "src/jev_factorio/planning/background_work.py",
    "src/jev_factorio/planning/capacity_evidence.py",
    "src/jev_factorio/planning/capital.py",
    "src/jev_factorio/planning/catalog.py",
    "src/jev_factorio/planning/coal_admission.py",
    "src/jev_factorio/planning/coal_current_evidence.py",
    "src/jev_factorio/planning/coal_economic_binding.py",
    "src/jev_factorio/planning/coal_economic_proof.py",
    "src/jev_factorio/planning/coal_economics.py",
    "src/jev_factorio/planning/coal_funding.py",
    "src/jev_factorio/planning/coal_goal_alignment.py",
    "src/jev_factorio/planning/coal_supply.py",
    "src/jev_factorio/planning/connection_identity.py",
    "src/jev_factorio/planning/connections.py",
    "src/jev_factorio/planning/demand.py",
    "src/jev_factorio/planning/economics.py",
    "src/jev_factorio/planning/factory.py",
    "src/jev_factorio/planning/fuel_failure_budget.py",
    "src/jev_factorio/planning/fuel_history.py",
    "src/jev_factorio/planning/fuel_service.py",
    "src/jev_factorio/planning/goals.py",
    "src/jev_factorio/planning/input_routes.py",
    "src/jev_factorio/planning/launch.py",
    "src/jev_factorio/planning/materials.py",
    "src/jev_factorio/planning/output_buffers.py",
    "src/jev_factorio/planning/productive_work.py",
    "src/jev_factorio/planning/ready_work.py",
    "src/jev_factorio/planning/research_trigger.py",
    "src/jev_factorio/planning/scheduling.py",
    "src/jev_factorio/planning/service_policy.py",
    "src/jev_factorio/planning/service_visits.py",
    "src/jev_factorio/planning/solid_funding.py",
    "src/jev_factorio/planning/solid_investment.py",
    "src/jev_factorio/planning/solid_routes.py",
    "src/jev_factorio/planning/successors.py",
)
# Absent in historical revisions. Omitting absent optional paths preserves the
# exact historical digest; present modules are fully blob-bound for recovery.
_OPTIONAL_CONTRACT_PATHS = (
    "src/jev_factorio/planning/buffer_demand.py",
    "src/jev_factorio/two_stage_decision.py",
    "src/jev_factorio/two_stage_controller.py",
)
_BLOCKED_REASONS = {
    "Candidate evidence insufficient", "low choice confidence",
    # A reviewed planner contract change may repair this exact idle failure.
    # This does not make it eligible for unchanged automatic polling/retries.
    "Current native boiler identity and coal stock are required",
    "Furnace fuel service requires current owned source identity",
    "Native buffer component dependency cycle",
}


def _git(root: Path, *args: str) -> bytes:
    """Run a read-only Git query without inherited repository/index controls."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_COUNT"] = "2"
    env["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
    env["GIT_CONFIG_VALUE_0"] = "false"
    env["GIT_CONFIG_KEY_1"] = "core.untrackedCache"
    env["GIT_CONFIG_VALUE_1"] = "false"
    command = ["git", "--no-optional-locks", "-c", f"safe.directory={root}",
               "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-C", os.fspath(root), *args]
    return subprocess.run(command, cwd=root, env=env, check=True,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=10).stdout


def _contract_sha256(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256(b"jev-factorio.blocked-decision-contract.v1\0")
    for name in (*_CONTRACT_PATHS, *_OPTIONAL_CONTRACT_PATHS):
        if name not in files and name in _OPTIONAL_CONTRACT_PATHS:
            continue
        data = files[name]
        encoded_name = name.encode("ascii")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def validate_source_revision(blocked_source_revision: str,
                             root: Path | None = None, *,
                             require_changed_contract: bool = True) -> dict:
    """Require clean descendant source with a changed decision contract.

    The replay identity hashes the judgment, evidence, and candidate-planning
    source files, not the Git commit, so unrelated commits cannot re-authorize
    an identical decision contract.
    """
    if type(blocked_source_revision) is not str or not _COMMIT.fullmatch(blocked_source_revision):
        raise ValueError("Blocked source revision must be a full commit SHA")
    checkout = (root or Path(__file__).resolve().parents[2]).resolve()
    try:
        top = Path(os.fsdecode(_git(checkout, "rev-parse", "--show-toplevel")).strip()).resolve()
        if top != checkout:
            raise ValueError("Decision re-evaluation requires the package's clean Git root")
        head = os.fsdecode(_git(checkout, "rev-parse", "--verify", "HEAD^{commit}")).strip()
        if not _COMMIT.fullmatch(head):
            raise ValueError("Decision re-evaluation requires a full current Git commit")
        if head == blocked_source_revision:
            raise ValueError("Current source must differ from the blocked source revision")
        old = os.fsdecode(_git(checkout, "rev-parse", "--verify",
                                f"{blocked_source_revision}^{{commit}}")).strip()
        if old != blocked_source_revision:
            raise ValueError("Blocked source revision did not resolve exactly")
        status = _git(checkout, "status", "--porcelain=v1", "--untracked-files=all",
                      "--ignore-submodules=none")
        if status:
            raise ValueError("Decision re-evaluation requires a clean Git worktree")
        ancestor = subprocess.run(
            ["git", "--no-optional-locks", "-c", f"safe.directory={checkout}",
             "-C", os.fspath(checkout), "merge-base", "--is-ancestor", old, head],
            cwd=checkout, env={**{key: value for key, value in os.environ.items()
                                  if not key.startswith("GIT_")}, "GIT_OPTIONAL_LOCKS": "0",
                               "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                               "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "core.untrackedCache",
                               "GIT_CONFIG_VALUE_1": "false"},
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10)
        if ancestor.returncode != 0:
            raise ValueError("Blocked source revision is not an ancestor of current HEAD")
        current_files = {}
        old_files = {}
        for name in (*_CONTRACT_PATHS, *_OPTIONAL_CONTRACT_PATHS):
            current_present = bool(_git(checkout, "ls-tree", "--name-only", head, "--", name))
            previous_present = bool(_git(checkout, "ls-tree", "--name-only", old, "--", name))
            if name in _OPTIONAL_CONTRACT_PATHS and not current_present:
                if (checkout / name).exists() or (checkout / name).is_symlink():
                    raise ValueError("Untracked optional decision contract module is present")
                if previous_present:
                    old_files[name] = _git(checkout, "show", f"{old}:{name}")
                continue
            path = checkout / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Decision contract source file is unavailable")
            head_blob = _git(checkout, "show", f"{head}:{name}")
            working_bytes = path.read_bytes()
            if working_bytes.replace(b"\r\n", b"\n") != head_blob:
                raise ValueError("Decision contract working file differs from its HEAD blob")
            current_files[name] = head_blob
            if previous_present or name not in _OPTIONAL_CONTRACT_PATHS:
                old_files[name] = _git(checkout, "show", f"{old}:{name}")
        current_contract = _contract_sha256(current_files)
        old_contract = _contract_sha256(old_files)
        if require_changed_contract and current_contract == old_contract:
            raise ValueError("Decision contract has not changed since the blocked source")
        if not require_changed_contract and current_contract != old_contract:
            raise ValueError("Compatible recovery requires an equal decision contract")
        # Recheck identity around file reads so a concurrent checkout cannot
        # turn the contract digest into an unbound claim.
        if os.fsdecode(_git(checkout, "rev-parse", "--verify", "HEAD^{commit}")).strip() != head:
            raise ValueError("Git HEAD changed during decision contract preflight")
        if _git(checkout, "status", "--porcelain=v1", "--untracked-files=all",
                "--ignore-submodules=none"):
            raise ValueError("Git worktree changed during decision contract preflight")
        return {
            "blocked_source_revision": old,
            "source_head": head,
            "previous_contract_sha256": old_contract,
            "decision_contract_sha256": current_contract,
        }
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ValueError("Decision re-evaluation Git preflight failed closed") from error


def validate_checkpoint_capture(raw: bytes, expected_sha256: str,
                                memory_type, target: str,
                                max_stalled_decisions: int = 4,
                                decision_contract_sha256: str | None = None,
                                checkpoint_path: Path | None = None):
    """Bind one exact checkpoint capture to a quiescent eligible blocked state."""
    validate_checkpoint_digest(raw, expected_sha256)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Blocked decision checkpoint is invalid") from error
    if not isinstance(data, dict) or type(data.get("session_id")) is not str or not data["session_id"]:
        raise ValueError("Blocked decision checkpoint has no session identity")
    memory = memory_type.from_bytes(raw, data["session_id"], target)
    if checkpoint_path is not None and memory.blocked_recovery_archive is not None:
        from .blocked_recovery_archive import build_index
        memory._blocked_recovery_archive_index = build_index(checkpoint_path, memory)
    validate_blocked_memory(memory, max_stalled_decisions)
    if (decision_contract_sha256 is not None
            and any(entry["decision_contract_sha256"] == decision_contract_sha256
                    for entry in memory.blocked_reevaluations)):
        raise ValueError("This decision contract already consumed a blocked re-evaluation")
    return memory


def validate_checkpoint_digest(raw: bytes, expected_sha256: str) -> None:
    """Check a caller-pinned immutable checkpoint capture."""
    if type(expected_sha256) is not str or not _DIGEST.fullmatch(expected_sha256):
        raise ValueError("Exact checkpoint SHA-256 must be lowercase hexadecimal")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Controller checkpoint differs from the authorized SHA-256")


def validate_blocked_memory(memory, max_stalled_decisions: int, *,
                            allow_model_abstention: bool = False) -> None:
    """Reject anything except the exact quiescent terminal decision state.

    Two states are terminal although the ordinary threshold rule does not show
    it. Persistent recovery marks a block at the first exhausted decision
    frontier, before the stalled-decision threshold, and verified native work
    since then (a craft finishing) resets the counter; the durable recovery
    ledger it wrote is the evidence of the block. And a tracked background
    craft job with its attempt record is verifiable work, not an ambiguous
    native request: the controller polls it on every observation. Half of that
    pair is still refused.
    """
    if type(allow_model_abstention) is not bool:
        raise ValueError("Invalid model-abstention eligibility flag")
    from .planner_fault_recovery import REASON, validate_record
    if memory.reason == REASON:
        validate_record(memory)
    eligible_reasons = _BLOCKED_REASONS | ({"model abstention"} if allow_model_abstention else set())
    ledger = memory.blocked_recovery
    persistent_block = isinstance(ledger, dict) and bool(ledger.get("attempts"))
    archive = getattr(memory, "blocked_recovery_archive", None)
    archive_index = getattr(memory, "_blocked_recovery_archive_index", None)
    if (not persistent_block and isinstance(archive, dict)
            and type(archive.get("entry_count")) is int and archive["entry_count"] > 0
            and archive_index is not None):
        # A verified rotation legitimately leaves the active tail empty. Its
        # authenticated rows still prove a persistent block below threshold.
        archive_index.validate_files()
        persistent_block = True
    job = getattr(memory, "background_job", None)
    attempt = getattr(memory, "background_attempt", None)
    if (memory.status != "blocked" or not isinstance(memory.reason, str)
            or memory.reason not in eligible_reasons
            or type(memory.stalled_decisions) is not int
            or (memory.stalled_decisions < max_stalled_decisions and not persistent_block)
            or memory.pending is not None or memory.attempt is not None
            or memory.active_plan is not None or memory.step_index != 0
            or memory.reservations or memory.transfer_recovery is not None
            or (job is None) != (attempt is None)):
        raise ValueError("Checkpoint is not a quiescent eligible blocked decision")
