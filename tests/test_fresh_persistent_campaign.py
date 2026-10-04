"""A new persistent campaign is explicit and cannot overwrite a resume boundary."""
import json
import sys
from types import SimpleNamespace

import pytest

from jev_factorio import main, provenance, operational_safety
from jev_factorio.backends.mock import MockBackend
from jev_factorio.controller import HierarchicalLoop


CONTEXT = {"run_id": "fresh-test", "segment_id": "initial", "execution_id": "start",
           "code_revision": {"commit": "a" * 40, "source_sha256": "b" * 64}}


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(provenance.CONTEXT_ENV, json.dumps(CONTEXT))
    for key in ("JEV_BACKEND", "JEV_RUN_DIR", "JEV_LOG_FILE", "JEV_DASHBOARD_EVENTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(operational_safety, "storage_ready", lambda _: True)
    monkeypatch.setattr("jev_factorio.jev_client.make_client", lambda **_: SimpleNamespace(model="live"))
    args = ["jev-factorio", "--backend", "fle", "--controller", "hierarchical",
            "--policy", "jev", "--until-complete", "--persist-recoverable-blocks",
            "--initialize-persistent-campaign", "--checkpoint", str(tmp_path / "controller.json"),
            "--run-dir", str(tmp_path / "research"), "--tick-seconds", "1"]
    monkeypatch.setattr(sys, "argv", args)
    return args


def test_explicit_new_mode_reaches_backend_once_and_records_truthful_configuration(invocation, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: calls.append(("backend", kw)) or MockBackend())

    class Loop:
        def __init__(self, backend, **kw):
            calls.append(("loop", kw))
        def run(self, **kw):
            calls.append(("run", kw))

    monkeypatch.setattr("jev_factorio.controller.HierarchicalLoop", Loop)
    main.cli()
    assert [name for name, _ in calls] == ["backend", "loop", "run"]
    assert calls[0][1]["resume"] is False
    assert calls[1][1]["resume_controller"] is False
    assert calls[1][1]["initialize_persistent_campaign"] is True
    assert calls[1][1]["persist_recoverable_blocks"] is True
    assert calls[2][1] == {"until_complete": True}
    config = json.loads((tmp_path / "research/manifest.json").read_text())["configuration"]
    assert config["initialize_persistent_campaign"] is True
    assert config["resume"] is config["resume_controller"] is False


@pytest.mark.parametrize("conflict", ["--resume", "--resume-controller", "--adopt-session",
                                      "--reevaluate-blocked-once", "--reconcile-only"])
def test_conflicting_mode_never_attaches(invocation, monkeypatch, conflict):
    invocation.append(conflict)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("backend started"))
    with pytest.raises(SystemExit) as error:
        main.cli()
    assert error.value.code == 2


def test_existing_checkpoint_is_preserved_before_backend(invocation, monkeypatch, tmp_path):
    path = tmp_path / "controller.json"
    path.write_bytes(b"retained checkpoint")
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("backend started"))
    with pytest.raises(SystemExit):
        main.cli()
    assert path.read_bytes() == b"retained checkpoint"
    assert not (tmp_path / "research").exists()


def test_checkpoint_appearing_during_preflight_never_attaches(invocation, monkeypatch, tmp_path):
    def ready(_):
        (tmp_path / "controller.json").write_bytes(b"concurrent owner")
        return True
    monkeypatch.setattr(operational_safety, "storage_ready", ready)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("backend started"))
    with pytest.raises(SystemExit):
        main.cli()
    assert (tmp_path / "controller.json").read_bytes() == b"concurrent owner"


def test_new_mode_requires_source_provenance(invocation, monkeypatch, tmp_path):
    monkeypatch.delenv(provenance.CONTEXT_ENV)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("backend started"))
    with pytest.raises(SystemExit):
        main.cli()
    assert not (tmp_path / "research").exists()


def test_controller_requires_explicit_new_mode_and_keeps_resume_guard(tmp_path, monkeypatch):
    monkeypatch.setenv(provenance.CONTEXT_ENV, json.dumps(CONTEXT))
    options = dict(jev=SimpleNamespace(is_mock=False), target="bootstrap_mining",
                   checkpoint=str(tmp_path / "controller.json"), persist_recoverable_blocks=True)
    with pytest.raises(ValueError, match="requires resumed"):
        HierarchicalLoop(MockBackend(), **options)
    loop = HierarchicalLoop(MockBackend(), initialize_persistent_campaign=True, **options)
    assert loop.persist_recoverable_blocks and not loop.resume_controller
    with pytest.raises(ValueError, match="existing controller checkpoint"):
        HierarchicalLoop(MockBackend(), resume_controller=True, **options)


def test_failed_initialization_cannot_reuse_research_directory(invocation, monkeypatch):
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise RuntimeError("initialization failed")
    monkeypatch.setattr(main, "make_backend", fail)
    with pytest.raises(RuntimeError, match="initialization failed"):
        main.cli()
    with pytest.raises(SystemExit):
        main.cli()
    assert calls == [1]


def test_failed_initialization_cannot_reset_again_with_different_research_directory(invocation, monkeypatch, tmp_path):
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise RuntimeError("partial native initialization")
    monkeypatch.setattr(main, "make_backend", fail)
    with pytest.raises(RuntimeError):
        main.cli()
    invocation[invocation.index("--run-dir") + 1] = str(tmp_path / "other-research")
    with pytest.raises(SystemExit):
        main.cli()
    intent = json.loads((tmp_path / "controller.json.initialization.json").read_text())
    assert intent["automatic_initialization_retry_allowed"] is False
    assert calls == [1]
