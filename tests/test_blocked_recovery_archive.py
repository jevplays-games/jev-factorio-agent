from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from jev_factorio import blocked_persistence
from jev_factorio.blocked_recovery_archive import archive_full_tail, build_index
from jev_factorio.backends.mock import MockBackend
from jev_factorio.checkpoint_io import checkpoint_data
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.judgments import Decision
from jev_factorio.memory import CampaignMemory
from jev_factorio.skills import Plan, Step


SOURCE = {"commit": "2" * 40, "source_sha256": "c" * 64}
SESSION = "archive-campaign"
TARGET = "rocket_launch"


class _LiveMockBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.actions = []
        self.observations = 0

    def observe(self):
        self.observations += 1
        return replace(super().observe(), session_id=SESSION,
                       tick=1025, world_kind="mock")


class _LiveClient:
    model = "archive-test-double"
    is_mock = False
    uses_http_provider = False


def _encode(memory) -> bytes:
    return json.dumps(checkpoint_data(memory), sort_keys=True,
                      allow_nan=False).encode("utf-8")


def _full_memory() -> CampaignMemory:
    memory = CampaignMemory(SESSION, TARGET, status="running", last_tick=1024)
    attempts = []
    for index in range(1024):
        digest = hashlib.sha256(f"decision-{index}".encode()).hexdigest()
        attempts.append({
            "source_revision": SOURCE.copy(),
            "decision_input_sha256": digest,
            "reason": "low choice confidence",
            "tick": index,
            "outcome": "rejected",
        })
    memory.blocked_recovery = {
        "schema": 1, "session_id": SESSION, "source_revision": SOURCE.copy(),
        "attempts": attempts, "last_input_sha256": attempts[-1]["decision_input_sha256"],
        "wait_level": 1,
    }
    return memory


def _fill_tail(memory: CampaignMemory, first: int, count: int) -> None:
    rows = []
    for index in range(first, first + count):
        rows.append({
            "source_revision": SOURCE.copy(),
            "decision_input_sha256": hashlib.sha256(f"decision-{index}".encode()).hexdigest(),
            "reason": "low choice confidence",
            "tick": index,
            "outcome": "rejected",
        })
    memory.last_tick = first + count
    memory.blocked_recovery["attempts"] = rows
    memory.blocked_recovery["last_input_sha256"] = rows[-1]["decision_input_sha256"]


def _allow_storage(monkeypatch):
    from jev_factorio import operational_safety
    seen = []

    def ready(paths, *, minimum_bytes=1024 ** 3, minimum_inodes=1024):
        seen.append((tuple(paths), minimum_bytes, minimum_inodes))
        return True

    monkeypatch.setattr(operational_safety, "storage_ready", ready)
    return seen


def test_rotation_preserves_every_attempt_and_duplicate_lookup_after_reload(tmp_path, monkeypatch):
    seen = _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    memory.blocked_recovery["attempts"][0].pop("outcome")
    checkpoint.write_bytes(_encode(memory))

    index = archive_full_tail(checkpoint, memory)
    try:
        assert memory.blocked_recovery_archive["entry_count"] == 1024
        assert memory.blocked_recovery["attempts"] == []
        assert index.find(SOURCE, hashlib.sha256(b"decision-0").hexdigest(),
                          memory=memory)["outcome"] == "pending"
        assert blocked_persistence.was_attempted(
            memory, SOURCE, hashlib.sha256(b"decision-1023").hexdigest(), archive_index=index)
        with pytest.raises(ValueError, match="index is required"):
            blocked_persistence.was_attempted(
                memory, SOURCE, hashlib.sha256(b"decision-0").hexdigest())
    finally:
        index.close()

    # A second full tail chains onto the first immutable segment, then the
    # active checkpoint remains bounded at zero rather than growing forever.
    first_digest = memory.blocked_recovery_archive["head_sha256"]
    _fill_tail(memory, 1024, 1024)
    index = archive_full_tail(checkpoint, memory)
    try:
        assert memory.blocked_recovery_archive["entry_count"] == 2048
        assert memory.blocked_recovery_archive["segment_count"] == 2
        assert memory.blocked_recovery["attempts"] == []
        assert index.find(SOURCE, hashlib.sha256(b"decision-0").hexdigest(),
                          memory=memory) is not None
        assert index.find(SOURCE, hashlib.sha256(b"decision-2047").hexdigest(),
                          memory=memory) is not None
        checkpoint.write_bytes(_encode(memory))
    finally:
        index.close()

    restored = CampaignMemory.load(checkpoint, SESSION, TARGET)
    restored_index = restored._blocked_recovery_archive_index
    try:
        assert restored.blocked_recovery_archive["entry_count"] == 2048
        assert len(restored.blocked_recovery["attempts"]) == 0
        for index_number in (0, 511, 1023, 1024, 1536, 2047):
            digest = hashlib.sha256(f"decision-{index_number}".encode()).hexdigest()
            archived = restored_index.find(SOURCE, digest, memory=restored)
            assert archived is not None
            if index_number == 0:
                assert archived["outcome"] == "pending"
                segment = checkpoint.with_name(
                    checkpoint.name + ".blocked-recovery-archive"
                ) / f"segment-{first_digest}.json"
                # The canonical immutable row remains legacy-shaped on disk;
                # only the lookup view supplies the safe ambiguous default.
                assert "outcome" not in json.loads(segment.read_bytes())["attempts"][0]
            assert blocked_persistence.was_attempted(restored, SOURCE, digest,
                                                     archive_index=restored_index)
        assert not blocked_persistence.was_attempted(
            restored, SOURCE, "f" * 64, archive_index=restored_index)
    finally:
        restored_index.close()
    assert seen and all(item[1] == 1024 ** 3 for item in seen)


def test_exact_orphan_segment_is_reused_after_crash_before_pointer_commit(tmp_path, monkeypatch):
    import jev_factorio.blocked_recovery_archive as archive_module

    _allow_storage(monkeypatch)
    syncs = []
    monkeypatch.setattr(archive_module, "_sync_directory",
                        lambda path: syncs.append(path))
    checkpoint = tmp_path / "campaign.json"
    original = _full_memory()
    original_bytes = _encode(original)
    checkpoint.write_bytes(original_bytes)

    first = archive_full_tail(checkpoint, original)
    first_pointer = original.blocked_recovery_archive.copy()
    first.close()
    archive_dir = checkpoint.with_name(checkpoint.name + ".blocked-recovery-archive")
    parent_dir = archive_dir.parent
    assert syncs[-1] == archive_dir
    # Simulate process loss before the checkpoint pointer was replaced.
    checkpoint.write_bytes(original_bytes)
    resumed = CampaignMemory.from_bytes(original_bytes, SESSION, TARGET)
    syncs.clear()
    second = archive_full_tail(checkpoint, resumed)
    try:
        assert syncs == [parent_dir, archive_dir]
        assert resumed.blocked_recovery_archive == first_pointer
        assert resumed.blocked_recovery_archive["entry_count"] == 1024
        assert len(resumed.blocked_recovery["attempts"]) == 0
        assert second.find(SOURCE, hashlib.sha256(b"decision-77").hexdigest(),
                           memory=resumed) is not None
    finally:
        second.close()


def test_archive_tampering_after_index_build_fails_before_attempt_lookup(tmp_path, monkeypatch):
    _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    checkpoint.write_bytes(_encode(memory))
    built = archive_full_tail(checkpoint, memory)
    try:
        checkpoint.write_bytes(_encode(memory))
        index = build_index(checkpoint, memory)
    finally:
        built.close()

    digest = memory.blocked_recovery_archive["head_sha256"]
    segment = checkpoint.with_name(checkpoint.name + ".blocked-recovery-archive") / f"segment-{digest}.json"
    with segment.open("ab") as output:
        output.write(b"tamper")
    with pytest.raises(ValueError, match="changed after verification"):
        blocked_persistence.was_attempted(
            memory, SOURCE, hashlib.sha256(b"decision-77").hexdigest(), archive_index=index)
    index.close()


def test_temporary_lookup_index_is_read_only_and_identity_checked(tmp_path, monkeypatch):
    _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    checkpoint.write_bytes(_encode(memory))
    built = archive_full_tail(checkpoint, memory)
    built.close()
    checkpoint.write_bytes(_encode(memory))
    index = build_index(checkpoint, memory)
    try:
        index._index_path.chmod(0o600)
        with index._index_path.open("ab") as output:
            output.write(b"tamper")
        with pytest.raises(ValueError, match="index changed after verification"):
            index.find(SOURCE, hashlib.sha256(b"decision-77").hexdigest(), memory=memory)
    finally:
        index.close()


def test_archive_index_rejects_wrong_checkpoint_binding(tmp_path, monkeypatch):
    _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    checkpoint.write_bytes(_encode(memory))
    built = archive_full_tail(checkpoint, memory)
    try:
        wrong_session = CampaignMemory("another-campaign", TARGET)
        wrong_session.blocked_recovery_archive = memory.blocked_recovery_archive.copy()
        with pytest.raises(ValueError, match="different checkpoint"):
            built.find(SOURCE, hashlib.sha256(b"decision-0").hexdigest(),
                       memory=wrong_session)

        wrong_pointer = CampaignMemory(SESSION, TARGET)
        wrong_pointer.blocked_recovery_archive = {
            **memory.blocked_recovery_archive, "entry_count": 1025,
        }
        with pytest.raises(ValueError, match="different checkpoint"):
            built.find(SOURCE, hashlib.sha256(b"decision-0").hexdigest(),
                       memory=wrong_pointer)
    finally:
        built.close()


def test_source_change_rotation_keeps_prior_fingerprints_and_new_wal(tmp_path, monkeypatch):
    _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    memory.status = "blocked"
    memory.reason = "low choice confidence"
    memory.stalled_decisions = 5
    checkpoint.write_bytes(_encode(memory))
    archived = archive_full_tail(checkpoint, memory)
    new_source = {"commit": "3" * 40, "source_sha256": "d" * 64}
    new_input = hashlib.sha256(b"changed-contract-native-decision").hexdigest()
    blocked_persistence.record_attempt(
        memory, new_source, new_input, memory.reason, 1024,
        allow_source_change=True, archive_index=archived)
    checkpoint.write_bytes(_encode(memory))
    archived.close()

    restored = CampaignMemory.load(checkpoint, SESSION, TARGET)
    index = restored._blocked_recovery_archive_index
    try:
        assert len(restored.blocked_recovery["attempts"]) == 1
        assert restored.blocked_recovery["source_revision"] == new_source
        assert blocked_persistence.was_attempted(
            restored, SOURCE, hashlib.sha256(b"decision-0").hexdigest(), archive_index=index)
        assert blocked_persistence.was_attempted(
            restored, new_source, new_input, archive_index=index)
        assert restored.blocked_recovery_archive["entry_count"] == 1024
    finally:
        index.close()


def test_resumed_active_attempt_outcome_overrides_index_startup_snapshot(tmp_path, monkeypatch):
    _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    checkpoint.write_bytes(_encode(memory))
    archive = archive_full_tail(checkpoint, memory)
    input_sha256 = hashlib.sha256(b"new-source-active-attempt").hexdigest()
    memory.last_tick = 1025
    blocked_persistence.record_attempt(
        memory, SOURCE, input_sha256, "low choice confidence", 1025,
        archive_index=archive)
    checkpoint.write_bytes(_encode(memory))
    archive.close()

    restored = CampaignMemory.load(checkpoint, SESSION, TARGET)
    index = restored._blocked_recovery_archive_index
    try:
        # The loaded SQLite index contains the persisted pending row. Finalizing
        # it must update the authoritative active checkpoint row, not the
        # immutable lookup snapshot built at load time.
        blocked_persistence.finish_attempt(
            restored, SOURCE, input_sha256, "selected", archive_index=index)
        attempt = blocked_persistence.find_attempt(
            restored, SOURCE, input_sha256, archive_index=index)
        assert attempt["outcome"] == "selected"
        with pytest.raises(ValueError, match="not pending"):
            blocked_persistence.finish_attempt(
                restored, SOURCE, input_sha256, "rejected", archive_index=index)
    finally:
        index.close()


def test_blocked_reevaluation_archives_full_tail_and_saves_authorization_before_selection(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller_module

    _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    memory.status = "blocked"
    memory.reason = "low choice confidence"
    memory.stalled_decisions = 5
    memory.failures = {"prior-failure": 2}
    memory.history = [{"kind": "preserved", "marker": "prior history"}]
    original_bytes = _encode(memory)
    checkpoint.write_bytes(original_bytes)

    new_source = {"commit": "3" * 40, "source_sha256": "d" * 64}
    input_sha256 = hashlib.sha256(b"authorized-changed-source-input").hexdigest()
    loop = HierarchicalLoop.__new__(HierarchicalLoop)
    loop.memory = memory
    loop.memory_type = CampaignMemory
    loop.checkpoint = checkpoint
    loop.target = TARGET
    loop.max_stalled_decisions = 4
    loop.policy = "jev"
    loop.jev = _LiveClient()
    loop.provenance = {"code_revision": new_source}
    loop._blocked_recovery_archive_index = None
    loop._blocked_reevaluation_source = {
        "blocked_source_revision": SOURCE["commit"],
        "source_head": new_source["commit"],
        "previous_contract_sha256": "a" * 64,
        "decision_contract_sha256": "b" * 64,
    }
    loop._blocked_reevaluation_checkpoint_sha256 = hashlib.sha256(original_bytes).hexdigest()
    loop._reevaluate_blocked_once = True
    loop._persistence_failed = False

    saves = []

    def durable_save():
        raw = _encode(loop.memory)
        checkpoint.write_bytes(raw)
        saves.append(json.loads(raw))

    loop._save = durable_save
    snapshot = replace(MockBackend().observe(), session_id=SESSION,
                       tick=1025, world_kind="fle")
    selection_calls = []
    monkeypatch.setattr(controller_module, "select_plan", lambda *_args: (
        selection_calls.append(True)))

    loop._consume_blocked_reevaluation(
        snapshot, input_sha256, authorization_reason="low choice confidence")

    assert len(saves) == 3
    before_rotation, archived, authorized = saves
    assert before_rotation.get("blocked_recovery_archive") is None
    assert len(before_rotation["blocked_recovery"]["attempts"]) == 1024
    assert archived["blocked_recovery_archive"]["entry_count"] == 1024
    assert archived["blocked_recovery"]["attempts"] == []
    assert archived["blocked_reevaluations"] == []
    assert authorized["blocked_recovery_archive"] == archived["blocked_recovery_archive"]
    assert authorized["blocked_recovery"]["source_revision"] == new_source
    assert authorized["blocked_recovery"]["attempts"] == [{
        "source_revision": new_source,
        "decision_input_sha256": input_sha256,
        "reason": "low choice confidence",
        "tick": 1025,
        "outcome": "pending",
    }]
    assert authorized["blocked_reevaluations"][-1]["state"] == "consumed"
    assert authorized["blocked_reevaluations"][-1]["checkpoint_sha256"] == hashlib.sha256(
        original_bytes).hexdigest()
    assert authorized["stalled_decisions"] == 5
    assert authorized["failures"] == {"prior-failure": 2}
    assert authorized["history"][0] == {"kind": "preserved", "marker": "prior history"}
    assert selection_calls == []
    assert checkpoint.read_bytes() == _encode(loop.memory)

    index = loop._blocked_recovery_archive_index
    try:
        old_digest = hashlib.sha256(b"decision-77").hexdigest()
        assert index.find(SOURCE, old_digest, memory=loop.memory) is not None
        assert blocked_persistence.find_attempt(
            loop.memory, new_source, input_sha256, archive_index=index)["outcome"] == "pending"
    finally:
        index.close()


def test_live_controller_rechecks_archive_before_model_selection(tmp_path, monkeypatch):
    import jev_factorio.controller as controller_module

    _allow_storage(monkeypatch)
    monkeypatch.setattr(controller_module, "gameplay_context",
                        lambda: {"code_revision": SOURCE})
    checkpoint = tmp_path / "campaign.json"
    memory = _full_memory()
    memory.status = "blocked"
    memory.reason = "low choice confidence"
    memory.stalled_decisions = 5
    built = archive_full_tail(checkpoint, memory)
    blocked_persistence.record_attempt(
        memory, SOURCE, "f" * 64, memory.reason, 1024, archive_index=built)
    checkpoint.write_bytes(_encode(memory))
    built.close()

    backend = _LiveMockBackend()
    loop = HierarchicalLoop(
        backend, jev=_LiveClient(), policy="jev", target=TARGET,
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    loop.memory = CampaignMemory.load(checkpoint, SESSION, TARGET)
    loop._blocked_recovery_archive_index = loop.memory._blocked_recovery_archive_index
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None
    plan = Plan("archive-plan", TARGET, "A test plan",
                (Step("walk_to_iron", "near", "iron-ore"),))
    loop._work_candidates = lambda _snapshot: ([plan], "")
    selections = []
    monkeypatch.setattr(controller_module, "select_plan", lambda *_args: (
        selections.append(True) or Decision(None, "observe", "should not run")))

    digest = loop.memory.blocked_recovery_archive["head_sha256"]
    segment = checkpoint.with_name(
        checkpoint.name + ".blocked-recovery-archive") / f"segment-{digest}.json"
    with segment.open("ab") as output:
        output.write(b"changed while controller was live")
    try:
        with pytest.raises(ValueError, match="changed after verification"):
            loop.step()
        assert backend.observations == 1
        assert backend.actions == []
        assert selections == []
    finally:
        loop._blocked_recovery_archive_index.close()


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("replace_before_error", [False, True])
def test_archive_rotation_save_failure_reconciles_exact_checkpoint_and_stops_observation(
        tmp_path, monkeypatch, replace_before_error, background):
    _allow_storage(monkeypatch)
    checkpoint = tmp_path / "campaign.json"
    memory = _full_native_background_memory() if background else _full_memory()
    prior_bytes = _encode(memory)
    checkpoint.write_bytes(prior_bytes)
    loop = HierarchicalLoop.__new__(HierarchicalLoop)
    loop.memory = memory
    loop.memory_type = type(memory)
    loop.checkpoint = checkpoint
    loop.target = TARGET
    loop._blocked_recovery_archive_index = None
    loop._persistence_failed = False
    saves = []

    def fail_rotation_commit():
        saves.append(True)
        if len(saves) == 1:
            return  # Establish the durable pre-rotation base.
        if replace_before_error:
            checkpoint.write_bytes(_encode(loop.memory))
        loop._persistence_failed = True
        raise OSError("injected pointer checkpoint failure")

    loop._save = fail_rotation_commit
    observations = []
    loop._observe_snapshot = lambda: observations.append(True)
    with pytest.raises(OSError, match="pointer checkpoint failure"):
        loop._archive_full_recovery_tail()

    assert len(saves) == 2
    if replace_before_error:
        assert checkpoint.read_bytes() != prior_bytes
        assert loop.memory.blocked_recovery_archive is not None
        assert loop.memory.blocked_recovery_archive["entry_count"] == 1024
        assert loop.memory.blocked_recovery["attempts"] == []
        assert loop._blocked_recovery_archive_index is not None
        loop._blocked_recovery_archive_index.close()
    else:
        assert checkpoint.read_bytes() == prior_bytes
        assert loop.memory.blocked_recovery_archive is None
        assert len(loop.memory.blocked_recovery["attempts"]) == 1024
    if background:
        for key in ('background_job', 'background_attempt', 'background_step'):
            assert getattr(loop.memory, key) == json.loads(prior_bytes)[key]
    assert loop._persistence_failed is True
    with pytest.raises(RuntimeError, match="reconstruct before continuing"):
        loop._observe()
    assert observations == []



def _full_native_background_memory():
    from pathlib import Path
    from jev_factorio.background import BackgroundMemory
    raw = json.loads((Path(__file__).parent / 'fixtures' /
                      'native-v20-archive-background-craft.json').read_text())
    memory = BackgroundMemory.from_bytes(_encode(_full_memory()), SESSION, TARGET)
    memory.active_goal = TARGET
    memory.background_schema = 3
    for key, value in raw.items():
        setattr(memory, key, value)
    memory.background_job['session_id'] = SESSION
    # Prove that the retained production job/attempt/step form a valid checkpoint.
    return BackgroundMemory.from_bytes(_encode(memory), SESSION, TARGET)


def test_native_background_craft_survives_full_tail_rotation_and_reload(tmp_path, monkeypatch):
    from copy import deepcopy
    from jev_factorio.background import BackgroundMemory
    _allow_storage(monkeypatch)
    checkpoint = tmp_path / 'campaign.json'
    memory = _full_native_background_memory()
    keys = ('background_job', 'background_attempt', 'background_step')
    retained = {key: deepcopy(getattr(memory, key)) for key in keys}
    checkpoint.write_bytes(_encode(memory))
    loop = HierarchicalLoop.__new__(HierarchicalLoop)
    loop.memory = memory
    loop.memory_type = BackgroundMemory
    loop.checkpoint = checkpoint
    loop.target = TARGET
    loop.provenance = {'code_revision': SOURCE}
    loop._blocked_recovery_archive_index = None
    loop._persistence_failed = False
    saves = []
    def save():
        data = _encode(loop.memory)
        checkpoint.write_bytes(data)
        saves.append(json.loads(data))
    loop._save = save
    digest = hashlib.sha256(b'new-decision-during-native-craft').hexdigest()
    snapshot = replace(MockBackend().observe(), session_id=SESSION, tick=memory.last_tick)
    try:
        loop._record_persistent_attempt(snapshot, digest)
        assert len(saves) == 3
        assert len(saves[0]['blocked_recovery']['attempts']) == 1024
        assert saves[1]['blocked_recovery_archive']['entry_count'] == 1024
        assert saves[1]['blocked_recovery']['attempts'] == []
        assert saves[2]['blocked_recovery']['attempts'][0]['decision_input_sha256'] == digest
        assert saves[2]['blocked_recovery']['attempts'][0]['outcome'] == 'pending'
        for saved in saves:
            assert {key: saved[key] for key in keys} == retained
    finally:
        if loop._blocked_recovery_archive_index:
            loop._blocked_recovery_archive_index.close()
    restored = BackgroundMemory.load(checkpoint, SESSION, TARGET)
    try:
        assert {key: getattr(restored, key) for key in keys} == retained
        index = restored._blocked_recovery_archive_index
        for n in (0, 511, 1023):
            assert blocked_persistence.was_attempted(restored, SOURCE,
                hashlib.sha256(f'decision-{n}'.encode()).hexdigest(), archive_index=index)
        assert blocked_persistence.was_attempted(restored, SOURCE, digest, archive_index=index)
    finally:
        restored._blocked_recovery_archive_index.close()


@pytest.mark.parametrize('change', ['receipt', 'step', 'missing_attempt', 'failed', 'pending'])
def test_archive_rotation_rejects_unbound_or_ambiguous_background_work(tmp_path, monkeypatch, change):
    _allow_storage(monkeypatch)
    memory = _full_native_background_memory()
    if change == 'receipt': memory.background_attempt['receipt'] = 'different'
    elif change == 'step': memory.background_step['threshold'] += 1
    elif change == 'missing_attempt': memory.background_attempt = None
    elif change == 'failed': memory.background_job['failed'] = 'native receipt mismatch'
    else: memory.pending = {'action': 'factory_insert'}
    checkpoint = tmp_path / 'campaign.json'
    before = _encode(memory);checkpoint.write_bytes(before)
    with pytest.raises(ValueError):
        archive_full_tail(checkpoint, memory)
    assert checkpoint.read_bytes() == before
    assert memory.blocked_recovery_archive is None
    assert len(memory.blocked_recovery['attempts']) == 1024
    assert not checkpoint.with_name(checkpoint.name + '.blocked-recovery-archive').exists()
