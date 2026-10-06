"""Durable, bounded archive for an exact asynchronous selection request.

The archive holds the selector context and exact provider payload separately
from the WAL.  The checkpoint stores only a digest-bound pointer.  Records are
never replaced while their WAL state is unresolved.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

from .operational_safety import SafetyStateError, atomic_json
from .provider_decision_wal import _writer_lock


MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_NODES = 100_000
MAX_ARCHIVE_DEPTH = 48
MAX_ARCHIVE_STRING = 256 * 1024
MAX_ARCHIVE_RECORDS = 128
_HEX = frozenset("0123456789abcdef")
_POINTER_FIELDS = {
    "schema", "archive_id", "archive_sha256", "request_id", "disposition",
    "selected_plan_id", "selected_plan_sha256",
}
_RECORD_FIELDS = {
    "schema", "archive_id", "request_id", "identity", "selector",
    "provider", "record_sha256",
}


class AsyncDecisionArchiveError(ValueError):
    """Invalid, unavailable, or conflicting exact-decision archive state."""


class AsyncDecisionArchiveBusy(AsyncDecisionArchiveError):
    """An unresolved archive cannot be replaced by another request."""


def _is_hash(value: Any) -> bool:
    return (type(value) is str and len(value) == 64
            and all(character in _HEX for character in value))


def _is_request_id(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_pointer(value: Any) -> dict:
    """Validate the optional checkpoint pointer without opening its archive."""
    if (type(value) is not dict or set(value) != _POINTER_FIELDS
            or type(value.get("schema")) is not int or value["schema"] != 1
            or not _is_hash(value.get("archive_id"))
            or not _is_hash(value.get("archive_sha256"))
            or not _is_request_id(value.get("request_id"))
            or value.get("disposition") not in {
                "pending", "selected", "no_action", "stale", "cancelled",
                "cancelled_before_send",
            }
            or (value.get("selected_plan_id") is not None
                and (type(value["selected_plan_id"]) is not str
                     or not value["selected_plan_id"].strip()))
            or (value.get("selected_plan_sha256") is not None
                and not _is_hash(value["selected_plan_sha256"]))
            or (value["disposition"] == "selected"
                and (value.get("selected_plan_id") is None
                     or value.get("selected_plan_sha256") is None))
            or (value["disposition"] != "selected"
                and (value.get("selected_plan_id") is not None
                     or value.get("selected_plan_sha256") is not None))):
        raise AsyncDecisionArchiveError("Invalid asynchronous decision archive pointer")
    return dict(value)


def _canonical(value: Any) -> bytes:
    nodes = 0
    active: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_ARCHIVE_NODES or depth > MAX_ARCHIVE_DEPTH:
            raise AsyncDecisionArchiveError("Async decision archive exceeds its shape bound")
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is str:
            if len(item) > MAX_ARCHIVE_STRING:
                raise AsyncDecisionArchiveError("Async decision archive string exceeds its bound")
            item.encode("utf-8")
            return
        if kind is int:
            if abs(item) > 2**63 - 1:
                raise AsyncDecisionArchiveError("Async decision archive integer exceeds its bound")
            return
        if kind is float:
            import math

            if not math.isfinite(item):
                raise AsyncDecisionArchiveError("Non-finite async decision archive number")
            return
        if kind not in (dict, list):
            raise AsyncDecisionArchiveError("Async decision archive requires built-in JSON values")
        marker = id(item)
        if marker in active:
            raise AsyncDecisionArchiveError("Cyclic async decision archive value")
        active.add(marker)
        try:
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str or not key or len(key) > 256:
                        raise AsyncDecisionArchiveError("Invalid async decision archive key")
                    key.encode("utf-8")
                    active_key = "".join(character for character in key.lower() if character.isalnum())
                    if any(part in active_key for part in (
                            "apikey", "apitoken", "authorization", "bearer", "password",
                            "credential", "secret", "cookie", "setcookie")):
                        raise AsyncDecisionArchiveError("Credential-like archive field is forbidden")
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
        finally:
            active.remove(marker)

    visit(value, 0)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as error:
        raise AsyncDecisionArchiveError("Async decision archive is not canonical JSON") from error
    if len(encoded) > MAX_ARCHIVE_BYTES:
        raise AsyncDecisionArchiveError("Async decision archive exceeds its byte bound")
    return encoded


def _duplicate_free(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate archive key")
        result[key] = value
    return result


def _read_document(path: Path) -> dict | None:
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise AsyncDecisionArchiveError("Async decision archive path is not a real file location")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AsyncDecisionArchiveError("Cannot safely open async decision archive") from error
    try:
        before = os.fstat(descriptor)
        by_path = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(by_path.st_mode)
                or before.st_dev != by_path.st_dev or before.st_ino != by_path.st_ino
                or before.st_size != by_path.st_size or before.st_size > MAX_ARCHIVE_BYTES
                or getattr(before, "st_nlink", 1) != 1
                or (os.name == "posix" and stat.S_IMODE(before.st_mode) & 0o077)):
            raise AsyncDecisionArchiveError("Async decision archive file identity or mode is invalid")
        chunks = []
        remaining = MAX_ARCHIVE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        after_path = path.stat(follow_symlinks=False)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns)):
            raise AsyncDecisionArchiveError("Async decision archive changed while being read")
        raw = b"".join(chunks)
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise AsyncDecisionArchiveError("Async decision archive exceeds its byte bound")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_duplicate_free,
            parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise AsyncDecisionArchiveError("Async decision archive is malformed") from error
    return value


def _read(path: Path) -> dict | None:
    value = _read_document(path)
    if value is not None:
        _validate_record(value)
    return value


def _validate_record(value: Any) -> dict:
    if type(value) is not dict or set(value) != _RECORD_FIELDS:
        raise AsyncDecisionArchiveError("Invalid async decision archive record fields")
    if (type(value.get("schema")) is not int or value["schema"] != 1
            or not _is_hash(value.get("archive_id"))
            or not _is_request_id(value.get("request_id"))
            or type(value.get("identity")) is not dict
            or type(value.get("selector")) is not dict
            or type(value.get("provider")) is not dict
            or not _is_hash(value.get("record_sha256"))):
        raise AsyncDecisionArchiveError("Invalid async decision archive record")
    identity_fields = {
        "session_id", "actor_id", "observation_id", "decision_id", "request_id",
        "provider_id", "model_id",
    }
    if (set(value["identity"]) != identity_fields
            or value["identity"].get("request_id") != value["request_id"]
            or any(type(item) is not str or not item.strip()
                   for item in value["identity"].values())):
        raise AsyncDecisionArchiveError("Async decision archive identity is invalid")
    selector = value["selector"]
    if set(selector) != {
            "source_state", "state", "questions", "candidate_plans", "offered_plans",
            "source_revision", "session_id", "actor_id", "runtime_identity",
            "observation_id", "decision_id", "request_id", "observation_tick",
            "target", "policy", "confidence_floor", "max_request_bytes",
            "request_order", "persistent_input_sha256", "selection_batch",
            "source_authorized", "source_auth_reason_sha256", "frontier_sha256"}:
        raise AsyncDecisionArchiveError("Async decision selector context is incomplete")
    provider = value["provider"]
    if set(provider) != {
            "provider_id", "model_id", "payload", "payload_json", "payload_sha256",
            "wire_body_base64", "wire_body_sha256", "wal_request", "wal_id"}:
        raise AsyncDecisionArchiveError("Async decision provider binding is incomplete")
    for name in ("payload_sha256", "wal_id"):
        if not _is_hash(provider.get(name)):
            raise AsyncDecisionArchiveError("Async decision provider digest is invalid")
    if (type(provider.get("provider_id")) is not str or not provider["provider_id"]
            or type(provider.get("model_id")) is not str or not provider["model_id"]
            or type(provider.get("payload")) is not dict
            or type(provider.get("wal_request")) is not dict):
        raise AsyncDecisionArchiveError("Async decision provider payload is invalid")
    if (selector.get("session_id") != value["identity"]["session_id"]
            or selector.get("actor_id") != value["identity"]["actor_id"]
            or selector.get("observation_id") != value["identity"]["observation_id"]
            or selector.get("decision_id") != value["identity"]["decision_id"]
            or selector.get("request_id") != value["identity"]["request_id"]
            or provider.get("provider_id") != value["identity"]["provider_id"]
            or provider.get("model_id") != value["identity"]["model_id"]
            or type(selector.get("source_state")) is not dict
            or type(selector.get("state")) is not dict
            or type(selector.get("questions")) is not dict
            or type(selector.get("candidate_plans")) is not list
            or type(selector.get("offered_plans")) is not list
            or type(selector.get("source_revision")) is not dict
            or type(selector.get("runtime_identity")) is not dict
            or set(selector["runtime_identity"]) != {
                "session_id", "actor_unit", "surface_index", "force_index",
            }
            or selector["runtime_identity"].get("session_id") != selector.get("session_id")
            or type(selector["runtime_identity"].get("actor_unit")) is not int
            or selector["runtime_identity"]["actor_unit"] < 0
            or any(type(selector["runtime_identity"].get(name)) is not int
                   or selector["runtime_identity"][name] < 0
                   for name in ("surface_index", "force_index"))
            or any(type(selector.get(name)) is not str or not selector[name]
                   for name in ("actor_id", "observation_id", "decision_id", "request_id"))
            or selector.get("request_id") != value["request_id"]
            or type(selector.get("observation_tick")) is not int
            or type(selector.get("target")) is not str
            or type(selector.get("policy")) is not str
            or type(selector.get("confidence_floor")) not in (int, float)
            or type(selector.get("max_request_bytes")) is not int
            or not 1 <= selector["max_request_bytes"] <= 2**31 - 1
            or type(selector.get("request_order")) is not dict
            or (selector.get("persistent_input_sha256") is not None
                and not _is_hash(selector["persistent_input_sha256"]))
            or (selector.get("selection_batch") is not None
                and type(selector["selection_batch"]) is not dict)
            or type(selector.get("source_authorized")) is not bool
            or (selector.get("source_auth_reason_sha256") is not None
                and not _is_hash(selector["source_auth_reason_sha256"]))
            or not _is_hash(selector.get("frontier_sha256"))):
        raise AsyncDecisionArchiveError("Async decision selector context is malformed")
    for name in ("candidate_plans", "offered_plans"):
        if (any(type(plan) is not dict for plan in selector[name])
                or len({plan.get("id") for plan in selector[name]}) != len(selector[name])):
            raise AsyncDecisionArchiveError("Async decision candidate plan binding is malformed")
    candidate_ids = {plan.get("id") for plan in selector["candidate_plans"]}
    offered_ids = {plan.get("id") for plan in selector["offered_plans"]}
    if (not candidate_ids or not offered_ids or not offered_ids <= candidate_ids
            or any(type(plan.get("id")) is not str or not plan["id"]
                   for plan in selector["candidate_plans"])):
        raise AsyncDecisionArchiveError("Async decision offered candidates are not bound")
    payload_digest = hashlib.sha256(_canonical(provider["payload"])).hexdigest()
    if payload_digest != provider["payload_sha256"]:
        raise AsyncDecisionArchiveError("Async decision provider payload digest mismatch")
    canonical_payload = _canonical(provider["payload"]).decode("utf-8")
    if (type(provider.get("payload_json")) is not str
            or provider["payload_json"] != canonical_payload
            or type(provider.get("wire_body_base64")) is not str
            or not _is_hash(provider.get("wire_body_sha256"))):
        raise AsyncDecisionArchiveError("Async decision serialized provider payload is malformed")
    try:
        wire_body = base64.b64decode(provider["wire_body_base64"], validate=True)
        wire_json = json.loads(wire_body.decode("utf-8"), object_pairs_hook=_duplicate_free)
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise AsyncDecisionArchiveError("Async decision wire payload is malformed") from error
    if (hashlib.sha256(wire_body).hexdigest() != provider["wire_body_sha256"]
            or wire_json != provider["payload"]):
        raise AsyncDecisionArchiveError("Async decision wire payload differs from its request")
    wal_request = provider["wal_request"]
    if (set(wal_request) != {
            "provider_payload_sha256", "trace_binding", "health_state_sha256",
        }
            or wal_request.get("provider_payload_sha256") != provider["payload_sha256"]
            or not _is_hash(wal_request.get("health_state_sha256"))):
        raise AsyncDecisionArchiveError("Async decision WAL payload binding is malformed")
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    if digest != value["record_sha256"]:
        raise AsyncDecisionArchiveError("Async decision archive digest mismatch")
    return value


class AsyncDecisionArchive:
    """One active bounded request archive per stable actor identity."""

    def __init__(self, directory: Path):
        self.directory = Path(os.path.abspath(directory))
        if self.directory.is_symlink() or self.directory.parent.is_symlink():
            raise AsyncDecisionArchiveError("Async decision archive directory cannot be a symlink")
        if not self.directory.exists():
            # Reuse the safety writer so missing parents are created privately
            # and the new directory entries are synced before a request can be
            # archived beneath them.
            from .operational_safety import atomic_json

            atomic_json(self.directory / ".archive-ready", {"schema": 1})
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise AsyncDecisionArchiveError("Async decision archive directory is invalid")
        marker = self.directory / ".archive-ready"
        if marker.is_symlink():
            raise AsyncDecisionArchiveError("Async decision archive marker cannot be a symlink")
        from .operational_safety import read_json

        if read_json(marker) != {"schema": 1}:
            raise AsyncDecisionArchiveError("Async decision archive marker is missing or malformed")

    def _path(self, archive_id: str) -> Path:
        if not _is_hash(archive_id):
            raise AsyncDecisionArchiveError("Invalid async archive identity")
        return self.directory / f"{archive_id}.json"

    def _settlement_path(self, archive_id: str) -> Path:
        if not _is_hash(archive_id):
            raise AsyncDecisionArchiveError("Invalid async settlement identity")
        return self.directory / f"{archive_id}.settled"

    def records(self) -> list[dict]:
        """Read every immutable archive record from this bounded directory."""
        paths = sorted(self.directory.glob("*.json"), key=lambda item: item.name)
        if len(paths) > MAX_ARCHIVE_RECORDS:
            raise AsyncDecisionArchiveError("Async decision archive exceeds its record bound")
        records = []
        for path in paths:
            if path.is_symlink() or path.name != f"{path.stem}.json" or not _is_hash(path.stem):
                raise AsyncDecisionArchiveError("Async decision archive contains an unsafe record name")
            record = _read(path)
            if record is None or record["archive_id"] != path.stem:
                raise AsyncDecisionArchiveError("Async decision archive filename differs from its record")
            records.append(record)
        return records

    @staticmethod
    def settlement_entry(record: dict, *, disposition: str, wal_state: str | None,
                         wal_phase: str | None, wal_record_sha256: str | None,
                         selected_plan_id: str | None = None,
                         selected_plan_sha256: str | None = None) -> dict:
        _validate_record(record)
        if (disposition not in {"selected", "no_action", "stale", "cancelled",
                                "cancelled_before_send"}
                or (wal_state is not None and (type(wal_state) is not str or not wal_state))
                or (wal_phase is not None and (type(wal_phase) is not str or not wal_phase))
                or ((wal_state is None) != (wal_phase is None))
                or ((wal_state is None) != (wal_record_sha256 is None))
                or (wal_record_sha256 is not None and not _is_hash(wal_record_sha256))
                or (wal_state is None and disposition == "selected")
                or (wal_state is not None and (wal_state, wal_phase) not in {
                    ("consumed", "response_received"),
                    ("failed", "not_sent"),
                    ("failed", "response_received"),
                })
                or (disposition == "selected"
                    and (wal_state, wal_phase) != ("consumed", "response_received"))
                or (disposition == "selected"
                    and (type(selected_plan_id) is not str or not selected_plan_id
                         or not _is_hash(selected_plan_sha256)))
                or (disposition != "selected"
                    and (selected_plan_id is not None or selected_plan_sha256 is not None))):
            raise AsyncDecisionArchiveError("Invalid async decision settlement")
        return {
            "archive_id": record["archive_id"],
            "archive_sha256": record["record_sha256"],
            "request_id": record["request_id"],
            "disposition": disposition,
            "selected_plan_id": selected_plan_id,
            "selected_plan_sha256": selected_plan_sha256,
            "wal_state": wal_state,
            "wal_phase": wal_phase,
            "wal_record_sha256": wal_record_sha256,
        }

    @staticmethod
    def validate_settlement_entry(value: Any) -> dict:
        fields = {
            "archive_id", "archive_sha256", "request_id", "disposition",
            "selected_plan_id", "selected_plan_sha256", "wal_state", "wal_phase",
            "wal_record_sha256",
        }
        if (type(value) is not dict or set(value) != fields
                or not _is_hash(value.get("archive_id"))
                or not _is_hash(value.get("archive_sha256"))
                or not _is_request_id(value.get("request_id"))
                or value.get("disposition") not in {
                    "selected", "no_action", "stale", "cancelled", "cancelled_before_send",
                }
                or (value.get("wal_state") is not None
                    and (type(value["wal_state"]) is not str or not value["wal_state"]))
                or (value.get("wal_phase") is not None
                    and (type(value["wal_phase"]) is not str or not value["wal_phase"]))
                or ((value.get("wal_state") is None) != (value.get("wal_phase") is None))
                or ((value.get("wal_state") is None)
                    != (value.get("wal_record_sha256") is None))
                or (value.get("wal_record_sha256") is not None
                    and not _is_hash(value.get("wal_record_sha256")))
                or (value.get("wal_state") is None
                    and value.get("disposition") == "selected")
                or (value.get("wal_state") is not None
                    and (value["wal_state"], value["wal_phase"]) not in {
                        ("consumed", "response_received"),
                        ("failed", "not_sent"),
                        ("failed", "response_received"),
                    })
                or (value.get("disposition") == "selected"
                    and (value.get("wal_state"), value.get("wal_phase"))
                    != ("consumed", "response_received"))
                or (value.get("disposition") == "selected"
                    and (type(value.get("selected_plan_id")) is not str
                         or not value["selected_plan_id"]
                         or not _is_hash(value.get("selected_plan_sha256"))))
                or (value.get("disposition") != "selected"
                    and (value.get("selected_plan_id") is not None
                         or value.get("selected_plan_sha256") is not None))):
            raise AsyncDecisionArchiveError("Malformed async decision settlement entry")
        return dict(value)

    def load_settlement(self, record: dict) -> dict | None:
        _validate_record(record)
        path = self._settlement_path(record["archive_id"])
        if path.is_symlink():
            raise AsyncDecisionArchiveError("Async decision settlement cannot be a symlink")
        with _writer_lock(path):
            marker = _read_document(path)
        if marker is None:
            return None
        fields = {
            "schema", "archive_id", "archive_sha256", "request_id", "disposition",
            "selected_plan_id", "selected_plan_sha256", "wal_state", "wal_phase",
            "wal_record_sha256", "entry_sha256",
        }
        if (type(marker) is not dict or set(marker) != fields
                or type(marker.get("schema")) is not int or marker["schema"] != 1
                or not _is_hash(marker.get("entry_sha256"))):
            raise AsyncDecisionArchiveError("Malformed async decision settlement marker")
        entry = {name: marker[name] for name in fields - {"schema", "entry_sha256"}}
        self.validate_settlement_entry(entry)
        if (entry["archive_id"] != record["archive_id"]
                or entry["archive_sha256"] != record["record_sha256"]
                or entry["request_id"] != record["request_id"]
                or hashlib.sha256(_canonical(entry)).hexdigest() != marker["entry_sha256"]):
            raise AsyncDecisionArchiveError("Async decision settlement marker does not bind its record")
        return entry

    def store_settlement(self, record: dict, entry: dict) -> None:
        """Durably mirror a checkpoint-retained settlement entry after its save."""
        _validate_record(record)
        entry = self.validate_settlement_entry(entry)
        if (entry["archive_id"] != record["archive_id"]
                or entry["archive_sha256"] != record["record_sha256"]
                or entry["request_id"] != record["request_id"]):
            raise AsyncDecisionArchiveError("Async settlement does not match its immutable archive")
        marker = {
            "schema": 1, **entry,
            "entry_sha256": hashlib.sha256(_canonical(entry)).hexdigest(),
        }
        path = self._settlement_path(record["archive_id"])
        already_exists = False
        with _writer_lock(path):
            existing = _read_document(path)
            if existing is not None:
                already_exists = True
            else:
                atomic_json(path, marker)
        observed = self.load_settlement(record)
        if observed != entry:
            if already_exists:
                raise AsyncDecisionArchiveBusy("Async decision settlement cannot be replaced")
            raise AsyncDecisionArchiveError("Async decision settlement changed after durable write")

    @staticmethod
    def pointer(record: dict, disposition: str = "pending",
                selected_plan_id: str | None = None,
                selected_plan_sha256: str | None = None) -> dict:
        _validate_record(record)
        pointer = {
            "schema": 1, "archive_id": record["archive_id"],
            "archive_sha256": record["record_sha256"],
            "request_id": record["request_id"],
            "disposition": disposition, "selected_plan_id": selected_plan_id,
            "selected_plan_sha256": selected_plan_sha256,
        }
        return validate_pointer(pointer)

    def load(self, pointer: dict) -> dict:
        pointer = validate_pointer(pointer)
        path = self._path(pointer["archive_id"])
        with _writer_lock(path):
            record = _read(path)
        if (record is None or record["archive_id"] != pointer["archive_id"]
                or record["request_id"] != pointer["request_id"]
                or record["record_sha256"] != pointer["archive_sha256"]):
            raise AsyncDecisionArchiveError("Async decision archive pointer does not match its record")
        return json.loads(_canonical(record).decode("utf-8"), object_pairs_hook=_duplicate_free)

    def store(self, record: dict) -> dict:
        """Durably write one exact record; archived requests are immutable."""
        if type(record) is not dict or set(record) != _RECORD_FIELDS - {"record_sha256"}:
            raise AsyncDecisionArchiveError("Invalid async decision archive input fields")
        body = json.loads(_canonical(record).decode("utf-8"), object_pairs_hook=_duplicate_free)
        sealed = {**body, "record_sha256": hashlib.sha256(_canonical(body)).hexdigest()}
        _validate_record(sealed)
        path = self._path(sealed["archive_id"])
        with _writer_lock(path):
            existing = _read(path)
            if existing is not None:
                if (existing["request_id"] == sealed["request_id"]
                        and existing["record_sha256"] == sealed["record_sha256"]):
                    return self.pointer(sealed)
                raise AsyncDecisionArchiveBusy(
                    "An existing actor request archive cannot be replaced")
            record_count = 0
            for item in self.directory.iterdir():
                if item.name.endswith(".json"):
                    if item.is_symlink() or not item.is_file():
                        raise AsyncDecisionArchiveError(
                            "Async decision archive directory contains an unsafe record path")
                    record_count += 1
            if existing is None and record_count >= MAX_ARCHIVE_RECORDS:
                raise AsyncDecisionArchiveBusy(
                    "Async decision archive reached its bounded record capacity")
            atomic_json(path, sealed)
            observed = _read(path)
            if observed != sealed:
                raise AsyncDecisionArchiveError("Async decision archive changed after durable write")
        return self.pointer(sealed)
