"""Durable one-use admission for an explicitly new persistent campaign."""
import json
import os
from pathlib import Path


def intent_path(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.name + ".initialization.json")


def require_unused(checkpoint: Path) -> None:
    for path in (checkpoint, intent_path(checkpoint)):
        if path.exists() or path.is_symlink():
            raise ValueError("New campaign checkpoint or initialization intent already exists; reconcile before retry")


def reserve(checkpoint: Path, context: dict) -> None:
    """Retain intent even if native initialization fails before a checkpoint exists.

    The owner holds its normal writer lock. O_EXCL also arbitrates two fresh
    invocations racing to initialize this checkpoint. This is not resume authority.
    """
    require_unused(checkpoint)
    value = {"schema": 1, "phase": "initialization_requested", "provenance": context,
             "automatic_initialization_retry_allowed": False}
    data = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    descriptor = os.open(intent_path(checkpoint), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        directory = os.open(checkpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
