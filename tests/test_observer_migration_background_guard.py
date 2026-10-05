"""Migration must not cross an unresolved composed background-work boundary."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict

import pytest

from jev_factorio.backends.native_attachment import (
    PINNED_ASSETS,
    PINNED_SOURCE_COMMIT,
    PINNED_SOURCE_TREE,
    PROBE,
)
from jev_factorio.backends.native_observation_migration import (
    LEGACY_OBSERVATION_PROFILE,
    SENTINEL,
    _manifest,
    migrate_legacy_observation_v2,
)
from jev_factorio.memory import load_checkpoint
from test_background_work import ReceiptBackend, controller
from test_native_observation_migration import legacy


pytestmark = pytest.mark.skipif(os.name != "posix", reason="migration requires the POSIX owner-lock host")


class MigrationClient:
    """Synthetic readback/mutation boundary; never contacts a native backend."""

    def __init__(self, session_id: str):
        self.row = legacy()
        self.row["session_id"] = session_id
        self.row["actor_unit"] = 9
        self.commands = []
        self.mutations = []

    def send_command(self, command: str) -> str:
        self.commands.append(command)
        if command == "/sc " + PROBE:
            return json.dumps(self.row)
        self.mutations.append(command)
        self.row["native_installation"] = _manifest(self.row)
        return "JEV_ATOMIC_READY|2\n" + SENTINEL


def _active_background_checkpoint(tmp_path, schema: int):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    record = loop.step()
    assert record["verified"] is False
    assert record["background_job"]

    checkpoint = tmp_path / "state.json"
    raw = checkpoint.read_bytes()
    saved = json.loads(raw)
    if schema == 2:
        saved["background_schema"] = 2
        saved["background_step"] = None
        raw = json.dumps(saved).encode("utf-8")
        checkpoint.write_bytes(raw)
    elif schema == 1:
        # Supported legacy extension shape: a paid job can predate its attempt record.
        saved["background_schema"] = 1
        saved["background_attempt"] = None
        saved["background_step"] = None
        raw = json.dumps(saved).encode("utf-8")
        checkpoint.write_bytes(raw)
    else:
        assert schema == 3
    checkpoint.chmod(0o600)
    memory = load_checkpoint(checkpoint, backend.state.session_id, loop.target)
    assert memory.background_job is not None
    if schema == 3:
        assert memory.background_step is not None
    else:
        assert memory.background_step is None
    assert memory.pending is None
    assert memory.attempt is None
    assert memory.transfer_recovery is None
    assert memory.status == "running"
    return backend, loop, checkpoint, raw, memory


def _migration_inputs(tmp_path, memory, loop):
    receipt = tmp_path / "attachment.json"
    receipt_raw = json.dumps({
        "schema": "jev.native-attachment.v1",
        "session_id": memory.session_id,
        "actor_unit": 9,
        "installed_source_commit": PINNED_SOURCE_COMMIT,
        "installed_source_tree": PINNED_SOURCE_TREE,
        "installed_assets": PINNED_ASSETS,
    }, sort_keys=True).encode("utf-8")
    receipt.write_bytes(receipt_raw)
    receipt.chmod(0o600)

    lock = tmp_path / "synthetic-owner-lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    client = MigrationClient(memory.session_id)
    kwargs = {
        "checkpoint_path": tmp_path / "state.json",
        "receipt_path": receipt,
        "lock_path": lock,
        "expected_session_id": memory.session_id,
        "expected_actor_unit": 9,
        "expected_target": loop.target,
        "expected_checkpoint_sha256": hashlib.sha256(
            (tmp_path / "state.json").read_bytes()).hexdigest(),
        "expected_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
    }
    return client, receipt, receipt_raw, kwargs


@pytest.mark.parametrize("schema", [1, 2, 3], ids=["legacy-job-only", "job-attempt-v2", "job-attempt-step-v3"])
def test_unresolved_background_extension_blocks_one_shot_migration_without_changing_checkpoint(
    tmp_path, schema
):
    backend, loop, checkpoint, checkpoint_raw, before = _active_background_checkpoint(tmp_path, schema)
    assert before.background_job["inputs"] == {"iron-plate": 10}
    assert before.background_job["outputs"] == {"automation-science-pack": 10}
    if schema == 1:
        assert before.background_attempt is None
    else:
        assert before.background_attempt is not None
    if schema == 3:
        assert before.background_step is not None
    else:
        assert before.background_step is None

    client, receipt, receipt_raw, kwargs = _migration_inputs(tmp_path, before, loop)
    before_memory = asdict(before)

    with pytest.raises(RuntimeError, match="unresolved work"):
        migrate_legacy_observation_v2(client, **kwargs)

    after = load_checkpoint(checkpoint, backend.state.session_id, loop.target)
    assert checkpoint.read_bytes() == checkpoint_raw
    assert receipt.read_bytes() == receipt_raw
    assert asdict(after) == before_memory
    assert after.background_job == before.background_job
    assert after.background_attempt == before.background_attempt
    assert after.background_step == before.background_step
    assert after.attempt_outcomes == before.attempt_outcomes
    assert after.history == before.history
    assert after.failures == before.failures
    assert after.reservations == before.reservations
    assert client.mutations == []
    assert not any(command != "/sc " + PROBE for command in client.commands)


def test_completed_background_craft_is_resumed_reconciled_then_migration_remains_one_shot(tmp_path):
    backend = ReceiptBackend()
    original = controller(backend, tmp_path)
    admitted = original.step()
    assert admitted["verified"] is False and admitted["background_job"]

    backend.complete()
    resumed = controller(backend, tmp_path, resume=True)
    resumed.reconcile_only()

    checkpoint = tmp_path / "state.json"
    checkpoint_raw = checkpoint.read_bytes()
    settled = load_checkpoint(checkpoint, backend.state.session_id, resumed.target)
    assert settled.background_job is None
    assert settled.background_attempt is None
    assert settled.background_step is None
    assert settled.background_schema == 2
    assert len(settled.attempt_outcomes) == 1
    assert settled.attempt_outcomes[0]["outcome"] == "verified"
    assert any(event.get("kind") == "background_job_completed" for event in settled.history)

    client, receipt, receipt_raw, kwargs = _migration_inputs(tmp_path, settled, resumed)
    result = migrate_legacy_observation_v2(client, **kwargs)
    assert result["native_installation"]["profile"] == LEGACY_OBSERVATION_PROFILE
    assert len(client.mutations) == 1
    assert checkpoint.read_bytes() == checkpoint_raw
    assert receipt.read_bytes() == receipt_raw
    assert load_checkpoint(checkpoint, backend.state.session_id, resumed.target).attempt_outcomes == settled.attempt_outcomes
