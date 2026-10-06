"""Legacy checkpoint preparation is one-use and does not dispatch gameplay."""
import hashlib
import json
import os
from dataclasses import asdict

import pytest

from jev_factorio.backends.native_attachment import (
    PINNED_ASSETS, PINNED_SOURCE_COMMIT, PINNED_SOURCE_TREE, PROBE,
)
from jev_factorio.backends.native_manual_cycle_checkpoint_prep import (
    ABSENCE_COMMAND, BACKUP_NAME, INTENT_NAME,
    prepare_legacy_empty_connector_checkpoint,
    reconcile_legacy_empty_connector_checkpoint,
)
from jev_factorio.memory import CampaignMemory
from jev_factorio.memory import load_checkpoint
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.solid_controller import solid_loop_type
from test_native_manual_cycle_migration import installed_v4
from coal_supply_fixtures import INTENTS, TARGETS


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fixture(tmp_path, *, change=None, memory_type=CampaignMemory):
    memory = memory_type('retained-session', 'rocket_launch')
    memory.status = 'running'
    memory.last_tick = 123
    memory.event('prior_verified', receipt='retained')
    if change:
        change(memory)
    checkpoint = tmp_path / 'controller.json'
    before = json.dumps(asdict(memory)).encode()
    checkpoint.write_bytes(before)
    checkpoint.chmod(0o600)
    receipt = tmp_path / 'native-attachment-receipt.json'
    receipt_bytes = json.dumps({
        'schema': 'jev.native-attachment.v1', 'session_id': 'retained-session',
        'actor_unit': 2543, 'installed_source_commit': PINNED_SOURCE_COMMIT,
        'installed_source_tree': PINNED_SOURCE_TREE, 'installed_assets': PINNED_ASSETS,
    }).encode()
    receipt.write_bytes(receipt_bytes)
    receipt.chmod(0o600)
    lock = tmp_path / 'single-writer.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    paths = dict(checkpoint_path=checkpoint, receipt_path=receipt, lock_path=lock,
                 intent_path=tmp_path / INTENT_NAME,
                 backup_path=tmp_path / BACKUP_NAME)
    kwargs = dict(**paths, expected_session_id='retained-session',
                  expected_actor_unit=2543, expected_target='rocket_launch',
                  expected_checkpoint_sha256=_sha(before),
                  expected_receipt_sha256=_sha(receipt_bytes))
    return memory, before, paths, kwargs


class ReadOnlyClient:
    def __init__(self, *, absent=True, idle=True):
        self.commands = []
        self.absent = absent
        self.idle = idle

    def send_command(self, command):
        self.commands.append(command)
        if command == '/sc ' + PROBE:
            return json.dumps(installed_v4())
        assert command == ABSENCE_COMMAND
        return json.dumps({'schema': 1, 'session_id': 'retained-session',
                           'actor_unit': 2543, 'absent': self.absent,
                           'idle': self.idle, 'coal_clear': True})


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
def test_legacy_none_prepares_only_empty_binding_and_consumes_one_use(tmp_path):
    _, before, paths, kwargs = _fixture(tmp_path)
    client = ReadOnlyClient()
    result = prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert result['status'] == 'checkpoint_prepared'
    assert client.commands == ['/sc ' + PROBE, ABSENCE_COMMAND]
    assert paths['backup_path'].read_bytes() == before
    after = json.loads(paths['checkpoint_path'].read_bytes())
    original = json.loads(before)
    assert after.pop('connector_ownership') == {
        'protocol': 1, 'session_id': 'retained-session', 'routes': {}}
    original.pop('connector_ownership')
    original.pop('capital_investment', None)  # normal checkpoint serializer omits null
    if original.get('async_decision') is None:
        original.pop('async_decision', None)  # optional async extension preserves historical null omission
    original.pop('blocked_recovery', None)  # empty extension state retains historical bytes
    original.pop('blocked_recovery_archive', None)  # absent archive retains historical bytes
    original.pop('two_stage_decision', None)
    original.pop('planner_fault_recovery', None)
    assert after == original
    assert paths['receipt_path'].read_bytes() == json.dumps({
        'schema': 'jev.native-attachment.v1', 'session_id': 'retained-session',
        'actor_unit': 2543, 'installed_source_commit': PINNED_SOURCE_COMMIT,
        'installed_source_tree': PINNED_SOURCE_TREE, 'installed_assets': PINNED_ASSETS,
    }).encode()
    assert reconcile_legacy_empty_connector_checkpoint(**paths) == 'prepared_checkpoint_present'
    with pytest.raises(RuntimeError, match='already reserved'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert client.commands == ['/sc ' + PROBE, ABSENCE_COMMAND]


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
def test_preparation_and_reconciliation_preserve_nonempty_blocked_recovery(tmp_path):
    from jev_factorio.blocked_persistence import record_attempt

    def blocked_ledger(memory):
        memory.status = 'blocked'
        memory.reason = 'low choice confidence'
        record_attempt(memory, {'commit': 'a' * 40, 'source_sha256': 'b' * 64},
                       'c' * 64, memory.reason, 123)

    original, before, paths, kwargs = _fixture(tmp_path, change=blocked_ledger)
    expected_ledger = original.blocked_recovery
    client = ReadOnlyClient()
    result = prepare_legacy_empty_connector_checkpoint(client, **kwargs)

    prepared = load_checkpoint(paths['checkpoint_path'], 'retained-session', 'rocket_launch')
    assert prepared.blocked_recovery == expected_ledger
    assert prepared.connector_ownership == {
        'protocol': 1, 'session_id': 'retained-session', 'routes': {}}
    assert result['after_sha256'] == _sha(paths['checkpoint_path'].read_bytes())
    assert paths['backup_path'].read_bytes() == before
    assert reconcile_legacy_empty_connector_checkpoint(**paths) == 'prepared_checkpoint_present'


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
def test_composed_solid_coal_checkpoint_preserves_all_other_fields(tmp_path):
    composed = coal_loop_type(solid_loop_type(HierarchicalLoop)).memory_type

    def establish_epoch(memory):
        memory.solid_intents = INTENTS
        memory.coal_targets = TARGETS
        memory.solid_epoch = {'actor_index': 1, 'surface_index': 1, 'force_index': 1}
        memory.coal_epoch = dict(memory.solid_epoch)
        memory.solid_science_policy = True
        memory.coal_kit_policy = True
        memory.coal_economic_admission = True
        memory.coal_supply_schema = 2

    _, before, paths, kwargs = _fixture(tmp_path, change=establish_epoch,
                                        memory_type=composed)
    prior = load_checkpoint(paths['checkpoint_path'], 'retained-session', 'rocket_launch')
    assert prior.connector_ownership is None
    client = ReadOnlyClient()
    result = prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    after = load_checkpoint(paths['checkpoint_path'], 'retained-session', 'rocket_launch')
    assert type(after).__name__ == type(prior).__name__ == 'CoalMemory'
    original_fields = asdict(prior)
    prepared_fields = asdict(after)
    original_fields.pop('connector_ownership')
    assert prepared_fields.pop('connector_ownership') == {
        'protocol': 1, 'session_id': 'retained-session', 'routes': {}}
    assert prepared_fields == original_fields
    assert paths['backup_path'].read_bytes() == before
    assert result['after_sha256'] == _sha(paths['checkpoint_path'].read_bytes())
    assert reconcile_legacy_empty_connector_checkpoint(**paths) == 'prepared_checkpoint_present'


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
@pytest.mark.parametrize('change', [
    lambda m: setattr(m, 'connector_ownership', {
        'protocol': 1, 'session_id': 'retained-session', 'routes': {}}),
    lambda m: setattr(m, 'capital_investment', {'owner': 'open'}),
    lambda m: setattr(m, 'reservations', {'owner': {'iron-plate': 1}}),
])
def test_preparation_rejects_existing_binding_or_ownership_before_game_query(tmp_path, change):
    _, before, paths, kwargs = _fixture(tmp_path, change=change)
    client = ReadOnlyClient()
    with pytest.raises((RuntimeError, ValueError)):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert client.commands == []
    assert paths['checkpoint_path'].read_bytes() == before
    assert not paths['intent_path'].exists()


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
@pytest.mark.parametrize('native', [(False, True), (True, False)])
def test_preparation_rejects_present_native_connector_or_busy_actor(tmp_path, native):
    _, before, paths, kwargs = _fixture(tmp_path)
    client = ReadOnlyClient(absent=native[0], idle=native[1])
    with pytest.raises(RuntimeError, match='absence or actor idleness'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert paths['checkpoint_path'].read_bytes() == before
    assert not paths['intent_path'].exists()


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
def test_ambiguous_write_requires_reconciliation_and_never_retries(tmp_path, monkeypatch):
    _, before, paths, kwargs = _fixture(tmp_path)
    original_save = CampaignMemory.save

    def saved_then_lost_ack(self, path):
        original_save(self, path)
        raise OSError('post-write failure')

    monkeypatch.setattr(CampaignMemory, 'save', saved_then_lost_ack)
    client = ReadOnlyClient()
    with pytest.raises(RuntimeError, match='outcome unknown'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert reconcile_legacy_empty_connector_checkpoint(**paths) == 'prepared_checkpoint_present'
    with pytest.raises(RuntimeError, match='already reserved'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert len(client.commands) == 2
    assert paths['backup_path'].read_bytes() == before
    paths['checkpoint_path'].write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='changed beyond'):
        reconcile_legacy_empty_connector_checkpoint(**paths)


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
def test_failure_before_checkpoint_write_consumes_attempt_without_replay(tmp_path, monkeypatch):
    _, before, paths, kwargs = _fixture(tmp_path)

    def write_failed(self, path):
        raise OSError('failed before replace')

    monkeypatch.setattr(CampaignMemory, 'save', write_failed)
    client = ReadOnlyClient()
    with pytest.raises(RuntimeError, match='outcome unknown'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert paths['checkpoint_path'].read_bytes() == before
    assert reconcile_legacy_empty_connector_checkpoint(**paths) == 'old_checkpoint_attempt_consumed'
    with pytest.raises(RuntimeError, match='already reserved'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert len(client.commands) == 2


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
def test_stale_checkpoint_or_original_receipt_rejects_before_native_query(tmp_path):
    _, before, paths, kwargs = _fixture(tmp_path)
    client = ReadOnlyClient()
    paths['checkpoint_path'].write_bytes(before + b' ')
    with pytest.raises(RuntimeError, match='evidence changed'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert client.commands == []
    paths['checkpoint_path'].write_bytes(before)
    paths['receipt_path'].write_bytes(paths['receipt_path'].read_bytes() + b' ')
    with pytest.raises(RuntimeError, match='evidence changed'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert client.commands == []


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX owner lock')
def test_existing_v5_installation_attempt_blocks_preparation(tmp_path):
    _, before, paths, kwargs = _fixture(tmp_path)
    (tmp_path / 'native-manual-cycle-v5.intent.jsonl').write_text('{"phase":"unknown"}\n')
    client = ReadOnlyClient()
    with pytest.raises(RuntimeError, match='Existing v5 installation attempt'):
        prepare_legacy_empty_connector_checkpoint(client, **kwargs)
    assert paths['checkpoint_path'].read_bytes() == before
    assert client.commands == []
