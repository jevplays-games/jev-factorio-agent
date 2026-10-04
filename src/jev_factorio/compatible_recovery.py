"""Explicit equal-contract source migration, never a new decision allowance.

The supervisor supplies and pins the authorization; this module does not sign
it or infer authority from ancestry. The migration changes only the current
recovery source and appends a durable lineage record before backend attachment.
"""
from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import stat
from copy import copy, deepcopy
from pathlib import Path

from .blocked_persistence import _source, _validate_state
from .provenance import digest_json, identifier

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KEYS = {"schema", "authorization_id", "checkpoint_sha256", "session_id", "target",
         "previous_source", "current_source", "decision_contract_sha256", "scope",
         "owner_invocation", "supervisor_history_sha256", "lock_path",
         "provider_state_sha256", "provider_identity_sha256"}
_SCOPE_KEYS = {"state_sha256", "blocked_recovery_archive", "background_job",
               "background_attempt", "stalled_decisions", "failures_sha256",
               "history_sha256", "attempt_outcomes_sha256", "active_recovery_attempts"}


def _digest(value):
    if type(value) is not str or not _DIGEST.fullmatch(value) or value == "0" * 64:
        raise ValueError("Compatible recovery requires a nonzero exact digest")
    return value


def scope(memory) -> dict:
    """Bind complete retained state, including counters and native ownership."""
    from dataclasses import asdict
    value = asdict(memory)
    # Exact checkpoint bytes and the full decoded-state digest bind every
    # counter/receipt/owner. Keep the paid background pair explicit without
    # copying an ever-growing archive/lineage into each later authorization.
    return {"state_sha256": digest_json(value),
            "blocked_recovery_archive": deepcopy(memory.blocked_recovery_archive),
            "background_job": deepcopy(getattr(memory, "background_job", None)),
            "background_attempt": deepcopy(getattr(memory, "background_attempt", None)),
            "stalled_decisions": memory.stalled_decisions,
            "failures_sha256": digest_json(memory.failures),
            "history_sha256": digest_json(memory.history),
            "attempt_outcomes_sha256": digest_json(memory.attempt_outcomes),
            "active_recovery_attempts": len(memory.blocked_recovery["attempts"])
                if memory.blocked_recovery is not None else 0}


def validate_lineage(memory) -> list[dict]:
    records = memory.compatible_source_recoveries
    if not isinstance(records, list) or len(records) > 128:
        raise ValueError("Invalid compatible-source recovery lineage")
    ids = set()
    prior = None
    contract = None
    for record_index, record in enumerate(records):
        if (not isinstance(record, dict) or set(record) not in (_KEYS | {"authorization_sha256"},
                                         _KEYS | {"authorization_sha256", "epoch_witness"})
                or type(record["schema"]) is not int or record["schema"] != 1
                or record["session_id"] != memory.session_id or record["target"] != memory.target):
            raise ValueError("Invalid compatible-source recovery record")
        identifier(record["authorization_id"])
        if record["authorization_id"] in ids:
            raise ValueError("Duplicate compatible-source authorization")
        ids.add(record["authorization_id"])
        old, new = _source(record["previous_source"]), _source(record["current_source"])
        _digest(old["source_sha256"])
        _digest(new["source_sha256"])
        boundary = prior is not None and (old != prior or record["decision_contract_sha256"] != contract)
        if old == new:
            raise ValueError("Compatible-source lineage is not a directed chain")
        if boundary:
            from .compatible_epoch import validate_epoch_witness
            validate_epoch_witness(record.get("epoch_witness"), memory, record, prior, contract,
                                   prior_records=records[:record_index])
        elif "epoch_witness" in record:
            raise ValueError("Unexpected compatible epoch boundary witness")
        if any(new in (r["previous_source"], r["current_source"])
               for r in records[:record_index]):
            raise ValueError("Compatible-source lineage contains a cycle")
        for key in ("authorization_sha256", "checkpoint_sha256", "decision_contract_sha256",
                    "supervisor_history_sha256"):
            _digest(record[key])
        if contract is not None and record["decision_contract_sha256"] != contract and not boundary:
            raise ValueError("Compatible-source lineage changed decision contract")
        contract = record["decision_contract_sha256"]
        for key in ("provider_state_sha256", "provider_identity_sha256"):
            if record[key] is not None:
                _digest(record[key])
        saved_scope = record["scope"]
        if (not isinstance(saved_scope, dict) or set(saved_scope) != _SCOPE_KEYS
                or not isinstance(record["owner_invocation"], dict)):
            raise ValueError("Compatible recovery lost original ownership scope")
        for key in ("state_sha256", "failures_sha256", "history_sha256", "attempt_outcomes_sha256"):
            _digest(saved_scope[key])
        if (type(saved_scope["stalled_decisions"]) is not int or saved_scope["stalled_decisions"] < 0
                or type(saved_scope["active_recovery_attempts"]) is not int
                or not 0 <= saved_scope["active_recovery_attempts"] <= 1024
                or (saved_scope["background_job"] is None) != (saved_scope["background_attempt"] is None)):
            raise ValueError("Compatible recovery scope has invalid counters or background pair")
        owner = record["owner_invocation"]
        if set(owner) != {"run_id", "segment_id", "execution_id"}:
            raise ValueError("Compatible recovery requires exact owner invocation")
        for value in owner.values():
            identifier(value)
        if type(record["lock_path"]) is not str or not Path(record["lock_path"]).is_absolute():
            raise ValueError("Compatible recovery requires the original absolute writer lock")
        # The retained authorization's complete preimage is validated, not just
        # any well-shaped digest placed in the checkpoint.
        authorization = {key: record[key] for key in _KEYS}
        if digest_json(authorization) != record["authorization_sha256"]:
            raise ValueError("Compatible recovery authorization preimage changed")
        prior = new
    return records


def approved_sources(memory, current_source: dict) -> list[dict]:
    """Only the lineage ending at the actual current source can share budgets."""
    current = _source(current_source)
    records = validate_lineage(memory)
    if not records or records[-1]["current_source"] != current:
        return [current]
    start = max((i for i, record in enumerate(records) if "epoch_witness" in record), default=0)
    epoch = records[start:]
    return [epoch[0]["previous_source"], *(r["current_source"] for r in epoch)]


def validate_current_owner(memory, context: dict | None) -> None:
    records = validate_lineage(memory)
    if records and (not isinstance(context, dict)
                    or context.get("run_id") != records[-1]["owner_invocation"]["run_id"]):
        raise ValueError("Compatible recovery cannot change original supervisor run identity")


def budget_contract(data: bytes) -> str:
    """Bind v1 hashing/normalization and limits, excluding ledger lookup code.

    Old state digests cannot be inverted. Rehashing current facts for an old
    source is valid only when the entire fingerprint protocol is unchanged.
    Cosmetic formatting is harmless; AST or constant changes fail closed.
    """
    tree = ast.parse(data.decode("utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_validate_state":
            break
        nodes.append(node)
    else:
        raise ValueError("Unknown legacy selection fingerprint protocol")
    return hashlib.sha256(ast.dump(ast.Module(body=nodes, type_ignores=[]),
                                  include_attributes=False).encode()).hexdigest()


def validate_budget_contract(old: str, new: str, checkout: Path | None = None) -> None:
    from .blocked_reevaluation import _git
    root = (checkout or Path(__file__).resolve().parents[2]).resolve()
    name = "src/jev_factorio/blocked_persistence.py"
    if budget_contract(_git(root, "show", f"{old}:{name}")) != budget_contract(
            _git(root, "show", f"{new}:{name}")):
        raise ValueError("Compatible recovery changed selection fingerprint protocol or budgets")


def read_authorization(path: Path, expected_sha256: str) -> dict:
    """Descriptor-bound immutable authority; a pathname alone is not a pin."""
    _digest(expected_sha256)
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("Compatible recovery authorization must be one regular file")
    if os.name == "posix" and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o400):
        raise ValueError("Compatible recovery authorization ownership/mode mismatch")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Compatible recovery authorization was replaced")
        raw = stream.read(4 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    stamp = lambda v: (v.st_dev, v.st_ino, v.st_uid, v.st_mode, v.st_nlink,
                       v.st_size, v.st_mtime_ns, v.st_ctime_ns)
    if stamp(before) != stamp(after) or stamp(after) != stamp(path.lstat()):
        raise ValueError("Compatible recovery authorization changed during read")
    if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Compatible recovery authorization differs from exact pin")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate authorization key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda v: (_ for _ in ()).throw(ValueError("Invalid authorization number")))
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise ValueError("Invalid compatible-source authorization schema")
    return value


def validate_selected_paid_handoff(memory) -> None:
    """Permit only an authenticated, undispatched paid plan at its first step.

    This is source-handoff eligibility, never new selection or native authority.
    The full existing decision/budget contracts and signed scope still apply.
    """
    from .paid_selection_reconciliation import validate_representation_budget_carry
    if (memory.status != "running" or type(memory.step_index) is not int
            or memory.step_index != 0 or not isinstance(memory.active_plan, dict)
            or any(getattr(memory, key, None) is not None for key in (
                "pending", "attempt", "native_pending", "native_attempt",
                "background_job", "background_attempt", "transfer_recovery"))
            or memory.reservations):
        raise ValueError("Compatible paid handoff requires an undispatched quiescent selected plan")
    records = [row for row in memory.history if isinstance(row, dict)
               and row.get("kind") == "paid_duplicate_selection_reconciled"]
    if len(records) != 1 or memory.active_plan != records[0].get("selected_plan"):
        raise ValueError("Compatible paid handoff differs from its unique signed selected plan")
    index = getattr(memory, "_blocked_recovery_archive_index", None)
    if memory.blocked_recovery_archive is not None and index is None:
        raise ValueError("Compatible paid handoff requires authenticated archive coverage")
    if index is not None:
        index.validate_files()
    verified = validate_representation_budget_carry(memory, records[0], archive_index=index)
    if len(verified["rows"]) != 2 or len(verified["seen_candidate_sha256"]) != 2:
        raise ValueError("Compatible paid handoff lost its billed rows or canonical seen candidates")
    if index is not None:
        index.validate_files()


def validate_authorization(authorization: dict, raw: bytes, memory, current_source: dict,
                           owner_invocation: dict, *, checkout: Path | None = None,
                           epoch_witness: dict | None = None) -> dict:
    """Validate exact source/contract/checkpoint without native observation."""
    from .blocked_reevaluation import validate_blocked_memory, validate_source_revision
    if not isinstance(authorization, dict) or set(authorization) != _KEYS:
        raise ValueError("Invalid compatible-source authorization schema")
    record = deepcopy(authorization)
    record["authorization_sha256"] = digest_json(authorization)
    old_records = memory.compatible_source_recoveries
    if epoch_witness is not None:
        if not old_records:
            raise ValueError("Epoch boundary requires a retained compatible predecessor")
        from .compatible_epoch import validate_epoch_witness
        validate_epoch_witness(epoch_witness, memory, record,
                               old_records[-1]["current_source"],
                               old_records[-1]["decision_contract_sha256"],
                               checkpoint_raw=raw, require_live_history=True)
        record["epoch_witness"] = deepcopy(epoch_witness)
    # Validate with a temporary append, without mutating caller state.
    trial = copy(memory)
    trial.compatible_source_recoveries = [*old_records, record]
    validate_lineage(trial)
    if (authorization["checkpoint_sha256"] != hashlib.sha256(raw).hexdigest()
            or authorization["scope"] != scope(memory)
            or authorization["owner_invocation"] != owner_invocation
            or authorization["current_source"] != _source(current_source)):
        raise ValueError("Compatible recovery differs from authorized checkpoint/owner/source scope")
    if memory.status == "running":
        validate_selected_paid_handoff(memory)
    else:
        validate_blocked_memory(memory, 4, allow_model_abstention=True)
    if memory.blocked_recovery is None:
        raise ValueError("Compatible recovery requires known persistent budget coverage")
    recovery = _validate_state(memory.blocked_recovery, memory.session_id)
    if recovery["source_revision"] != authorization["previous_source"]:
        raise ValueError("Compatible recovery previous source does not own current ledger")
    source = validate_source_revision(authorization["previous_source"]["commit"], checkout,
                                      require_changed_contract=False)
    if (source["source_head"] != current_source["commit"]
            or source["decision_contract_sha256"] != authorization["decision_contract_sha256"]):
        raise ValueError("Compatible recovery contract differs from source pin")
    validate_budget_contract(authorization["previous_source"]["commit"],
                             current_source["commit"], checkout)
    return record


def require_writer_lock(lock_fd: int, lock_path: str) -> None:
    """Retain the supervisor's inherited original flock, never create another."""
    if os.name != "posix" or type(lock_fd) is not int or lock_fd < 0:
        raise ValueError("Compatible migration requires an inherited native POSIX writer-lock descriptor")
    info = os.fstat(lock_fd)
    path_info = Path(lock_path).lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
            or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)):
        raise ValueError("Compatible migration writer-lock identity/mode mismatch")
    # flock(EX|NB) would silently acquire an initially unlocked supplied FD.
    # Linux fdinfo instead reports locks already held by THIS open-file
    # description, including one inherited through fork/exec. An independently
    # opened FD for the same inode has no record even while another FD owns it.
    try:
        fdinfo = Path(f"/proc/self/fdinfo/{lock_fd}").read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise ValueError("Compatible migration cannot prove inherited original flock") from error
    matching = []
    for line in fdinfo.splitlines():
        if not line.startswith("lock:"):
            continue
        match = re.fullmatch(r"lock:\s+\d+:\s+FLOCK\s+ADVISORY\s+WRITE\s+([0-9]+)\s+"
                             r"([0-9a-f]+):([0-9a-f]+):([0-9]+)\s+0\s+EOF", line)
        if (match is None or int(match[1]) <= 0
                or (int(match[2], 16), int(match[3], 16), int(match[4]))
                   != (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)):
            raise ValueError("Compatible migration descriptor has an unexpected lock record")
        matching.append(match)
    if len(matching) != 1:
        raise ValueError("Compatible migration requires an already-held inherited exclusive flock")
    after = os.fstat(lock_fd)
    current = Path(lock_path).lstat()
    if ((after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
            or current.st_uid != info.st_uid or stat.S_IMODE(current.st_mode) != stat.S_IMODE(info.st_mode)):
        raise ValueError("Compatible migration writer lock changed during ownership proof")


def provider_state_digest(checkpoint: Path) -> str | None:
    from .operational_safety import safety_dir
    path = safety_dir(checkpoint) / "provider.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Compatible recovery provider state must not be a symlink")
    parent_before = path.parent.lstat() if path.parent.exists() else None
    if parent_before is not None and (not stat.S_ISDIR(parent_before.st_mode)
            or os.name == "posix" and (parent_before.st_uid != os.geteuid()
                or stat.S_IMODE(parent_before.st_mode) & 0o077)):
        raise ValueError("Compatible recovery provider directory must be private and owned")
    if not path.exists():
        return None  # Explicit absence is pinned, never silently fabricated coverage.
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or os.name == "posix" and (before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077)):
        raise ValueError("Compatible recovery provider state identity mismatch")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    stamp = lambda v: (v.st_dev, v.st_ino, v.st_uid, v.st_mode, v.st_nlink,
                       v.st_size, v.st_mtime_ns, v.st_ctime_ns)
    if (len(raw) > 1024 * 1024 or stamp(before) != stamp(opened)
            or stamp(opened) != stamp(after) or stamp(after) != stamp(path.lstat())):
        raise ValueError("Compatible recovery provider state changed during capture")
    parent_after = path.parent.lstat()
    if (parent_before is None or stamp(parent_before) != stamp(parent_after)
            or parent_after.st_uid != parent_before.st_uid
            or stat.S_IMODE(parent_after.st_mode) != stat.S_IMODE(parent_before.st_mode)):
        raise ValueError("Compatible recovery provider directory changed during capture")
    return hashlib.sha256(raw).hexdigest()


def migrate_checkpoint(path: Path, authorization: dict, memory_type, current_source: dict,
                       owner_invocation: dict, *, lock_fd: int, checkout: Path | None = None,
                       provider_identity_sha256: str | None = None, epoch_witness: dict | None = None):
    """Durably consume once before attachment; never mutate native assets.

    A failed/ambiguous save is fatal. A later invocation must read/reconcile the
    installed checkpoint, not retry this old exact-checkpoint authorization.
    """
    require_writer_lock(lock_fd, authorization["lock_path"])
    provider_digest = provider_state_digest(path)
    if (provider_digest != authorization["provider_state_sha256"]
            or provider_identity_sha256 != authorization["provider_identity_sha256"]):
        raise ValueError("Compatible recovery cannot reset or change provider identity/state budgets")
    raw = path.read_bytes()
    memory = memory_type.from_bytes(raw, authorization["session_id"], authorization["target"])
    if memory.blocked_recovery_archive is not None:
        from .blocked_recovery_archive import build_index
        index = build_index(path, memory)
        memory._blocked_recovery_archive_index = index
    else:
        index = None
    try:
        record = validate_authorization(authorization, raw, memory, current_source,
                                        owner_invocation, checkout=checkout, epoch_witness=epoch_witness)
        # Every potentially billed legacy row must have exact batch coverage.
        # Frontier-only rows prove no provider request and need no offered list.
        from itertools import chain
        rows = chain(memory.blocked_recovery["attempts"],
                     index.compatibility_rows(memory=memory) if index is not None else ())
        sources = approved_sources(memory, authorization["previous_source"])
        for row in rows:
            if row["source_revision"] not in sources:
                continue
            if "selection_batch" not in row and row.get("outcome") != "frontier":
                raise ValueError("Compatible recovery has unknown legacy paid-request coverage")
        if path.read_bytes() != raw:
            raise ValueError("Compatible checkpoint changed during migration")
        require_writer_lock(lock_fd, authorization["lock_path"])
        memory.compatible_source_recoveries.append(record)
        memory.blocked_recovery["source_revision"] = _source(current_source)
        # The append-only lineage is the durable authorization event. Do not
        # append to the bounded gameplay history: dropping its oldest event
        # would change the selection fingerprint and manufacture fresh budget.
        memory.save(path)  # Existing atomic replacement + fsync durability barrier.
        require_writer_lock(lock_fd, authorization["lock_path"])
        if provider_state_digest(path) != provider_digest:
            raise ValueError("Provider state changed during migration; reconcile installed checkpoint")
        return memory
    finally:
        if index is not None:
            if getattr(memory, "_blocked_recovery_archive_index", None) is index:
                del memory._blocked_recovery_archive_index
            index.close()
