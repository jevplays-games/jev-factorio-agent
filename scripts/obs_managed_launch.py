#!/usr/bin/env python3
"""Linux OBS owner wrapper: preserve crash markers and bound unattended recovery."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from uuid import uuid4


def regular(path, uid):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1:
        raise ValueError('Expected an owned regular file: ' + path.name)
    return info


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def recover_markers(root, uid, now):
    """Archive only OBS 32 run_UUID markers; never delete crash evidence."""
    sentinel = root / '.sentinel'
    if sentinel.is_symlink():
        raise ValueError('Symlink sentinel directory')
    if not sentinel.exists():
        return 0
    info = sentinel.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise ValueError('Invalid sentinel directory')
    markers = sorted(sentinel.iterdir())
    if len(markers) > 64:
        raise ValueError('Too many crash markers; manual diagnosis required')
    for path in markers:
        if not re.fullmatch(r'run_[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', path.name):
            raise ValueError('Unknown sentinel entry')
        if regular(path, uid).st_size != 0:
            raise ValueError('Nonempty crash marker')
    if not markers:
        return 0
    archive = root / 'managed-crash-history'
    if not archive.exists():
        archive.mkdir(mode=0o700)
    info = archive.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise ValueError('Invalid crash archive')
    recent = 0
    for path in archive.iterdir():
        if not path.is_dir() or path.is_symlink():
            raise ValueError('Invalid crash history entry')
        record = path / 'recovery.json'
        if not record.exists():
            raise ValueError('Incomplete crash recovery; reconcile before restart')
        regular(record, uid)
        row = json.loads(record.read_text())
        if (not isinstance(row, dict) or type(row.get('at')) not in (int, float)
                or not math.isfinite(row['at'])):
            raise ValueError('Invalid crash history record')
        if row['at'] > now - 900:
            recent += 1
    if recent >= 3:
        raise ValueError('Three crash recoveries within 15 minutes; diagnose before restart')
    target = archive / (str(time.time_ns()) + '-' + uuid4().hex)
    target.mkdir(mode=0o700)
    # Preserve the marker in two places until the archive entry is durable.
    for path in markers:
        regular(path, uid)
        os.link(path, target / path.name, follow_symlinks=False)
    record = {'at': now, 'reason': 'OBS unclean exit observed before managed restart',
              'markers': [p.name for p in markers]}
    with (target / 'recovery.json').open('x') as out:
        json.dump(record, out); out.flush(); os.fsync(out.fileno())
    sync_dir(target); sync_dir(archive)
    for path in markers:
        old, saved = path.lstat(), (target / path.name).lstat()
        if (old.st_dev, old.st_ino) != (saved.st_dev, saved.st_ino):
            raise ValueError('Crash marker changed during recovery')
        path.unlink()  # Evidence remains at its durable archival hard link.
    sync_dir(sentinel)
    print(json.dumps({'event': 'obs_crash_recovery', 'record': str(target / 'recovery.json')}), flush=True)
    return len(markers)


def software_browser(root, uid):
    path = root / 'global.ini'
    regular(path, uid)
    raw = path.read_bytes()
    # Preserve all unrelated settings and credentials byte for byte.
    lines = raw.splitlines(keepends=True)
    section = None
    matches = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(b'['):
            section = stripped
        if section == b'[General]' and stripped.startswith(b'BrowserHWAccel='):
            matches.append(index)
    if len(matches) != 1:
        raise ValueError('Expected one existing General/BrowserHWAccel setting')
    index = matches[0]
    if lines[index].strip() == b'BrowserHWAccel=false':
        return
    if lines[index].strip() != b'BrowserHWAccel=true':
        raise ValueError('Unexpected browser acceleration value')
    lines[index] = lines[index].replace(b'=true', b'=false')
    backup = root / ('global.ini.before-software-browser-' + uuid4().hex)
    with backup.open('xb') as out:
        out.write(raw); out.flush(); os.fsync(out.fileno())
    os.chmod(backup, 0o600)
    temp = root / ('global.ini.managed-' + uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(b''.join(lines)); out.flush(); os.fsync(out.fileno())
    if path.read_bytes() != raw:
        raise ValueError('OBS configuration changed while stopped')
    os.replace(temp, path); sync_dir(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, required=True)
    parser.add_argument('--recover-crash-markers', action='store_true')
    parser.add_argument('--software-browser', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    uid = os.getuid()
    root = args.config_dir.absolute()
    if uid == 0 or root.is_symlink() or root.resolve() != root or root.stat().st_uid != uid:
        raise ValueError('Run as the owning desktop user with a canonical config directory')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or Path(command[0]).name != 'obs' or '--multi' in command or '-m' in command:
        raise ValueError('Expected a single OBS launch')
    fd = os.open(root / 'managed-launch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)  # Ownership survives exec until OBS exits.
    running = subprocess.run(['pgrep', '-u', str(uid), '-x', 'obs'], capture_output=True)
    if running.returncode != 1:
        raise ValueError('OBS already runs or process visibility is unavailable')
    if args.software_browser:
        software_browser(root, uid)
    if args.recover_crash_markers:
        recover_markers(root, uid, time.time())
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
