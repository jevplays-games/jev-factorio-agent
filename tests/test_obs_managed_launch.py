import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
pytest.importorskip('fcntl')
spec = importlib.util.spec_from_file_location('obs_managed_launch', Path(__file__).parents[1] / 'scripts/obs_managed_launch.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def marker(root):
    folder = root / '.sentinel'
    folder.mkdir(exist_ok=True)
    path = folder / ('run_' + str(uuid4()))
    path.touch()
    return path


def test_crash_evidence_is_archived_before_marker_is_cleared(tmp_path):
    path = marker(tmp_path)
    assert module.recover_markers(tmp_path, os.getuid(), 1000) == 1
    assert not path.exists()
    archive = next((tmp_path/'managed-crash-history').iterdir())
    assert (archive/path.name).read_bytes() == b''
    assert json.loads((archive/'recovery.json').read_text())['markers'] == [path.name]
    assert module.recover_markers(tmp_path, os.getuid(), 1001) == 0


def test_crash_loop_is_bounded_without_erasing_fourth_failure(tmp_path):
    for tick in range(3):
        marker(tmp_path)
        module.recover_markers(tmp_path, os.getuid(), 1000+tick)
    path = marker(tmp_path)
    with pytest.raises(ValueError, match='Three crash'):
        module.recover_markers(tmp_path, os.getuid(), 1003)
    assert path.exists()
    assert module.recover_markers(tmp_path, os.getuid(), 2000) == 1


@pytest.mark.parametrize('kind', ['symlink', 'unknown', 'nonempty', 'partial_archive'])
def test_ambiguous_crash_state_is_retained(tmp_path, kind):
    path = marker(tmp_path)
    if kind == 'symlink':
        path.unlink(); path.symlink_to(tmp_path/'other')
    elif kind == 'unknown': path.rename(path.with_name('unknown'))
    elif kind == 'nonempty': path.write_text('unexpected')
    else: (tmp_path/'managed-crash-history'/'partial').mkdir(parents=True)
    before = sorted(p.name for p in (tmp_path/'.sentinel').iterdir())
    with pytest.raises(ValueError): module.recover_markers(tmp_path, os.getuid(), 1000)
    assert sorted(p.name for p in (tmp_path/'.sentinel').iterdir()) == before


def test_dangling_sentinel_symlink_is_rejected(tmp_path):
    (tmp_path/'.sentinel').symlink_to(tmp_path/'missing', target_is_directory=True)
    with pytest.raises(ValueError): module.recover_markers(tmp_path, os.getuid(), 1000)


def test_browser_change_preserves_every_other_byte_and_backup(tmp_path):
    raw = b'[General]\r\nBrowserHWAccel=true\r\nOther=preserved\r\n[Audio]\r\nVolume=unchanged\r\n'
    (tmp_path/'global.ini').write_bytes(raw)
    module.software_browser(tmp_path, os.getuid())
    assert (tmp_path/'global.ini').read_bytes() == raw.replace(b'BrowserHWAccel=true', b'BrowserHWAccel=false')
    assert next(tmp_path.glob('global.ini.before-*')).read_bytes() == raw
    module.software_browser(tmp_path, os.getuid())
    assert len(list(tmp_path.glob('global.ini.before-*'))) == 1


def test_wrong_or_duplicate_browser_setting_is_not_rewritten(tmp_path):
    for raw in (b'[Other]\nBrowserHWAccel=true\n',b'[General]\nBrowserHWAccel=true\nBrowserHWAccel=true\n'):
        (tmp_path/'global.ini').write_bytes(raw)
        with pytest.raises(ValueError): module.software_browser(tmp_path, os.getuid())
        assert (tmp_path/'global.ini').read_bytes() == raw
