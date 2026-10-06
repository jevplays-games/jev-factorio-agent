"""Exact-state checkpoint coalescing; never debounce a changed write-ahead state."""
from __future__ import annotations

import errno
import json
import math
import os
import stat
import tempfile
import time
from dataclasses import asdict, fields
from contextlib import ExitStack, contextmanager
from pathlib import Path
from .iteration_timing import measured, span


def _stamp(path: Path) -> tuple | None:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError:
        return None
    return _identity(value) if stat.S_ISREG(value.st_mode) else None



def _identity(info) -> tuple:
    # Keep ctime and birth time separately. On Windows path stat and fstat can
    # report different ctime domains for the same file; the remaining fields
    # still bind the file identity, content version, and exposed attributes.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, getattr(info, 'st_file_attributes', None),
            getattr(info, 'st_birthtime_ns', None), getattr(info, 'st_nlink', None))


def _same_identity_views(path_identity: tuple, descriptor_identity: tuple) -> bool:
    """Compare one installed file through path and open-handle metadata views."""
    if os.name == 'nt':
        # Windows Python can expose different ctime and birth-time values through
        # path stat, a flushed temporary handle, and the reopened installed file.
        # Retain each full view independently across the final verification.
        return (path_identity[:4] == descriptor_identity[:4]
                and path_identity[5:7] == descriptor_identity[5:7]
                and path_identity[8:] == descriptor_identity[8:])
    return path_identity == descriptor_identity


def _same_renamed_identity(installed: tuple, flushed: tuple) -> bool:
    """Compare file identity across rename while allowing view-specific times."""
    if os.name == 'nt':
        return (installed[:4] == flushed[:4]
                and installed[5:7] == flushed[5:7]
                and installed[8:] == flushed[8:])
    return (installed[:4] == flushed[:4]
            and installed[5:] == flushed[5:])


def _elapsed(metrics: dict, key: str, began: int, *, failed: bool) -> None:
    """Never replace a primary failure with a secondary diagnostic failure."""
    try:
        metrics[key] += time.perf_counter_ns() - began
    except BaseException:
        if not failed:
            raise


@contextmanager
def _managed_stream(stream):
    try:
        yield stream
    except BaseException:
        try:
            stream.close()
        except BaseException:
            pass
        raise
    else:
        # A lone close failure is still a failed save, not hidden success.
        stream.close()


@contextmanager
def _descriptor_stream(fd: int, owned_identity: tuple):
    try:
        stream = os.fdopen(fd, 'w+b')
    except BaseException:
        try:
            # A failed wrapper may already have released the descriptor. Do
            # not close a number now bound to another owner's file.
            if _identity(os.fstat(fd))[:2] == owned_identity:
                os.close(fd)
        except BaseException:
            pass
        raise
    with _managed_stream(stream) as owned:
        yield owned


def _directory_sync(directory: Path, metrics: dict, *, parent_entry: bool = False) -> None:
    if os.name != 'posix':
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    failed = False
    try:
        began = time.perf_counter_ns()
        try:
            with span('checkpoint_parent_sync' if parent_entry else 'checkpoint_directory_sync'):
                metrics['directory_sync_calls'] += 1
                if parent_entry:
                    metrics['parent_directory_sync_calls'] += 1
                os.fsync(descriptor)
        except BaseException:
            failed = True
            raise
        finally:
            _elapsed(metrics, 'directory_sync_ns', began, failed=failed)
    except BaseException:
        failed = True
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException:
            if not failed:
                raise


def _provision_parent(directory: Path, metrics: dict) -> None:
    """Sync each newly created entry in its parent before writing a checkpoint.

    Existing directories remain an operator-provisioned precondition. A failed
    setup leaves its directories in place for explicit durable provisioning;
    the owning controller's existing persistence barrier prevents blind retry.
    """
    missing = []
    current = directory
    while not current.is_dir():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise OSError(errno.ENOTDIR, 'Checkpoint parent is not a directory')
        current = parent
    for child in reversed(missing):
        child.mkdir()  # A conflicting concurrent creator is not silently adopted.
        _directory_sync(child.parent, metrics, parent_entry=True)


def _verify_installation(stream, path: Path, payload: bytes, flushed: tuple, metrics: dict,
                         *, verify_bytes: bool = True, parent_identity=None,
                         expected_path_identity=None, expected_descriptor_identity=None) -> tuple:
    """Bind the installed entry and bytes to the flushed descriptor.

    Rename may change ctime. Windows path stat and fstat may also expose ctime
    through different metadata domains. Compare shared identity/metadata across
    those boundaries, then pin the complete path and descriptor stamps
    independently across the synchronization barrier. This is not an
    interprocess lock or an ABA proof.
    """
    began = time.perf_counter_ns()
    failed = False
    try:
        with span('checkpoint_installation_check'):
            installed = _stamp(path)
            descriptor = _identity(os.fstat(stream.fileno()))
            if installed is None:
                raise OSError(errno.EIO, 'Checkpoint installation changed before synchronization')
            if ((expected_path_identity is not None and installed != expected_path_identity)
                    or (expected_descriptor_identity is not None
                        and descriptor != expected_descriptor_identity)):
                raise OSError(errno.EIO, 'Checkpoint installation changed across synchronization')
            if (not _same_identity_views(installed, descriptor)
                    or not _same_renamed_identity(installed, flushed)):
                raise OSError(errno.EIO, 'Checkpoint installation changed before synchronization')
            if not verify_bytes:
                return installed, descriptor
            if parent_identity is not None:
                current_parent = path.parent.stat()
                if ((current_parent.st_dev, current_parent.st_ino)
                        != (parent_identity.st_dev, parent_identity.st_ino)
                        or not _same_renamed_identity(installed, flushed)):
                    raise OSError(errno.EIO, 'Checkpoint installation changed across synchronization')
            stream.seek(0)
            metrics['verification_read_calls'] += 1
            observed = stream.read(len(payload) + 1)
            metrics['verification_read_bytes'] += len(observed)
            path_after = _stamp(path)
            descriptor_after = _identity(os.fstat(stream.fileno()))
            if (observed != payload or path_after != installed
                    or descriptor_after != descriptor
                    or path_after is None
                    or not _same_identity_views(path_after, descriptor_after)):
                raise OSError(errno.EIO, 'Checkpoint installation bytes changed across synchronization')
            return installed, descriptor
    except BaseException:
        failed = True
        raise
    finally:
        _elapsed(metrics, 'installation_check_ns', began, failed=failed)

def _same_value(live, captured) -> bool:
    """Exact built-in JSON shape only; unsupported values take the slow path.

    Python equality alone merges bool/int/float and positive/negative zero.
    None of those may suppress a changed persisted representation. Captured
    containers come from the last successful asdict, never from live memory.
    """
    kind = type(live)
    if kind is not type(captured):
        return False
    if kind is dict:
        return (len(live) == len(captured)
                and all(type(key) is str and key in captured
                        and _same_value(value, captured[key]) for key, value in live.items()))
    if kind in {list, tuple}:
        return len(live) == len(captured) and all(_same_value(a, b) for a, b in zip(live, captured))
    if kind is float:
        return math.isfinite(live) and live.hex() == captured.hex()
    return kind in {str, int, bool, type(None)} and live == captured


def _same_memory(memory, captured: dict) -> bool:
    names = [field.name for field in fields(memory)
             if (field.name != 'capital_investment' or memory.capital_investment is not None)
             and (field.name != 'blocked_recovery_archive'
                  or memory.blocked_recovery_archive is not None)
             and (field.name != 'blocked_recovery' or memory.blocked_recovery is not None)
             and (field.name != 'async_decision' or memory.async_decision is not None)]
    return (len(names) == len(captured)
            and all(name in captured and _same_value(getattr(memory, name), captured[name])
                    for name in names))


def checkpoint_data(memory) -> dict:
    """Return the canonical backward-compatible checkpoint snapshot.

    Optional extension fields with no state are omitted, matching the bytes
    written before those fields were added. Nonempty values are preserved
    exactly; unrelated ``None`` fields remain part of the schema.
    """
    data = asdict(memory)
    for name in ('capital_investment', 'blocked_recovery_archive', 'blocked_recovery',
                 'async_decision'):
        if data.get(name) is None:
            data.pop(name, None)
    return data


@measured("checkpoint")
def save_checkpoint(memory, path: Path | None) -> None:
    """Cache only successfully synced bytes in this memory object's lifetime.

    One controller owns the path. An external edit/replacement/deletion or a new
    memory instance invalidates coalescing. Exact typed structural comparison
    against the last detached capture avoids copying/serializing unchanged
    state. Every changed authoritative field still uses the durable writer.
    The cache and timings are not fields
    of the checkpoint schema. With preprovisioned parents, a changed save uses
    one file sync, one containing-directory sync and one bounded verification
    read. New parent entries are synced before checkpoint preparation. Any
    failure drops the cache; a secondary diagnostic/cleanup failure cannot
    replace the primary exception. One operational owner remains required.
    """
    metrics = {'status': 'disabled', 'bytes': 0, 'serialize_ns': 0,
               # serialize_ns retains its historical inclusive capture+JSON scope.
               # These two exclusive phases sit within that scope; the residual
               # includes setup and normalization between them.
               'capture_ns': 0, 'json_encode_ns': 0,
               'file_sync_ns': 0, 'directory_sync_ns': 0, 'total_ns': 0,
               'compare_ns': 0, 'capture_calls': 0, 'serialization_calls': 0,
               'file_sync_calls': 0, 'directory_sync_calls': 0, 'parent_directory_sync_calls': 0,
               'verification_read_calls': 0, 'verification_read_bytes': 0,
               'installation_check_ns': 0}
    memory._checkpoint_metrics = metrics
    if path is None:
        memory._checkpoint_cache = None
        return
    start = None
    temporary = None
    temporary_identity = None
    failed = False
    try:
        start = time.perf_counter_ns()
        path = Path(os.path.abspath(path))
        cache = getattr(memory, '_checkpoint_cache', None)
        if (cache is not None and len(cache) == 4 and cache[0] == path
                and cache[2] is not None):
            began = time.perf_counter_ns()
            with span("checkpoint_compare"):
                identical = _same_memory(memory, cache[3])
            metrics['compare_ns'] = time.perf_counter_ns() - began
            if identical and cache[2] == _stamp(path):
                metrics.update(status='unchanged', bytes=len(cache[1]))
                return
        began = time.perf_counter_ns()
        metrics['capture_calls'] = 1
        phase_began = time.perf_counter_ns()
        phase_failed = False
        try:
            with span("checkpoint_capture"):
                data = checkpoint_data(memory)
        except BaseException:
            phase_failed = True
            raise
        finally:
            _elapsed(metrics, 'capture_ns', phase_began, failed=phase_failed)
        metrics['serialization_calls'] = 1
        phase_began = time.perf_counter_ns()
        phase_failed = False
        try:
            with span("checkpoint_serialize"):
                payload = json.dumps(data, sort_keys=True, allow_nan=False).encode('utf-8')
        except BaseException:
            phase_failed = True
            raise
        finally:
            _elapsed(metrics, 'json_encode_ns', phase_began, failed=phase_failed)
        metrics.update(bytes=len(payload), serialize_ns=time.perf_counter_ns() - began)
        cache = getattr(memory, '_checkpoint_cache', None)
        if (cache is not None and cache[0] == path and cache[1] == payload
                and cache[2] is not None and cache[2] == _stamp(path)):
            metrics['status'] = 'unchanged'
            return
        memory._checkpoint_cache = None
        _provision_parent(path.parent, metrics)
        parent_identity = path.parent.stat()
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
        try:
            temporary_identity = _identity(os.fstat(fd))[:2]
        except BaseException:
            # mkstemp transferred this raw descriptor to us. No wrapper has
            # received it yet; close it even if identity capture fails. The
            # unknown pathname remains evidence rather than deletion authority.
            try:
                os.close(fd)
            except BaseException:
                pass
            raise
        with ExitStack() as handles:
            stream = handles.enter_context(_descriptor_stream(fd, temporary_identity))
            with span("checkpoint_write"):
                stream.write(payload)
                stream.flush()
            began = time.perf_counter_ns()
            sync_failed = False
            try:
                with span("checkpoint_file_sync"):
                    metrics['file_sync_calls'] += 1
                    os.fsync(stream.fileno())
            except BaseException:
                sync_failed = True
                raise
            finally:
                _elapsed(metrics, 'file_sync_ns', began, failed=sync_failed)
            flushed = _identity(os.fstat(stream.fileno()))
            if flushed[2] != len(payload):
                raise OSError(errno.EIO, 'Checkpoint write length mismatch')
            # Windows ordinary file handles do not share delete/rename access.
            # Retain the flushed identity, then revalidate the reopened entry.
            if os.name != 'posix':
                stream.close()
            with span("checkpoint_replace"):
                os.replace(temporary, path)
            temporary = None
            if os.name != 'posix':
                try:
                    reopened = path.open('rb')
                except FileNotFoundError as error:
                    raise OSError(errno.EIO,
                                  'Checkpoint installation changed before synchronization') from error
                stream = handles.enter_context(_managed_stream(reopened))
            installed_path, installed_descriptor = _verify_installation(
                stream, path, payload, flushed, metrics, verify_bytes=False)
            _directory_sync(path.parent, metrics)
            _verify_installation(
                stream, path, payload, installed_descriptor, metrics,
                parent_identity=parent_identity,
                expected_path_identity=installed_path,
                expected_descriptor_identity=installed_descriptor)
        memory._checkpoint_cache = (path, payload, installed_path, data)
        metrics['status'] = 'written'
    except BaseException:
        failed = True
        memory._checkpoint_cache = None
        metrics['status'] = 'failed'
        raise
    finally:
        try:
            if start is not None:
                _elapsed(metrics, 'total_ns', start, failed=failed)
        except BaseException:
            failed = True
            memory._checkpoint_cache = None
            metrics['status'] = 'failed'
            raise
        finally:
            if temporary is not None:
                try:
                    current = _stamp(Path(temporary))
                    # Unknown or substituted entries remain evidence. Only the
                    # original temporary inode is eligible for best-effort cleanup.
                    if current is not None and current[:2] == temporary_identity:
                        os.unlink(temporary)
                except BaseException:
                    if not failed:
                        memory._checkpoint_cache = None
                        metrics['status'] = 'failed'
                        raise
