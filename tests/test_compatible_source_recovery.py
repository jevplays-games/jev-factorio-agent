"""Original paid craft and durable budget history survive explicit migration."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os

import pytest

from jev_factorio import compatible_recovery as recovery
from jev_factorio import blocked_reevaluation
from jev_factorio.blocked_persistence import selection_attempts_for_state, selection_state_sha256
from jev_factorio.background import BackgroundMemory
from test_background_work import ReceiptBackend, controller

OLD = {"commit": "1" * 40, "source_sha256": "a" * 64}
NEW = {"commit": "2" * 40, "source_sha256": "b" * 64}
OWNER = {"run_id": "original-run", "segment_id": "new-segment", "execution_id": "owner-265"}


@pytest.mark.parametrize("source_key", ["previous_source", "current_source"])
def test_new_lineage_rejects_zero_source_digest_without_checkpoint_effect(tmp_path, source_key):
    memory = BackgroundMemory(session_id="original-session", target="rocket_launch")
    authority = {"schema": 1, "authorization_id": "migration-zero-source",
        "checkpoint_sha256": "c" * 64, "session_id": memory.session_id,
        "target": memory.target, "previous_source": OLD.copy(), "current_source": NEW.copy(),
        "decision_contract_sha256": "d" * 64, "scope": recovery.scope(memory),
        "owner_invocation": OWNER.copy(), "supervisor_history_sha256": "e" * 64,
        "lock_path": str((tmp_path / "original-writer.lock").resolve()),
        "provider_state_sha256": None, "provider_identity_sha256": None}
    authority[source_key]["source_sha256"] = "0" * 64
    # A matching authorization preimage must not make a zero source digest valid.
    record = dict(authority, authorization_sha256=recovery.digest_json(authority))
    memory.compatible_source_recoveries = [record]
    before = deepcopy(asdict(memory))
    with pytest.raises(ValueError, match="nonzero exact digest"):
        recovery.validate_lineage(memory)
    assert asdict(memory) == before


def test_legacy_compatible_scope_without_background_step_keeps_its_exact_preimage(tmp_path):
    memory = BackgroundMemory(session_id="original-session", target="rocket_launch")
    authority = {"schema": 1, "authorization_id": "migration-legacy-scope",
        "checkpoint_sha256": "c" * 64, "session_id": memory.session_id,
        "target": memory.target, "previous_source": OLD.copy(), "current_source": NEW.copy(),
        "decision_contract_sha256": "d" * 64, "scope": recovery.scope(memory),
        "owner_invocation": OWNER.copy(), "supervisor_history_sha256": "e" * 64,
        "lock_path": str((tmp_path / "original-writer.lock").resolve()),
        "provider_state_sha256": None, "provider_identity_sha256": None}
    authority["scope"].pop("background_step")
    record = dict(authority, authorization_sha256=recovery.digest_json(authority))
    memory.compatible_source_recoveries = [record]

    assert recovery.validate_lineage(memory) == [record]
    assert "background_step" not in record["scope"]


def test_compatible_scope_rejects_an_orphan_background_step(tmp_path):
    memory = BackgroundMemory(session_id="original-session", target="rocket_launch")
    authority = {"schema": 1, "authorization_id": "migration-orphan-step",
        "checkpoint_sha256": "c" * 64, "session_id": memory.session_id,
        "target": memory.target, "previous_source": OLD.copy(), "current_source": NEW.copy(),
        "decision_contract_sha256": "d" * 64, "scope": recovery.scope(memory),
        "owner_invocation": OWNER.copy(), "supervisor_history_sha256": "e" * 64,
        "lock_path": str((tmp_path / "original-writer.lock").resolve()),
        "provider_state_sha256": None, "provider_identity_sha256": None}
    authority["scope"]["background_step"] = {"action": "factory_craft_job"}
    record = dict(authority, authorization_sha256=recovery.digest_json(authority))
    memory.compatible_source_recoveries = [record]

    with pytest.raises(ValueError, match="background pair"):
        recovery.validate_lineage(memory)


def test_compatible_paid_handoff_quiescence_includes_background_step():
    memory = BackgroundMemory(
        session_id="original-session", target="rocket_launch", status="running",
        active_plan={"selected": "paid"},
        background_step={"action": "factory_craft_job", "receipt": "retained-owner"},
    )

    with pytest.raises(ValueError, match="quiescent selected plan"):
        recovery.validate_selected_paid_handoff(memory)


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_FACTORIO_PROVENANCE", json.dumps({**OWNER, "code_revision": NEW}))
    backend = ReceiptBackend()
    original = controller(backend, tmp_path)
    original.step()  # Actual dispatcher, paid receipt and paired background owner.
    memory = original.memory
    memory.status, memory.reason = "blocked", "low choice confidence"
    memory.stalled_decisions = 1
    memory.failures["retained-failure"] = 2
    memory.blocked_recovery = {"schema": 1, "session_id": memory.session_id,
        "source_revision": OLD.copy(), "attempts": [{"source_revision": OLD.copy(),
        "decision_input_sha256": "c" * 64, "reason": memory.reason,
        "tick": memory.last_tick, "outcome": "frontier"}],
        "last_input_sha256": "c" * 64, "wait_level": 4}
    path = tmp_path / "state.json"
    memory.save(path)
    authority = {"schema": 1, "authorization_id": "migration-265",
        "checkpoint_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "session_id": memory.session_id, "target": memory.target,
        "previous_source": OLD.copy(), "current_source": NEW.copy(),
        "decision_contract_sha256": "d" * 64, "scope": recovery.scope(memory),
        "owner_invocation": OWNER.copy(), "supervisor_history_sha256": "e" * 64,
        "lock_path": str((tmp_path / "original-writer.lock").resolve()),
        "provider_state_sha256": None, "provider_identity_sha256": None}
    monkeypatch.setattr(recovery, "require_writer_lock", lambda fd, path: None)
    def contract(old, root=None, *, require_changed_contract=True):
        assert old == OLD["commit"] and require_changed_contract is False
        return {"source_head": NEW["commit"], "decision_contract_sha256": "d" * 64}
    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision", contract)
    monkeypatch.setattr(recovery, "validate_budget_contract", lambda *args: None)
    return backend, original, path, authority


def migrate(path, authority):
    return recovery.migrate_checkpoint(path, authority, BackgroundMemory, NEW, OWNER, lock_fd=9)


def test_paid_background_migration_precedes_observation_and_preserves_all_state(tmp_path, monkeypatch):
    backend, original, path, authority = setup(tmp_path, monkeypatch)
    before, observed, calls = asdict(original.memory), backend.observations, deepcopy(backend.calls)
    migrated = migrate(path, authority)
    assert backend.observations == observed and backend.calls == calls
    after = asdict(BackgroundMemory.load(path, migrated.session_id, migrated.target))
    assert after["background_job"] == before["background_job"]
    assert after["background_attempt"] == before["background_attempt"]
    assert authority["scope"]["background_step"] == before["background_step"]
    assert after["background_step"] == before["background_step"]
    assert after["failures"] == before["failures"]
    assert after["attempt_outcomes"] == before["attempt_outcomes"]
    assert after["blocked_recovery"]["attempts"] == before["blocked_recovery"]["attempts"]
    assert after["blocked_recovery"]["wait_level"] == 4 and after["stalled_decisions"] == 1
    assert after["blocked_recovery"]["source_revision"] == NEW
    unchanged = set(before) - {"blocked_recovery", "compatible_source_recoveries"}
    assert {key: after[key] for key in unchanged} == {key: before[key] for key in unchanged}
    assert recovery.approved_sources(migrated, NEW) == [OLD, NEW]
    assert recovery.approved_sources(migrated, {"commit": "3" * 40, "source_sha256": "f" * 64}) != [OLD, NEW]
    with pytest.raises(ValueError, match="supervisor run identity"):
        recovery.validate_current_owner(migrated, {**OWNER, "run_id": "foreign-run"})


def test_migration_rejects_changed_exact_background_step_without_checkpoint_effect(tmp_path, monkeypatch):
    _, original, path, authority = setup(tmp_path, monkeypatch)
    before = path.read_bytes()
    history = deepcopy(original.memory.history)
    authority["scope"]["background_step"]["threshold"] += 1

    with pytest.raises(ValueError, match="authorized checkpoint/owner/source scope"):
        migrate(path, authority)

    assert path.read_bytes() == before
    assert original.memory.history == history


def test_legacy_scope_migrates_without_rewriting_its_old_digest_or_history(tmp_path, monkeypatch):
    _, original, path, authority = setup(tmp_path, monkeypatch)
    legacy_data = json.loads(path.read_text())
    legacy_data["background_schema"] = 2
    legacy_data.pop("background_step")
    raw = json.dumps(legacy_data, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    legacy_memory = BackgroundMemory.from_bytes(raw, original.memory.session_id, original.memory.target)
    authority["checkpoint_sha256"] = hashlib.sha256(raw).hexdigest()
    authority["scope"] = recovery.scope(
        legacy_memory, include_background_step=False, include_step_in_state=False)
    history = deepcopy(legacy_memory.history)

    migrated = migrate(path, authority)

    record = migrated.compatible_source_recoveries[-1]
    assert "background_step" not in record["scope"]
    assert record["authorization_sha256"] == recovery.digest_json(authority)
    assert migrated.background_schema == 2 and migrated.background_step is None
    assert migrated.background_job == legacy_memory.background_job
    assert migrated.background_attempt == legacy_memory.background_attempt
    assert migrated.history == history


def test_legacy_scope_cannot_authorize_nonnull_background_step(tmp_path, monkeypatch):
    _, original, path, authority = setup(tmp_path, monkeypatch)
    authority["scope"] = recovery.scope(original.memory, include_background_step=False)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="authorized checkpoint/owner/source scope"):
        migrate(path, authority)

    assert path.read_bytes() == before


@pytest.mark.parametrize("result", ["running", "completed", "mismatched", "failed"])
def test_paid_craft_restart_verification_never_redispatches(tmp_path, monkeypatch, result):
    backend, original, path, authority = setup(tmp_path, monkeypatch)
    migrate(path, authority)
    calls = deepcopy(backend.calls)
    if result == "completed":
        backend.complete()
    elif result == "mismatched":
        backend.state.factory["craft_job"]["id"] = "different-native-receipt"
    elif result == "failed":
        backend.state.factory["craft_job"].update(status="failed", queue_valid=False)
    restored = controller(backend, tmp_path, resume=True)
    response = restored.reconcile_only()
    assert backend.calls == calls
    assert restored.memory.compatible_source_recoveries
    if result == "completed":
        assert response["background_state"] == "verified_completed"
        assert restored.memory.background_job is None
    elif result == "running":
        assert restored.memory.background_job is not None
    else:
        assert restored.memory.status == "uncertain"
        assert restored.memory.background_job is not None


@pytest.mark.parametrize("field,value", [("checkpoint_sha256", "f" * 64),
    ("session_id", "other"), ("target", "rocket_launch"),
    ("current_source", OLD), ("decision_contract_sha256", "0" * 64),
    ("schema", True), ("owner_invocation", {**OWNER, "execution_id": "wrong"})])
def test_invalid_exact_authority_has_no_effect(tmp_path, monkeypatch, field, value):
    backend, _, path, authority = setup(tmp_path, monkeypatch)
    before = path.read_bytes()
    authority[field] = value
    with pytest.raises(ValueError):
        migrate(path, authority)
    assert path.read_bytes() == before
    assert len(backend.calls) == 1


def test_unknown_legacy_billed_coverage_is_not_new_budget(tmp_path, monkeypatch):
    _, original, path, authority = setup(tmp_path, monkeypatch)
    original.memory.blocked_recovery["attempts"][0]["outcome"] = "rejected"
    original.memory.save(path)
    authority["checkpoint_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    authority["scope"] = recovery.scope(original.memory)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unknown legacy paid-request coverage"):
        migrate(path, authority)
    assert path.read_bytes() == before


@pytest.mark.parametrize("after_replace", [False, True])
def test_migration_crash_retains_original_or_consumed_no_blind_repeat(tmp_path, monkeypatch, after_replace):
    _, _, path, authority = setup(tmp_path, monkeypatch)
    saved = BackgroundMemory.save
    def fail(memory, target):
        if after_replace:
            saved(memory, target)
        raise OSError("injected migration storage failure")
    with monkeypatch.context() as ctx:
        ctx.setattr(BackgroundMemory, "save", fail)
        with pytest.raises(OSError, match="storage failure"):
            migrate(path, authority)
    loaded = BackgroundMemory.load(path, authority["session_id"], authority["target"])
    assert loaded.blocked_recovery["source_revision"] == (NEW if after_replace else OLD)
    assert bool(loaded.compatible_source_recoveries) == after_replace
    if after_replace:
        with pytest.raises(ValueError):
            migrate(path, authority)


def test_equal_contract_selection_history_aggregates_source_bound_state_aliases(tmp_path, monkeypatch):
    _, _, path, authority = setup(tmp_path, monkeypatch)
    memory = migrate(path, authority)
    state, plans = {"facts": {"inventory": {"iron-ore": 33}}}, [{"id": "pickup"}]
    def key(source):
        return selection_state_sha256(state, plans, session_id=memory.session_id,
            source_revision=source, target=memory.target, policy="jev",
            confidence_floor=.45, current_tick=memory.last_tick)
    def row(source, index):
        return {"source_revision": source, "decision_input_sha256": str(index) * 64,
            "reason": memory.reason, "tick": memory.last_tick, "outcome": "rejected",
            "selection_batch": {"schema": 1, "state_sha256": key(source),
                "frontier_sha256": "f" * 64, "request_sha256": str(index) * 64,
                "offered": [{"plan_id": "choice-" + str(index), "candidate_sha256": str(index) * 64}]}}
    memory.blocked_recovery["attempts"] = [row(OLD, 1), row(NEW, 2)]
    aliases = [(OLD, key(OLD)), (NEW, key(NEW))]
    result = selection_attempts_for_state(memory, NEW, key(NEW), compatible_state_hashes=aliases)
    assert [r["source_revision"] for r in result] == [OLD, NEW]
    with pytest.raises(ValueError, match="complete source-bound"):
        selection_attempts_for_state(memory, NEW, key(NEW))
    memory.blocked_recovery["attempts"][1]["selection_batch"]["offered"][0]["candidate_sha256"] = "1" * 64
    with pytest.raises(ValueError, match="already offered"):
        selection_attempts_for_state(memory, NEW, key(NEW), compatible_state_hashes=aliases)


def test_authority_file_is_exact_digest_pinned_and_duplicate_keys_rejected(tmp_path):
    path = tmp_path / "authority.json"
    raw = b'{"schema":1,"schema":1}'
    path.write_bytes(raw)
    if os.name == "posix":
        path.chmod(0o400)
    with pytest.raises(ValueError, match="Duplicate"):
        recovery.read_authorization(path, hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="exact pin"):
        recovery.read_authorization(path, "f" * 64)


def test_budget_protocol_is_equal_only_for_cosmetic_or_lookup_changes():
    from pathlib import Path
    raw = (Path(__file__).parents[1] / "src/jev_factorio/blocked_persistence.py").read_bytes()
    digest = recovery.budget_contract(raw)
    assert recovery.budget_contract(raw + b"\n# cosmetic\n") == digest
    assert recovery.budget_contract(raw.replace(b"return result\n", b"return result  # cosmetic\n")) == digest
    assert recovery.budget_contract(raw.replace(b"MAX_SELECTION_BATCHES_PER_STATE = 3", b"MAX_SELECTION_BATCHES_PER_STATE = 4")) != digest
    assert recovery.budget_contract(raw.replace(b'"schema": 1, "kind": "persistent_selection_state"',
        b'"schema": 2, "kind": "persistent_selection_state"')) != digest


def test_provider_budget_sidecar_mismatch_rejects_before_migration(tmp_path, monkeypatch):
    _, _, path, authority = setup(tmp_path, monkeypatch)
    from jev_factorio.operational_safety import safety_dir
    directory = safety_dir(path)
    directory.mkdir(exist_ok=True)
    provider = directory / "provider.json"
    provider.write_text('{"attempts":8,"phase":"exhausted"}', encoding="ascii")
    if os.name == "posix":
        directory.chmod(0o700)
        provider.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="provider identity/state budgets"):
        migrate(path, authority)
    assert path.read_bytes() == before
    authority["provider_state_sha256"] = hashlib.sha256(provider.read_bytes()).hexdigest()
    saved = provider.read_bytes()
    migrated = migrate(path, authority)
    assert migrated.compatible_source_recoveries[-1]["provider_state_sha256"] == hashlib.sha256(saved).hexdigest()
    assert provider.read_bytes() == saved


def test_fingerprint_protocol_real_clean_descendant_and_changed_protocol(tmp_path):
    from pathlib import Path
    from test_blocked_reevaluation import _git, _source_repo
    checkout = tmp_path / "clean-source"
    checkout.mkdir()
    _source_repo(checkout)
    name = "src/jev_factorio/blocked_persistence.py"
    path = checkout / name
    raw = (Path(__file__).parents[1] / name).read_bytes()
    path.write_bytes(raw)
    _git(checkout, "add", name)
    _git(checkout, "commit", "--quiet", "-m", "known budget protocol")
    old = _git(checkout, "rev-parse", "HEAD")
    (checkout / "README.md").write_text("persistence-only source commit\n")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "--quiet", "-m", "same contract source")
    new = _git(checkout, "rev-parse", "HEAD")
    proof = blocked_reevaluation.validate_source_revision(old, checkout, require_changed_contract=False)
    assert proof["source_head"] == new
    recovery.validate_budget_contract(old, new, checkout)
    with pytest.raises(ValueError, match="contract has not changed"):
        blocked_reevaluation.validate_source_revision(old, checkout)
    path.write_bytes(raw.replace(b"MAX_SELECTION_BATCHES_PER_STATE = 3", b"MAX_SELECTION_BATCHES_PER_STATE = 4"))
    _git(checkout, "add", name)
    _git(checkout, "commit", "--quiet", "-m", "incompatible budget change")
    with pytest.raises(ValueError, match="fingerprint protocol or budgets"):
        recovery.validate_budget_contract(old, _git(checkout, "rev-parse", "HEAD"), checkout)


def test_authenticated_archive_and_active_source_budgets_aggregate(tmp_path, monkeypatch):
    from jev_factorio.blocked_recovery_archive import archive_full_tail
    from test_blocked_recovery_archive import _allow_storage
    _allow_storage(monkeypatch)
    backend, original, path, authority = setup(tmp_path, monkeypatch)
    memory = original.memory
    state, plans = {"facts": {"inventory": {"iron-ore": 33}}}, [{"id": "pickup"}]
    def key(source):
        return selection_state_sha256(state, plans, session_id=memory.session_id,
            source_revision=source, target=memory.target, policy="jev",
            confidence_floor=.45, current_tick=memory.last_tick)
    rows = []
    for n in range(1024):
        rows.append({"source_revision": OLD.copy(),
            "decision_input_sha256": hashlib.sha256(str(n).encode()).hexdigest(),
            "reason": memory.reason, "tick": memory.last_tick, "outcome": "frontier"})
    rows[0]["outcome"] = "rejected"
    rows[0]["selection_batch"] = {"schema": 1, "state_sha256": key(OLD),
        "frontier_sha256": "f" * 64, "request_sha256": "1" * 64,
        "offered": [{"plan_id": "pickup13", "candidate_sha256": "1" * 64}]}
    memory.blocked_recovery["attempts"] = rows
    memory.blocked_recovery["last_input_sha256"] = rows[-1]["decision_input_sha256"]
    memory.save(path)
    with pytest.raises(ValueError, match="quiescent decision boundary"):
        archive_full_tail(path, memory)
    # Complete the tracked paid job through its ordinary receipt/postcondition
    # verifier. Rotation must never obtain quiescence by erasing a pending pair.
    before_calls = deepcopy(backend.calls)
    backend.complete()
    restored = controller(backend, tmp_path, resume=True)
    completed = restored.reconcile_only()
    memory = restored.memory
    assert completed["background_state"] == "verified_completed"
    assert backend.calls == before_calls
    assert memory.background_job is None and memory.background_attempt is None
    memory.status, memory.reason = "blocked", "low choice confidence"
    memory.save(path)
    archived = archive_full_tail(path, memory)
    archived.close()
    # Rotation persists the pointer/tail transaction before migration.
    memory.save(path)
    pointer = deepcopy(memory.blocked_recovery_archive)
    authority["checkpoint_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    authority["scope"] = recovery.scope(memory)
    migrated = migrate(path, authority)
    migrated = BackgroundMemory.load(path, migrated.session_id, migrated.target)
    index = migrated._blocked_recovery_archive_index
    try:
        assert migrated.blocked_recovery_archive == pointer
        result = selection_attempts_for_state(migrated, NEW, key(NEW),
            archive_index=index, compatible_state_hashes=[(OLD, key(OLD)), (NEW, key(NEW))])
        assert len(result) == 1 and result[0]["source_revision"] == OLD
        assert result[0]["selection_batch"]["offered"][0]["candidate_sha256"] == "1" * 64
        assert len(list(index.compatibility_rows(memory=migrated))) == 1024
    finally:
        index.close()


def test_composed_provider_frontier_stays_exhausted_after_equal_contract_migration(tmp_path, monkeypatch):
    from test_blocked_persistence import _alternative_loop, LiveClient
    from jev_factorio.memory import CampaignMemory
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.judgments import Decision
    import jev_factorio.controller as controller_module
    loop, backend, path, plans = _alternative_loop(tmp_path, monkeypatch, 3)
    initial = CampaignMemory.load(path, backend.session_id, "bootstrap_mining")
    initial.blocked_recovery["source_revision"] = OLD.copy()
    initial.blocked_recovery["attempts"][0].update(source_revision=OLD.copy(), outcome="frontier")
    initial.save(path)
    loop.provenance = {**OWNER, "code_revision": OLD}
    calls = []
    def reject(_client, _state, remaining, *_args, prepared_batch=None, **kwargs):
        saved = CampaignMemory.load(path, backend.session_id, "bootstrap_mining")
        assert saved.blocked_recovery["attempts"][-1]["outcome"] == "pending"
        calls.append(prepared_batch[2][0].id)
        return Decision(None, "observe", "Candidate evidence insufficient", model_called=True,
            diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})
    monkeypatch.setattr(controller_module, "select_plan", reject)
    loop.step()
    assert len(calls) == 3 and backend.actions == []
    memory = CampaignMemory.load(path, backend.session_id, "bootstrap_mining")
    authority = {"schema": 1, "authorization_id": "complete-frontier-265",
        "checkpoint_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "session_id": memory.session_id, "target": memory.target,
        "previous_source": OLD.copy(), "current_source": NEW.copy(),
        "decision_contract_sha256": "d" * 64, "scope": recovery.scope(memory),
        "owner_invocation": OWNER.copy(), "supervisor_history_sha256": "e" * 64,
        "lock_path": str((tmp_path / "original-writer.lock").resolve()),
        "provider_state_sha256": None, "provider_identity_sha256": None}
    monkeypatch.setattr(recovery, "require_writer_lock", lambda *args: None)
    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision", lambda *args, **kwargs:
        {"source_head": NEW["commit"], "decision_contract_sha256": "d" * 64})
    monkeypatch.setattr(recovery, "validate_budget_contract", lambda *args: None)
    recovery.migrate_checkpoint(path, authority, CampaignMemory, NEW, OWNER, lock_fd=9)
    monkeypatch.setattr(controller_module, "gameplay_context", lambda: {**OWNER, "code_revision": NEW})
    resumed = HierarchicalLoop(backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(path), resume_controller=True, tick_seconds=0, persist_recoverable_blocks=True)
    resumed._safety.admission = lambda *args: None
    resumed._work_candidates = lambda snapshot: (plans, "")
    monkeypatch.setattr(controller_module, "select_plan", lambda *args, **kwargs:
        pytest.fail("equal-contract source granted extra paid selection"))
    result = resumed.step()
    assert result["model_call"] is False and backend.actions == []
    assert result["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
    assert len(calls) == 3
    saved = CampaignMemory.load(path, backend.session_id, "bootstrap_mining")
    assert [row["source_revision"] for row in saved.blocked_recovery["attempts"]
            if "selection_batch" in row] == [OLD] * 3


@pytest.mark.skipif(os.name != "posix", reason="Inherited native writer flock requires POSIX")
def test_inherited_original_writer_lock_excludes_second_owner_and_replacement(tmp_path):
    import fcntl
    path = tmp_path / "original-writer.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    second = None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recovery.require_writer_lock(descriptor, str(path))
        second = os.open(path, os.O_RDWR)
        with pytest.raises(ValueError, match="already-held inherited exclusive flock"):
            recovery.require_writer_lock(second, str(path))
        path.unlink()
        path.write_bytes(b"other owner")
        path.chmod(0o600)
        with pytest.raises(ValueError, match="writer-lock identity"):
            recovery.require_writer_lock(descriptor, str(path))
    finally:
        if second is not None:
            os.close(second)
        os.close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="Native Linux inherited descriptor proof")
def test_unlocked_private_descriptor_is_rejected_without_acquiring_lock(tmp_path):
    import fcntl
    path = tmp_path / "original-writer.lock"
    first = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    second = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(ValueError, match="already-held inherited exclusive flock"):
            recovery.require_writer_lock(first, str(path))
        # The rejected validation must not acquire the first FD's lock.
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(second)
        os.close(first)


@pytest.mark.skipif(os.name != "posix", reason="Native Linux inherited descriptor proof")
def test_original_locked_descriptor_survives_pass_fds_exec(tmp_path):
    import fcntl
    import subprocess
    import sys
    path = tmp_path / "original-writer.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proof = subprocess.run([sys.executable, "-c",
            "import sys; from jev_factorio.compatible_recovery import require_writer_lock; "
            "require_writer_lock(int(sys.argv[1]),sys.argv[2])", str(descriptor), str(path)],
            pass_fds=(descriptor,), check=False, capture_output=True, text=True, timeout=15)
        assert proof.returncode == 0, proof.stderr
        # Child validation did not release the original parent's ownership.
        second = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(second)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="Native private provider-sidecar modes")
@pytest.mark.parametrize("which,mode", [("provider", 0o644), ("provider", 0o620),
                                       ("directory", 0o755), ("directory", 0o720)])
def test_provider_file_and_directory_permission_rejection(tmp_path, which, mode):
    from jev_factorio.operational_safety import safety_dir
    checkpoint = tmp_path / "campaign.json"
    directory = safety_dir(checkpoint)
    directory.mkdir(mode=0o700)
    provider = directory / "provider.json"
    provider.write_text('{"attempts":8,"phase":"exhausted"}')
    provider.chmod(0o600)
    assert recovery.provider_state_digest(checkpoint) == hashlib.sha256(provider.read_bytes()).hexdigest()
    (provider if which == "provider" else directory).chmod(mode)
    with pytest.raises(ValueError, match="provider"):
        recovery.provider_state_digest(checkpoint)


@pytest.mark.skipif(os.name != "posix", reason="Native provider euid ownership")
def test_provider_capture_rejects_foreign_owner(tmp_path, monkeypatch):
    from jev_factorio.operational_safety import safety_dir
    checkpoint = tmp_path / "campaign.json"
    directory = safety_dir(checkpoint)
    directory.mkdir(mode=0o700)
    provider = directory / "provider.json"
    provider.write_text('{"attempts":8}')
    provider.chmod(0o600)
    actual_uid = os.geteuid()
    monkeypatch.setattr(recovery.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(ValueError, match="private and owned"):
        recovery.provider_state_digest(checkpoint)
