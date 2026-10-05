"""Durable, checkpoint-bound archival for persistent blocked-recovery attempts.

The JSON checkpoint keeps a bounded active tail. Immutable content-addressed
segments preserve older source-bound decision fingerprints and their outcomes.
The checkpoint pointer is the only authority for which segments are committed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path


MAX_SEGMENT_ENTRIES = 1024
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _directory(checkpoint: Path) -> Path:
    path = Path(os.path.abspath(checkpoint))
    return path.with_name(path.name + ".blocked-recovery-archive")


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _check_owner_mode(info, *, directory: bool) -> None:
    if directory and not stat.S_ISDIR(info.st_mode) or not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("Blocked-recovery archive entry has the wrong file type")
    if os.name == "posix":
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
            raise ValueError("Blocked-recovery archive ownership or mode is invalid")
        if not directory and info.st_nlink != 1:
            raise ValueError("Blocked-recovery archive file has an unexpected hard link")


def _metadata(info) -> tuple:
    """Return the full identity used to detect archive replacement or edits."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, getattr(info, "st_uid", None),
            getattr(info, "st_gid", None), info.st_mode,
            getattr(info, "st_nlink", None))


def _same_handle_path(handle_info, path_info) -> bool:
    if os.name == "posix":
        return _metadata(handle_info) == _metadata(path_info)
    # Windows' descriptor and pathname stat APIs can report timestamp skew for
    # the same file. Keep exact timestamps within the opened handle and bind
    # the pathname through every other identity/permission field; cached
    # pathname metadata is then compared exactly on later lookups.
    return (handle_info.st_dev, handle_info.st_ino, handle_info.st_size,
            getattr(handle_info, "st_uid", None), getattr(handle_info, "st_gid", None), handle_info.st_mode,
            getattr(handle_info, "st_nlink", None)) == (
            path_info.st_dev, path_info.st_ino, path_info.st_size,
            getattr(path_info, "st_uid", None), getattr(path_info, "st_gid", None), path_info.st_mode,
            getattr(path_info, "st_nlink", None))


def _ensure_directory(checkpoint: Path) -> Path:
    directory = _directory(checkpoint)
    parent = directory.parent
    try:
        info = directory.lstat()
    except FileNotFoundError:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("Blocked-recovery archive directory cannot be a symlink")
    _check_owner_mode(info, directory=True)
    # The directory can preexist after a crash between mkdir and its parent
    # sync. Re-establish that ordering before committing a checkpoint pointer.
    _sync_directory(parent)
    return directory


def _read_regular(path: Path, *, max_bytes: int = MAX_SEGMENT_BYTES) -> tuple[bytes, tuple]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        _check_owner_mode(before, directory=False)
        if before.st_size < 1 or before.st_size > max_bytes:
            raise ValueError("Blocked-recovery archive segment size is invalid")
        chunks = []
        remaining = before.st_size
        while remaining:
            part = os.read(fd, min(remaining, 1024 * 1024))
            if not part:
                raise ValueError("Blocked-recovery archive segment was truncated")
            chunks.append(part)
            remaining -= len(part)
        if os.read(fd, 1):
            raise ValueError("Blocked-recovery archive segment changed while reading")
        after = os.fstat(fd)
        path_info = path.lstat()
        _check_owner_mode(after, directory=False)
        _check_owner_mode(path_info, directory=False)
        if _metadata(before) != _metadata(after) or not _same_handle_path(after, path_info):
            raise ValueError("Blocked-recovery archive segment changed while reading")
        return b"".join(chunks), _metadata(path_info)
    finally:
        os.close(fd)


def _validate_attempt(row: object, session_id: str, last_tick: int) -> dict:
    if not isinstance(row, dict):
        raise ValueError("Invalid archived persistent blocked-recovery attempt")
    from .blocked_persistence import _source, _validate_state
    source = _source(row.get("source_revision"))
    digest = row.get("decision_input_sha256")
    if type(digest) is not str or not _DIGEST.fullmatch(digest):
        raise ValueError("Invalid archived persistent blocked-recovery fingerprint")
    state = {
        "schema": 1, "session_id": session_id, "source_revision": source,
        "attempts": [row], "last_input_sha256": digest, "wait_level": 0,
    }
    try:
        _validate_state(state, session_id)
    except ValueError as error:
        raise ValueError("Invalid archived persistent blocked-recovery attempt") from error
    if row["tick"] > last_tick:
        raise ValueError("Archived blocked-recovery attempt is newer than its checkpoint")
    return row


class VerifiedAttemptArchive:
    """Bounded-cache disk index over the hash-verified archive and active tail."""

    def __init__(self, tempdir: tempfile.TemporaryDirectory, connection: sqlite3.Connection,
                 index_path: Path, archive_directory: Path | None,
                 directory_metadata: tuple | None):
        self._tempdir = tempdir
        self._connection = connection
        self._index_path = index_path
        self._index_metadata = None
        self._archive_directory = archive_directory
        self._directory_metadata = directory_metadata
        self._session_id = None
        self._target = None
        self._archive_pointer = None

    def seal(self) -> None:
        """Make the temporary lookup index read-only and pin its identity."""
        self._connection.execute("PRAGMA query_only=ON")
        if os.name == "posix":
            os.chmod(self._index_path, 0o400)
        info = self._index_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("Temporary blocked-recovery index was replaced")
        if os.name == "posix" and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o400
                or info.st_nlink != 1):
            raise ValueError("Temporary blocked-recovery index ownership or mode is invalid")
        self._index_metadata = _metadata(info)

    def validate_files(self) -> None:
        """Revalidate bounded metadata before each duplicate lookup or WAL."""
        try:
            index_info = self._index_path.lstat()
        except OSError as error:
            raise ValueError("Temporary blocked-recovery index disappeared") from error
        if (self._index_metadata is None or stat.S_ISLNK(index_info.st_mode)
                or not stat.S_ISREG(index_info.st_mode)
                or _metadata(index_info) != self._index_metadata):
            raise ValueError("Temporary blocked-recovery index changed after verification")
        if os.name == "posix" and (
                index_info.st_uid != os.geteuid() or stat.S_IMODE(index_info.st_mode) != 0o400
                or index_info.st_nlink != 1):
            raise ValueError("Temporary blocked-recovery index ownership or mode changed")
        if self._archive_directory is None:
            return
        try:
            current = self._archive_directory.lstat()
        except OSError as error:
            raise ValueError("Blocked-recovery archive directory disappeared") from error
        _check_owner_mode(current, directory=True)
        if _metadata(current) != self._directory_metadata:
            raise ValueError("Blocked-recovery archive directory changed after verification")
        cursor = self._connection.execute(
            "SELECT digest, metadata FROM segments ORDER BY digest")
        try:
            for digest, encoded_metadata in cursor:
                path = self._archive_directory / f"segment-{digest}.json"
                try:
                    info = path.lstat()
                except OSError as error:
                    raise ValueError("Blocked-recovery archive segment disappeared") from error
                _check_owner_mode(info, directory=False)
                if _metadata(info) != tuple(json.loads(encoded_metadata)):
                    raise ValueError("Blocked-recovery archive segment changed after verification")
        finally:
            cursor.close()
        try:
            current_after = self._archive_directory.lstat()
        except OSError as error:
            raise ValueError("Blocked-recovery archive directory disappeared") from error
        _check_owner_mode(current_after, directory=True)
        if _metadata(current_after) != self._directory_metadata:
            raise ValueError("Blocked-recovery archive directory changed during verification")

    def find(self, source_revision: dict, input_sha256: str, *, memory) -> dict | None:
        if (memory.session_id != self._session_id or memory.target != self._target
                or memory.blocked_recovery_archive != self._archive_pointer):
            raise ValueError("Blocked-recovery archive index belongs to a different checkpoint")
        self.validate_files()
        from .blocked_persistence import _source
        source = _source(source_revision)
        row = self._connection.execute(
            "SELECT payload FROM attempts WHERE source_commit=? AND source_sha256=? AND input_sha256=?",
            (source["commit"], source["source_sha256"], input_sha256)).fetchone()
        if row is None:
            return None
        result = json.loads(row[0])
        # Legacy rows without an outcome are ambiguous, exactly like active
        # legacy rows. Never reinterpret them as completed or safe to replay.
        result.setdefault("outcome", "pending")
        return result

    def selection_attempts(self, source_revision: dict, state_sha256: str, *, memory) -> list[dict]:
        """Return authenticated selection-batch rows for one source/state pair."""
        if (memory.session_id != self._session_id or memory.target != self._target
                or memory.blocked_recovery_archive != self._archive_pointer):
            raise ValueError("Blocked-recovery archive index belongs to a different checkpoint")
        from .blocked_persistence import MAX_SELECTION_BATCHES_PER_STATE, _source
        source = _source(source_revision)
        if type(state_sha256) is not str or not _DIGEST.fullmatch(state_sha256):
            raise ValueError("Invalid persistent selection state fingerprint")
        self.validate_files()
        cursor = self._connection.execute(
            "SELECT payload FROM attempts WHERE source_commit=? AND source_sha256=?",
            (source["commit"], source["source_sha256"]))
        try:
            rows = []
            for (payload,) in cursor:
                row = json.loads(payload)
                batch = row.get("selection_batch")
                if isinstance(batch, dict) and batch.get("state_sha256") == state_sha256:
                    rows.append(row)
                    if len(rows) > MAX_SELECTION_BATCHES_PER_STATE:
                        raise ValueError("Persistent selection batch limit is exceeded for one state")
        finally:
            cursor.close()
        seen_candidates = set()
        for row in rows:
            batch = row["selection_batch"]
            for offered in batch["offered"]:
                candidate_sha256 = offered["candidate_sha256"]
                if candidate_sha256 in seen_candidates:
                    raise ValueError("Persistent selection candidate was already offered for this state")
                seen_candidates.add(candidate_sha256)
        return rows

    def compatibility_rows(self, *, memory):
        """Authenticated archived coverage audit before explicit source migration."""
        if (memory.session_id != self._session_id or memory.target != self._target
                or memory.blocked_recovery_archive != self._archive_pointer):
            raise ValueError("Blocked-recovery archive index belongs to a different checkpoint")
        self.validate_files()
        cursor = self._connection.execute("SELECT payload FROM attempts")
        try:
            for (payload,) in cursor:
                yield json.loads(payload)
        finally:
            cursor.close()

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            self._tempdir.cleanup()

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass


def _new_index() -> VerifiedAttemptArchive:
    tempdir = tempfile.TemporaryDirectory(prefix="jev-blocked-recovery-index-")
    try:
        directory = Path(tempdir.name)
        if os.name == "posix":
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("Temporary blocked-recovery index directory is unsafe")
        database = directory / "attempts.sqlite3"
        connection = sqlite3.connect(database)
        if os.name == "posix":
            os.chmod(database, 0o600)
            _check_owner_mode(database.stat(follow_symlinks=False), directory=False)
        connection.execute("PRAGMA cache_size=-2048")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("CREATE TABLE attempts ("
                            "source_commit TEXT NOT NULL, source_sha256 TEXT NOT NULL, "
                            "input_sha256 TEXT NOT NULL, payload TEXT NOT NULL, "
                            "PRIMARY KEY(source_commit, source_sha256, input_sha256)) WITHOUT ROWID")
        connection.execute("CREATE TABLE segments (digest TEXT PRIMARY KEY, metadata TEXT NOT NULL) WITHOUT ROWID")
        return VerifiedAttemptArchive(tempdir, connection, database, None, None)
    except BaseException:
        tempdir.cleanup()
        raise


def _index_row(index: VerifiedAttemptArchive, row: dict, session_id: str, last_tick: int) -> None:
    _validate_attempt(row, session_id, last_tick)
    source = row["source_revision"]
    try:
        index._connection.execute(
            "INSERT INTO attempts VALUES (?, ?, ?, ?)",
            (source["commit"], source["source_sha256"],
             row["decision_input_sha256"], _canonical(row).decode("utf-8")))
    except sqlite3.IntegrityError as error:
        raise ValueError("Duplicate archived persistent blocked-recovery attempt") from error


def _validate_pointer(pointer: object, session_id: str, target: str) -> dict:
    required = {"schema", "session_id", "target", "entry_count", "segment_count", "head_sha256"}
    if (not isinstance(pointer, dict) or set(pointer) != required
            or type(pointer["schema"]) is not int or pointer["schema"] != 1
            or pointer["session_id"] != session_id or pointer["target"] != target
            or type(pointer["entry_count"]) is not int or pointer["entry_count"] < 1
            or type(pointer["segment_count"]) is not int or pointer["segment_count"] < 1
            or pointer["entry_count"] < pointer["segment_count"]
            or pointer["entry_count"] > pointer["segment_count"] * MAX_SEGMENT_ENTRIES
            or type(pointer["head_sha256"]) is not str
            or not _DIGEST.fullmatch(pointer["head_sha256"])):
        raise ValueError("Invalid blocked-recovery archive pointer")
    return pointer


def build_index(checkpoint: Path, memory) -> VerifiedAttemptArchive:
    """Verify all referenced rows and build a temporary uniqueness index.

    The temporary index has a bounded SQLite cache and is never checkpoint
    authority. Every segment is authenticated from the checkpoint pointer and
    re-read before it can seed duplicate prevention.
    """
    index = _new_index()
    try:
        pointer = memory.blocked_recovery_archive
        index._session_id = memory.session_id
        index._target = memory.target
        index._archive_pointer = (None if pointer is None else dict(pointer))
        if pointer is not None:
            from .operational_safety import storage_ready
            if not storage_ready([Path(index._tempdir.name)], minimum_bytes=1024 ** 3):
                raise OSError("Storage reserve is unavailable for blocked-recovery archive verification")
            pointer = _validate_pointer(pointer, memory.session_id, memory.target)
            if memory.blocked_recovery is None:
                raise ValueError("Blocked-recovery archive has no active recovery state")
            directory = _directory(checkpoint)
            try:
                info = directory.lstat()
            except OSError as error:
                raise ValueError("Blocked-recovery archive directory is missing") from error
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Blocked-recovery archive directory cannot be a symlink")
            _check_owner_mode(info, directory=True)
            index._archive_directory = directory
            index._directory_metadata = _metadata(info)
            expected_digest = pointer["head_sha256"]
            expected_end = pointer["entry_count"] - 1
            expected_segments = pointer["segment_count"]
            seen_segments = 0
            while expected_digest is not None:
                if seen_segments >= expected_segments:
                    raise ValueError("Blocked-recovery archive chain exceeds its checkpoint pointer")
                segment_path = directory / f"segment-{expected_digest}.json"
                raw, metadata = _read_regular(segment_path)
                if hashlib.sha256(raw).hexdigest() != expected_digest:
                    raise ValueError("Blocked-recovery archive segment hash mismatch")
                try:
                    segment = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("Blocked-recovery archive segment is malformed") from error
                if raw != _canonical(segment):
                    raise ValueError("Blocked-recovery archive segment is not canonical")
                required = {"schema", "session_id", "target", "first_sequence",
                            "last_sequence", "previous_sha256", "attempts"}
                if (not isinstance(segment, dict) or set(segment) != required
                        or type(segment["schema"]) is not int or segment["schema"] != 1
                        or segment["session_id"] != memory.session_id
                        or segment["target"] != memory.target
                        or type(segment["first_sequence"]) is not int
                        or type(segment["last_sequence"]) is not int
                        or not isinstance(segment["attempts"], list)
                        or not 1 <= len(segment["attempts"]) <= MAX_SEGMENT_ENTRIES
                        or segment["last_sequence"] != expected_end
                        or segment["first_sequence"] != expected_end - len(segment["attempts"]) + 1):
                    raise ValueError("Blocked-recovery archive segment identity or sequence is invalid")
                previous = segment["previous_sha256"]
                if previous is not None and (type(previous) is not str or not _DIGEST.fullmatch(previous)):
                    raise ValueError("Blocked-recovery archive predecessor is invalid")
                for row in segment["attempts"]:
                    _index_row(index, row, memory.session_id, memory.last_tick)
                index._connection.execute(
                    "INSERT INTO segments VALUES (?, ?)",
                    (expected_digest, json.dumps(metadata, separators=(",", ":"))))
                expected_end = segment["first_sequence"] - 1
                expected_digest = previous
                seen_segments += 1
            if (seen_segments != expected_segments or expected_end != -1
                    or index._connection.execute("SELECT count(*) FROM attempts").fetchone()[0]
                    != pointer["entry_count"]):
                raise ValueError("Blocked-recovery archive chain is incomplete")
        if memory.blocked_recovery is not None:
            for row in memory.blocked_recovery["attempts"]:
                _index_row(index, row, memory.session_id, memory.last_tick)
        index._connection.commit()
        if pointer is not None:
            from .operational_safety import storage_ready
            if not storage_ready([Path(index._tempdir.name)], minimum_bytes=1024 ** 3):
                raise OSError("Storage reserve fell below the limit during archive verification")
        index.seal()
        index.validate_files()
        return index
    except BaseException:
        index.close()
        raise


def verify_archive(checkpoint: Path, memory, *, reject_key: tuple[str, str, str] | None = None) -> None:
    """Verify the archive and active tail, optionally rejecting one prior key."""
    index = build_index(checkpoint, memory)
    try:
        if reject_key is not None:
            commit, source_sha, input_sha = reject_key
            if index.find({"commit": commit, "source_sha256": source_sha}, input_sha,
                          memory=memory) is not None:
                raise ValueError("Persistent blocked-recovery input was already attempted")
    finally:
        index.close()


def archive_full_tail(checkpoint: Path, memory) -> VerifiedAttemptArchive:
    """Durably create the next immutable segment and move the bounded tail.

    Caller must hold the original controller owner lock and commit the returned
    pointer in a checkpoint before recording a new provider-call WAL row.
    """
    if memory.blocked_recovery is None or len(memory.blocked_recovery["attempts"]) != MAX_SEGMENT_ENTRIES:
        raise ValueError("Blocked-recovery archive rotation requires a full active tail")
    if (memory.status not in {"running", "blocked"} or memory.active_plan is not None
            or memory.pending is not None or memory.attempt is not None
            or memory.transfer_recovery is not None):
        raise ValueError("Blocked-recovery archive rotation requires a quiescent decision boundary")
    if any(getattr(memory, key, None) is not None for key in
           ("background_job", "background_attempt", "background_step")):
        # A paid background craft is independent of the provider-call ledger.
        # Reuse the full checkpoint validator before carrying it unchanged
        # through the pointer commit; never discard or replay its receipt.
        from .background import BackgroundMemory
        from .checkpoint_io import checkpoint_data
        if not isinstance(memory, BackgroundMemory):
            raise ValueError("Archive rotation requires validated background ownership")
        type(memory).from_bytes(_canonical(checkpoint_data(memory)),
                                memory.session_id, memory.target)
        if memory.background_job is None or memory.background_job.get("failed"):
            raise ValueError("Archive rotation cannot carry failed background work")
    prior_index = build_index(checkpoint, memory)
    prior_index.close()
    directory = _ensure_directory(checkpoint)
    pointer = memory.blocked_recovery_archive
    previous = None if pointer is None else pointer["head_sha256"]
    prior_entries = 0 if pointer is None else pointer["entry_count"]
    prior_segments = 0 if pointer is None else pointer["segment_count"]
    attempts = memory.blocked_recovery["attempts"]
    segment = {
        "schema": 1, "session_id": memory.session_id, "target": memory.target,
        "first_sequence": prior_entries,
        "last_sequence": prior_entries + len(attempts) - 1,
        "previous_sha256": previous, "attempts": attempts,
    }
    raw = _canonical(segment)
    if len(raw) > MAX_SEGMENT_BYTES:
        raise ValueError("Blocked-recovery archive segment exceeds its size bound")
    from .operational_safety import storage_ready
    if (not storage_ready([directory], minimum_bytes=1024 ** 3)
            or shutil.disk_usage(directory).free < len(raw) * 2 + 1024 * 1024):
        raise OSError("Insufficient free space to archive blocked-recovery history")
    digest = hashlib.sha256(raw).hexdigest()
    destination = directory / f"segment-{digest}.json"
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        existing, _ = _read_regular(destination)
        if existing != raw:
            raise ValueError("Blocked-recovery orphan segment does not match exact history")
    else:
        try:
            _check_owner_mode(os.fstat(fd), directory=False)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("Short blocked-recovery archive write")
                view = view[written:]
            os.fsync(fd)
            actual = os.fstat(fd)
            _check_owner_mode(actual, directory=False)
            entry = destination.lstat()
            if ((actual.st_dev, actual.st_ino, actual.st_size)
                    != (entry.st_dev, entry.st_ino, len(raw))):
                raise OSError("Blocked-recovery archive entry changed during write")
        finally:
            os.close(fd)
    # Also sync on exact-orphan reuse: the previous writer may have crashed
    # after file fsync but before making the directory entry durable.
    _sync_directory(directory)
    new_pointer = {
        "schema": 1, "session_id": memory.session_id, "target": memory.target,
        "entry_count": prior_entries + len(attempts),
        "segment_count": prior_segments + 1, "head_sha256": digest,
    }
    prior_pointer = memory.blocked_recovery_archive
    prior_attempts = memory.blocked_recovery["attempts"]
    memory.blocked_recovery_archive = new_pointer
    memory.blocked_recovery["attempts"] = []
    try:
        index = build_index(checkpoint, memory)
        return index
    except BaseException:
        memory.blocked_recovery_archive = prior_pointer
        memory.blocked_recovery["attempts"] = prior_attempts
        raise
