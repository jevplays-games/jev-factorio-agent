"""Offline public-step controls for the opt-in durable async controller."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import threading
import json
from pathlib import Path

import pytest
import httpx
import jev_factorio.checkpoint_io as checkpoint_io
import jev_factorio.controller as controller_module

from jev_factorio.backends.mock import MockBackend
from jev_factorio.controller import AsyncControllerBusy, HierarchicalLoop
from jev_factorio.jev_client import (
    AsyncJevClient, AsyncMockJevClient, MockJevClient, make_result,
    request_payload_sha256,
)


SOURCE = {"commit": "a" * 40, "source_sha256": "b" * 64}


class CountingAsyncMock(AsyncMockJevClient):
    def __init__(self):
        self.calls = 0

    async def evaluate(self, *args, **kwargs):
        self.calls += 1
        return await super().evaluate(*args, **kwargs)


def _controller(tmp_path, *, backend=None, client=None, resume=False):
    backend = backend or MockBackend()
    client = client or CountingAsyncMock()
    loop = HierarchicalLoop(
        backend, jev=client, policy="jev", target="bootstrap_mining",
        checkpoint=str(tmp_path / "campaign.json"), resume_controller=resume,
        tick_seconds=0, async_decisions=True,
    )
    # The async archive intentionally refuses unattributed source revisions.
    loop.provenance["code_revision"] = SOURCE
    return loop, backend, client


def _sync_controller(tmp_path, *, backend):
    loop = HierarchicalLoop(
        backend, policy="deterministic", target="bootstrap_mining",
        checkpoint=str(tmp_path / "sync-campaign.json"), tick_seconds=0,
    )
    return loop


class SemanticMockBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.decision_deadline_tick = 100
        self.completed_batches = 0

    def observe(self):
        snapshot = super().observe()
        snapshot.factory["decision_deadline_tick"] = self.decision_deadline_tick
        snapshot.factory["completed_batches"] = self.completed_batches
        return snapshot


class IdentifiedMockFLEBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.actor_unit = 41
        self.surface_index = 2
        self.force_index = 3
        self._native_attachment = {
            "qualified": True, "session_id": self.session_id,
            "actor_unit": self.actor_unit,
        }

    def observe(self):
        snapshot = super().observe()
        snapshot.world_kind = "fle"
        snapshot.factory["acceptance_runtime"] = {
            "session_id": snapshot.session_id, "actor_unit": self.actor_unit,
            "surface_index": self.surface_index, "force_index": self.force_index,
        }
        return snapshot


class OfflineFLEAsyncMock(CountingAsyncMock):
    # Local deterministic answers exercise the identified-FLE identity gate;
    # no provider transport or native backend is present in this fixture.
    is_mock = False


def test_public_async_step_consumes_one_exact_response_then_verifies_one_action(tmp_path):
    loop, backend, client = _controller(tmp_path)

    result = asyncio.run(loop.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 1
    assert loop.memory.async_decision is None
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.active_plan is not None
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "verified"
    assert any(row.get("kind") == "async_decision_settled" for row in loop.memory.history)
    assert all(row.get("kind") != "async_decision_settled"
               for row in loop._model_history())
    archive_dir = tmp_path / "campaign.json.safety" / "decision-archives"
    archived = [json.loads(path.read_text()) for path in archive_dir.glob("*.json")]
    assert len(archived) == 1
    assert loop._async_lease_from_record(archived[0]).inspect().state == "consumed"
    assert loop.jev.state["decision_outcome"]["state"] == "consumed"


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_unselected_checkpoint_roundtrips_and_starts_new_async_archive(tmp_path, version):
    from jev_factorio.memory import CampaignMemory

    backend = MockBackend()
    client = CountingAsyncMock()
    checkpoint = tmp_path / "campaign.json"
    legacy = CampaignMemory(backend.session_id, "bootstrap_mining", version=version)
    legacy_data = checkpoint_io.checkpoint_data(legacy)
    if version == 1:
        legacy_data.pop("attempt")
        legacy_data.pop("attempt_outcomes")
    checkpoint.write_text(json.dumps(legacy_data), encoding="utf-8")
    from jev_factorio.provider_decision_wal import ProviderDecisionWAL
    safety = checkpoint.with_name(checkpoint.name + ".safety")
    safety.mkdir(mode=0o700)
    ProviderDecisionWAL.initialize(
        safety / "provider-decisions.json")

    loaded = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert loaded.version == 2
    assert loaded.async_decision is None
    assert not any(row.get("kind") == "async_plan_lineage" for row in loaded.history)
    loop, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(loop.step_async())
    assert loop.memory.async_decision is None

    assert result["verified"] is True
    assert client.calls == 1
    assert len(loop._async_archive.records()) == 1
    assert len(loop._async_plan_lineage_entries()) == 1


def test_persistent_async_attempt_and_exact_archive_are_checkpointed_before_send(
        tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_FACTORIO_PROVENANCE", json.dumps({
        "run_id": "async-run", "segment_id": "async-segment",
        "execution_id": "async-execution", "code_revision": SOURCE,
    }))

    class PausedLiveShapedClient(CountingAsyncMock):
        is_mock = False

        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def evaluate(self, state, questions, *, identity, deadline=None,
                           decision_lease=None):
            self.calls += 1
            payload = self.prepare_decision_payload(state, questions)
            decision_lease.mark_may_have_been_sent()
            self.started.set()
            await self.release.wait()
            answers = MockJevClient().evaluate(payload["state"], payload["questions"])
            result = make_result(
                identity, answers, {"total_tokens": 1}, self.model,
                "offline-resolved", request_payload_sha256(payload),
            )
            decision_lease.save_response(result)
            return result

    backend = MockBackend()
    client = PausedLiveShapedClient()
    checkpoint = tmp_path / "campaign.json"
    loop = HierarchicalLoop(
        backend, jev=client, policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), tick_seconds=0, async_decisions=True,
        persist_recoverable_blocks=True, initialize_persistent_campaign=True,
    )

    async def run():
        task = asyncio.create_task(loop.step_async())
        await asyncio.wait_for(client.started.wait(), 2)
        captured = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert captured["async_decision"]["disposition"] == "pending"
        assert captured["blocked_recovery"]["attempts"][-1]["outcome"] == "pending"
        record = loop._async_archive.load(captured["async_decision"])
        lease = loop._async_lease_from_record(record)
        assert lease.inspect().state == "may_have_been_sent"
        assert record["provider"]["payload_json"]
        assert record["provider"]["wire_body_sha256"]
        client.release.set()
        result = await task
        return result, captured, record

    result, captured, record = asyncio.run(run())

    assert client.calls == 1
    assert result["verified"] is True
    assert captured["async_decision"]["request_id"] == record["request_id"]
    assert loop.memory.async_decision is None
    assert loop._async_lease_from_record(record).inspect().state == "consumed"


def test_async_step_requires_explicit_opt_in_and_sync_step_rejects_async_mode(tmp_path):
    loop, _, _ = _controller(tmp_path)
    with pytest.raises(RuntimeError, match="await step_async"):
        loop.step()


@pytest.mark.parametrize("failure_phase", [
    "event_construction", "bridge_construction", "dispatch_gate_construction",
    "executor_submission",
])
def test_pre_worker_async_failure_releases_claim_and_allows_retry(
        tmp_path, monkeypatch, failure_phase):
    loop, backend, client = _controller(tmp_path)

    async def run():
        event_loop = asyncio.get_running_loop()
        before_workers = controller_module._ASYNC_WORKER_SLOTS._value
        before_async_claims = dict(controller_module._ASYNC_RESOURCE_SLOTS)
        before_sync_claims = dict(controller_module._SYNC_RESOURCE_USERS)
        before_unknown_sync = controller_module._UNKNOWN_SYNC_RESOURCE_USERS
        new_claim_lock_count = sum(
            key not in before_async_claims
            for key in loop._async_resource_lock_keys())

        with monkeypatch.context() as patch:
            if failure_phase == "event_construction":
                def fail_event():
                    raise RuntimeError("async cancellation event construction rejected")

                patch.setattr(controller_module.threading, "Event", fail_event)
            elif failure_phase == "bridge_construction":
                def fail_bridge(*_args, **_kwargs):
                    raise RuntimeError("async provider bridge setup rejected")

                patch.setattr(controller_module, "_AsyncProviderBridge", fail_bridge)
            elif failure_phase == "dispatch_gate_construction":
                original_lock = controller_module.threading.Lock
                lock_calls = 0

                def fail_dispatch_gate():
                    nonlocal lock_calls
                    lock_calls += 1
                    if lock_calls > new_claim_lock_count:
                        raise RuntimeError("async dispatch gate construction rejected")
                    return original_lock()

                patch.setattr(controller_module.threading, "Lock", fail_dispatch_gate)
            else:
                def reject_submission(*_args, **_kwargs):
                    raise RuntimeError("async worker submission rejected")

                patch.setattr(event_loop, "run_in_executor", reject_submission)

            expected_message = {
                "event_construction": "event construction rejected",
                "bridge_construction": "bridge setup rejected",
                "dispatch_gate_construction": "dispatch gate construction rejected",
                "executor_submission": "worker submission rejected",
            }[failure_phase]
            with pytest.raises(RuntimeError, match=expected_message):
                await loop.step_async()

        assert loop._async_execution is None
        assert controller_module._ASYNC_WORKER_SLOTS._value == before_workers
        assert controller_module._ASYNC_RESOURCE_SLOTS == before_async_claims
        assert controller_module._SYNC_RESOURCE_USERS == before_sync_claims
        assert controller_module._UNKNOWN_SYNC_RESOURCE_USERS == before_unknown_sync
        assert backend.tick == 0
        assert client.calls == 0
        # Admission/setup failure precedes the worker's initial checkpoint
        # load, so it must not create campaign memory or provider/action state.
        assert loop.memory is None

        # Retry on the same event loop verifies that the failed pre-worker
        # attempt left neither a phantom resource claim nor a depleted slot.
        result = await loop.step_async()
        assert result["verified"] is True
        assert loop.memory.pending is None
        assert loop.memory.attempt is None
        assert loop.memory.async_decision is None
        assert loop._async_execution is None
        assert controller_module._ASYNC_WORKER_SLOTS._value == before_workers
        assert controller_module._ASYNC_RESOURCE_SLOTS == before_async_claims
        return result

    result = asyncio.run(run())

    assert result["verified"] is True
    assert backend.tick == 1
    assert client.calls == 1


def test_same_stable_actor_rejects_overlapping_async_steps(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class WaitingClient(CountingAsyncMock):
        async def evaluate(self, *args, **kwargs):
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return await super().evaluate(*args, **kwargs)

    async def run():
        loop, _, _ = _controller(tmp_path, client=WaitingClient())
        first = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(AsyncControllerBusy, match="already has a controller step"):
            await loop.step_async()
        release.set()
        await first

    asyncio.run(run())


def test_same_session_serializes_distinct_actor_facades_sharing_backend(tmp_path):
    class GatedSharedBackend(MockBackend):
        def __init__(self):
            super().__init__()
            self.guard = threading.Lock()
            self.entries = 0
            self.first_entered = threading.Event()
            self.release_actions = threading.Event()
            self.active_actions = 0
            self.max_active_actions = 0

        def act(self, action):
            with self.guard:
                self.entries += 1
                self.active_actions += 1
                self.max_active_actions = max(
                    self.max_active_actions, self.active_actions)
                self.first_entered.set()
            if not self.release_actions.wait(5):
                raise TimeoutError("test did not release the shared backend action")
            try:
                return super().act(action)
            finally:
                with self.guard:
                    self.active_actions -= 1

    class ActorView:
        def __init__(self, shared, actor_unit):
            self._shared = shared
            self.session_id = shared.session_id
            self._native_attachment = {
                "qualified": True, "session_id": shared.session_id,
                "actor_unit": actor_unit,
            }
            self.actor_unit = actor_unit
            self.surface_index = 2
            self.force_index = 3

        def __getattr__(self, name):
            return getattr(self._shared, name)

        def observe(self):
            snapshot = self._shared.observe()
            snapshot.world_kind = "fle"
            snapshot.factory["acceptance_runtime"] = {
                "session_id": snapshot.session_id,
                "actor_unit": self.actor_unit,
                "surface_index": self.surface_index,
                "force_index": self.force_index,
            }
            return snapshot

        def act(self, action):
            return self._shared.act(action)

    shared = GatedSharedBackend()
    first_dir, second_dir = tmp_path / "actor-41", tmp_path / "actor-42"
    first_dir.mkdir()
    second_dir.mkdir()
    first, _, first_client = _controller(
        first_dir, backend=ActorView(shared, 41), client=OfflineFLEAsyncMock())
    second, _, second_client = _controller(
        second_dir, backend=ActorView(shared, 42), client=OfflineFLEAsyncMock())

    async def run():
        first_task = asyncio.create_task(first.step_async())
        assert await asyncio.to_thread(shared.first_entered.wait, 5)
        second_task = asyncio.create_task(second.step_async())
        await asyncio.sleep(0.03)
        second_done_before_release = second_task.done()
        second_error = (second_task.exception() if second_done_before_release else None)
        active_before_release = shared.active_actions
        maximum_before_release = shared.max_active_actions
        shared.release_actions.set()
        first_result = await first_task
        if not second_done_before_release:
            try:
                await second_task
                second_error = None
            except BaseException as error:
                second_error = error
        sequential_result = None
        if isinstance(second_error, AsyncControllerBusy):
            sequential_result = await second.step_async()
        return (first_result, second_error, sequential_result,
                active_before_release, maximum_before_release)

    first_result, second_error, sequential_result, active, maximum = asyncio.run(run())

    assert isinstance(second_error, AsyncControllerBusy)
    assert active == 1
    assert maximum == 1
    assert first_result["verified"] is True
    assert first_client.calls == 1
    assert shared.tick == 2
    assert second_client.calls == 1
    assert sequential_result is not None and sequential_result["verified"] is True


def test_distinct_sessions_sharing_one_transport_are_serialized(tmp_path):
    class GatedSharedBackend(MockBackend):
        def __init__(self):
            super().__init__()
            self.guard = threading.Lock()
            self.first_entered = threading.Event()
            self.release_action = threading.Event()
            self.active = 0
            self.maximum = 0

        def act(self, action):
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.first_entered.set()
            if not self.release_action.wait(5):
                raise TimeoutError("test did not release the shared transport")
            try:
                return super().act(action)
            finally:
                with self.guard:
                    self.active -= 1

    class SessionView:
        def __init__(self, shared, session_id):
            self._shared = shared
            self.session_id = session_id
            self.actor_unit = 41
            self.surface_index = 2
            self.force_index = 3
            self._native_attachment = {
                "qualified": True, "session_id": session_id, "actor_unit": 41,
            }

        def __getattr__(self, name):
            return getattr(self._shared, name)

        def observe(self):
            snapshot = self._shared.observe()
            snapshot.session_id = self.session_id
            snapshot.world_kind = "fle"
            snapshot.factory["acceptance_runtime"] = {
                "session_id": self.session_id, "actor_unit": 41,
                "surface_index": 2, "force_index": 3,
            }
            return snapshot

        def act(self, action):
            return self._shared.act(action)

    shared = GatedSharedBackend()
    first_dir, second_dir = tmp_path / "session-a", tmp_path / "session-b"
    first_dir.mkdir()
    second_dir.mkdir()
    first, _, first_client = _controller(
        first_dir, backend=SessionView(shared, "fle-session-a"),
        client=OfflineFLEAsyncMock())
    second, _, second_client = _controller(
        second_dir, backend=SessionView(shared, "fle-session-b"),
        client=OfflineFLEAsyncMock())

    async def run():
        first_task = asyncio.create_task(first.step_async())
        assert await asyncio.to_thread(shared.first_entered.wait, 5)
        second_task = asyncio.create_task(second.step_async())
        await asyncio.sleep(0.03)
        second_blocked = second_task.done()
        error = second_task.exception() if second_blocked else None
        active, maximum = shared.active, shared.maximum
        shared.release_action.set()
        first_result = await first_task
        if not second_blocked:
            try:
                await second_task
                error = None
            except BaseException as caught:
                error = caught
        retry = None
        if isinstance(error, AsyncControllerBusy):
            retry = await second.step_async()
        return first_result, error, retry, active, maximum

    first_result, error, retry, active, maximum = asyncio.run(run())
    assert isinstance(error, AsyncControllerBusy)
    assert active == 1 and maximum == 1
    assert first_result["verified"] is True
    assert first_client.calls == 1 and second_client.calls == 1
    assert retry is not None and retry["verified"] is True
    assert shared.tick == 2


def test_distinct_sessions_and_transports_remain_parallel(tmp_path):
    class GatedBackend(MockBackend):
        def __init__(self, barrier):
            super().__init__()
            self.barrier = barrier

        def act(self, action):
            self.barrier.entered()
            if not self.barrier.release.wait(5):
                raise TimeoutError("test did not release independent transports")
            return super().act(action)

    class Barrier:
        def __init__(self):
            self.guard = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.both_entered = threading.Event()
            self.release = threading.Event()

        def entered(self):
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 2:
                    self.both_entered.set()

    barrier = Barrier()
    first_dir, second_dir = tmp_path / "transport-a", tmp_path / "transport-b"
    first_dir.mkdir()
    second_dir.mkdir()
    first, first_backend, first_client = _controller(
        first_dir, backend=GatedBackend(barrier))
    second, second_backend, second_client = _controller(
        second_dir, backend=GatedBackend(barrier))

    async def run():
        first_task = asyncio.create_task(first.step_async())
        second_task = asyncio.create_task(second.step_async())
        both = await asyncio.to_thread(barrier.both_entered.wait, 5)
        barrier.release.set()
        results = await asyncio.gather(first_task, second_task)
        return both, results

    both, results = asyncio.run(run())
    assert first_backend is not second_backend
    assert both and barrier.maximum == 2
    assert all(result["verified"] for result in results)
    assert first_client.calls == second_client.calls == 1


def test_async_worker_rejects_backend_without_supported_transport_identity(tmp_path):
    class UnknownTransportBackend:
        def __init__(self):
            self.inner = MockBackend()
            self.session_id = self.inner.session_id
            self.actor_unit = 41
            self.actions = 0

        def observe(self):
            return self.inner.observe()

        def act(self, action):
            self.actions += 1
            return self.inner.act(action)

    backend = UnknownTransportBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    with pytest.raises(AsyncControllerBusy, match="cannot establish a supported backend transport identity"):
        asyncio.run(loop.step_async())
    assert backend.actions == 0
    assert client.calls == 0


class _InjectedCrash(RuntimeError):
    pass


def _interrupt_before_plan_checkpoint(loop):
    def crash():
        raise _InjectedCrash("crash after durable provider response")

    loop._trace_decision = crash


def test_public_async_crash_after_response_resumes_without_a_second_provider_call(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)

    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    assert client.calls == 1
    assert first.memory.active_plan is None
    assert first.memory.async_decision["disposition"] == "pending"
    assert first._async_lease_from_record(
        first._async_archive.load(first.memory.async_decision)).inspect().state == "response_received"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 1
    assert resumed.memory.async_decision is None
    assert first._async_lease_from_record(
        first._async_archive.load(first.memory.async_decision)).inspect().state == "consumed"


def test_crash_after_checkpoint_pointer_before_wal_reuses_exact_archived_request(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    prepare = first._async_prepare_selection

    def crash_after_checkpoint(**kwargs):
        prepare(**kwargs)
        raise _InjectedCrash("crash after checkpoint pointer, before WAL reservation")

    first._async_prepare_selection = crash_after_checkpoint
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    pointer = first.memory.async_decision
    record = first._async_archive.load(pointer)
    assert client.calls == 0
    assert first._async_lease_from_record(record).inspect_optional() is None

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["action"] == "observe"
    assert client.calls == 1
    assert resumed.memory.async_decision is None
    assert first._async_lease_from_record(record).inspect().state == "consumed"


def test_archive_before_pointer_crash_is_unpaid_orphan_and_resume_is_safe(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    first._observe()
    first._save()
    store = first._async_archive.store

    def store_then_crash(record):
        store(record)
        raise _InjectedCrash("crash after archive fsync, before checkpoint pointer")

    first._async_archive.store = store_then_crash
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    archives = first._async_archive.records()
    assert len(archives) == 1
    assert first.memory.async_decision is None
    assert first._async_lease_from_record(archives[0]).inspect_optional() is None

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert len(resumed._async_archive.records()) == 2
    assert result["action"] in {"walk_to_coal", "observe"}


def test_rolled_back_reserved_orphan_is_abandoned_only_before_transport(tmp_path):
    class CrashOnceBeforeTransport(CountingAsyncMock):
        async def evaluate(self, state, questions, *, identity, deadline=None,
                           decision_lease=None):
            self.calls += 1
            if self.calls == 1:
                raise _InjectedCrash("crash after WAL reserve, before MAY_HAVE_BEEN_SENT")
            return await AsyncMockJevClient.evaluate(
                self, state, questions, identity=identity, deadline=deadline,
                decision_lease=decision_lease)

    backend = MockBackend()
    client = CrashOnceBeforeTransport()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    record = first._async_archive.load(first.memory.async_decision)
    lease = first._async_lease_from_record(record)
    assert lease.inspect().state == "reserved"

    # A retained provider-health reservation survives, but the checkpoint
    # pointer is rolled back. Recovery may release it only because the WAL
    # still proves that transport was never authorized.
    first.memory.async_decision = None
    first._save()
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert result["verified"] is True
    assert client.calls == 2
    assert lease.inspect().state == "failed"
    assert lease.inspect().phase == "not_sent"
    assert backend.tick == 1


@pytest.mark.parametrize("crash_point", ["before_marker", "after_marker_fsync"])
def test_checkpoint_settlement_survives_marker_write_crash_and_repairs_marker(
        tmp_path, crash_point):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    store_settlement = first._async_archive.store_settlement

    def crash_before_marker(record, entry):
        if crash_point == "after_marker_fsync":
            store_settlement(record, entry)
            raise _InjectedCrash("crash after marker file and directory fsync")
        raise _InjectedCrash("crash after settlement checkpoint, before archive marker")

    first._async_archive.store_settlement = crash_before_marker
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    record = first._async_archive.records()[0]
    assert first.memory.async_decision is None
    assert any(row.get("kind") == "async_decision_settled"
               for row in first.memory.history)
    marker = first._async_archive.load_settlement(record)
    assert marker == (None if crash_point == "before_marker" else
                      next(row for row in first.memory.history
                           if row.get("kind") == "async_decision_settled")
                      ["entries"][0])
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["verified"] is True
    assert client.calls == 1
    assert resumed._async_archive.load_settlement(record) is not None
    assert backend.tick == 1


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(),
                    reason="descriptor-path fsync fault injection requires procfs")
def test_settlement_marker_file_fsync_failure_recovers_from_checkpoint_lineage(
        tmp_path, monkeypatch):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    real_fsync = os.fsync
    archive_directory = tmp_path / "campaign.json.safety" / "decision-archives"
    failed = False
    archive_syncs = 0

    def fail_marker_file_once(descriptor):
        nonlocal failed, archive_syncs
        try:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            target = None
        if (target is not None and target.parent == archive_directory
                and target.name.startswith(".safety-")):
            archive_syncs += 1
            if not failed and archive_syncs == 2:
                failed = True
                raise OSError("injected settlement marker file fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_marker_file_once)
    with pytest.raises(OSError, match="settlement marker file fsync failure"):
        asyncio.run(first.step_async())
    assert failed
    record = first._async_archive.records()[0]
    assert first.memory.async_decision is None
    assert first._async_archive.load_settlement(record) is None
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["verified"] is True
    assert client.calls == 1
    assert resumed._async_archive.load_settlement(record) is not None
    assert backend.tick == 1


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(),
                    reason="descriptor-path fsync fault injection requires procfs")
def test_pointer_clear_checkpoint_fsync_failure_keeps_selected_response_recoverable(
        tmp_path, monkeypatch):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    real_save = checkpoint_io.save_checkpoint
    real_fsync = os.fsync
    checkpoint_path = tmp_path / "campaign.json"
    failed = False

    def save_with_one_final_fsync_failure(memory, path):
        nonlocal failed
        has_settlement = any(row.get("kind") == "async_decision_settled"
                             for row in memory.history)
        if failed or memory.async_decision is not None or not has_settlement:
            return real_save(memory, path)

        def fail_checkpoint_temp_once(descriptor):
            nonlocal failed
            try:
                target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                target = None
            if (not failed and target is not None and target.parent == checkpoint_path.parent
                    and target.name.startswith(checkpoint_path.name + ".")):
                failed = True
                raise OSError("injected pointer-clear checkpoint file fsync failure")
            return real_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fail_checkpoint_temp_once)
        try:
            return real_save(memory, path)
        finally:
            monkeypatch.setattr(os, "fsync", real_fsync)

    monkeypatch.setattr(checkpoint_io, "save_checkpoint", save_with_one_final_fsync_failure)
    with pytest.raises(OSError, match="pointer-clear checkpoint file fsync failure"):
        asyncio.run(first.step_async())
    assert failed
    durable = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert durable["async_decision"]["disposition"] == "selected"
    record = first._async_archive.load(durable["async_decision"])
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["verified"] is True
    assert client.calls == 1
    assert resumed.memory.active_plan is not None
    assert resumed.memory.async_decision is None
    assert resumed._async_archive.load_settlement(record) is not None
    assert backend.tick == 1


def test_settled_marker_without_checkpoint_disposition_fails_closed_after_rollback(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    result = asyncio.run(first.step_async())
    assert result["verified"] is True
    record = first._async_archive.records()[0]
    assert first._async_archive.load_settlement(record) is not None
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert first.memory.active_plan is not None

    # Model rollback to a checkpoint with neither the selected plan nor the
    # retained disposition, while the consumed WAL and marker remain durable.
    first.memory.active_plan = None
    first.memory.async_decision = None
    first.memory.history = [row for row in first.memory.history
                            if row.get("kind") not in {
                                "async_decision_settled", "async_plan_lineage"}]
    first._save()
    with pytest.raises(ValueError, match="marker has no retained checkpoint disposition"):
        resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
        asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert backend.tick == 1


def test_selected_settlement_without_archive_specific_lineage_fails_closed(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    result = asyncio.run(first.step_async())
    assert result["verified"] is True
    record = first._async_archive.records()[0]
    assert first._async_archive.load_settlement(record)["disposition"] == "selected"

    # Preserve the selected-decision marker but roll the checkpoint back to
    # one that predates the archive-specific selection/attempt disposition.
    first.memory.active_plan = None
    first.memory.step_index = 0
    first.memory.attempt_outcomes = []
    first.memory.history = [row for row in first.memory.history
                            if row.get("kind") != "async_plan_lineage"]
    first._save()
    with pytest.raises(ValueError, match="lacks archive-specific plan lineage"):
        resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
        asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert backend.tick == 1


def test_selected_plan_rechecks_pre_dispatch_failure_as_exact_abandoned_lineage(tmp_path):
    from jev_factorio.skills import Plan, Step

    backend = MockBackend()

    class RemoveCoalAfterSelection(CountingAsyncMock):
        async def evaluate(self, *args, **kwargs):
            result = await super().evaluate(*args, **kwargs)
            backend.coal_left = 0
            return result

    client = RemoveCoalAfterSelection()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    plan = Plan("selected:coal", "stockpile_fuel", "Walk to observed coal",
                (Step("walk_to_coal", "near", "coal"),))
    loop._work_candidates = lambda snapshot: ([plan], "")

    result = asyncio.run(loop.step_async())

    assert result["verified"] is False
    assert backend.tick == 0
    assert client.calls == 1
    assert loop.memory.active_plan is None
    assert loop.memory.pending is None
    lineage = loop._async_plan_lineage_entries()
    assert len(lineage) == 1
    assert lineage[0]["state"] == "abandoned"
    assert lineage[0]["terminal"]["kind"] == "plan_failed"
    assert lineage[0]["terminal"]["attempt"] is None
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    resumed._observe()
    resumed._async_reconcile_terminal_pointer()
    assert client.calls == 1


def test_ambiguous_selected_dispatch_keeps_active_lineage_and_original_attempt(tmp_path):
    from jev_factorio.skills import Plan, Step

    class LostReplyBackend(MockBackend):
        def act(self, action):
            super().act(action)
            raise ConnectionError("simulated lost dispatch reply after effect")

    backend = LostReplyBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    plan = Plan("selected:coal", "stockpile_fuel", "Walk to observed coal",
                (Step("walk_to_coal", "near", "coal"),))
    loop._work_candidates = lambda snapshot: ([plan], "")

    result = asyncio.run(loop.step_async())
    assert result["verified"] is False
    assert "Ambiguous dispatch" in result["outcome"]
    assert backend.tick == 1
    assert client.calls == 1
    assert loop.memory.pending["dispatch"] == "ambiguous"
    original_attempt = dict(loop.memory.attempt)
    lineage = loop._async_plan_lineage_entries()
    assert len(lineage) == 1
    assert lineage[0]["state"] == "active"
    assert lineage[0]["terminal"] is None
    assert lineage[0]["verified_steps"] == []

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    recovered = asyncio.run(resumed.step_async())
    assert recovered["verified"] is True
    assert recovered["action"] == "verify"
    assert recovered["outcome"] == "Observed expected postcondition"
    assert resumed.memory.pending is None
    assert resumed.memory.attempt is None
    assert resumed.memory.attempt_outcomes[-1]["id"] == original_attempt["id"]
    assert client.calls == 1
    assert backend.tick == 1
    completed = resumed._async_plan_lineage_entries()
    assert completed[0]["state"] == "completed"
    assert completed[0]["verified_steps"][0]["attempt"]["id"] == original_attempt["id"]


def test_retained_settlement_and_active_plan_allow_normal_resume(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    initial = asyncio.run(first.step_async())
    assert initial["verified"] is True
    record = first._async_archive.records()[0]
    assert first._async_archive.load_settlement(record)["disposition"] == "selected"
    assert first.memory.active_plan is not None

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    next_step = asyncio.run(resumed.step_async())
    assert next_step["verified"] is True
    assert client.calls == 1
    assert resumed.memory.active_plan is None

    # Once the selected plan is complete, later checkpoints may legitimately
    # omit it; the retained verified step history must reconcile the old
    # settlement before a distinct next-goal request can be made.
    completed_resume, _, _ = _controller(
        tmp_path, backend=backend, client=client, resume=True)
    completion = asyncio.run(completed_resume.step_async())
    assert completion["action"] == "walk_to_iron"
    assert completion["verified"] is True
    assert client.calls == 2
    assert completed_resume.memory.active_plan is not None
    ledgers = [row for row in completed_resume.memory.history
               if row.get("kind") == "async_decision_settled"]
    assert len(ledgers) == 1
    assert len(ledgers[0]["entries"]) == 2

    reloaded, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    following = asyncio.run(reloaded.step_async())
    assert following["verified"] is True
    assert client.calls == 2
    assert len(reloaded.memory.attempt_outcomes) >= 3


def test_repeated_same_plan_archives_remain_bound_to_their_verified_attempts(tmp_path):
    from jev_factorio.skills import Plan, Step

    loop, backend, client = _controller(tmp_path)
    recurring = Plan(
        "recurring:walk", "bootstrap_mining", "Walk to currently observed coal",
        (Step("walk_to_coal", "near", "coal"),),
    )
    loop._work_candidates = lambda snapshot: ([recurring], "")

    async def repeat():
        first = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        second = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        third = await loop.step_async()
        return first, second, third

    first, second, third = asyncio.run(repeat())

    assert [first["verified"], second["verified"], third["verified"]] == [True, True, True]
    assert client.calls == 3
    assert backend.tick >= 3
    assert len(loop.memory.attempt_outcomes) == 3
    assert len(loop._async_archive.records()) == 3


def test_completed_lineage_does_not_claim_identical_later_active_selection(tmp_path):
    from jev_factorio.skills import Plan, Step

    loop, backend, client = _controller(tmp_path)
    repeated = Plan(
        "recurring:walk", "bootstrap_mining", "Two-step repeated definition",
        (Step("walk_to_coal", "near", "coal"),
         Step("walk_to_iron", "near", "iron-ore")),
    )
    loop._work_candidates = lambda snapshot: ([repeated], "")

    async def run_twice():
        results = [await loop.step_async(), await loop.step_async()]
        backend.at_resource = None
        backend.tick += 1
        results.extend([await loop.step_async(), await loop.step_async()])
        return results

    results = asyncio.run(run_twice())
    assert all(result["verified"] for result in results)
    assert client.calls == 2
    lineages = loop._async_plan_lineage_entries()
    assert len(lineages) == 2
    assert all(entry["state"] == "completed" for entry in lineages)


def test_active_selection_lineage_survives_attempt_history_pruning_on_reload(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    selected_step = asyncio.run(first.step_async())
    assert selected_step["verified"] is True
    assert first.memory.active_plan is not None
    assert first.memory.step_index == 1
    assert len(first.memory.attempt_outcomes) == 1

    # The archive-specific bounded ledger retains the full exact attempt
    # witness when the ordinary rolling attempt window has pruned it.
    first.memory.attempt_outcomes = []
    first._save()
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    next_step = asyncio.run(resumed.step_async())
    assert next_step["verified"] is True
    assert client.calls == 1
    assert resumed.memory.attempt_outcomes[-1]["outcome"] == "verified"


def test_completed_old_lineage_survives_same_id_with_new_plan_content(tmp_path):
    from jev_factorio.skills import Plan, Step

    loop, backend, client = _controller(tmp_path)
    original = Plan(
        "recurring:walk", "bootstrap_mining", "Original one-step definition",
        (Step("walk_to_coal", "near", "coal"),),
    )
    replacement = Plan(
        "recurring:walk", "bootstrap_mining", "Later two-step definition",
        (Step("walk_to_coal", "near", "coal"),
         Step("walk_to_iron", "near", "iron-ore")),
    )
    loop._work_candidates = lambda snapshot: ([original], "")

    async def run():
        first = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        loop._work_candidates = lambda snapshot: ([replacement], "")
        second = await loop.step_async()
        assert loop.memory.active_plan == replacement.to_dict()
        assert loop.memory.step_index == 1
        third = await loop.step_async()
        return first, second, third

    first, second, third = asyncio.run(run())
    assert [first["verified"], second["verified"], third["verified"]] == [True, True, True]
    assert client.calls == 2
    assert len(loop._async_archive.records()) == 2


def test_reload_rejects_verified_attempt_copied_between_archived_selections(tmp_path):
    from jev_factorio.skills import Plan, Step

    backend = MockBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    plan = Plan(
        "recurring:walk", "bootstrap_mining", "Walk to currently observed coal",
        (Step("walk_to_coal", "near", "coal"),),
    )
    loop._work_candidates = lambda snapshot: ([plan], "")

    async def execute_twice():
        first = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        second = await loop.step_async()
        return first, second

    first, second = asyncio.run(execute_twice())
    assert first["verified"] and second["verified"]
    lineage = next(row for row in loop.memory.history
                   if row.get("kind") == "async_plan_lineage")
    entries = lineage["entries"]
    assert len(entries) == 2
    entries[1]["verified_steps"] = [entries[0]["verified_steps"][0]]
    encoded = json.dumps(entries, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    lineage["entries_sha256"] = hashlib.sha256(encoded).hexdigest()
    loop._save()

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    with pytest.raises(ValueError, match="attempt witness is not unique and verified"):
        asyncio.run(resumed.step_async())
    assert client.calls == 2


@pytest.mark.parametrize("crash_boundary", ["before_consume", "after_consume", "after_ack"])
def test_selected_plan_checkpoint_recovers_each_wal_health_finalize_boundary(
        tmp_path, crash_boundary):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)

    if crash_boundary == "before_consume":
        def crash_before_consume(identity, request, result_sha256):
            raise _InjectedCrash("crash after selected-plan checkpoint")
        first._async_wal.consume_response = crash_before_consume
    elif crash_boundary == "after_consume":
        def crash_before_health_ack(lease):
            raise _InjectedCrash("crash after WAL consume")
        first.jev.acknowledge_decision_consumed = crash_before_health_ack
    else:
        acknowledge = first.jev.acknowledge_decision_consumed

        def crash_after_health_ack(lease):
            acknowledge(lease)
            raise _InjectedCrash("crash after health acknowledgment")
        first.jev.acknowledge_decision_consumed = crash_after_health_ack

    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    pointer = first.memory.async_decision
    assert pointer["disposition"] == "selected"
    assert first.memory.active_plan["id"] == pointer["selected_plan_id"]
    saved_record = first._async_archive.load(pointer)
    saved_lease = first._async_lease_from_record(saved_record)
    expected_wal_state = "response_received" if crash_boundary == "before_consume" else "consumed"
    assert saved_lease.inspect().state == expected_wal_state
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 1
    assert resumed.memory.async_decision is None
    assert resumed._async_lease_from_record(saved_record).inspect().state == "consumed"


def test_stale_async_response_is_consumed_without_dispatch_or_reselection(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    backend.inv["coal"] = 1
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "observe"
    assert result["decision"]["diagnostics"]["outcome"] == "stale_async_response"
    assert backend.tick == 0
    assert resumed.memory.active_plan is None
    assert resumed.memory.async_decision is None
    archive = json.loads(next(
        (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
    ).read_text())
    assert resumed._async_lease_from_record(archive).inspect().state == "consumed"


def test_tick_only_progress_reduces_archived_response_then_uses_fresh_admission(tmp_path):
    backend = SemanticMockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    # The game clock advanced while the saved response was pending, but all
    # decision-relevant facts and the source-bound plan remain unchanged.
    backend.tick += 1
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 2
    assert resumed.memory.async_decision is None


@pytest.mark.parametrize("change", [
    "site", "provider_model", "source_revision", "request_budget", "deadline", "progress",
])
def test_saved_async_result_is_stale_after_bound_context_change(tmp_path, change):
    backend = SemanticMockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    if change == "site":
        backend.pos = (1.0, 0.0)
    elif change == "deadline":
        backend.decision_deadline_tick += 1
    elif change == "progress":
        backend.completed_batches += 1
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    if change == "provider_model":
        # Preserve the already-validated provider-health identity, then change
        # the live model before the saved response is admitted.
        client.model = "changed-model"
    elif change == "source_revision":
        resumed.provenance["code_revision"] = {
            "commit": "c" * 40, "source_sha256": "d" * 64,
        }
    elif change == "request_budget":
        resumed.max_request_bytes -= 1
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "observe"
    assert result["decision"]["diagnostics"]["outcome"] == "stale_async_response"
    assert backend.tick == 0
    assert resumed.memory.active_plan is None
    assert resumed.memory.async_decision is None


@pytest.mark.parametrize("change", ["actor", "session"])
def test_saved_async_result_rejects_changed_runtime_identity(tmp_path, change):
    backend = IdentifiedMockFLEBackend()
    client = OfflineFLEAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    if change == "actor":
        backend.actor_unit += 1
        backend._native_attachment["actor_unit"] = backend.actor_unit
    else:
        backend.session_id = "session-changed-after-provider-response"
        backend._native_attachment["session_id"] = backend.session_id

    with pytest.raises(ValueError):
        resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
        asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert backend.tick == 0


def test_actual_transport_body_matches_durable_archived_request_bytes(tmp_path):
    observed = {}

    def handler(request):
        observed["body"] = request.content
        payload = json.loads(request.content)
        answers = MockJevClient().evaluate(payload["state"], payload["questions"])
        return httpx.Response(200, json={
            "answers": answers, "usage": {"total_tokens": 1},
            "model": "offline-resolved",
        })

    client = AsyncJevClient(
        api_key="offline-test", base_url="https://provider.invalid/evaluate",
        model="offline-model", transport=httpx.MockTransport(handler),
    )
    backend = MockBackend()
    loop = HierarchicalLoop(
        backend, jev=client, policy="jev", target="bootstrap_mining",
        checkpoint=str(tmp_path / "campaign.json"), tick_seconds=0,
        async_decisions=True,
    )
    loop.provenance["code_revision"] = SOURCE

    async def run():
        try:
            result = await loop.step_async()
            assert result["verified"] is True, result.get("decision")
        finally:
            await loop.aclose_async_provider()

    asyncio.run(run())
    archive = json.loads(next(
        (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
    ).read_text())
    archived_body = base64.b64decode(archive["provider"]["wire_body_base64"], validate=True)

    assert observed["body"] == archived_body
    assert hashlib.sha256(observed["body"]).hexdigest() == \
        archive["provider"]["wire_body_sha256"]


def test_cancellation_before_action_admission_waits_for_worker_and_keeps_selected_plan(tmp_path):
    observed = threading.Event()
    release = threading.Event()
    backend = MockBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    original_observe = loop._observe

    def gated_observe(phase_name=None):
        snapshot = (original_observe() if phase_name is None
                    else original_observe(phase_name))
        if phase_name == "pre_dispatch_observe":
            observed.set()
            assert release.wait(2)
        return snapshot

    loop._observe = gated_observe

    async def run():
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(observed.wait, 2)
        cancellations = await _cancel_three_times_while_worker_is_gated(task)
        task_still_waiting = not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return cancellations, task_still_waiting

    cancellations, task_still_waiting = asyncio.run(run())

    assert cancellations == [True, True, True]
    assert task_still_waiting is True
    assert client.calls == 1
    assert backend.tick == 0
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.active_plan is not None
    assert loop.memory.async_decision is None


def test_cancellation_after_action_entry_waits_for_receipt_and_verification(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class GatedBackend(MockBackend):
        def act(self, action):
            entered.set()
            assert release.wait(2)
            return super().act(action)

    backend = GatedBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)

    async def run():
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert client.calls == 1
    assert backend.tick == 1
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "verified"


async def _cancel_three_times_while_worker_is_gated(task):
    observations = []
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0.02)
        observations.append(not task.done())
    return observations


@pytest.mark.parametrize("fault", [
    "constructor_first", "constructor", "acquire_exception", "acquire_after_lock",
    "acquire_rejected",
])
def test_partial_async_resource_admission_rolls_back_exact_claims_and_can_retry(
        tmp_path, monkeypatch, fault):
    """A failed second claim must not strand the first session/transport lock."""
    backend = MockBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    real_lock = threading.Lock
    sentinel_async = controller_module._claim_async_resources(
        (("session", "v6-unrelated-async"),))
    sentinel_sync = controller_module._claim_sync_resources(
        (("transport", "v6-unrelated-sync"),))
    before_slots = dict(controller_module._ASYNC_RESOURCE_SLOTS)
    before_sync = dict(controller_module._SYNC_RESOURCE_USERS)
    before_workers = controller_module._ASYNC_WORKER_SLOTS._value
    original_claim = controller_module._claim_async_resources

    class FaultLock:
        def __init__(self, *, fail=False):
            self.lock = real_lock()
            self.fail = fail

        def locked(self):
            return self.lock.locked()

        def acquire(self, blocking=True):
            if self.fail:
                if fault == "acquire_exception":
                    raise RuntimeError("injected second-lock acquisition failure")
                if fault == "acquire_after_lock":
                    self.lock.acquire(blocking)
                    raise RuntimeError("injected post-acquisition exception")
                if fault == "acquire_rejected":
                    return False
            return self.lock.acquire(blocking)

        def release(self):
            return self.lock.release()

    def faulting_claim(keys):
        created = 0

        def factory():
            nonlocal created
            created += 1
            if ((fault == "constructor_first" and created == 1)
                    or (fault == "constructor" and created == 2)):
                raise RuntimeError("injected lock construction failure")
            return FaultLock(fail=created == 2)

        monkeypatch.setattr(controller_module.threading, "Lock", factory)
        try:
            return original_claim(keys)
        finally:
            monkeypatch.setattr(controller_module.threading, "Lock", real_lock)

    monkeypatch.setattr(controller_module, "_claim_async_resources", faulting_claim)

    async def first_admission_fails_then_same_loop_retries():
        with pytest.raises((RuntimeError, AsyncControllerBusy)):
            await loop.step_async()
        assert loop._async_execution is None
        assert controller_module._ASYNC_RESOURCE_SLOTS == before_slots
        assert controller_module._SYNC_RESOURCE_USERS == before_sync
        assert controller_module._ASYNC_WORKER_SLOTS._value == before_workers
        assert client.calls == 0 and backend.tick == 0
        assert not (tmp_path / "campaign.json").exists()

        monkeypatch.setattr(controller_module, "_claim_async_resources", original_claim)
        result = await loop.step_async()
        assert result["verified"] is True
        assert client.calls == 1 and backend.tick == 1
        assert controller_module._ASYNC_RESOURCE_SLOTS == before_slots
        assert controller_module._SYNC_RESOURCE_USERS == before_sync

    try:
        asyncio.run(first_admission_fails_then_same_loop_retries())
    finally:
        controller_module._release_async_resources(sentinel_async)
        controller_module._release_sync_resources(sentinel_sync)


def test_resource_constructor_failure_preserves_existing_slot_and_retries(tmp_path, monkeypatch):
    backend = MockBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    keys = loop._async_resource_lock_keys()
    existing_key = keys[0]
    real_lock = threading.Lock
    with controller_module._ASYNC_RESOURCE_GUARD:
        assert existing_key not in controller_module._ASYNC_RESOURCE_SLOTS
        existing_slot = {"lock": real_lock()}
        controller_module._ASYNC_RESOURCE_SLOTS[existing_key] = existing_slot
    before_slots = dict(controller_module._ASYNC_RESOURCE_SLOTS)
    original_claim = controller_module._claim_async_resources

    def faulting_claim(resource_keys):
        monkeypatch.setattr(
            controller_module.threading, "Lock",
            lambda: (_ for _ in ()).throw(RuntimeError("injected next lock constructor failure")))
        try:
            return original_claim(resource_keys)
        finally:
            monkeypatch.setattr(controller_module.threading, "Lock", real_lock)

    monkeypatch.setattr(controller_module, "_claim_async_resources", faulting_claim)

    async def fail_then_retry_same_loop():
        with pytest.raises(RuntimeError, match="next lock constructor"):
            await loop.step_async()
        assert controller_module._ASYNC_RESOURCE_SLOTS == before_slots
        assert not existing_slot["lock"].locked()
        assert loop._async_execution is None
        assert client.calls == 0 and backend.tick == 0

        monkeypatch.setattr(controller_module, "_claim_async_resources", original_claim)
        result = await loop.step_async()
        assert result["verified"] is True
        assert client.calls == 1 and backend.tick == 1

    try:
        asyncio.run(fail_then_retry_same_loop())
    finally:
        with controller_module._ASYNC_RESOURCE_GUARD:
            if (controller_module._ASYNC_RESOURCE_SLOTS.get(existing_key) is existing_slot
                    and not existing_slot["lock"].locked()):
                del controller_module._ASYNC_RESOURCE_SLOTS[existing_key]


def test_dispatch_boundary_cancellation_is_durable_and_preserves_plan_reservation(
        tmp_path):
    from copy import deepcopy
    from jev_factorio.memory import CampaignMemory
    from jev_factorio.skills import Plan, Step

    observed = threading.Event()
    release = threading.Event()
    backend = MockBackend()
    backend.inv["wood"] = 1
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    plan = Plan("cancel-before-entry", "stockpile_fuel", "Reach the coal patch",
                (Step("walk_to_coal", "near", "coal", costs={"wood": 1}),))
    loop._work_candidates = lambda snapshot: ([plan], "")

    original_trace = loop._diagnostic_trace

    def gated_trace(event):
        original_trace(event)
        if event.get("stage") == "dispatch" and event.get("status") == "started":
            observed.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the pre-dispatch gate")

    loop._diagnostic_trace = gated_trace

    async def cancel_after_prepared_attempt():
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(observed.wait, 3)
        selected_plan = deepcopy(loop.memory.active_plan)
        selected_reservations = deepcopy(loop.memory.reservations)
        assert selected_plan is not None
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return selected_plan, selected_reservations

    selected_plan, selected_reservations = asyncio.run(cancel_after_prepared_attempt())

    saved = CampaignMemory.load(tmp_path / "campaign.json", backend.session_id,
                                "bootstrap_mining")
    assert client.calls == 1 and backend.tick == 0
    assert saved.pending is None and saved.attempt is None
    assert Plan.from_dict(saved.active_plan).to_dict() == Plan.from_dict(selected_plan).to_dict()
    assert saved.reservations == selected_reservations == {plan.id: {"wood": 1}}
    outcome = saved.attempt_outcomes[-1]
    assert outcome["outcome"] == "cancelled_before_dispatch"
    assert outcome["dispatch_phases"]["dispatch"]["status"] == "failed"
    assert outcome["dispatch_phases"]["dispatch"]["error_code"] == "cancelled_before_entry"

    resumed, _, resumed_client = _controller(
        tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    reloaded = CampaignMemory.load(tmp_path / "campaign.json", backend.session_id,
                                   "bootstrap_mining")
    assert result["verified"] is True and result["action"] == "walk_to_coal"
    assert resumed_client.calls == 1
    assert backend.tick == 1
    assert reloaded.attempt_outcomes[-1]["outcome"] == "verified"



def test_repeated_cancellation_keeps_actor_and_worker_owned_until_action_finishes(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class GatedBackend(MockBackend):
        def act(self, action):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the admitted action")
            return super().act(action)

    backend = GatedBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)

    async def run():
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        execution = loop._async_execution
        assert execution is not None
        cancellation_observations = await _cancel_three_times_while_worker_is_gated(task)
        task_still_waiting = not task.done()
        execution_still_owned = loop._async_execution is execution
        worker_still_running = not execution["worker"].done()
        second_controller_rejected = False
        provider_close_rejected = False
        second = None
        if task_still_waiting:
            secondary_dir = tmp_path / "secondary"
            secondary_dir.mkdir()
            second, _, _ = _controller(secondary_dir, backend=backend)
            try:
                await second.step_async()
            except AsyncControllerBusy:
                second_controller_rejected = True
            try:
                await loop.aclose_async_provider()
            except AsyncControllerBusy:
                provider_close_rejected = True
        release.set()
        try:
            await task
            caller_cancelled = False
        except asyncio.CancelledError:
            caller_cancelled = True
        sequential_result = await second.step_async() if second is not None else None
        return (cancellation_observations, task_still_waiting,
                execution_still_owned, worker_still_running,
                second_controller_rejected, provider_close_rejected,
                caller_cancelled, sequential_result)

    (observations, task_waiting, execution_owned, worker_running,
     second_rejected, close_rejected, caller_cancelled,
     sequential_result) = asyncio.run(run())

    assert observations == [True, True, True]
    assert task_waiting is True
    assert execution_owned is True
    assert worker_running is True
    assert second_rejected is True
    assert close_rejected is True
    assert caller_cancelled is True
    assert client.calls == 1
    assert sequential_result is not None and sequential_result["verified"] is True
    assert backend.tick == 2
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "verified"
    assert loop._async_execution is None


@pytest.mark.parametrize("phase", ["after_send", "after_response"])
def test_repeated_cancellation_during_provider_phase_keeps_worker_owned(tmp_path, phase):
    entered = threading.Event()
    cancel_seen = threading.Event()

    async def run():
        release = asyncio.Event()

        class GatedProvider(CountingAsyncMock):
            async def evaluate(self, state, questions, *, identity, deadline=None,
                               decision_lease=None):
                self.calls += 1
                if phase == "after_send":
                    decision_lease.mark_may_have_been_sent()
                    response = None
                else:
                    response = await AsyncMockJevClient.evaluate(
                        self, state, questions, identity=identity,
                        deadline=deadline, decision_lease=decision_lease)
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancel_seen.set()
                    await release.wait()
                    raise
                return response

        backend = MockBackend()
        client = GatedProvider()
        loop, _, _ = _controller(tmp_path, backend=backend, client=client)
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        execution = loop._async_execution
        assert execution is not None
        if phase == "after_response":
            record = loop._async_archive.records()[0]
            assert loop._async_lease_from_record(record).inspect().state == "response_received"
        cancellations = await _cancel_three_times_while_worker_is_gated(task)
        task_still_waiting = not task.done()
        worker_still_running = not execution["worker"].done()
        execution_still_owned = loop._async_execution is execution
        provider_cancellation_delivered = cancel_seen.is_set()
        close_rejected = False
        if task_still_waiting:
            try:
                await loop.aclose_async_provider()
            except AsyncControllerBusy:
                close_rejected = True
        release.set()
        try:
            await task
            caller_cancelled = False
        except asyncio.CancelledError:
            caller_cancelled = True
        record = loop._async_archive.records()[0]
        wal_state = loop._async_lease_from_record(record).inspect().state
        pointer = loop.memory.async_decision
        return (backend, client, loop, cancellations, task_still_waiting,
                worker_still_running, execution_still_owned,
                provider_cancellation_delivered, close_rejected,
                caller_cancelled, wal_state, pointer)

    (backend, client, loop, cancellations, task_waiting, worker_running,
     execution_owned, cancel_delivered, close_rejected, caller_cancelled,
     wal_state, pointer) = asyncio.run(run())

    assert cancellations == [True, True, True]
    assert task_waiting is True
    assert worker_running is True
    assert execution_owned is True
    assert cancel_delivered is True
    assert close_rejected is True
    assert caller_cancelled is True
    assert client.calls == 1
    assert backend.tick == 0
    if phase == "after_send":
        assert wal_state == "may_have_been_sent"
        assert pointer is not None and pointer["disposition"] == "pending"
    else:
        assert wal_state == "consumed"
        assert pointer is None


@pytest.mark.parametrize("phase", ["before_send", "after_send", "after_response"])
def test_cancellation_preserves_the_exact_provider_phase_and_prevents_action(tmp_path, phase):
    entered = threading.Event()

    class PhaseGateClient(CountingAsyncMock):
        async def evaluate(self, state, questions, *, identity, deadline=None,
                           decision_lease=None):
            self.calls += 1
            if phase == "after_send":
                decision_lease.mark_may_have_been_sent()
            if phase == "after_response":
                await AsyncMockJevClient.evaluate(
                    self,
                    state, questions, identity=identity, deadline=deadline,
                    decision_lease=decision_lease)
            entered.set()
            await asyncio.Event().wait()

    async def run():
        backend = MockBackend()
        client = PhaseGateClient()
        loop, _, _ = _controller(tmp_path, backend=backend, client=client)
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pointer_after_cancel = (dict(loop.memory.async_decision)
                                if loop.memory.async_decision is not None else None)
        reconciled = await loop.step_async() if phase == "after_send" else None
        return loop, backend, client, reconciled, pointer_after_cancel

    loop, backend, client, reconciled, pointer_after_cancel = asyncio.run(run())

    assert client.calls == 1
    assert backend.tick == 0
    if phase == "before_send":
        assert loop.memory.async_decision is None
        assert loop.jev.state.get("in_flight") is None
        wal_record = loop._async_lease_from_record(
            json.loads(next(
                (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
            ).read_text())
        ).inspect()
        assert wal_record.state == "failed" and wal_record.phase == "not_sent"
        archived = loop._async_archive.records()[0]
        settlement = loop._async_archive.load_settlement(archived)
        assert settlement["disposition"] == "cancelled_before_send"
        assert any(row.get("kind") == "async_decision_settled"
                   for row in loop.memory.history)
        resumed_client = CountingAsyncMock()
        resumed, _, _ = _controller(
            tmp_path, backend=backend, client=resumed_client, resume=True)
        resumed_result = asyncio.run(resumed.step_async())
        assert resumed_result["action"] in {"walk_to_coal", "observe"}
        assert resumed_client.calls == 1
    elif phase == "after_send":
        assert pointer_after_cancel["disposition"] == "pending"
        assert reconciled is not None
        assert reconciled["action"] == "observe"
        assert loop.memory.async_decision["disposition"] == "no_action"
        archived = loop._async_archive.load(loop.memory.async_decision)
        assert loop._async_lease_from_record(archived).inspect().state == "ambiguous"
        assert loop.jev.state["phase"] == "exhausted"
        assert loop._async_lease_from_record(archived).inspect().state == "ambiguous"
    else:
        assert loop.memory.async_decision is None
        archived = json.loads(next(
            (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
        ).read_text())
        assert loop._async_lease_from_record(archived).inspect().state == "consumed"
        assert loop.jev.state["decision_outcome"]["state"] == "consumed"


class _MixedModeActionBarrier:
    def __init__(self, expected_entries=1):
        self.expected_entries = expected_entries
        self.guard = threading.Lock()
        self.entries = 0
        self.active = 0
        self.maximum = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        with self.guard:
            self.entries += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.entries >= self.expected_entries:
                self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release the mixed-mode action barrier")
        with self.guard:
            self.active -= 1


class _MixedModeGatedBackend(MockBackend):
    def __init__(self, barrier):
        super().__init__()
        self.barrier = barrier

    def act(self, action):
        self.barrier.wait()
        return super().act(action)


class _MixedModeActorView:
    def __init__(self, shared, session_id, actor_unit):
        self._shared = shared
        self.session_id = session_id
        self.actor_unit = actor_unit
        self.surface_index = 2
        self.force_index = 3
        self._native_attachment = {
            "qualified": True, "session_id": session_id,
            "actor_unit": actor_unit,
        }

    def __getattr__(self, name):
        return getattr(self._shared, name)

    def observe(self):
        snapshot = self._shared.observe()
        snapshot.session_id = self.session_id
        snapshot.world_kind = "fle"
        snapshot.factory["acceptance_runtime"] = {
            "session_id": self.session_id, "actor_unit": self.actor_unit,
            "surface_index": self.surface_index,
            "force_index": self.force_index,
        }
        return snapshot

    def act(self, action):
        return self._shared.act(action)


class _UnknownTransportSyncView:
    """A sync facade that shares a backend but exposes no transport identity."""

    def __init__(self, shared):
        self.inner = shared
        self.session_id = shared.session_id
        self.actor_unit = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _capture_controller_call(call):
    try:
        return ("result", call())
    except BaseException as error:
        return ("error", error)


def test_same_controller_busy_rejection_precedes_trace_and_profile_mutation(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class GatedBackend(MockBackend):
        def act(self, action):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the admitted step")
            return super().act(action)

    class Sink:
        def __init__(self):
            self.guard = threading.Lock()
            self.rows = []

        def emit(self, event_type, payload):
            with self.guard:
                self.rows.append({"event_type": event_type, **payload})

    backend = GatedBackend()
    sink = Sink()
    loop = HierarchicalLoop(
        backend, policy="deterministic", target="bootstrap_mining",
        checkpoint=str(tmp_path / "campaign.json"), tick_seconds=0,
        research_log=sink,
    )
    loop.profile_latency = True
    outcomes = {}
    second_done = threading.Event()

    def invoke(name, done=None):
        outcomes[name] = _capture_controller_call(loop.step)
        if done is not None:
            done.set()

    first = threading.Thread(target=invoke, args=("first",), daemon=True)
    first.start()
    assert entered.wait(3), "first public step never entered backend act"

    second = threading.Thread(target=invoke, args=("second", second_done), daemon=True)
    second.start()
    try:
        assert second_done.wait(3), "overlapping public step did not reject promptly"
        assert outcomes["second"][0] == "error"
        assert isinstance(outcomes["second"][1], AsyncControllerBusy)
        with sink.guard:
            before_release = list(sink.rows)
        started_before_release = [row for row in before_release
                                  if row["event_type"] == "step_started"]
        failed_before_release = [row for row in before_release
                                 if row["event_type"] == "step_failed"]
        prepared_before_release = [row for row in before_release
                                   if row["event_type"] == "action_prepared"]
        assert [row.get("decision_id") for row in started_before_release] == ["decision:1"]
        assert failed_before_release == []
        assert len(prepared_before_release) == 1
        admitted_decision = prepared_before_release[0]["decision_id"]
        admitted_action = prepared_before_release[0]["action_id"]
        assert admitted_decision == "decision:1"
        assert loop._trace.decision_id == admitted_decision
        assert loop._timing_index == 1
        assert loop._trace._failed is False
    finally:
        release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive(), "step threads leaked"

    assert outcomes["first"][0] == "result"
    assert outcomes["first"][1]["verified"] is True
    with sink.guard:
        rows = list(sink.rows)
    started = [row for row in rows if row["event_type"] == "step_started"]
    failed = [row for row in rows if row["event_type"] == "step_failed"]
    finished = [row for row in rows if row["event_type"] == "step_finished"]
    actions = [row for row in rows if row["event_type"] in {"action_prepared", "action_returned"}]
    assert [row.get("decision_id") for row in started] == [admitted_decision]
    assert failed == []
    assert [row.get("decision_id") for row in finished] == [admitted_decision]
    assert len(actions) == 2
    assert {row.get("decision_id") for row in actions} == {admitted_decision}
    assert {row.get("action_id") for row in actions} == {admitted_action}
    assert loop._timing_index == 1
    assert loop._timing_pending["summary"]["status"] == "returned"
    assert loop._timing_pending["summary"]["phases"]["iteration"]["calls"] == 1
    assert loop._timing_pending["summary"]["phases"]["iteration"]["failed"] == 0
    assert backend.tick == 1


def test_admitted_step_failure_keeps_trace_and_profile_failure_denominators(tmp_path):
    class FailingBackend(MockBackend):
        def observe(self):
            raise ValueError("injected observation failure")

    class Sink:
        def __init__(self):
            self.rows = []

        def emit(self, event_type, payload):
            self.rows.append({"event_type": event_type, **payload})

    sink = Sink()
    loop = HierarchicalLoop(
        FailingBackend(), policy="deterministic", target="bootstrap_mining",
        checkpoint=str(tmp_path / "campaign.json"), tick_seconds=0,
        research_log=sink,
    )
    loop.profile_latency = True

    with pytest.raises(ValueError, match="injected observation failure"):
        loop.step()

    started = [row for row in sink.rows if row["event_type"] == "step_started"]
    failed = [row for row in sink.rows if row["event_type"] == "step_failed"]
    finished = [row for row in sink.rows if row["event_type"] == "step_finished"]
    assert [row.get("decision_id") for row in started] == ["decision:1"]
    assert [row.get("decision_id") for row in failed] == ["decision:1"]
    assert finished == []
    assert loop._timing_index == 1
    assert loop._timing_pending["summary"]["status"] == "error"
    assert loop._timing_pending["summary"]["phases"]["iteration"]["calls"] == 1
    assert loop._timing_pending["summary"]["phases"]["iteration"]["failed"] == 1


@pytest.mark.parametrize("different_session", [False, True],
                         ids=["same-session-different-actor", "shared-transport-different-session"])
def test_sync_step_is_rejected_during_async_ownership_of_session_or_transport(
        tmp_path, different_session):
    barrier = _MixedModeActionBarrier()
    shared = _MixedModeGatedBackend(barrier)
    async_session = "mixed-async-session"
    sync_session = "mixed-sync-session" if different_session else async_session
    async_backend = _MixedModeActorView(shared, async_session, 41)
    sync_backend = _MixedModeActorView(shared, sync_session, 42)
    async_dir, sync_dir = tmp_path / "async", tmp_path / "sync"
    async_dir.mkdir()
    sync_dir.mkdir()
    async_loop, _, client = _controller(
        async_dir, backend=async_backend, client=OfflineFLEAsyncMock())
    sync_loop = _sync_controller(sync_dir, backend=sync_backend)

    async def run():
        async_task = asyncio.create_task(async_loop.step_async())
        assert await asyncio.to_thread(barrier.entered.wait, 3)
        sync_attempt = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, sync_loop.step))
        try:
            await asyncio.sleep(0.05)
            rejected_before_release = sync_attempt.done()
            barrier_entries_before_release = barrier.entries
        finally:
            barrier.release.set()
        sync_outcome = await sync_attempt
        async_result = await async_task
        return rejected_before_release, barrier_entries_before_release, sync_outcome, async_result

    rejected, entries, sync_outcome, async_result = asyncio.run(run())
    assert rejected is True
    assert entries == 1
    assert sync_outcome[0] == "error"
    assert isinstance(sync_outcome[1], AsyncControllerBusy)
    assert async_result["verified"] is True
    assert client.calls == 1
    assert shared.tick == 1


def test_async_step_is_rejected_during_sync_ownership_then_runs_after_release(tmp_path):
    barrier = _MixedModeActionBarrier()
    shared = _MixedModeGatedBackend(barrier)
    sync_backend = _MixedModeActorView(shared, "reverse-session", 41)
    async_backend = _MixedModeActorView(shared, "reverse-session", 42)
    sync_dir, async_dir = tmp_path / "sync", tmp_path / "async"
    sync_dir.mkdir()
    async_dir.mkdir()
    sync_loop = _sync_controller(sync_dir, backend=sync_backend)
    async_loop, _, client = _controller(
        async_dir, backend=async_backend, client=OfflineFLEAsyncMock())

    async def run():
        sync_task = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, sync_loop.step))
        assert await asyncio.to_thread(barrier.entered.wait, 3)
        async_task = asyncio.create_task(async_loop.step_async())
        try:
            await asyncio.sleep(0.05)
            rejected_before_release = async_task.done()
            entries_before_release = barrier.entries
            if rejected_before_release:
                try:
                    await async_task
                except BaseException as error:
                    async_error = error
                else:
                    async_error = None
            else:
                async_error = None
        finally:
            barrier.release.set()
        sync_outcome = await sync_task
        if not rejected_before_release:
            try:
                await async_task
            except BaseException as error:
                async_error = error
        sequential_result = await async_loop.step_async()
        return (rejected_before_release, entries_before_release, async_error,
                sync_outcome, sequential_result)

    rejected, entries, async_error, sync_outcome, sequential_result = asyncio.run(run())
    assert rejected is True
    assert entries == 1
    assert isinstance(async_error, AsyncControllerBusy)
    assert sync_outcome[0] == "result"
    assert sequential_result["verified"] is True
    assert client.calls == 1
    assert shared.tick == 2


def test_distinct_known_sync_and_async_transports_remain_parallel(tmp_path):
    barrier = _MixedModeActionBarrier(expected_entries=2)
    sync_backend = _MixedModeGatedBackend(barrier)
    async_backend = _MixedModeGatedBackend(barrier)
    sync_dir, async_dir = tmp_path / "sync", tmp_path / "async"
    sync_dir.mkdir()
    async_dir.mkdir()
    sync_loop = _sync_controller(sync_dir, backend=sync_backend)
    async_loop, _, client = _controller(async_dir, backend=async_backend)

    async def run():
        sync_task = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, sync_loop.step))
        async_task = asyncio.create_task(async_loop.step_async())
        try:
            both_entered = await asyncio.to_thread(barrier.entered.wait, 3)
            maximum_before_release = barrier.maximum
        finally:
            barrier.release.set()
        sync_outcome = await sync_task
        async_result = await async_task
        return both_entered, maximum_before_release, sync_outcome, async_result

    both, maximum, sync_outcome, async_result = asyncio.run(run())
    assert both is True
    assert maximum == 2
    assert sync_outcome[0] == "result"
    assert async_result["verified"] is True
    assert client.calls == 1


def test_unknown_sync_transport_fails_closed_against_active_async_owner(tmp_path):
    barrier = _MixedModeActionBarrier()
    shared = _MixedModeGatedBackend(barrier)
    async_dir, sync_dir = tmp_path / "async", tmp_path / "sync"
    async_dir.mkdir()
    sync_dir.mkdir()
    async_loop, _, _ = _controller(async_dir, backend=shared)
    sync_loop = _sync_controller(
        sync_dir, backend=_UnknownTransportSyncView(shared))

    async def run():
        async_task = asyncio.create_task(async_loop.step_async())
        assert await asyncio.to_thread(barrier.entered.wait, 3)
        sync_attempt = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, sync_loop.step))
        try:
            await asyncio.sleep(0.05)
            rejected_before_release = sync_attempt.done()
            entries_before_release = barrier.entries
        finally:
            barrier.release.set()
        sync_outcome = await sync_attempt
        async_result = await async_task
        return rejected_before_release, entries_before_release, sync_outcome, async_result

    rejected, entries, sync_outcome, async_result = asyncio.run(run())
    assert rejected is True
    assert entries == 1
    assert sync_outcome[0] == "error"
    assert isinstance(sync_outcome[1], AsyncControllerBusy)
    assert async_result["verified"] is True


def test_active_unknown_sync_identity_blocks_known_async_admission(tmp_path):
    barrier = _MixedModeActionBarrier()
    shared = _MixedModeGatedBackend(barrier)
    sync_dir, async_dir = tmp_path / "sync", tmp_path / "async"
    sync_dir.mkdir()
    async_dir.mkdir()
    sync_loop = _sync_controller(
        sync_dir, backend=_UnknownTransportSyncView(shared))
    async_loop, _, client = _controller(async_dir, backend=shared)

    async def run():
        sync_task = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, sync_loop.step))
        assert await asyncio.to_thread(barrier.entered.wait, 3)
        async_task = asyncio.create_task(async_loop.step_async())
        try:
            await asyncio.sleep(0.05)
            rejected_before_release = async_task.done()
            entries_before_release = barrier.entries
            if rejected_before_release:
                try:
                    await async_task
                except BaseException as error:
                    async_error = error
                else:
                    async_error = None
            else:
                async_error = None
        finally:
            barrier.release.set()
        sync_outcome = await sync_task
        if not rejected_before_release:
            try:
                await async_task
            except BaseException as error:
                async_error = error
        sequential_result = await async_loop.step_async()
        return (rejected_before_release, entries_before_release, async_error,
                sync_outcome, sequential_result)

    rejected, entries, async_error, sync_outcome, sequential_result = asyncio.run(run())
    assert rejected is True
    assert entries == 1
    assert isinstance(async_error, AsyncControllerBusy)
    assert sync_outcome[0] == "result"
    assert sequential_result["verified"] is True
    assert client.calls == 1


def test_sync_only_calls_keep_independent_known_resources_parallel(tmp_path):
    barrier = _MixedModeActionBarrier(expected_entries=2)
    first_backend = _MixedModeGatedBackend(barrier)
    second_backend = _MixedModeGatedBackend(barrier)
    first_dir, second_dir = tmp_path / "sync-one", tmp_path / "sync-two"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _sync_controller(first_dir, backend=first_backend)
    second = _sync_controller(second_dir, backend=second_backend)

    async def run():
        first_task = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, first.step))
        second_task = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, second.step))
        try:
            both_entered = await asyncio.to_thread(barrier.entered.wait, 3)
            maximum_before_release = barrier.maximum
        finally:
            barrier.release.set()
        return both_entered, maximum_before_release, await first_task, await second_task

    both, maximum, first_outcome, second_outcome = asyncio.run(run())
    assert both is True
    assert maximum == 2
    assert first_outcome[0] == "result"
    assert second_outcome[0] == "result"


@pytest.mark.parametrize("different_session", [False, True],
                         ids=["same-session-different-actor", "shared-transport-different-session"])
def test_sync_step_rejects_overlapping_known_resource_then_retries(
        tmp_path, different_session):
    barrier = _MixedModeActionBarrier()
    shared = _MixedModeGatedBackend(barrier)
    first_session = "sync-shared-session"
    second_session = "sync-other-session" if different_session else first_session
    first_backend = _MixedModeActorView(shared, first_session, 41)
    second_backend = _MixedModeActorView(shared, second_session, 42)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _sync_controller(first_dir, backend=first_backend)
    second = _sync_controller(second_dir, backend=second_backend)
    second_checkpoint = second_dir / "sync-campaign.json"
    second_finished = threading.Event()

    def invoke_second():
        try:
            return _capture_controller_call(second.step)
        finally:
            second_finished.set()

    async def run():
        first_task = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, first.step))
        assert await asyncio.to_thread(barrier.entered.wait, 3)
        second_task = asyncio.create_task(asyncio.to_thread(invoke_second))
        try:
            rejected_before_release = await asyncio.to_thread(second_finished.wait, 0.2)
            entries_before_release = barrier.entries
            tick_before_release = shared.tick
            second_memory_before_release = second.memory
            checkpoint_before_release = second_checkpoint.exists()
        finally:
            barrier.release.set()
        first_outcome = await first_task
        second_outcome = await second_task
        return (rejected_before_release, entries_before_release, tick_before_release,
                second_memory_before_release, checkpoint_before_release,
                first_outcome, second_outcome)

    (rejected, entries, tick, second_memory, checkpoint_exists,
     first_outcome, second_outcome) = asyncio.run(run())
    assert rejected is True
    assert entries == 1
    assert tick == 0
    assert second_memory is None
    assert checkpoint_exists is False
    assert first_outcome[0] == "result"
    assert second_outcome[0] == "error"
    assert isinstance(second_outcome[1], AsyncControllerBusy)
    assert shared.tick == 1

    retried = second.step()
    assert retried["verified"] is True
    assert shared.tick == 2


@pytest.mark.parametrize(("first_unknown", "second_unknown"), [
    (False, True), (True, False), (True, True),
], ids=["known-then-unknown", "unknown-then-known", "unknown-then-unknown"])
def test_unknown_sync_identity_excludes_every_active_sync_owner_then_retries(
        tmp_path, first_unknown, second_unknown):
    barrier = _MixedModeActionBarrier()
    shared = _MixedModeGatedBackend(barrier)
    unknown_backend = _UnknownTransportSyncView(shared)
    first_backend = unknown_backend if first_unknown else shared
    second_backend = unknown_backend if second_unknown else shared
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _sync_controller(first_dir, backend=first_backend)
    second = _sync_controller(second_dir, backend=second_backend)
    second_checkpoint = second_dir / "sync-campaign.json"
    second_finished = threading.Event()

    def invoke_second():
        try:
            return _capture_controller_call(second.step)
        finally:
            second_finished.set()

    async def run():
        first_task = asyncio.create_task(asyncio.to_thread(
            _capture_controller_call, first.step))
        assert await asyncio.to_thread(barrier.entered.wait, 3)
        second_task = asyncio.create_task(asyncio.to_thread(invoke_second))
        try:
            rejected_before_release = await asyncio.to_thread(second_finished.wait, 0.2)
            entries_before_release = barrier.entries
            tick_before_release = shared.tick
            second_memory_before_release = second.memory
            checkpoint_before_release = second_checkpoint.exists()
        finally:
            barrier.release.set()
        first_outcome = await first_task
        second_outcome = await second_task
        return (rejected_before_release, entries_before_release, tick_before_release,
                second_memory_before_release, checkpoint_before_release,
                first_outcome, second_outcome)

    (rejected, entries, tick, second_memory, checkpoint_exists,
     first_outcome, second_outcome) = asyncio.run(run())
    assert rejected is True
    assert entries == 1
    assert tick == 0
    assert second_memory is None
    assert checkpoint_exists is False
    assert first_outcome[0] == "result"
    assert second_outcome[0] == "error"
    assert isinstance(second_outcome[1], AsyncControllerBusy)
    assert shared.tick == 1

    retried = second.step()
    assert retried["verified"] is True
    assert shared.tick == 2


def test_sync_resource_claim_fault_does_not_publish_partial_registry_update(
        tmp_path, monkeypatch):
    backend = MockBackend()
    loop = _sync_controller(tmp_path, backend=backend)
    keys = tuple(sorted(set(loop._async_resource_lock_keys())))
    assert len(keys) == 2

    class FaultingSyncUsers(dict):
        def __init__(self, initial=(), *, fail_key=None):
            super().__init__(initial)
            self.fail_key = fail_key

        def copy(self):
            return type(self)(dict(self), fail_key=self.fail_key)

        def __setitem__(self, key, value):
            if key == self.fail_key:
                raise RuntimeError("injected sync resource registry update failure")
            return super().__setitem__(key, value)

    original = controller_module._SYNC_RESOURCE_USERS
    before = dict(original)
    faulting = FaultingSyncUsers(before, fail_key=keys[-1])
    checkpoint = tmp_path / "sync-campaign.json"
    with monkeypatch.context() as patch:
        patch.setattr(controller_module, "_SYNC_RESOURCE_USERS", faulting)
        with pytest.raises(RuntimeError, match="registry update failure"):
            loop.step()
        assert controller_module._SYNC_RESOURCE_USERS is faulting
        assert dict(faulting) == before
        assert loop.memory is None
        assert backend.tick == 0
        assert not checkpoint.exists()

    assert controller_module._SYNC_RESOURCE_USERS is original
    result = loop.step()
    assert result["verified"] is True
    assert controller_module._SYNC_RESOURCE_USERS == before


@pytest.mark.parametrize("unknown_identity", [False, True],
                         ids=["known-resource-count", "unknown-global-count"])
def test_sync_resource_claim_is_released_when_step_raises_before_observation(
        tmp_path, unknown_identity):
    class FailOnceObserveBackend(MockBackend):
        def __init__(self):
            super().__init__()
            self.observations = 0

        def observe(self):
            self.observations += 1
            if self.observations == 1:
                raise RuntimeError("injected pre-observation failure")
            return super().observe()

    backend = FailOnceObserveBackend()
    sync_dir, async_dir = tmp_path / "sync", tmp_path / "async"
    sync_dir.mkdir()
    async_dir.mkdir()
    sync_backend = _UnknownTransportSyncView(backend) if unknown_identity else backend
    sync_loop = _sync_controller(sync_dir, backend=sync_backend)
    async_loop, _, client = _controller(async_dir, backend=backend)

    with pytest.raises(RuntimeError, match="injected pre-observation failure"):
        sync_loop.step()

    result = asyncio.run(async_loop.step_async())
    assert result["verified"] is True
    assert client.calls == 1
    assert backend.observations > 1
