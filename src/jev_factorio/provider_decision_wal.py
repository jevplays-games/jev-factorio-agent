"""Unused, bounded write-ahead ledger for future provider-decision replay.

This module is deliberately not wired into the synchronous or asynchronous
controller. It records a request fingerprint and a small, validated answer
projection; it never stores request bodies, headers, raw provider responses,
game-action attempts, or arbitrary exception text. Credential-like result field
names are rejected, and callers must pass only secret-free answer values.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator
from uuid import uuid4

from .operational_safety import atomic_json

if os.name == "nt":
    import msvcrt
else:
    import fcntl


NOT_SENT = "not_sent"
MAY_HAVE_BEEN_SENT = "may_have_been_sent"
RESPONSE_RECEIVED = "response_received"

MAX_LEDGER_BYTES = 1_048_576
MAX_RECORDS = 128
MAX_UNRESOLVED_RECORDS = 8
MAX_ACTIVE_PER_ACTOR = 1
MAX_REQUESTS_PER_OBSERVATION = 3
MAX_PROCESS_LOCK_PATHS = 64
MAX_REQUEST_BYTES = 65_536
MAX_RESULT_BYTES = 32_768
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 16_384
MAX_STRING_CHARS = 8192
MAX_KEY_CHARS = 256
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROVIDER_ID = re.compile(
    r"^(?:(?:typesafe|cloudflare|mock)(?::sha256:[0-9a-f]{64})?"
    r"|other:sha256:[0-9a-f]{64})$"
)
_ERROR_CATEGORIES = frozenset({
    "application_schema", "authentication_authorization", "account_quota",
    "rate_limit", "service_network", "unknown_outcome", "local_admission",
})
_SENSITIVE_RESULT_KEY_PARTS = (
    "apikey", "authorization", "credential", "password", "secret",
    "accesstoken", "refreshtoken", "bearertoken", "header", "cookie",
)
_PROCESS_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class ProviderDecisionWALError(RuntimeError):
    """Base class for fail-closed provider-decision ledger errors."""


class WALIntegrityError(ProviderDecisionWALError):
    """Missing, malformed, replaced, oversized, or corrupt ledger evidence."""


class WALBusyError(ProviderDecisionWALError):
    """Another process currently owns the ledger writer lock."""


class WALCapacityError(ProviderDecisionWALError):
    """A fixed record, unresolved-request, or byte bound has been reached."""


class WALIdentityConflict(ProviderDecisionWALError):
    """A request or logical decision identity was reused with different input."""


class WALNotFound(ProviderDecisionWALError):
    """The requested immutable decision identity has no reservation."""


class WALInvalidTransition(ProviderDecisionWALError):
    """The durable request phase does not permit the requested transition."""


class WALAmbiguousRequest(WALInvalidTransition):
    """A provider request may have been sent; this ledger will not resend it."""


class WALResponseConsumed(WALInvalidTransition):
    """The saved response was already committed by the future controller."""


def _check_json(value: Any, *, max_bytes: int) -> bytes:
    nodes = 0
    active: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON value exceeds the decision ledger shape bound")
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is str:
            if len(item) > MAX_STRING_CHARS:
                raise ValueError("JSON string exceeds the decision ledger bound")
            item.encode("utf-8")
            return
        if kind is int:
            if abs(item) > (2 ** 63 - 1):
                raise ValueError("JSON integer exceeds the decision ledger bound")
            return
        if kind is float:
            if not math.isfinite(item):
                raise ValueError("Non-finite JSON number")
            return
        if kind not in (dict, list):
            raise ValueError("Decision ledger accepts only built-in JSON values")
        marker = id(item)
        if marker in active:
            raise ValueError("Cyclic JSON value")
        active.add(marker)
        try:
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str or not key or len(key) > MAX_KEY_CHARS:
                        raise ValueError("Invalid JSON object key")
                    key.encode("utf-8")
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
        finally:
            active.remove(marker)

    visit(value, 0)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as error:
        raise ValueError("Value is not canonical JSON") from error
    if len(encoded) > max_bytes:
        raise ValueError("JSON value exceeds the decision ledger byte bound")
    return encoded


def canonical_sha256(value: Any, *, max_bytes: int = MAX_REQUEST_BYTES) -> str:
    """Hash a bounded JSON value using stable UTF-8 canonical encoding."""
    return hashlib.sha256(_check_json(value, max_bytes=max_bytes)).hexdigest()


def _request_sha256(request: Any) -> str:
    if type(request) is not dict:
        raise TypeError("request must be a built-in JSON object")
    encoded = _check_json(request, max_bytes=MAX_REQUEST_BYTES)
    detached = json.loads(encoded.decode("utf-8"), object_pairs_hook=_duplicate_free_object)
    _reject_sensitive_keys(detached)
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Invalid {name} identity")
    return value


@dataclass(frozen=True, slots=True)
class ProviderDecisionIdentity:
    """Immutable provider request binding, distinct from game-action IDs.

    provider_id is a supported provider label or a sha256 fingerprint for its
    non-secret endpoint/config identity; credentials and credential URLs are
    not valid provider IDs.
    """

    session_id: str
    actor_id: str
    observation_id: str
    decision_id: str
    provider_id: str
    model_id: str
    request_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        for name in (
            "session_id", "actor_id", "observation_id", "decision_id",
            "provider_id", "model_id", "request_id",
        ):
            _identifier(getattr(self, name), name)
        if _PROVIDER_ID.fullmatch(self.provider_id) is None:
            raise ValueError("provider_id must be a supported label or config fingerprint")

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "actor_id": self.actor_id,
            "observation_id": self.observation_id,
            "decision_id": self.decision_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "request_id": self.request_id,
        }


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Detached immutable view of one provider request and its durable phase."""

    identity: ProviderDecisionIdentity
    request_sha256: str
    phase: str
    state: str
    result_sha256: str | None
    result: Any
    error_category: str | None
    event_count: int


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _reject_sensitive_keys(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            for key, child in item.items():
                normalized = "".join(character for character in key.lower() if character.isalnum())
                if any(part in normalized for part in _SENSITIVE_RESULT_KEY_PARTS):
                    raise ValueError("Decision payload contains a credential-like field")
                stack.append(child)
        elif type(item) is list:
            stack.extend(item)


def _identity_from_dict(value: Any) -> ProviderDecisionIdentity:
    names = {
        "session_id", "actor_id", "observation_id", "decision_id",
        "provider_id", "model_id", "request_id",
    }
    if type(value) is not dict or set(value) != names:
        raise WALIntegrityError("Invalid decision identity in ledger")
    try:
        return ProviderDecisionIdentity(**value)
    except (TypeError, ValueError) as error:
        raise WALIntegrityError("Invalid decision identity in ledger") from error


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_check_json(value, max_bytes=MAX_LEDGER_BYTES)).hexdigest()


def _record_unsigned(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "record_sha256"}


def _seal_record(record: dict) -> None:
    record["record_sha256"] = _digest_json(_record_unsigned(record))


def _seal_document(document: dict) -> None:
    unsigned = {key: value for key, value in document.items() if key != "ledger_sha256"}
    document["ledger_sha256"] = _digest_json(unsigned)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_stamp(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev, info.st_ino, info.st_size,
        info.st_mtime_ns, stat.S_IMODE(info.st_mode),
    )


@contextmanager
def _os_writer_lock(path: Path) -> Iterator[None]:
    """Fail-fast interprocess lock; kernel release recovers a crashed owner."""
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise WALIntegrityError("Decision ledger parent must be an existing real directory")
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise WALIntegrityError("Decision ledger lock must not be a symlink")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(lock_path, flags | nofollow)
        except OSError as error:
            raise WALIntegrityError("Cannot safely open decision ledger lock") from error
    locked = False
    failed = False
    try:
        before = os.fstat(descriptor)
        by_path = lock_path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(by_path.st_mode)
                or _file_stamp(before)[:2] != _file_stamp(by_path)[:2]
                or getattr(before, "st_nlink", 1) != 1
                or (os.name == "posix" and stat.S_IMODE(before.st_mode) & 0o077)):
            raise WALIntegrityError("Decision ledger lock identity or permissions are invalid")
        if created:
            if os.name == "posix":
                _sync_directory(parent)
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise WALBusyError("Provider decision ledger already has a writer") from error
            raise
        after = os.fstat(descriptor)
        current_path = lock_path.stat(follow_symlinks=False)
        if (_file_stamp(after)[:2] != _file_stamp(current_path)[:2]
                or not stat.S_ISREG(current_path.st_mode)):
            raise WALIntegrityError("Decision ledger lock changed while acquiring it")
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if locked:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException:
            if not failed:
                raise
        finally:
            try:
                os.close(descriptor)
            except BaseException:
                if not failed:
                    raise


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    """Serialize same-process threads and independent processes on one ledger."""
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _PROCESS_LOCKS_GUARD:
        entry = _PROCESS_LOCKS.get(key)
        if entry is None:
            if len(_PROCESS_LOCKS) >= MAX_PROCESS_LOCK_PATHS:
                raise WALCapacityError("Process ledger lock table is full")
            lock = threading.Lock()
            users = 0
        else:
            lock, users = entry
        _PROCESS_LOCKS[key] = (lock, users + 1)
        if not lock.acquire(blocking=False):
            if users:
                _PROCESS_LOCKS[key] = (lock, users)
            else:
                del _PROCESS_LOCKS[key]
            raise WALBusyError("Provider decision ledger already has a local writer")
    failed = False
    try:
        with _os_writer_lock(path):
            yield
    except BaseException:
        failed = True
        raise
    finally:
        with _PROCESS_LOCKS_GUARD:
            try:
                lock.release()
            except BaseException:
                if not failed:
                    raise
            if users:
                _PROCESS_LOCKS[key] = (lock, users)
            else:
                _PROCESS_LOCKS.pop(key, None)


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("Duplicate JSON object key")
        value[key] = child
    return value


def _read_document(path: Path) -> dict:
    if path.is_symlink():
        raise WALIntegrityError("Decision ledger must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise WALNotFound("Provider decision ledger has not been initialized") from error
    except OSError as error:
        raise WALIntegrityError("Cannot safely open provider decision ledger") from error
    try:
        before = os.fstat(descriptor)
        by_path = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(by_path.st_mode)
                or _file_stamp(before) != _file_stamp(by_path)
                or getattr(before, "st_nlink", 1) != 1
                or (os.name == "posix" and stat.S_IMODE(before.st_mode) & 0o077)
                or before.st_size > MAX_LEDGER_BYTES):
            raise WALIntegrityError("Decision ledger file identity, permissions, or size is invalid")
        chunks = []
        remaining = MAX_LEDGER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        after_path = path.stat(follow_symlinks=False)
        if (_file_stamp(before) != _file_stamp(after)
                or _file_stamp(after) != _file_stamp(after_path)):
            raise WALIntegrityError("Decision ledger changed while being read")
        payload = b"".join(chunks)
        if len(payload) > MAX_LEDGER_BYTES:
            raise WALIntegrityError("Decision ledger exceeds its size bound")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_free_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number")),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise WALIntegrityError("Provider decision ledger is malformed") from error
    try:
        return _validate_document(document)
    except WALIntegrityError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as error:
        raise WALIntegrityError("Provider decision ledger has invalid field values") from error


def _validate_result(value: Any, identity: ProviderDecisionIdentity) -> tuple[dict, str]:
    if type(value) is not dict or set(value) != {
        "answers", "usage", "requested_model", "resolved_model",
    }:
        raise ValueError("Provider decision result has an unsupported shape")
    if type(value["answers"]) is not dict:
        raise ValueError("Provider decision answers must be an object")
    if value["requested_model"] != identity.model_id:
        raise ValueError("Provider decision result model differs from its reservation")
    if value["resolved_model"] is not None:
        _identifier(value["resolved_model"], "resolved model")
    encoded = _check_json(value, max_bytes=MAX_RESULT_BYTES)
    detached = json.loads(encoded.decode("utf-8"), object_pairs_hook=_duplicate_free_object)
    _reject_sensitive_keys(detached)
    return detached, hashlib.sha256(encoded).hexdigest()


def _validate_document(document: Any) -> dict:
    if type(document) is not dict or set(document) != {
        "schema", "last_sequence", "records", "ledger_sha256",
    }:
        raise WALIntegrityError("Invalid provider decision ledger envelope")
    if type(document["schema"]) is not int or document["schema"] != 1:
        raise WALIntegrityError("Unsupported provider decision ledger schema")
    if type(document["last_sequence"]) is not int or document["last_sequence"] < 0:
        raise WALIntegrityError("Invalid provider decision ledger sequence")
    if type(document["records"]) is not list or len(document["records"]) > MAX_RECORDS:
        raise WALIntegrityError("Invalid provider decision ledger records")
    if type(document["ledger_sha256"]) is not str or _HASH.fullmatch(document["ledger_sha256"]) is None:
        raise WALIntegrityError("Invalid provider decision ledger digest")

    seen_request_ids: set[str] = set()
    seen_logical: set[tuple[str, str, str, str]] = set()
    seen_request_contexts: set[tuple[str, str, str, str, str, str]] = set()
    sequences: set[int] = set()
    unresolved = 0
    for record in document["records"]:
        if type(record) is not dict or set(record) != {
            "identity", "request_sha256", "events", "result", "error_category", "record_sha256",
        }:
            raise WALIntegrityError("Invalid provider decision record")
        identity = _identity_from_dict(record["identity"])
        request_hash = record["request_sha256"]
        if type(request_hash) is not str or _HASH.fullmatch(request_hash) is None:
            raise WALIntegrityError("Invalid provider request digest")
        logical = (identity.session_id, identity.actor_id, identity.observation_id, identity.decision_id)
        context = (
            identity.session_id, identity.actor_id, identity.observation_id,
            identity.provider_id, identity.model_id, request_hash,
        )
        if (identity.request_id in seen_request_ids or logical in seen_logical
                or context in seen_request_contexts):
            raise WALIntegrityError("Duplicate provider decision identity in ledger")
        seen_request_ids.add(identity.request_id)
        seen_logical.add(logical)
        seen_request_contexts.add(context)
        events = record["events"]
        if type(events) is not list or not 1 <= len(events) <= 4:
            raise WALIntegrityError("Invalid provider decision transition history")
        previous_hash = None
        previous_sequence = 0
        previous_kind = None
        result_hash = None
        error_category = None
        for index, event in enumerate(events):
            if type(event) is not dict:
                raise WALIntegrityError("Invalid provider decision transition")
            kind = event.get("kind")
            if type(kind) is not str:
                raise WALIntegrityError("Invalid provider decision transition kind")
            fields = {
                "reserved": {"sequence", "kind", "phase", "previous_event_sha256", "event_sha256"},
                "dispatch_authorized": {"sequence", "kind", "phase", "previous_event_sha256", "event_sha256"},
                "response_saved": {"sequence", "kind", "phase", "previous_event_sha256", "result_sha256", "event_sha256"},
                "consumed": {"sequence", "kind", "phase", "previous_event_sha256", "result_sha256", "event_sha256"},
                "failed": {"sequence", "kind", "phase", "previous_event_sha256", "error_category", "event_sha256"},
            }.get(kind)
            if fields is None or set(event) != fields:
                raise WALIntegrityError("Unknown or malformed provider decision transition")
            sequence = event["sequence"]
            if (type(sequence) is not int or sequence < 1 or sequence in sequences
                    or sequence <= previous_sequence or sequence > document["last_sequence"]):
                raise WALIntegrityError("Provider decision sequence is invalid")
            sequences.add(sequence)
            previous_sequence = sequence
            if event["previous_event_sha256"] != previous_hash:
                raise WALIntegrityError("Provider decision transition chain is broken")
            event_hash = event["event_sha256"]
            unsigned_event = {key: value for key, value in event.items() if key != "event_sha256"}
            if type(event_hash) is not str or _HASH.fullmatch(event_hash) is None:
                raise WALIntegrityError("Invalid provider decision transition digest")
            if _digest_json(unsigned_event) != event_hash:
                raise WALIntegrityError("Provider decision transition digest mismatch")
            phase = event["phase"]
            if (type(phase) is not str
                    or phase not in {NOT_SENT, MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED}):
                raise WALIntegrityError("Invalid provider delivery phase")
            if index == 0:
                if kind != "reserved" or phase != NOT_SENT or previous_hash is not None:
                    raise WALIntegrityError("Provider decision history does not begin with reservation")
            elif kind == "dispatch_authorized":
                if previous_kind != "reserved" or phase != MAY_HAVE_BEEN_SENT:
                    raise WALIntegrityError("Invalid provider dispatch transition")
            elif kind == "response_saved":
                if previous_kind != "dispatch_authorized" or phase != RESPONSE_RECEIVED:
                    raise WALIntegrityError("Invalid provider response transition")
                result_hash = event["result_sha256"]
                if type(result_hash) is not str or _HASH.fullmatch(result_hash) is None:
                    raise WALIntegrityError("Invalid provider result digest")
            elif kind == "consumed":
                if previous_kind != "response_saved" or phase != RESPONSE_RECEIVED:
                    raise WALIntegrityError("Invalid provider result consumption transition")
                if event["result_sha256"] != result_hash:
                    raise WALIntegrityError("Consumed provider result digest changed")
            elif kind == "failed":
                if (previous_kind == "reserved"
                        and (phase != NOT_SENT or event["error_category"] != "local_admission")):
                    raise WALIntegrityError("Invalid pre-send provider failure")
                if (previous_kind == "dispatch_authorized"
                        and not (
                            (phase in {MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED}
                             and event["error_category"] != "local_admission")
                            or (phase == NOT_SENT
                                and event["error_category"] == "local_admission")
                        )):
                    raise WALIntegrityError("Invalid provider failure phase")
                if previous_kind not in {"reserved", "dispatch_authorized"}:
                    raise WALIntegrityError("Provider failure followed a terminal transition")
                error_category = event["error_category"]
                if type(error_category) is not str or error_category not in _ERROR_CATEGORIES:
                    raise WALIntegrityError("Invalid provider failure category")
                if phase != NOT_SENT and error_category == "local_admission":
                    raise WALIntegrityError("Local admission failure cannot follow send admission")
            elif index > 0:
                raise WALIntegrityError("Illegal provider decision transition")
            previous_hash = event_hash
            previous_kind = kind
        state = {
            "reserved": "reserved",
            "dispatch_authorized": "may_have_been_sent",
            "response_saved": "response_received",
            "consumed": "consumed",
            "failed": (
                "ambiguous" if events[-1]["phase"] == MAY_HAVE_BEEN_SENT else "failed"
            ),
        }[previous_kind]
        if state in {"reserved", "may_have_been_sent", "response_received", "ambiguous"}:
            unresolved += 1
        if state in {"response_received", "consumed"}:
            if type(record["result"]) is not dict or record["error_category"] is not None:
                raise WALIntegrityError("Saved provider result is missing or conflicts with error")
            try:
                validated_result, observed_hash = _validate_result(record["result"], identity)
            except (ValueError, TypeError) as error:
                raise WALIntegrityError("Invalid saved provider result") from error
            if observed_hash != result_hash or validated_result != record["result"]:
                raise WALIntegrityError("Saved provider result digest mismatch")
        elif state in {"failed", "ambiguous"}:
            if record["result"] is not None or record["error_category"] != error_category:
                raise WALIntegrityError("Failed provider result state is inconsistent")
        elif record["result"] is not None or record["error_category"] is not None:
            raise WALIntegrityError("Unsent provider request contains an outcome")
        if type(record["record_sha256"]) is not str or _HASH.fullmatch(record["record_sha256"]) is None:
            raise WALIntegrityError("Invalid provider decision record digest")
        if _digest_json(_record_unsigned(record)) != record["record_sha256"]:
            raise WALIntegrityError("Provider decision record digest mismatch")
    if unresolved > MAX_UNRESOLVED_RECORDS:
        raise WALIntegrityError("Provider decision ledger exceeds its unresolved bound")
    if (len(sequences) != document["last_sequence"]
            or sequences != set(range(1, document["last_sequence"] + 1))):
        raise WALIntegrityError("Provider decision ledger sequence does not match its records")
    unsigned_document = {key: value for key, value in document.items() if key != "ledger_sha256"}
    if _digest_json(unsigned_document) != document["ledger_sha256"]:
        raise WALIntegrityError("Provider decision ledger digest mismatch")
    return document


class ProviderDecisionWAL:
    """Atomic, bounded provider request/result ledger; intentionally not wired.

    One transition is atomically replaced and fsynced while holding a
    fail-fast process lock. No API deletes or compacts records. A MAY_HAVE_BEEN_SENT
    or saved-but-unconsumed result is never re-dispatched by this module.
    """

    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(path))

    @classmethod
    def initialize(cls, path: Path) -> ProviderDecisionWAL:
        """Create a new empty ledger; existing or malformed paths are refused."""
        wal = cls(path)
        wal._check_parent()
        with _writer_lock(wal.path):
            if wal.path.exists() or wal.path.is_symlink():
                raise WALIntegrityError("Refusing to replace an existing decision ledger")
            document = {
                "schema": 1,
                "last_sequence": 0,
                "records": [],
                "ledger_sha256": "",
            }
            wal._commit(document)
        return wal

    def _check_parent(self) -> None:
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise WALIntegrityError("Decision ledger parent must be an existing real directory")

    def _commit(self, document: dict) -> None:
        _seal_document(document)
        try:
            storage_payload = json.dumps(
                document, sort_keys=True, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, UnicodeError) as error:
            raise WALIntegrityError("Decision ledger cannot be encoded") from error
        if len(storage_payload) > MAX_LEDGER_BYTES:
            raise WALCapacityError("Provider decision ledger byte bound reached")
        atomic_json(self.path, document)
        observed = _read_document(self.path)
        if observed != document:
            raise WALIntegrityError("Provider decision ledger changed after durable write")

    @staticmethod
    def _append_event(document: dict, record: dict, kind: str, phase: str, **extra) -> None:
        previous = record["events"][-1]["event_sha256"] if record["events"] else None
        event = {
            "sequence": document["last_sequence"] + 1,
            "kind": kind,
            "phase": phase,
            "previous_event_sha256": previous,
            **extra,
        }
        event["event_sha256"] = _digest_json(event)
        record["events"].append(event)
        document["last_sequence"] = event["sequence"]
        _seal_record(record)

    @staticmethod
    def _logical_key(identity: ProviderDecisionIdentity) -> tuple[str, str, str, str]:
        return identity.session_id, identity.actor_id, identity.observation_id, identity.decision_id

    @staticmethod
    def _request_context(identity: ProviderDecisionIdentity, request_hash: str) -> tuple[str, ...]:
        return (
            identity.session_id, identity.actor_id, identity.observation_id,
            identity.provider_id, identity.model_id, request_hash,
        )

    def _match(self, document: dict, identity: ProviderDecisionIdentity,
               request_hash: str) -> dict:
        request_matches = []
        logical_matches = []
        content_matches = []
        for record in document["records"]:
            stored = _identity_from_dict(record["identity"])
            if stored.request_id == identity.request_id:
                request_matches.append(record)
            if self._logical_key(stored) == self._logical_key(identity):
                logical_matches.append(record)
            if self._request_context(stored, record["request_sha256"]) == self._request_context(
                    identity, request_hash):
                content_matches.append(record)
        matches = request_matches or logical_matches or content_matches
        if not matches:
            raise WALNotFound("Provider decision reservation was not found")
        if (len(matches) != 1
                or matches[0]["identity"] != identity.to_dict()
                or matches[0]["request_sha256"] != request_hash):
            raise WALIdentityConflict("Provider request or logical decision identity changed")
        return matches[0]

    @staticmethod
    def _view(record: dict) -> DecisionRecord:
        latest = record["events"][-1]
        kind = latest["kind"]
        state = {
            "reserved": "reserved",
            "dispatch_authorized": "may_have_been_sent",
            "response_saved": "response_received",
            "consumed": "consumed",
            "failed": (
                "ambiguous" if latest["phase"] == MAY_HAVE_BEEN_SENT else "failed"
            ),
        }[kind]
        result_hash = None
        result = None
        error_category = None
        for event in record["events"]:
            if event["kind"] == "response_saved":
                result_hash = event["result_sha256"]
            elif event["kind"] == "failed":
                error_category = event["error_category"]
        if state in {"response_received", "consumed"}:
            result = _freeze(record["result"])
        return DecisionRecord(
            identity=_identity_from_dict(record["identity"]),
            request_sha256=record["request_sha256"],
            phase=latest["phase"],
            state=state,
            result_sha256=result_hash,
            result=result,
            error_category=error_category,
            event_count=len(record["events"]),
        )

    def reserve(self, identity: ProviderDecisionIdentity, request: dict) -> DecisionRecord:
        """Durably reserve one exact request in NOT_SENT before transport entry.

        The request payload is bounded and hashed but not stored. Exact repeated
        reservations return the existing state and never authorize a send.
        """
        if not isinstance(identity, ProviderDecisionIdentity):
            raise TypeError("identity must be ProviderDecisionIdentity")
        request_hash = _request_sha256(request)
        self._check_parent()
        with _writer_lock(self.path):
            document = _read_document(self.path)
            try:
                existing = self._match(document, identity, request_hash)
            except WALNotFound:
                if len(document["records"]) >= MAX_RECORDS:
                    raise WALCapacityError("Provider decision history is full")
                actor_active = sum(
                    self._view(record).state in {
                        "reserved", "may_have_been_sent", "response_received", "ambiguous",
                    }
                    and record["identity"]["session_id"] == identity.session_id
                    and record["identity"]["actor_id"] == identity.actor_id
                    for record in document["records"]
                )
                if actor_active >= MAX_ACTIVE_PER_ACTOR:
                    raise WALCapacityError("Actor already has an unresolved provider decision")
                observation_requests = sum(
                    record["identity"]["session_id"] == identity.session_id
                    and record["identity"]["actor_id"] == identity.actor_id
                    and record["identity"]["observation_id"] == identity.observation_id
                    for record in document["records"]
                )
                if observation_requests >= MAX_REQUESTS_PER_OBSERVATION:
                    raise WALCapacityError("Provider request budget for this observation is full")
                unresolved = sum(
                    self._view(record).state in {
                        "reserved", "may_have_been_sent", "response_received", "ambiguous",
                    }
                    for record in document["records"]
                )
                if unresolved >= MAX_UNRESOLVED_RECORDS:
                    raise WALCapacityError("Provider decision unresolved bound is full")
                existing = {
                    "identity": identity.to_dict(),
                    "request_sha256": request_hash,
                    "events": [],
                    "result": None,
                    "error_category": None,
                    "record_sha256": "",
                }
                self._append_event(document, existing, "reserved", NOT_SENT)
                document["records"].append(existing)
                self._commit(document)
            return self._view(existing)

    def inspect(self, identity: ProviderDecisionIdentity, request: dict) -> DecisionRecord:
        """Read a record only when its full identity and current input digest match."""
        request_hash = _request_sha256(request)
        with _writer_lock(self.path):
            document = _read_document(self.path)
            return self._view(self._match(document, identity, request_hash))

    def mark_may_have_been_sent(self, identity: ProviderDecisionIdentity,
                                request: dict) -> DecisionRecord:
        """Persist MAY_HAVE_BEEN_SENT; caller may enter transport only after return."""
        request_hash = _request_sha256(request)
        with _writer_lock(self.path):
            document = _read_document(self.path)
            record = self._match(document, identity, request_hash)
            view = self._view(record)
            if view.state in {"may_have_been_sent", "ambiguous"}:
                raise WALAmbiguousRequest("Request may already have entered provider transport")
            if view.state != "reserved":
                raise WALInvalidTransition("Only a reserved NOT_SENT request can enter transport")
            self._append_event(document, record, "dispatch_authorized", MAY_HAVE_BEEN_SENT)
            self._commit(document)
            return self._view(record)

    def save_response(self, identity: ProviderDecisionIdentity, request: dict,
                      result: dict) -> DecisionRecord:
        """Persist a bounded response projection before any controller consumption."""
        request_hash = _request_sha256(request)
        detached, result_hash = _validate_result(result, identity)
        with _writer_lock(self.path):
            document = _read_document(self.path)
            record = self._match(document, identity, request_hash)
            view = self._view(record)
            if view.state in {"response_received", "consumed"}:
                if view.result_sha256 == result_hash:
                    return view
                raise WALIdentityConflict("A different provider result is already durable")
            if view.state != "may_have_been_sent":
                raise WALInvalidTransition("A response requires a MAY_HAVE_BEEN_SENT request")
            record["result"] = detached
            self._append_event(
                document, record, "response_saved", RESPONSE_RECEIVED,
                result_sha256=result_hash,
            )
            self._commit(document)
            return self._view(record)

    def record_error(self, identity: ProviderDecisionIdentity, request: dict,
                     category: str, phase: str) -> DecisionRecord:
        """Persist only a safe error category; never serialize exception text/body."""
        if type(category) is not str or category not in _ERROR_CATEGORIES:
            raise ValueError("Unsupported provider error category")
        if (type(phase) is not str
                or phase not in {NOT_SENT, MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED}):
            raise ValueError("Unsupported provider delivery phase")
        request_hash = _request_sha256(request)
        with _writer_lock(self.path):
            document = _read_document(self.path)
            record = self._match(document, identity, request_hash)
            view = self._view(record)
            if view.state in {"failed", "ambiguous"}:
                latest = record["events"][-1]
                if latest["phase"] == phase and latest["error_category"] == category:
                    return view
                raise WALIdentityConflict("A different provider error is already durable")
            if view.state == "reserved":
                allowed = phase == NOT_SENT and category == "local_admission"
            elif view.state == "may_have_been_sent":
                allowed = (
                    (phase == NOT_SENT and category == "local_admission")
                    or (phase in {MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED}
                        and category != "local_admission")
                )
            else:
                allowed = False
            if not allowed:
                raise WALInvalidTransition("Provider error does not match the durable send phase")
            self._append_event(
                document, record, "failed", phase, error_category=category,
            )
            record["error_category"] = category
            _seal_record(record)
            self._commit(document)
            return self._view(record)

    def recover_response(self, identity: ProviderDecisionIdentity,
                         request: dict) -> DecisionRecord:
        """Return only a complete unconsumed response; ambiguity never resends."""
        view = self.inspect(identity, request)
        if view.state in {"may_have_been_sent", "ambiguous"}:
            raise WALAmbiguousRequest("Provider outcome is ambiguous; automatic retry is forbidden")
        if view.state == "consumed":
            raise WALResponseConsumed("Provider response was already consumed")
        if view.state == "failed":
            raise WALInvalidTransition("Provider request has a durable terminal failure")
        if view.state == "reserved":
            raise WALInvalidTransition("Provider request has no durable response to recover")
        return view

    def consume_response(self, identity: ProviderDecisionIdentity, request: dict,
                         result_sha256: str) -> DecisionRecord:
        """Mark a result consumed only after the caller saves its selected plan."""
        if type(result_sha256) is not str or _HASH.fullmatch(result_sha256) is None:
            raise ValueError("Invalid result digest")
        request_hash = _request_sha256(request)
        with _writer_lock(self.path):
            document = _read_document(self.path)
            record = self._match(document, identity, request_hash)
            view = self._view(record)
            if view.state == "consumed":
                if view.result_sha256 == result_sha256:
                    return view
                raise WALIdentityConflict("Consumed provider result digest changed")
            if view.state != "response_received" or view.result_sha256 != result_sha256:
                raise WALInvalidTransition("Only the exact durable response can be consumed")
            self._append_event(
                document, record, "consumed", RESPONSE_RECEIVED,
                result_sha256=result_sha256,
            )
            self._commit(document)
            return self._view(record)
