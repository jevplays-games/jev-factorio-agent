import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def renderer(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'docs/workflows'
    directory = tmp_path / 'workspace/docs/workflows'
    directory.mkdir(parents=True)
    shutil.copytree(source / 'mmd', directory / 'mmd')
    shutil.copyfile(source / 'mermaid-config.json', directory / 'mermaid-config.json')
    spec = importlib.util.spec_from_file_location('workflow_renderer', source / 'render.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(directory / 'render.py')
    module.split_sections(directory)
    output = directory / 'pngs'
    output.mkdir()
    vectors = directory / 'svgs'
    vectors.mkdir()
    records = []
    for path in sorted((directory / 'mmd').glob('*.mmd')):
        image = output / (path.stem + '.png')
        image.write_bytes(b'unchanged published image')
        vector = vectors / (path.stem + '.svg')
        vector.write_bytes(b'unchanged published vector')
        records.append({'source': path.relative_to(directory).as_posix(),
                        'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'config_sha256': hashlib.sha256((directory / 'mermaid-config.json').read_bytes()).hexdigest(),
                        'png': image.relative_to(directory).as_posix(),
                        'svg': vector.relative_to(directory).as_posix(),
                        'png_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                        'svg_sha256': hashlib.sha256(vector.read_bytes()).hexdigest()})
    (output / 'manifest.json').write_text(json.dumps(records), encoding='utf-8')
    complete = directory / 'mmd/00-complete-workflow.mmd'
    complete.write_text(complete.read_text(encoding='utf-8').replace(
        'Start or resume the existing campaign', 'Start or resume the edited campaign'),
        encoding='utf-8')
    return module, directory


def bytes_by_name(directory):
    return {p.name: p.read_bytes() for p in (directory / 'mmd').glob('*.mmd')}


def run_cli(module, monkeypatch, *arguments):
    monkeypatch.setattr(sys, 'argv', ['render.py', *arguments])
    module.main()


@pytest.mark.parametrize('only', ['00-complete-workflow', '01-campaign-supervision', None])
@pytest.mark.parametrize('changed_theme', [False, True])
def test_partial_render_keeps_retained_sources_and_manifest_aligned(renderer, monkeypatch, only, changed_theme):
    module, directory = renderer
    before = bytes_by_name(directory)
    if changed_theme:
        config_path = directory / 'mermaid-config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['themeVariables']['background'] = 'black'
        config_path.write_text(json.dumps(config), encoding='utf-8')
    calls = []

    # Test the production CLI orchestration, not browser rasterization.
    class Image:
        def save(self, path):
            Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 8 + struct.pack('>II', 2, 2))

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'PIL', SimpleNamespace(
        Image=SimpleNamespace(new=lambda *args: Image())))

    def render(command, **kwargs):
        calls.append(command)
        if '-o' in command:
            Path(command[command.index('-o') + 1]).write_text(
                '<svg viewBox="0 0 2 2"/>', encoding='utf-8')
        else:
            tiles = Path(command[5])
            tiles.mkdir(parents=True, exist_ok=True)
            (tiles / 'tiles.json').write_text('[]', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, 'run', render)
    arguments = ['--mmdc', 'fake-renderer', '--browser', 'fake-browser']
    if only:
        arguments += ['--only', only]
    run_cli(module, monkeypatch, *arguments)
    records = json.loads((directory / 'pngs/manifest.json').read_text(encoding='utf-8'))
    assert len(records) == (1 if only and changed_theme else 8 if only == '01-campaign-supervision' else 9)
    assert all(hashlib.sha256((directory / a['source']).read_bytes()).hexdigest()
               == a['source_sha256'] for a in records)
    assert all(a['config_sha256'] == hashlib.sha256(
        (directory / 'mermaid-config.json').read_bytes()).hexdigest() for a in records)
    assert all(a[kind + '_sha256'] == hashlib.sha256(
        (directory / a[kind]).read_bytes()).hexdigest()
        for a in records for kind in ['png', 'svg'])
    if only:
        assert len(calls) == 2
        for name, raw in before.items():
            if Path(name).stem != only:
                assert (directory / 'mmd' / name).read_bytes() == raw
                assert (directory / 'pngs' / (Path(name).stem + '.png')).read_bytes() == b'unchanged published image'
                assert (directory / 'svgs' / (Path(name).stem + '.svg')).read_bytes() == b'unchanged published vector'
    else:
        assert len(calls) == 18


@pytest.mark.parametrize('only', ['00-complete-workflow', '01-campaign-supervision', None])
def test_split_only_respects_selection(renderer, monkeypatch, only):
    module, directory = renderer
    before = bytes_by_name(directory)
    arguments = ['--split-only'] + (['--only', only] if only else [])
    run_cli(module, monkeypatch, *arguments)
    changed = {name for name, raw in before.items() if (directory / 'mmd' / name).read_bytes() != raw}
    assert changed == (set() if only == '00-complete-workflow' else {'01-campaign-supervision.mmd'})


@pytest.mark.parametrize('only', ['00-complete-workflow', '01-campaign-supervision', None])
@pytest.mark.parametrize('changed_theme', [False, True])
def test_split_only_invalidates_stale_manifest_without_rendering(renderer, monkeypatch, only, changed_theme):
    module, directory = renderer
    before = bytes_by_name(directory)
    images = {p.name: p.read_bytes() for p in (directory / 'pngs').glob('*.png')}
    vectors = {p.name: p.read_bytes() for p in (directory / 'svgs').glob('*.svg')}
    if changed_theme:
        config_path = directory / 'mermaid-config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['themeVariables']['background'] = 'black'
        config_path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('split-only launched a renderer'))
    run_cli(module, monkeypatch, '--split-only', *(['--only', only] if only else []))
    records = json.loads((directory / 'pngs/manifest.json').read_text(encoding='utf-8'))
    assert len(records) == (0 if changed_theme else 8 if only == '00-complete-workflow' else 7)
    assert all(a['source_sha256'] == hashlib.sha256((directory / a['source']).read_bytes()).hexdigest()
               for a in records)
    assert all(a['config_sha256'] == hashlib.sha256((directory / 'mermaid-config.json').read_bytes()).hexdigest()
               for a in records)
    if only:
        assert all((directory / 'mmd' / name).read_bytes() == data for name, data in before.items()
                   if Path(name).stem != only)
    assert images == {p.name: p.read_bytes() for p in (directory / 'pngs').glob('*.png')}
    assert vectors == {p.name: p.read_bytes() for p in (directory / 'svgs').glob('*.svg')}


@pytest.mark.parametrize('only', ['00-complete-workflow', '01-campaign-supervision', None])
def test_split_only_preserves_already_current_manifest_bytes(renderer, monkeypatch, only):
    module, directory = renderer
    complete = directory / 'mmd/00-complete-workflow.mmd'
    complete.write_text(complete.read_text(encoding='utf-8').replace(
        'Start or resume the edited campaign', 'Start or resume the existing campaign'), encoding='utf-8', newline='\n')
    before = (directory / 'pngs/manifest.json').read_bytes()
    run_cli(module, monkeypatch, '--split-only', *(['--only', only] if only else []))
    assert (directory / 'pngs/manifest.json').read_bytes() == before


@pytest.mark.parametrize('failure', ['missing_section', 'malformed_manifest', 'missing_config'])
def test_split_only_input_failure_preserves_all_published_files(renderer, monkeypatch, failure):
    module, directory = renderer
    if failure == 'missing_section':
        complete = directory / 'mmd/00-complete-workflow.mmd'
        complete.write_text(complete.read_text(encoding='utf-8').replace(
            'subgraph REPAIR[', 'subgraph UNAVAILABLE['), encoding='utf-8')
        exception = ValueError
    elif failure == 'malformed_manifest':
        (directory / 'pngs/manifest.json').write_text('not-json', encoding='utf-8')
        exception = json.JSONDecodeError
    else:
        (directory / 'mermaid-config.json').unlink()
        exception = FileNotFoundError
    sources = bytes_by_name(directory)
    manifest = (directory / 'pngs/manifest.json').read_bytes()
    with pytest.raises(exception):
        run_cli(module, monkeypatch, '--split-only')
    assert bytes_by_name(directory) == sources
    assert (directory / 'pngs/manifest.json').read_bytes() == manifest


@pytest.mark.parametrize('arguments', [ ['--split-only', '--only', 'unknown'], ['--only', '01-campaign-supervision'] ])
def test_invalid_cli_does_not_rewrite_sources(renderer, monkeypatch, arguments):
    module, directory = renderer
    before = bytes_by_name(directory)
    with pytest.raises(SystemExit) as error:
        run_cli(module, monkeypatch, *arguments)
    assert error.value.code == 2
    assert bytes_by_name(directory) == before


@pytest.mark.parametrize('only,failure_phase', [
    (only, phase) for only in ['00-complete-workflow', '01-campaign-supervision', None]
    for phase in ['vector', 'tiles']
] + [(None, 'later')])
def test_failed_render_preserves_published_sources_images_and_manifest(renderer, monkeypatch, only, failure_phase):
    module, directory = renderer
    before = bytes_by_name(directory)
    manifest = (directory / 'pngs/manifest.json').read_bytes()
    images = {path.name: path.read_bytes() for path in (directory / 'pngs').glob('*.png')}
    vectors = {path.name: path.read_bytes() for path in (directory / 'svgs').glob('*.svg')}
    class Image:
        def save(self, path):
            Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 8 + struct.pack('>II', 2, 2))
        def close(self):
            pass
    monkeypatch.setitem(sys.modules, 'PIL', SimpleNamespace(
        Image=SimpleNamespace(new=lambda *args: Image())))
    calls = []
    def fail(command, **kwargs):
        calls.append(command)
        if (failure_phase == 'tiles' or failure_phase == 'later' and len(calls) < 3) and '-o' in command:
            Path(command[command.index('-o') + 1]).write_text(
                '<svg viewBox="0 0 2 2"/>', encoding='utf-8')
            return subprocess.CompletedProcess(command, 0)
        if failure_phase == 'later' and len(calls) == 2:
            tiles = Path(command[5])
            tiles.mkdir(parents=True, exist_ok=True)
            (tiles / 'tiles.json').write_text('[]', encoding='utf-8')
            return subprocess.CompletedProcess(command, 0)
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(module.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        arguments = ['--mmdc', 'fake', '--browser', 'fake']
        if only:
            arguments += ['--only', only]
        run_cli(module, monkeypatch, *arguments)
    assert bytes_by_name(directory) == before
    assert (directory / 'pngs/manifest.json').read_bytes() == manifest
    assert {path.name: path.read_bytes() for path in (directory / 'pngs').glob('*.png')} == images
    assert {path.name: path.read_bytes() for path in (directory / 'svgs').glob('*.svg')} == vectors
