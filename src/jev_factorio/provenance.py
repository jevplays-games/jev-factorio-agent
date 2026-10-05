"""Small provenance bridge for supervised gameplay, independent of model logging.

The supervisor is the sole writer of its audit. Children receive an immutable
context, not a writable shared journal. No function here queries Factorio or Jev.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

CONTEXT_ENV = "JEV_FACTORIO_PROVENANCE"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def identifier(value: str) -> str:
    """Validate opaque IDs without allowing paths, whitespace, or log injection."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Provenance IDs must be 1-128 ASCII letters, digits, or _.:-")
    return value


def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def gameplay_context() -> dict:
    """Read and validate only the supervisor's allowlisted, secret-free context."""
    raw = os.environ.get(CONTEXT_ENV)
    if raw is None:
        return {}
    value = json.loads(raw)
    required = {"run_id", "segment_id", "execution_id", "code_revision"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Invalid supervised gameplay provenance context")
    for key in required - {"code_revision"}:
        identifier(value[key])
    revision = value["code_revision"]
    if revision is not None and (
        not isinstance(revision, dict) or set(revision) != {"commit", "source_sha256"}
        or not isinstance(revision["commit"], str) or not _SHA.fullmatch(revision["commit"])
        or not isinstance(revision["source_sha256"], str)
        or not _DIGEST.fullmatch(revision["source_sha256"])
    ):
        raise ValueError("Invalid supervised source revision")
    return value


def source_revision(cwd: Path, *, timeout: float = 10,
                    exclude_untracked: tuple[Path, ...] = (),
                    exclude_untracked_prefixes: tuple[Path, ...] = ()) -> dict | None:
    """Hash HEAD, index entries, tracked files and nonignored untracked files.

    Read-only Git commands are bounded and their output is never copied into
    logs. Runtime outputs may be excluded only when untracked; tracked files
    are always included. Prefix exclusions match full paths, allowing a
    checkpoint path plus "." to exclude its atomic temporary siblings.
    Symlinks are hashed as links, not followed.
    A missing Git checkout, timeout, or racing/unreadable file is unknown, never
    a fabricated clean revision. This is a source fingerprint, not a world save.
    """
    deadline = time.monotonic() + timeout
    try:
        root = cwd.resolve()
        # Keep lexical names for matching Git's repository-relative paths, but
        # also inspect resolved names so a symlink alias cannot turn an
        # apparently narrow exclusion into the checkout (or one of its parents).
        excluded_lexical = tuple(Path(os.path.abspath(os.fspath(path)))
                                 for path in exclude_untracked)
        excluded_resolved = tuple(path.resolve() for path in exclude_untracked)
        prefixes = tuple(Path(os.path.abspath(os.fspath(path)))
                         for path in exclude_untracked_prefixes)
        resolved_prefixes = tuple(path.resolve() for path in prefixes)
        if any(exclusion == root or root.is_relative_to(exclusion)
               for exclusion in (*excluded_resolved, *resolved_prefixes)):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    excluded_prefixes = tuple(os.path.normcase(os.fspath(path)) for path in prefixes)
    root_name = os.path.normcase(os.fspath(root))
    # Prefix exclusions are intentionally lexical (for atomic temp siblings),
    # so Path.is_relative_to alone cannot detect a prefix such as
    # /tmp/campaign. that also matches a checkout at /tmp/campaign.runtime.
    # Returning unknown is safer than silently omitting every untracked source
    # file under that checkout; unrelated external checkpoint paths still work.
    if any(root_name.startswith(prefix) for prefix in excluded_prefixes):
        return None

    def git(*args: str) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return subprocess.run(["git", *args], cwd=root, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=remaining).stdout

    def add(digest, data: bytes) -> None:
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    def untracked() -> set[bytes]:
        names = git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        included = set()
        for name in names:
            if not name:
                continue
            path = Path(os.path.abspath(os.fspath(root / os.fsdecode(name))))
            if any(path.is_relative_to(exclusion) for exclusion in excluded_lexical):
                continue
            if os.path.normcase(os.fspath(path)).startswith(excluded_prefixes):
                continue
            included.add(name)
        return included

    try:
        if Path(os.fsdecode(git("rev-parse", "--show-toplevel")).rstrip("\n")).resolve() != root:
            return None
        head = git("rev-parse", "HEAD").decode("ascii").strip()
        if not _SHA.fullmatch(head):
            return None
        index = git("ls-files", "--stage", "-z")
        entries = [entry.split(b"\t", 1) for entry in index.split(b"\0") if entry]
        # Gitlinks need their own source provenance. Detect the index mode
        # before consulting the worktree path: an absent submodule directory
        # is just as unsupported as a materialized one.
        if any(parts and parts[0].split() and parts[0].split()[0] == b"160000"
               for parts in entries):
            return None
        tracked = {parts[1] for parts in entries}
        others = untracked()
        digest = hashlib.sha256()
        add(digest, b"jev-factorio.source.v1")
        add(digest, index)
        for name in sorted(tracked | others):
            if time.monotonic() >= deadline:
                raise TimeoutError
            path = root / os.fsdecode(name)
            add(digest, name)
            if not path.exists() and not path.is_symlink():
                add(digest, b"missing")
                continue
            before = path.lstat()
            add(digest, str(before.st_mode).encode("ascii"))
            if path.is_symlink():
                add(digest, os.fsencode(os.readlink(path)))
            elif path.is_file():
                content = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        if time.monotonic() >= deadline:
                            raise TimeoutError
                        content.update(chunk)
                add(digest, content.digest())
            else:
                # A gitlink/submodule needs separate provenance; do not pretend
                # that hashing a directory entry fingerprints its working tree.
                return None
            after = path.lstat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode
            ):
                return None
        if (git("rev-parse", "HEAD").decode("ascii").strip() != head
                or git("ls-files", "--stage", "-z") != index
                or untracked() != others):
            return None
        return {"commit": head, "source_sha256": digest.hexdigest()}
    except (OSError, subprocess.SubprocessError, TimeoutError, UnicodeError, ValueError, IndexError):
        return None


def append_audit(path: Path, record: dict) -> None:
    """Append an outbox event once, or verify an already-durable identical tail.

    Caller holds the supervisor lock. A partial tail is evidence of an interrupted
    append and requires explicit repair; it is never silently truncated.
    """
    if path.exists() and path.stat().st_size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                raise ValueError("Supervisor audit has an incomplete tail")
            position = stream.tell() - 2
            chunks = []
            while position >= 0:
                start = max(0, position - 4095)
                stream.seek(start)
                chunk = stream.read(position - start + 1)
                split = chunk.rfind(b"\n")
                chunks.insert(0, chunk[split + 1:] if split >= 0 else chunk)
                if split >= 0:
                    break
                position = start - 1
            tail = json.loads(b"".join(chunks))
        if not isinstance(tail, dict):
            raise ValueError("Supervisor audit tail is not an object")
        if tail.get("event_id") == record["event_id"]:
            if tail != record:
                raise ValueError("Supervisor audit event ID collision")
            # The previous process may have died before its fsync completed.
            with path.open("ab") as stream:
                os.fsync(stream.fileno())
            return
        if tail.get("run_id") not in {None, record["run_id"]}:
            raise ValueError("Supervisor audit belongs to a different run")
        if tail.get("sequence", 0) != record["sequence"] - 1:
            raise ValueError("Supervisor audit sequence differs from durable state")
    elif record["sequence"] != 1:
        raise ValueError("Supervisor audit is missing earlier events")
    with path.open("ab") as stream:
        stream.write((json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
