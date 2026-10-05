"""One-way, source-bound migration of the retained e759 coherent observer.

No controller invokes this automatically. The existing owner must first clear
all pending work, quiesce dispatch, and supply exact private evidence hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from importlib.resources import files

from ..memory import load_checkpoint
from .native_attachment import (
    CALLBACKS_EXPR, LEGACY_OBSERVATION_PROFILE, LEGACY_OBSERVATION_SHA256,
    NATIVE_SCHEMA, PINNED_ASSETS, readback,
)


SENTINEL = 'JEV_NATIVE_OBSERVATION_MIGRATED|1'


def _private_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Migration evidence is missing or is a symlink')
    stat = path.stat()
    if os.name == 'posix' and (stat.st_uid != os.geteuid() or stat.st_mode & 0o077):
        raise RuntimeError('Migration evidence must be owned by the controller and private')
    return path.read_bytes()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest(attachment: dict) -> dict:
    if (attachment.get('native_installation') is not False
            or attachment['modules']['connector_ownership']
            or attachment['modules']['successors']):
        raise RuntimeError('Legacy migration requires unmodified e759 installation')
    assets = {name: PINNED_ASSETS[name]
              for name, enabled in attachment['modules'].items() if enabled}
    assets['observation_v2'] = LEGACY_OBSERVATION_SHA256
    return {'schema': NATIVE_SCHEMA, 'profile': LEGACY_OBSERVATION_PROFILE,
            'session_id': attachment['session_id'],
            'actor_unit': attachment['actor_unit'], 'assets': assets}


def _command(attachment: dict) -> str:
    """The only mutation is one observer closure plus one installation receipt."""
    source = files('jev_factorio').joinpath('lua/observation_v2.lua').read_bytes()
    if _digest(source) != LEGACY_OBSERVATION_SHA256:
        raise RuntimeError('Migration source is not the reviewed output-tile observer')
    body = source.decode('utf-8')
    manifest = json.dumps(_manifest(attachment), sort_keys=True, separators=(',', ':'))
    session = json.dumps(attachment['session_id'])
    actor = attachment['actor_unit']
    return '/sc ' + (
        'local storage=assert(jev_fle_runtime); local c=assert(storage.campaign); '
        'local f=assert(storage.fair); local a=assert(storage.agent_characters[1]); '
        'assert(storage.native_installation==nil and storage.jev_session_id==' + session + '); '
        'assert(a.valid and a.unit_number==' + str(actor) + ' and f.actor().character==a); '
        'assert(game.speed==1 and not game.tick_paused); '
        'assert(not f.job or f.job.status=="completed" or f.job.status=="failed"); '
        'assert(not f.actor().walking_state.walking and not f.actor().mining_state.mining); '
        'local old=assert(c.observation_snapshot_v2); '
        'local ok,err=pcall(function() do\n' + body + '\nend; '
        'assert(c.observation_snapshot_v2~=old); '
        'local n=helpers.json_to_table(' + json.dumps(manifest) + '); '
        'n.callbacks=' + CALLBACKS_EXPR + '; storage.native_installation=n end); '
        'if not ok then c.observation_snapshot_v2=old; '
        'storage.native_installation=nil; error(err) end; '
        'rcon.print(' + json.dumps(SENTINEL) + ')'
    )


def migrate_legacy_observation_v2(client, *, checkpoint_path: Path,
                                  receipt_path: Path, lock_path: Path,
                                  expected_session_id: str, expected_actor_unit: int,
                                  expected_target: str, expected_checkpoint_sha256: str,
                                  expected_receipt_sha256: str) -> dict:
    """Perform one migration under the existing owner's single-writer lock.

    A lost RCON acknowledgement is ambiguous. Never call this again merely to
    retry; inspect the native manifest and original receipts read-only.
    """
    if os.name != 'posix':
        raise RuntimeError('Native migration requires the POSIX owner-lock host')
    import fcntl

    lock_path = Path(lock_path)
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError('Existing single-writer lock is required')
    lock_stat = lock_path.stat()
    if lock_stat.st_uid != os.geteuid() or lock_stat.st_mode & 0o077:
        raise RuntimeError('Single-writer lock must be owned by the controller and private')
    with lock_path.open('r+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        checkpoint_path, receipt_path = Path(checkpoint_path), Path(receipt_path)
        checkpoint = _private_bytes(checkpoint_path)
        receipt = _private_bytes(receipt_path)
        if (_digest(checkpoint) != expected_checkpoint_sha256
                or _digest(receipt) != expected_receipt_sha256):
            raise RuntimeError('Migration evidence hash changed')
        memory = load_checkpoint(checkpoint_path, expected_session_id, expected_target)
        background_pending = any(
            getattr(memory, field, None) is not None
            for field in ('background_job', 'background_attempt', 'background_step')
        )
        if (memory.status not in {'running', 'blocked'} or memory.pending is not None
                or memory.attempt is not None or memory.transfer_recovery is not None
                or background_pending):
            raise RuntimeError('Controller has unresolved work; migration refused')
        attachment = readback(client, receipt_path=receipt_path)
        if (attachment['session_id'] != expected_session_id
                or attachment['actor_unit'] != expected_actor_unit):
            raise RuntimeError('Native session or actor changed')
        _manifest(attachment)
        if (_digest(_private_bytes(checkpoint_path)) != expected_checkpoint_sha256
                or _digest(_private_bytes(receipt_path)) != expected_receipt_sha256):
            raise RuntimeError('Migration evidence changed during preflight')
        command = _command(attachment)
        try:
            response = client.send_command(command)
        except Exception as exc:
            raise RuntimeError(
                'Migration outcome unknown; inspect native manifest read-only, never retry'
            ) from exc
        if not isinstance(response, str) or not response.strip().endswith(SENTINEL):
            raise RuntimeError('Migration acknowledgement absent; inspect native manifest read-only')
        after = readback(client)
        if (after['session_id'] != expected_session_id
                or after['actor_unit'] != expected_actor_unit
                or after['native_installation']['profile'] != LEGACY_OBSERVATION_PROFILE):
            raise RuntimeError('Migration postcondition failed; stop dispatch')
        return after
