"""Portable context and real local-Git fingerprint tests (no network)."""
import json
import subprocess

import pytest

from jev_factorio.provenance import CONTEXT_ENV, append_audit, gameplay_context, identifier, source_revision


@pytest.mark.parametrize("value", [None, "", "../run", "run\nnext", "a" * 129, 1, True])
def test_invalid_id(value):
    with pytest.raises(ValueError):
        identifier(value)


def test_absent_context_is_exactly_legacy(monkeypatch):
    monkeypatch.delenv(CONTEXT_ENV, raising=False)
    assert gameplay_context() == {}


@pytest.mark.parametrize("raw", ["null", "[]", "{", '{"run_id": "r"}'])
def test_malformed_context_rejected(monkeypatch, raw):
    monkeypatch.setenv(CONTEXT_ENV, raw)
    with pytest.raises(ValueError):
        gameplay_context()


def test_context_allowlist_rejects_secret_fields(monkeypatch):
    context = {"run_id": "r", "segment_id": "s", "execution_id": "e", "code_revision": None}
    monkeypatch.setenv(CONTEXT_ENV, json.dumps(context))
    assert gameplay_context() == context
    context["api_key"] = "do-not-export"
    monkeypatch.setenv(CONTEXT_ENV, json.dumps(context))
    with pytest.raises(ValueError):
        gameplay_context()


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text(".env\n__pycache__/\n")
    (tmp_path / "source.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    return tmp_path, git


def test_fingerprint_detects_dirty_staged_untracked_and_committed_changes(repo):
    root, git = repo
    initial = source_revision(root)
    assert initial is not None and source_revision(root) == initial
    (root / "source.py").write_text("value = 2\n")
    dirty = source_revision(root)
    assert dirty["commit"] == initial["commit"] and dirty != initial
    git("add", "source.py")
    staged = source_revision(root)
    assert staged != dirty
    git("commit", "-qm", "second")
    committed = source_revision(root)
    assert committed["commit"] != initial["commit"]
    (root / "new.py").write_text("new = True\n")
    assert source_revision(root) != committed
    (root / "new.py").unlink()
    assert source_revision(root) == committed
    (root / "source.py").unlink()
    assert source_revision(root) != committed


def test_nested_checkout_directory_is_not_a_repository_root(repo):
    root, _ = repo
    nested = root / "nested"
    nested.mkdir()
    assert source_revision(root) is not None
    assert source_revision(nested) is None


def test_ignored_secrets_and_untracked_runtime_do_not_change_revision(repo):
    root, git = repo
    runtime = root / "supervision"
    runtime.mkdir()
    before = source_revision(root, exclude_untracked=(runtime,))
    (root / ".env").write_text("API_KEY=do-not-log\n")
    (runtime / "events.jsonl").write_text("generated evidence\n")
    assert source_revision(root, exclude_untracked=(runtime,)) == before
    # Exclusions must not conceal files that Git actually tracks.
    (runtime / "code.py").write_text("tracked = True\n")
    git("add", "supervision/code.py")
    assert source_revision(root, exclude_untracked=(runtime,)) != before


def test_broad_runtime_exclusions_fail_closed(repo):
    root, _ = repo
    assert source_revision(root, exclude_untracked=(root,)) is None
    assert source_revision(root, exclude_untracked_prefixes=(root,)) is None


def test_symlink_targets_are_not_read(repo):
    root, _ = repo
    (root / "link").symlink_to("/a/nonexistent/secret")
    first = source_revision(root)
    assert first is not None
    (root / "link").unlink()
    (root / "link").symlink_to("/different/nonexistent/secret")
    assert source_revision(root) != first


def test_non_git_checkout_is_unknown(tmp_path):
    assert source_revision(tmp_path) is None


def test_expired_budget_is_unknown(repo):
    assert source_revision(repo[0], timeout=0) is None


def test_missing_git_is_unknown(repo, monkeypatch):
    import jev_factorio.provenance as module
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert source_revision(repo[0]) is None


def test_large_audit_tail_and_exact_retry(tmp_path):
    path = tmp_path / "events.jsonl"
    row = {"run_id": "run", "event_id": "one", "sequence": 1, "payload": "x" * 20000}
    append_audit(path, row)
    append_audit(path, row)
    assert len(path.read_text().splitlines()) == 1
    append_audit(path, {"run_id": "run", "event_id": "two", "sequence": 2})
    assert len(path.read_text().splitlines()) == 2


def test_audit_rejects_identity_collision_and_missing_history(tmp_path):
    path = tmp_path / "events.jsonl"
    first = {"run_id": "run", "event_id": "one", "sequence": 1}
    append_audit(path, first)
    with pytest.raises(ValueError, match="collision"):
        append_audit(path, {**first, "payload": "altered"})
    with pytest.raises(ValueError, match="different run"):
        append_audit(path, {"run_id": "different", "event_id": "two", "sequence": 2})
    path.unlink()
    with pytest.raises(ValueError, match="missing earlier"):
        append_audit(path, {"run_id": "run", "event_id": "two", "sequence": 2})
