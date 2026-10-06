"""Offline CLI and run-lifecycle controls for opt-in async decisions."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time

import pytest
import requests

from jev_factorio import main
from jev_factorio.backends.mock import MockBackend
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.jev_client import AsyncMockJevClient
from jev_factorio.memory import CampaignMemory


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JEV_FACTORIO_PROVENANCE", json.dumps({
        "run_id": "cli-test-run", "segment_id": "cli-test-segment",
        "execution_id": "cli-test-execution",
        "code_revision": {"commit": "a" * 40, "source_sha256": "b" * 64},
    }))
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("unexpected provider call"))


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["jev-factorio", *map(str, arguments)])
    main.cli()


class CountingBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.actions = []

    def act(self, action):
        self.actions.append(action)
        return super().act(action)


class LoopRecordingAsyncMock(AsyncMockJevClient):
    def __init__(self):
        self.evaluate_loop_ids = []
        self.close_loop_ids = []
        self.close_calls = 0

    async def evaluate(self, *args, **kwargs):
        self.evaluate_loop_ids.append(id(asyncio.get_running_loop()))
        return await super().evaluate(*args, **kwargs)

    async def aclose(self):
        self.close_calls += 1
        self.close_loop_ids.append(id(asyncio.get_running_loop()))


def patch_async_mock_client(monkeypatch, client):
    from jev_factorio import jev_client

    monkeypatch.setattr(jev_client, "AsyncMockJevClient", lambda: client)


def test_async_cli_runs_real_controller_and_closes_provider_on_owning_loop(
        tmp_path, monkeypatch, capsys):
    from jev_factorio import jev_client

    backend = CountingBackend()
    client = LoopRecordingAsyncMock()
    factory_calls = []

    def make_async_mock():
        factory_calls.append("explicit mock")
        return client

    monkeypatch.setattr(jev_client, "AsyncMockJevClient", make_async_mock)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    checkpoint = tmp_path / "campaign.json"
    run_dir = tmp_path / "research"

    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
           "--checkpoint", checkpoint, "--steps", "1", "--tick-seconds", "0",
           "--async-decision-timeout-seconds", "4", "--run-dir", run_dir)

    assert factory_calls == ["explicit mock"]
    assert len(client.evaluate_loop_ids) == 1
    assert client.close_calls == 1
    assert client.close_loop_ids == client.evaluate_loop_ids
    assert len(backend.actions) == backend.tick == 1
    data = json.loads(checkpoint.read_bytes())
    memory = CampaignMemory.from_bytes(checkpoint.read_bytes(), data["session_id"],
                                       "bootstrap_mining")
    assert memory.async_decision is None
    assert (checkpoint.with_name("campaign.json.safety") / "decision-archives").is_dir()

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    initialized = next(row for row in events if row["event_type"] == "controller_initialized")
    assert initialized["payload"]["async_decisions"] == {
        "enabled": True,
        "provider": "mock",
        "decision_deadline_seconds": 4.0,
        "transport_timeout_seconds": None,
        "max_client_concurrency": 1,
        "max_queued_requests": 0,
    }
    assert events[-2]["event_type"] == "controller_stopped"
    output = capsys.readouterr().out
    assert '"max_queued_requests": 0' in output
    assert "TYPESAFE_API_KEY" not in output and "api_token" not in output


def test_async_cli_zero_steps_still_closes_client_on_one_loop(tmp_path, monkeypatch):
    from jev_factorio import jev_client

    backend = CountingBackend()
    client = LoopRecordingAsyncMock()
    patch_async_mock_client(monkeypatch, client)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    checkpoint = tmp_path / "zero.json"

    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
           "--checkpoint", checkpoint, "--steps", "0", "--tick-seconds", "0")

    assert backend.actions == [] and backend.tick == 0
    assert client.evaluate_loop_ids == []
    assert client.close_calls == 1 and len(client.close_loop_ids) == 1
    assert not checkpoint.exists()


def test_async_cli_until_complete_runs_through_terminal_and_closes_on_same_loop(
        tmp_path, monkeypatch):
    from jev_factorio import jev_client

    backend = CountingBackend()
    client = LoopRecordingAsyncMock()
    patch_async_mock_client(monkeypatch, client)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    calls = []
    real_step_async = HierarchicalLoop.step_async

    async def complete_after_one_step(loop):
        calls.append(id(asyncio.get_running_loop()))
        # Run one real offline controller step, then use a synthetic terminal
        # status only to exercise until-complete loop exit without a campaign.
        result = await real_step_async(loop)
        loop.memory.status = "completed"
        loop.memory.reason = "offline terminal-loop control"
        loop._save()
        return result

    monkeypatch.setattr(HierarchicalLoop, "step_async", complete_after_one_step)
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
           "--checkpoint", tmp_path / "until-complete.json", "--until-complete",
           "--tick-seconds", "0")

    assert len(calls) == 1
    assert len(backend.actions) == backend.tick == 1
    assert client.evaluate_loop_ids == calls
    assert client.close_calls == 1
    assert client.close_loop_ids == calls


def test_async_cli_duration_uses_monotonic_deadline_and_closes_provider(
        tmp_path, monkeypatch):
    from jev_factorio import jev_client

    class SlowBackend(CountingBackend):
        def act(self, action):
            time.sleep(0.02)
            return super().act(action)

    backend = SlowBackend()
    client = LoopRecordingAsyncMock()
    patch_async_mock_client(monkeypatch, client)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
           "--checkpoint", tmp_path / "duration.json", "--duration-hours",
           "0.0000002777777778", "--tick-seconds", "0")

    # The first action finishes after the one-millisecond budget; the monotonic
    # deadline prevents another decision and the provider still closes cleanly.
    assert len(backend.actions) == backend.tick == 1
    assert len(client.evaluate_loop_ids) == 1
    assert client.close_calls == 1
    assert client.close_loop_ids == client.evaluate_loop_ids


def test_async_run_applies_owner_step_gate_after_verified_step(tmp_path):
    backend = CountingBackend()
    client = LoopRecordingAsyncMock()
    loop = HierarchicalLoop(
        backend, jev=client, target="bootstrap_mining", policy="jev",
        checkpoint=str(tmp_path / "owner-gate.json"), tick_seconds=0,
        async_decisions=True)
    gates = []

    def owner_gate(controller, completed, record):
        gates.append((completed, record["action"], controller is loop))
        return False

    asyncio.run(loop.run_async(steps=4, after_step=owner_gate))

    assert gates == [(1, backend.actions[0], True)]
    assert len(backend.actions) == backend.tick == 1
    assert len(client.evaluate_loop_ids) == 1
    assert loop._async_execution is None


def test_sync_cli_remains_default_and_does_not_construct_async_provider(
        tmp_path, monkeypatch, capsys):
    backend = CountingBackend()
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    monkeypatch.setattr("jev_factorio.jev_client.make_async_client",
                        lambda **kw: pytest.fail("async provider constructed by default"))
    monkeypatch.setattr("jev_factorio.jev_client.AsyncMockJevClient",
                        lambda **kw: pytest.fail("async mock constructed by default"))
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--checkpoint",
           tmp_path / "sync.json", "--steps", "1", "--tick-seconds", "0")
    assert len(backend.actions) == 1
    assert "async_decisions" not in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [
    ("--async-decisions", "--backend", "mock", "--mock-model", "--target", "bootstrap_mining"),
    ("--async-decisions", "--backend", "mock", "--controller", "flat", "--mock-model",
     "--checkpoint", "unused.json"),
    ("--async-decisions", "--backend", "mock", "--controller", "hierarchical", "--policy",
     "deterministic", "--checkpoint", "unused.json"),
    ("--async-decisions", "--backend", "mock", "--controller", "hierarchical", "--mock-model",
     "--reconcile-only", "--checkpoint", "unused.json"),
    ("--async-decision-timeout-seconds", "5", "--backend", "mock"),
])
def test_unsupported_async_cli_combinations_fail_before_backend(
        tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(main, "make_backend",
                        lambda *a, **kw: pytest.fail("backend attached before async CLI validation"))
    monkeypatch.setattr("jev_factorio.jev_client.make_async_client",
                        lambda **kw: pytest.fail("provider constructed before async CLI validation"))
    monkeypatch.setattr("jev_factorio.jev_client.AsyncMockJevClient",
                        lambda **kw: pytest.fail("mock provider constructed before async CLI validation"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *arguments)
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "31"])
def test_invalid_async_deadline_fails_before_backend(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(main, "make_backend",
                        lambda *a, **kw: pytest.fail("backend attached before deadline validation"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--async-decisions", "--backend", "mock", "--mock-model",
               "--target", "bootstrap_mining", "--checkpoint", tmp_path / "unused.json",
               "--steps", "0", "--async-decision-timeout-seconds", timeout)
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_missing_credentials_and_malformed_model_fail_before_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "make_backend",
                        lambda *a, **kw: pytest.fail("backend attached before provider preflight"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--async-decisions", "--backend", "mock", "--target",
               "bootstrap_mining", "--checkpoint", tmp_path / "no-key.json", "--steps", "0")
    assert error.value.code == 2

    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--async-decisions", "--backend", "mock", "--mock-model",
               "--target", "bootstrap_mining", "--checkpoint", tmp_path / "bad-model.json",
               "--steps", "0", "--model", " mock ")
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_async_provider_is_closed_if_backend_startup_fails(tmp_path, monkeypatch):
    from jev_factorio import jev_client

    client = LoopRecordingAsyncMock()
    patch_async_mock_client(monkeypatch, client)
    monkeypatch.setattr(main, "make_backend",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline backend failure")))
    with pytest.raises(RuntimeError, match="offline backend failure"):
        invoke(monkeypatch, "--async-decisions", "--backend", "mock", "--mock-model",
               "--controller", "hierarchical", "--target",
               "bootstrap_mining", "--checkpoint", tmp_path / "campaign.json", "--steps", "0")
    assert client.close_calls == 1
    assert len(client.close_loop_ids) == 1
    assert client.evaluate_loop_ids == []


@pytest.mark.parametrize("provider,environment", [
    ("typesafe", {"TYPESAFE_API_KEY": "local-test-key"}),
    ("cloudflare", {"CLOUDFLARE_API_TOKEN": "local-test-token",
                     "CLOUDFLARE_ACCOUNT_ID": "local-test-account"}),
])
def test_async_factory_selects_supported_provider_without_network(
        monkeypatch, provider, environment):
    from jev_factorio.jev_client import (
        AsyncCloudflareJevClient, AsyncJevClient, make_async_client,
    )

    for name in ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    client = make_async_client(allow_mock=False, model="fixture/model",
                               max_concurrency=1, max_queue=0, timeout=3)
    expected = AsyncJevClient if provider == "typesafe" else AsyncCloudflareJevClient
    assert isinstance(client, expected)
    assert client.model == "fixture/model"
    assert client.max_concurrency == 1 and client.max_queue == 0 and client.timeout == 3
    asyncio.run(client.aclose())


def test_lifecycle_cancellation_drains_actual_controller_then_closes_provider(
        tmp_path):
    entered = asyncio.Event()

    class GatedClient(LoopRecordingAsyncMock):
        async def evaluate(self, *args, **kwargs):
            self.evaluate_loop_ids.append(id(asyncio.get_running_loop()))
            entered.set()
            await asyncio.Event().wait()

    async def exercise():
        backend = CountingBackend()
        client = GatedClient()
        loop = HierarchicalLoop(
            backend, jev=client, target="bootstrap_mining", policy="jev",
            checkpoint=str(tmp_path / "cancel.json"), tick_seconds=0,
            async_decisions=True)
        # This offline control binds a stable test source; no native attachment
        # or provider transport is involved.
        loop.provenance["code_revision"] = {
            "commit": "a" * 40, "source_sha256": "b" * 64,
        }
        close_state = {"attempted": False}
        run = asyncio.create_task(main._run_async_controller_lifecycle(
            loop, close_state, steps=1))
        await asyncio.wait_for(entered.wait(), 2)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert close_state["attempted"] is True
        assert loop._async_execution is None
        assert backend.actions == [] and backend.tick == 0
        assert client.close_calls == 1
        assert client.close_loop_ids == client.evaluate_loop_ids
        return loop

    asyncio.run(exercise())


def _sync_resume_arguments(checkpoint):
    return ("--backend", "mock", "--controller", "hierarchical",
            "--target", "bootstrap_mining", "--mock-model",
            "--checkpoint", checkpoint, "--resume-controller",
            "--steps", "0", "--tick-seconds", "0")


def _sidecar_bytes(safety):
    return {path.relative_to(safety).as_posix(): path.read_bytes()
            for path in safety.rglob("*") if path.is_file()}


def test_sync_rollback_rejects_real_cancelled_async_provider_request_before_clients(
        tmp_path, monkeypatch):
    from jev_factorio import jev_client
    from jev_factorio.async_decision_archive import AsyncDecisionArchive
    from jev_factorio.operational_safety import safety_dir
    from jev_factorio.provider_decision_wal import ProviderDecisionWAL, _read_document

    checkpoint = tmp_path / "active.json"
    entered = None

    class GatedClient(AsyncMockJevClient):
        async def evaluate(self, *args, **kwargs):
            lease = kwargs.get("decision_lease")
            if lease is not None:
                # Model the durable transport-entry boundary without making
                # any provider call; cancellation after this point is ambiguous.
                lease.mark_may_have_been_sent()
            entered.set()
            await asyncio.Event().wait()

    async def create_ambiguous_checkpoint():
        nonlocal entered
        entered = asyncio.Event()
        backend = CountingBackend()
        loop = HierarchicalLoop(
            backend, jev=GatedClient(), target="bootstrap_mining", policy="jev",
            checkpoint=str(checkpoint), tick_seconds=0, async_decisions=True)
        task = asyncio.create_task(loop.step_async())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await loop.aclose_async_provider()
        assert backend.actions == [] and backend.tick == 0

    asyncio.run(create_ambiguous_checkpoint())
    saved = json.loads(checkpoint.read_bytes())
    assert saved["async_decision"]["disposition"] == "pending"
    safety = safety_dir(checkpoint)
    wal_path = safety / "provider-decisions.json"
    wal_document = _read_document(wal_path)
    session_rows = [row for row in wal_document["records"]
                    if row["identity"]["session_id"] == saved["session_id"]]
    assert len(session_rows) == 1
    assert ProviderDecisionWAL._view(session_rows[0]).state in {
        "may_have_been_sent", "ambiguous"}
    assert AsyncDecisionArchive(safety / "decision-archives").records()

    checkpoint_before = checkpoint.read_bytes()
    sidecars_before = _sidecar_bytes(safety)
    monkeypatch.setattr(main, "make_backend",
                        lambda *a, **kw: pytest.fail("backend acquired before rollback rejection"))
    monkeypatch.setattr(jev_client, "MockJevClient",
                        lambda *a, **kw: pytest.fail("provider client acquired before rollback rejection"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *_sync_resume_arguments(checkpoint))
    assert error.value.code == 2
    assert checkpoint.read_bytes() == checkpoint_before
    assert _sidecar_bytes(safety) == sidecars_before


def test_sync_rollback_accepts_real_fully_settled_async_archive(
        tmp_path, monkeypatch):
    from jev_factorio import jev_client
    from jev_factorio.async_decision_archive import AsyncDecisionArchive
    from jev_factorio.operational_safety import read_json, safety_dir
    from jev_factorio.provider_decision_wal import ProviderDecisionWAL, _read_document

    checkpoint = tmp_path / "settled.json"
    async_backend = CountingBackend()
    async_client = LoopRecordingAsyncMock()
    patch_async_mock_client(monkeypatch, async_client)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: async_backend)
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
           "--checkpoint", checkpoint, "--steps", "1", "--tick-seconds", "0")

    safety = safety_dir(checkpoint)
    memory_data = json.loads(checkpoint.read_bytes())
    memory = CampaignMemory.from_bytes(checkpoint.read_bytes(), memory_data["session_id"],
                                       "bootstrap_mining")
    assert memory.async_decision is None
    assert any(row.get("kind") == "async_decision_settled" for row in memory.history)
    assert AsyncDecisionArchive(safety / "decision-archives").records()
    wal_doc = _read_document(safety / "provider-decisions.json")
    assert any(ProviderDecisionWAL._view(row).state == "consumed"
               for row in wal_doc["records"])
    health = read_json(safety / "provider.json")
    assert health["in_flight"] is None
    assert health["decision_outcome"]["state"] == "consumed"
    checkpoint_before = checkpoint.read_bytes()
    sidecars_before = _sidecar_bytes(safety)

    sync_backend = CountingBackend()
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: sync_backend)
    invoke(monkeypatch, *_sync_resume_arguments(checkpoint))
    assert sync_backend.actions == [] and sync_backend.tick == 0

    assert checkpoint.read_bytes() == checkpoint_before
    assert _sidecar_bytes(safety) == sidecars_before

    health_path = safety / "provider.json"
    tampered_health = read_json(health_path)
    tampered_health["decision_outcome"]["request_sha256"] = "f" * 64
    from jev_factorio.operational_safety import atomic_json
    atomic_json(health_path, tampered_health)
    mismatch_checkpoint = checkpoint.read_bytes()
    mismatch_sidecars = _sidecar_bytes(safety)
    monkeypatch.setattr(main, "make_backend",
                        lambda *a, **kw: pytest.fail("backend acquired for mismatched health state"))
    monkeypatch.setattr(jev_client, "MockJevClient",
                        lambda *a, **kw: pytest.fail("provider acquired for mismatched health state"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *_sync_resume_arguments(checkpoint))
    assert error.value.code == 2
    assert checkpoint.read_bytes() == mismatch_checkpoint
    assert _sidecar_bytes(safety) == mismatch_sidecars


def test_sync_rollback_allows_legacy_checkpoint_with_empty_sidecars_and_unrelated_history(
        tmp_path, monkeypatch):
    from jev_factorio import jev_client
    from jev_factorio.async_decision_archive import AsyncDecisionArchive
    from jev_factorio.operational_safety import safety_dir
    from jev_factorio.provider_decision_wal import (
        NOT_SENT, ProviderDecisionIdentity, ProviderDecisionWAL,
    )

    checkpoint = tmp_path / "legacy.json"
    initial_backend = CountingBackend()
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: initial_backend)
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--checkpoint",
           checkpoint, "--steps", "1", "--tick-seconds", "0")
    legacy = json.loads(checkpoint.read_bytes())
    assert "async_decision" not in legacy

    safety = safety_dir(checkpoint)
    safety.mkdir(mode=0o700, exist_ok=True)
    wal = ProviderDecisionWAL.initialize(safety / "provider-decisions.json")
    other = ProviderDecisionIdentity(
        session_id="other-session", actor_id="other-actor",
        observation_id="other-observation", decision_id="other-decision",
        provider_id="mock", model_id="fixture-model", request_id="other-request")
    request = {"candidate_ids": ["plan-a"], "facts": {"tick": 1},
               "prompt_version": "selection-v1"}
    wal.reserve(other, request)
    wal.record_error(other, request, "local_admission", NOT_SENT)
    AsyncDecisionArchive(safety / "decision-archives")

    sync_backend = CountingBackend()
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: sync_backend)
    invoke(monkeypatch, *_sync_resume_arguments(checkpoint))
    assert sync_backend.actions == [] and sync_backend.tick == 0

    wal_path = safety / "provider-decisions.json"
    wal_path.write_bytes(b"{\"schema\": 1")
    malformed_wal = wal_path.read_bytes()
    checkpoint_before = checkpoint.read_bytes()
    monkeypatch.setattr(main, "make_backend",
                        lambda *a, **kw: pytest.fail("backend acquired for malformed WAL"))
    monkeypatch.setattr(jev_client, "MockJevClient",
                        lambda *a, **kw: pytest.fail("provider acquired for malformed WAL"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *_sync_resume_arguments(checkpoint))
    assert error.value.code == 2
    assert checkpoint.read_bytes() == checkpoint_before
    assert wal_path.read_bytes() == malformed_wal


@pytest.mark.parametrize(("source", "credentials"), [
    ("none", {}),
    ("environment", {"TYPESAFE_API_KEY": "sentinel-typesafe"}),
    ("environment", {"CLOUDFLARE_API_TOKEN": "sentinel-cloudflare",
                       "CLOUDFLARE_ACCOUNT_ID": "sentinel-account"}),
    ("environment", {"TYPESAFE_API_KEY": "sentinel-typesafe",
                       "CLOUDFLARE_API_TOKEN": "sentinel-cloudflare",
                       "CLOUDFLARE_ACCOUNT_ID": "sentinel-account"}),
    ("dotenv", {"TYPESAFE_API_KEY": "sentinel-typesafe"}),
    ("dotenv", {"CLOUDFLARE_API_TOKEN": "sentinel-cloudflare",
                 "CLOUDFLARE_ACCOUNT_ID": "sentinel-account"}),
    ("dotenv", {"TYPESAFE_API_KEY": "sentinel-typesafe",
                 "CLOUDFLARE_API_TOKEN": "sentinel-cloudflare",
                 "CLOUDFLARE_ACCOUNT_ID": "sentinel-account"}),
])
def test_explicit_async_mock_ignores_exported_and_dotenv_provider_credentials(
        source, credentials, tmp_path, monkeypatch, capsys):
    import httpx

    if source == "environment":
        for name, value in credentials.items():
            monkeypatch.setenv(name, value)
    elif source == "dotenv":
        (tmp_path / ".env").write_text(
            "".join(f"{name}={value}\n" for name, value in credentials.items()),
            encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("explicit --mock-model acquired an HTTP provider")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)

    run_dir = tmp_path / "research"
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
           "--checkpoint", tmp_path / "mock.json", "--steps", "1",
           "--tick-seconds", "0", "--run-dir", run_dir)

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    initialized = next(row for row in events if row["event_type"] == "controller_initialized")
    assert initialized["payload"]["requested_model"] == "mock-rule-based"
    assert initialized["payload"]["model_is_mock"] is True
    assert initialized["payload"]["async_decisions"] == {
        "enabled": True,
        "provider": "mock",
        "decision_deadline_seconds": 30.0,
        "transport_timeout_seconds": None,
        "max_client_concurrency": 1,
        "max_queued_requests": 0,
    }
    assert any(row["event_type"] == "controller_stopped" for row in events)
    output = capsys.readouterr().out
    assert '"provider": "mock"' in output
    assert "sentinel-" not in output
    assert "sentinel-" not in (run_dir / "events.jsonl").read_text()


@pytest.mark.parametrize("async_decisions", [False, True], ids=["sync", "async"])
def test_duration_hours_conversion_overflow_fails_before_provider_or_backend(
        async_decisions, tmp_path, monkeypatch, capsys):
    from jev_factorio import jev_client

    monkeypatch.setenv("TYPESAFE_API_KEY", "sentinel-typesafe")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "sentinel-cloudflare")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "sentinel-account")

    def forbidden(*args, **kwargs):
        pytest.fail("overflowing duration acquired a provider or backend")

    monkeypatch.setattr(main, "make_backend", forbidden)
    monkeypatch.setattr(jev_client, "make_client", forbidden)
    monkeypatch.setattr(jev_client, "MockJevClient", forbidden)
    monkeypatch.setattr(jev_client, "make_async_client", forbidden)
    monkeypatch.setattr(jev_client, "AsyncMockJevClient", forbidden)
    checkpoint = tmp_path / "overflow.json"
    arguments = ["--backend", "mock", "--controller", "hierarchical",
                 "--target", "bootstrap_mining", "--mock-model",
                 "--checkpoint", checkpoint, "--duration-hours", "1e308"]
    if async_decisions:
        arguments.append("--async-decisions")

    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *arguments)

    assert error.value.code == 2
    assert "duration" in capsys.readouterr().err.lower()
    assert not checkpoint.exists()


@pytest.mark.parametrize("async_decisions", [False, True], ids=["sync", "async"])
def test_finite_duration_hours_are_preserved_without_an_arbitrary_cap(
        async_decisions, tmp_path, monkeypatch):
    calls = []

    def capture_sync(loop, *, steps, duration_seconds, **kwargs):
        calls.append((steps, duration_seconds))

    async def capture_async(loop, *, steps, duration_seconds, **kwargs):
        calls.append((steps, duration_seconds))

    monkeypatch.setattr(HierarchicalLoop, "run", capture_sync)
    monkeypatch.setattr(HierarchicalLoop, "run_async", capture_async)
    arguments = ["--backend", "mock", "--controller", "hierarchical",
                 "--target", "bootstrap_mining", "--mock-model",
                 "--checkpoint", tmp_path / f"large-finite-{async_decisions}.json",
                 "--duration-hours", "1e300", "--tick-seconds", "0",
                 "--run-dir", tmp_path / f"large-finite-research-{async_decisions}"]
    if async_decisions:
        arguments.append("--async-decisions")

    invoke(monkeypatch, *arguments)

    assert calls == [(None, 1e300 * 3600)]


@pytest.mark.parametrize("async_decisions", [False, True], ids=["sync", "async"])
def test_public_run_methods_reject_nonfinite_duration_without_a_step(
        async_decisions, tmp_path):
    backend = CountingBackend()
    client = LoopRecordingAsyncMock()
    loop = HierarchicalLoop(
        backend, jev=client, target="bootstrap_mining", policy="jev",
        checkpoint=str(tmp_path / f"nonfinite-{async_decisions}.json"),
        tick_seconds=0, async_decisions=async_decisions)

    if async_decisions:
        with pytest.raises(ValueError, match="finite"):
            asyncio.run(loop.run_async(steps=0, duration_seconds=float("inf")))
    else:
        with pytest.raises(ValueError, match="finite"):
            loop.run(steps=0, duration_seconds=float("inf"))

    assert backend.actions == [] and backend.tick == 0
    assert client.evaluate_loop_ids == []


def test_async_cli_dashboard_preserves_mock_ownership_with_process_and_dotenv_credentials(
        tmp_path, monkeypatch, capsys):
    """The telemetry wrapper must not erase the concrete mock transport identity."""
    import httpx

    from jev_factorio import jev_client

    backend = CountingBackend()
    client = LoopRecordingAsyncMock()
    patch_async_mock_client(monkeypatch, client)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    monkeypatch.setenv("TYPESAFE_API_KEY", "process-sentinel-typesafe")
    (tmp_path / ".env").write_text(
        "CLOUDFLARE_API_TOKEN=dotenv-sentinel-cloudflare\n"
        "CLOUDFLARE_ACCOUNT_ID=dotenv-sentinel-account\n",
        encoding="utf-8")

    def forbidden_provider(*args, **kwargs):
        pytest.fail("explicit mock dashboard run constructed a real provider")

    monkeypatch.setattr(jev_client, "make_async_client", forbidden_provider)
    monkeypatch.setattr(jev_client, "AsyncJevClient", forbidden_provider)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden_provider)
    monkeypatch.setattr(httpx, "Client", forbidden_provider)

    checkpoint = tmp_path / "dashboard-async-campaign.json"
    dashboard_path = tmp_path / "dashboard-async.jsonl"
    invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
           "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
           "--dashboard-events", dashboard_path, "--checkpoint", checkpoint,
           "--steps", "1", "--tick-seconds", "0")

    assert backend.tick == len(backend.actions) == 1
    assert checkpoint.is_file()
    assert len(client.evaluate_loop_ids) == 1
    assert client.close_calls == 1
    assert client.close_loop_ids == client.evaluate_loop_ids
    rows = [json.loads(line) for line in dashboard_path.read_text().splitlines()]
    kinds = [row["kind"] for row in rows]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "action" in kinds and "dispatch_started" in kinds
    assert rows[-1]["data"]["outcome"] == "returned"
    output = capsys.readouterr().out
    assert '"provider": "mock"' in output
    assert "sentinel-" not in output
    assert "sentinel-" not in dashboard_path.read_text()


def test_async_cli_dashboard_failure_is_recorded_and_provider_closes_once(
        tmp_path, monkeypatch):
    import httpx

    from jev_factorio import jev_client

    class FailingMock(LoopRecordingAsyncMock):
        async def evaluate(self, *args, **kwargs):
            self.evaluate_loop_ids.append(id(asyncio.get_running_loop()))
            raise RuntimeError("offline injected provider failure")

    backend = CountingBackend()
    client = FailingMock()
    patch_async_mock_client(monkeypatch, client)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(
        jev_client, "make_async_client",
        lambda **kwargs: pytest.fail("mock failure path constructed a real provider"))
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: pytest.fail("mock failure path constructed HTTP"))
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **kw: pytest.fail("mock failure path constructed HTTP"))

    dashboard_path = tmp_path / "dashboard-failure.jsonl"
    with pytest.raises(RuntimeError, match="offline injected provider failure"):
        invoke(monkeypatch, "--backend", "mock", "--controller", "hierarchical",
               "--target", "bootstrap_mining", "--mock-model", "--async-decisions",
               "--dashboard-events", dashboard_path,
               "--checkpoint", tmp_path / "dashboard-failure-campaign.json",
               "--steps", "1", "--tick-seconds", "0")

    rows = [json.loads(line) for line in dashboard_path.read_text().splitlines()]
    kinds = [row["kind"] for row in rows]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "cycle_failed" in kinds
    assert rows[-1]["data"]["outcome"] == "error"
    assert len(client.evaluate_loop_ids) == 1
    assert client.close_calls == 1
    assert client.close_loop_ids == client.evaluate_loop_ids
    assert backend.actions == [] and backend.tick == 0


def test_dashboard_async_cancellation_drains_worker_and_closes_provider(tmp_path):
    from jev_factorio.dashboard import EventWriter, attach

    async def exercise():
        entered = asyncio.Event()

        class GatedClient(LoopRecordingAsyncMock):
            async def evaluate(self, *args, **kwargs):
                self.evaluate_loop_ids.append(id(asyncio.get_running_loop()))
                entered.set()
                await asyncio.Event().wait()

        backend = CountingBackend()
        client = GatedClient()
        loop = HierarchicalLoop(
            backend, jev=client, target="bootstrap_mining", policy="jev",
            checkpoint=str(tmp_path / "dashboard-cancel.json"), tick_seconds=0,
            async_decisions=True)
        loop.provenance["code_revision"] = {
            "commit": "a" * 40, "source_sha256": "b" * 64,
        }
        close_state = {"attempted": False}
        dashboard_path = tmp_path / "dashboard-cancel.jsonl"
        with EventWriter(dashboard_path) as writer:
            attach(loop, writer)
            run = asyncio.create_task(main._run_async_controller_lifecycle(
                loop, close_state, steps=1))
            await asyncio.wait_for(entered.wait(), 2)
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        rows = [json.loads(line) for line in dashboard_path.read_text().splitlines()]
        assert close_state["attempted"] is True
        assert loop._async_execution is None
        assert backend.actions == [] and backend.tick == 0
        assert client.close_calls == 1
        assert client.close_loop_ids == client.evaluate_loop_ids
        assert rows[0]["kind"] == "run_started" and rows[-1]["kind"] == "run_finished"
        assert rows[-1]["data"]["outcome"] == "returned"
        assert "action" not in [row["kind"] for row in rows]

    asyncio.run(exercise())


def test_dashboard_wrappers_reject_second_controller_on_shared_mock_transport(tmp_path):
    from jev_factorio.controller import AsyncControllerBusy
    from jev_factorio.dashboard import EventWriter, attach

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        class GatedClient(LoopRecordingAsyncMock):
            async def evaluate(self, *args, **kwargs):
                self.evaluate_loop_ids.append(id(asyncio.get_running_loop()))
                entered.set()
                await release.wait()
                return await super().evaluate(*args, **kwargs)

        backend = CountingBackend()
        first_client, second_client = GatedClient(), LoopRecordingAsyncMock()
        first = HierarchicalLoop(
            backend, jev=first_client, target="bootstrap_mining", policy="jev",
            checkpoint=str(tmp_path / "dashboard-shared-first.json"), tick_seconds=0,
            async_decisions=True)
        second = HierarchicalLoop(
            backend, jev=second_client, target="bootstrap_mining", policy="jev",
            checkpoint=str(tmp_path / "dashboard-shared-second.json"), tick_seconds=0,
            async_decisions=True)
        for loop in (first, second):
            loop.provenance["code_revision"] = {
                "commit": "a" * 40, "source_sha256": "b" * 64,
            }
        with EventWriter(tmp_path / "dashboard-shared-first.jsonl") as first_writer:
            with EventWriter(tmp_path / "dashboard-shared-second.jsonl") as second_writer:
                attach(first, first_writer)
                attach(second, second_writer)
                first_keys = first._async_resource_lock_keys()
                second_keys = second._async_resource_lock_keys()
                assert first_keys == second_keys
                first_step = asyncio.create_task(first.step_async())
                await asyncio.wait_for(entered.wait(), 2)
                with pytest.raises(AsyncControllerBusy, match="already has a controller step"):
                    await second.step_async()
                assert second_client.evaluate_loop_ids == []
                assert backend.actions == [] and backend.tick == 0
                release.set()
                await first_step
        await first.aclose_async_provider()
        await second.aclose_async_provider()
        assert backend.tick == len(backend.actions) == 1
        assert first_client.close_calls == second_client.close_calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("malformed_wal", [False, True], ids=["return", "reject"])
def test_async_rollback_closes_archived_loader_on_return_and_rejection(
        tmp_path, monkeypatch, malformed_wal):
    from jev_factorio import blocked_recovery_archive as archive
    from jev_factorio import operational_safety
    from jev_factorio.provider_decision_wal import ProviderDecisionWAL, WALIntegrityError
    from test_blocked_recovery_archive import _full_memory

    monkeypatch.setattr(operational_safety, "storage_ready", lambda *a, **kw: True)
    checkpoint = tmp_path / "archived-controller.json"
    memory = _full_memory()
    initial = archive.archive_full_tail(checkpoint, memory)
    memory.save(checkpoint)
    initial.close()
    safety = operational_safety.safety_dir(checkpoint)
    safety.mkdir(exist_ok=True)
    wal_path = safety / "provider-decisions.json"
    ProviderDecisionWAL.initialize(wal_path)
    if malformed_wal:
        wal_path.write_bytes(b'{"schema": 1')
    checkpoint_before = checkpoint.read_bytes()
    wal_before = wal_path.read_bytes()
    opened, closed = [], []
    original_build = archive.build_index

    def track_index(*args, **kwargs):
        index = original_build(*args, **kwargs)
        close = index.close
        opened.append(index)

        def tracked_close():
            closed.append(index)
            close()

        index.close = tracked_close
        return index

    monkeypatch.setattr(archive, "build_index", track_index)
    if malformed_wal:
        with pytest.raises(WALIntegrityError):
            main._preflight_sync_async_rollback(checkpoint, "rocket_launch")
    else:
        assert main._preflight_sync_async_rollback(
            checkpoint, "rocket_launch") == checkpoint_before
    assert len(opened) == 1
    assert closed == opened
    assert checkpoint.read_bytes() == checkpoint_before
    assert wal_path.read_bytes() == wal_before
