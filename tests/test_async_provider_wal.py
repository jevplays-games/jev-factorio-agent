"""Public async client/provider health binding tests for the decision WAL."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from jev_factorio import provider_decision_wal as wal_module
from jev_factorio.async_provider import (
    AsyncProviderCancelled,
    AsyncProviderDeadlineExceeded,
    AsyncProviderLocalError,
    RequestIdentity,
)
from jev_factorio.jev_client import (
    AsyncCloudflareJevClient,
    AsyncJevClient,
    AsyncMockJevClient,
    AsyncTracedClient,
)
from jev_factorio.causal_trace import CausalTrace
from jev_factorio.provider_decision_wal import ProviderDecisionWAL
from jev_factorio.provider_decision_wal import WALBusyError
from jev_factorio.provider_health import ProviderBlocked, ProviderCircuit, SafetyStateError


STATE = {"tick": 42, "inventory": {"coal": 3}}
QUESTIONS = {"ready": {"type": "choice", "criteria": {"yes": "ready"}}}


def _answers():
    return {"ready": {"type": "choice", "choice": "yes",
                      "probabilities": {"yes": 1.0}, "confidence": 1.0}}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, kind, payload):
        self.events.append((kind, payload))


def _identity(label: str) -> RequestIdentity:
    return RequestIdentity(
        session_id=f"session-{label}", actor_id=f"actor-{label}",
        observation_id=f"observation-{label}", decision_id=f"decision-{label}",
        request_id=str(uuid4()),
    )


def test_public_mock_decision_lease_saves_before_health_clear(tmp_path):
    async def exercise():
        client = AsyncMockJevClient()
        circuit = ProviderCircuit(client, tmp_path / "health.json")
        wal = ProviderDecisionWAL.initialize(tmp_path / "decision.json")
        identity = _identity("mock")
        lease = circuit.prepare_decision_lease(wal, STATE, QUESTIONS, identity=identity)

        result = await circuit.evaluate_async(
            STATE, QUESTIONS, identity=identity, decision_lease=lease,
        )

        record = lease.inspect()
        assert result.identity is identity
        assert record.state == "response_received"
        assert record.identity.request_id == identity.request_id
        assert circuit.state["in_flight"] is None
        assert circuit.state["decision_outcome"]["result_sha256"] == record.result_sha256
        assert circuit.state["decision_outcome"]["identity"]["decision_id"] == identity.decision_id

    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["typesafe", "cloudflare"])
def test_direct_and_cloudflare_leases_bind_fake_transport_and_replay(tmp_path, provider):
    calls = []

    def handler(request):
        calls.append(request)
        if provider == "typesafe":
            body = {"answers": _answers(), "usage": {"input_tokens": 1},
                    "model": "resolved-typesafe"}
        else:
            body = {"success": True, "result": {
                "answers": _answers(), "usage": {"input_tokens": 2},
                "model": "resolved-cloudflare"}}
        return httpx.Response(200, json=body, request=request)

    async def exercise():
        if provider == "typesafe":
            client = AsyncJevClient(
                api_key="offline-typesafe-secret", base_url="https://offline.invalid/type",
                model="model-typesafe", transport=httpx.MockTransport(handler),
            )
        else:
            client = AsyncCloudflareJevClient(
                "offline-account", "offline-cloudflare-secret", model="model-cloudflare",
                transport=httpx.MockTransport(handler),
            )
        health_path = tmp_path / f"health-{provider}.json"
        circuit = ProviderCircuit(client, health_path)
        wal = ProviderDecisionWAL.initialize(tmp_path / f"wal-{provider}.json")
        request_identity = _identity(provider)
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            result = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            replay = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
        finally:
            await client.aclose()
        assert result == replay
        assert result.identity is request_identity
        assert result.request_payload_sha256 == lease.request_payload_sha256
        assert result.requested_model == client.model
        assert len(calls) == 1
        assert lease.inspect().state == "response_received"
        assert circuit.state["decision_outcome"]["result_sha256"] == lease.inspect().result_sha256
        for path in (health_path, wal.path):
            text = path.read_text(encoding="utf-8")
            assert "offline-typesafe-secret" not in text
            assert "offline-cloudflare-secret" not in text
            assert "Authorization" not in text

    asyncio.run(exercise())


def test_lease_rejects_changed_model_before_wal_or_health_mutation(tmp_path):
    client = AsyncMockJevClient()
    health_path = tmp_path / "health.json"
    circuit = ProviderCircuit(client, health_path)
    wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
    request_identity = _identity("model-drift")
    lease = circuit.prepare_decision_lease(
        wal, STATE, QUESTIONS, identity=request_identity)
    before = dict(circuit.state)
    client.model = "unreviewed-model"

    with pytest.raises(ValueError, match="configuration or request changed"):
        asyncio.run(circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity, decision_lease=lease))

    assert circuit.state == before
    assert not health_path.exists()
    assert lease.inspect_optional() is None


def test_trace_model_call_identity_is_part_of_the_wal_binding(tmp_path):
    async def exercise():
        sink = Sink()
        trace = CausalTrace(sink, "lease-trace")
        request_identity = _identity("traced")
        trace.begin_step()
        trace.decision_id = request_identity.decision_id
        trace.observation_id = request_identity.observation_id
        trace._session_id = request_identity.session_id
        client = AsyncTracedClient(AsyncMockJevClient(), trace)
        circuit = ProviderCircuit(client, tmp_path / "health.json")
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        result = await circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
        request_event = next(payload for kind, payload in sink.events
                             if kind == "model_request")
        response_event = next(payload for kind, payload in sink.events
                              if kind == "model_response")
        assert result.identity is request_identity
        assert lease.trace_binding["model_call_id"] == "model-request:" + request_identity.request_id
        assert request_event["model_call_id"] == lease.trace_binding["model_call_id"]
        assert response_event["model_call_id"] == lease.trace_binding["model_call_id"]
        assert lease.inspect().request_sha256 == lease.request_sha256
        assert circuit.state["in_flight"] is None

    asyncio.run(exercise())


def test_deadline_before_transport_durably_closes_not_sent_and_restores_health(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        health_path = tmp_path / "health.json"
        circuit = ProviderCircuit(client, health_path)
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("expired")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        before = dict(circuit.state)
        try:
            with pytest.raises(AsyncProviderDeadlineExceeded) as raised:
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity,
                    decision_lease=lease, deadline=time.monotonic() - 1,
                )
            assert lease.inspect().state == "failed"
            assert lease.inspect().phase == "not_sent"
            assert lease.inspect().error_category == "local_admission"
            assert circuit.state == before
            assert json.loads(health_path.read_text())["in_flight"] is None
            assert calls == []
            with pytest.raises(AsyncProviderLocalError):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease,
                )
            assert calls == []
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_dispatch_fsync_failure_never_enters_http_and_reserved_retry_is_safe(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, tmp_path / "health.json")
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("may-fsync")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        atomic = wal_module.atomic_json

        def fail_may_before_replace(path, value):
            if path == wal.path and value.get("last_sequence") == 2:
                raise OSError("injected MAY fsync boundary failure")
            return atomic(path, value)

        monkeypatch.setattr(wal_module, "atomic_json", fail_may_before_replace)
        try:
            with pytest.raises(OSError, match="MAY fsync"):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            assert calls == []
            assert lease.inspect().state == "reserved"
            assert circuit.state["in_flight"]["decision_binding"] == lease.binding()
            monkeypatch.setattr(wal_module, "atomic_json", atomic)
            result = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            assert result.answers["ready"]["choice"] == "yes"
            assert len(calls) == 1
            assert lease.inspect().state == "response_received"
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_saved_response_survives_final_health_write_failure_and_restart(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        health_path = tmp_path / "health.json"
        circuit = ProviderCircuit(client, health_path)
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("health-write")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        real_save = circuit._save

        def fail_only_after_response():
            if circuit.state["in_flight"] is None:
                raise OSError("injected health outcome persistence failure")
            real_save()

        monkeypatch.setattr(circuit, "_save", fail_only_after_response)
        try:
            with pytest.raises(OSError, match="health outcome"):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            assert len(calls) == 1
            assert lease.inspect().state == "response_received"
            assert json.loads(health_path.read_text())["in_flight"] is not None
        finally:
            await client.aclose()

        recovered_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        recovered = ProviderCircuit(recovered_client, health_path)
        recovered_lease = recovered.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            result = await recovered.evaluate_async(
                STATE, QUESTIONS, identity=request_identity,
                decision_lease=recovered_lease)
        finally:
            await recovered_client.aclose()
        assert result.identity is request_identity
        assert result.answers["ready"]["choice"] == "yes"
        assert len(calls) == 1
        assert recovered.state["in_flight"] is None
        assert recovered.state["decision_outcome"]["result_sha256"] == lease.inspect().result_sha256

    asyncio.run(exercise())


def test_response_is_durable_before_release_cancellation_then_replays(tmp_path):
    calls = []

    async def exercise():
        entered, answer, release_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            entered.set()
            await answer.wait()
            return httpx.Response(200, json={"answers": _answers()}, request=request)

        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        health_path = tmp_path / "health.json"
        circuit = ProviderCircuit(client, health_path)
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("release-cancel")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        original_release = client._release

        async def observe_release():
            release_started.set()
            return await original_release()

        client._release = observe_release
        task = asyncio.create_task(circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity, decision_lease=lease))
        await entered.wait()
        await client._condition.acquire()
        try:
            answer.set()
            await asyncio.wait_for(release_started.wait(), timeout=2)
            assert lease.inspect().state == "response_received"
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            client._condition.release()
        with pytest.raises(AsyncProviderCancelled) as cancelled:
            await task
        assert cancelled.value.delivery_phase == "response_received"
        assert len(calls) == 1
        assert circuit.state["in_flight"] is not None
        await client.aclose()

        recovered_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        recovered = ProviderCircuit(recovered_client, health_path)
        recovered_lease = recovered.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            result = await recovered.evaluate_async(
                STATE, QUESTIONS, identity=request_identity,
                decision_lease=recovered_lease)
        finally:
            await recovered_client.aclose()
        assert result.answers["ready"]["choice"] == "yes"
        assert len(calls) == 1
        assert recovered.state["in_flight"] is None

    asyncio.run(exercise())


def test_retry_after_metadata_recovers_without_extending_or_losing_source_clock(tmp_path, monkeypatch):
    calls = []
    clock = Clock()

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "12"},
                              json={"secret": "do-not-persist"}, request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        health_path = tmp_path / "health.json"
        circuit = ProviderCircuit(client, health_path, clock=clock)
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("retry-after")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        real_save = circuit._save

        def fail_only_after_provider_error():
            if circuit.state["in_flight"] is None:
                raise OSError("injected post-error health write failure")
            real_save()

        monkeypatch.setattr(circuit, "_save", fail_only_after_provider_error)
        try:
            with pytest.raises(OSError, match="post-error"):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            record = lease.inspect()
            assert record.state == "failed"
            assert record.phase == "response_received"
            assert record.error_category == "rate_limit"
            assert record.http_status == 429
            assert dict(record.cooldown) == {
                "received_at": 1000.0, "retry_after_seconds": 12.0}
            ledger_text = wal.path.read_text()
            assert "Retry-After" not in ledger_text
            assert "do-not-persist" not in ledger_text
            assert len(calls) == 1
        finally:
            await client.aclose()

        clock.now = 1005.0
        recovered_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        recovered = ProviderCircuit(recovered_client, health_path, clock=clock)
        recovered_lease = recovered.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            with pytest.raises(ProviderBlocked) as blocked:
                await recovered.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity,
                    decision_lease=recovered_lease)
            assert blocked.value.state["category"] == "rate_limit"
            assert blocked.value.state["next_probe_at"] == 1012.0
            assert blocked.value.delivery_phase == "response_received"
            assert len(calls) == 1
        finally:
            await recovered_client.aclose()

    asyncio.run(exercise())


def test_cleanup_cancellation_keeps_durable_response_for_exact_replay(tmp_path):
    calls = []

    async def exercise():
        entered, response_gate, release_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            entered.set()
            await response_gate.wait()
            return httpx.Response(200, json={"answers": _answers()}, request=request)

        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("cleanup-cancel")
        circuit = ProviderCircuit(client, health_path)
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        original_release = client._release

        async def observe_release():
            release_started.set()
            return await original_release()

        client._release = observe_release
        task = asyncio.create_task(circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity, decision_lease=lease))
        await entered.wait()
        await client._condition.acquire()
        try:
            response_gate.set()
            await asyncio.wait_for(release_started.wait(), timeout=2)
            assert lease.inspect().state == "response_received"
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            client._condition.release()
        with pytest.raises(AsyncProviderCancelled) as cancelled:
            await task
        assert cancelled.value.delivery_phase == "response_received"
        assert circuit.state["in_flight"] is not None
        assert len(calls) == 1
        await client.aclose()

        restarted_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        restarted = ProviderCircuit(restarted_client, health_path)
        restarted_lease = restarted.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            result = await restarted.evaluate_async(
                STATE, QUESTIONS, identity=request_identity,
                decision_lease=restarted_lease)
        finally:
            await restarted_client.aclose()
        assert result.identity is request_identity
        assert result.answers["ready"]["choice"] == "yes"
        assert len(calls) == 1
        assert restarted.state["in_flight"] is None

    asyncio.run(exercise())


def test_ambiguous_timeout_is_billed_once_and_never_reenters_transport(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("offline ambiguous transport", request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, tmp_path / "health.json", clock=Clock())
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("ambiguous-timeout")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            with pytest.raises(ProviderBlocked) as blocked:
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            assert blocked.value.state["category"] == "service_network"
            assert blocked.value.delivery_phase == "may_have_been_sent"
            record = lease.inspect()
            assert record.state == "ambiguous"
            assert record.error_category == "service_network"
            with pytest.raises(ProviderBlocked) as replay:
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            assert replay.value.delivery_phase == "may_have_been_sent"
            assert replay.value.state["incident_id"] == blocked.value.state["incident_id"]
            assert len(calls) == 1
            assert circuit.state["in_flight"] is None
            with pytest.raises(SafetyStateError, match="must be replayed or consumed"):
                circuit.prepare_decision_lease(
                    wal, STATE, QUESTIONS, identity=_identity("after-ambiguous"))
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_invalid_answer_is_closed_in_wal_as_schema_failure(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "answers": {"ready": {"type": "not-a-choice", "secret": "do-not-save"}},
        }, request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, tmp_path / "health.json", clock=Clock())
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("invalid-answer")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            with pytest.raises(ProviderBlocked) as blocked:
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            assert blocked.value.state["category"] == "application_schema"
            record = lease.inspect()
            assert record.state == "failed"
            assert record.phase == "response_received"
            assert record.error_category == "application_schema"
            assert record.result is None
            assert "do-not-save" not in wal.path.read_text()
            assert len(calls) == 1
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_external_authorization_change_blocks_health_result_commit(tmp_path):
    from jev_factorio.operational_safety import atomic_json
    from jev_factorio.provider_health import SafetyStateError

    calls = []

    async def exercise():
        entered, return_response = asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            entered.set()
            await return_response.wait()
            return httpx.Response(200, json={"answers": _answers()}, request=request)

        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        health_path = tmp_path / "health.json"
        circuit = ProviderCircuit(client, health_path)
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("auth-change")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        task = asyncio.create_task(circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity, decision_lease=lease))
        try:
            await entered.wait()
            auth_path = tmp_path / "provider-authorization.json"
            replacement = {"operator_event": "independent durable update"}
            atomic_json(auth_path, replacement)
            before = auth_path.read_bytes()
            return_response.set()
            with pytest.raises(SafetyStateError, match="authorization changed"):
                await task
            assert auth_path.read_bytes() == before
            assert json.loads(health_path.read_text())["in_flight"] is not None
            assert lease.inspect().state == "response_received"
            assert len(calls) == 1
        finally:
            return_response.set()
            await client.aclose()

        blocked_health_bytes = health_path.read_bytes()
        restarted_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        restarted = ProviderCircuit(restarted_client, health_path)
        restarted_lease = restarted.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            with pytest.raises(SafetyStateError, match="authorization changed"):
                await restarted.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity,
                    decision_lease=restarted_lease)
            assert health_path.read_bytes() == blocked_health_bytes
            assert len(calls) == 1
        finally:
            await restarted_client.aclose()

    asyncio.run(exercise())


def test_restart_after_may_sent_uses_exact_wal_binding_and_charges_unknown_once(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        first_client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        first = ProviderCircuit(first_client, health_path, clock=Clock())
        request_identity = _identity("crash-after-may")
        lease = first.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)

        # This is the durable crash checkpoint: health owns the exact flight,
        # while WAL says dispatch may have begun and has no response evidence.
        before = deepcopy(first.state)
        lease.reserve()
        started_at = first.clock()
        first.state["in_flight"] = {
            "request_id": request_identity.request_id,
            "started_at": started_at,
            "healthy_start": True,
            "decision_binding": lease.binding(),
            "decision_before": before,
            "authorization_sha256": first._decision_auth_digest(),
        }
        first._save_decision_state(before)
        lease.mark_may_have_been_sent()
        assert lease.inspect().state == "may_have_been_sent"
        await first_client.aclose()

        restarted_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        restarted = ProviderCircuit(restarted_client, health_path, clock=Clock())
        resumed = restarted.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            with pytest.raises(ProviderBlocked) as blocked:
                await restarted.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=resumed)
            assert blocked.value.called is True
            assert blocked.value.delivery_phase == "may_have_been_sent"
            assert blocked.value.state["category"] == "unknown_outcome"
            assert resumed.inspect().state == "ambiguous"
            assert resumed.inspect().phase == "may_have_been_sent"
            assert restarted.state["in_flight"] is None
            assert restarted.state["attempts"] == 1
            assert restarted.state["phase"] == "exhausted"
            assert calls == []
        finally:
            await restarted_client.aclose()

    asyncio.run(exercise())


def test_health_write_error_after_atomic_install_replays_without_double_charge(
        tmp_path, monkeypatch):
    from jev_factorio import provider_health as health_module

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        clock = Clock()
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        seed = ProviderCircuit(client, health_path, clock=clock)
        existing_incident = "incident-before-lease"
        preexisting = deepcopy(seed.state)
        preexisting.update(
            phase="cooldown", category="rate_limit", attempts=1,
            next_probe_at=900.0, incident_id=existing_incident,
            first_failure_at=950.0, budget_category="rate_limit", budget_limit=8,
        )
        health_module.atomic_json(health_path, preexisting)
        circuit = ProviderCircuit(client, health_path, clock=clock)
        request_identity = _identity("health-install-then-error")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        real_atomic_json = health_module.atomic_json

        def install_then_raise(path, value):
            real_atomic_json(path, value)
            if (path == health_path and value.get("in_flight") is None
                    and value.get("decision_outcome", {}).get("state") == "response_received"):
                raise OSError("injected post-install health durability error")

        monkeypatch.setattr(health_module, "atomic_json", install_then_raise)
        try:
            with pytest.raises(OSError, match="post-install health"):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
            committed = health_path.read_bytes()
            committed_state = json.loads(committed)
            assert calls and len(calls) == 1
            assert committed_state["in_flight"] is None
            assert committed_state["phase"] == "healthy"
            assert committed_state["attempts"] == 0
            assert committed_state["previous_incident"]["incident_id"] == existing_incident
            assert lease.inspect().state == "response_received"
        finally:
            monkeypatch.setattr(health_module, "atomic_json", real_atomic_json)
            await client.aclose()

        replay_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        replay = ProviderCircuit(replay_client, health_path, clock=clock)
        replay_lease = replay.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            result = await replay.evaluate_async(
                STATE, QUESTIONS, identity=request_identity,
                decision_lease=replay_lease)
        finally:
            await replay_client.aclose()
        assert result.identity is request_identity
        assert len(calls) == 1
        assert health_path.read_bytes() == committed
        assert replay.state["attempts"] == 0
        assert replay.state["previous_incident"]["incident_id"] == existing_incident

    asyncio.run(exercise())


def test_restart_rejects_coherent_external_incident_and_budget_rewrite(tmp_path):
    from jev_factorio.operational_safety import atomic_json, SafetyStateError

    calls = []

    async def exercise():
        entered, return_response = asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            entered.set()
            await return_response.wait()
            return httpx.Response(200, json={"answers": _answers()}, request=request)

        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        request_identity = _identity("external-budget-change")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        task = asyncio.create_task(circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity, decision_lease=lease))
        try:
            await entered.wait()
            current = json.loads(health_path.read_text())
            flight = current["in_flight"]
            changed_before = deepcopy(flight["decision_before"])
            changed_before.update(
                phase="cooldown", category="rate_limit", attempts=1,
                next_probe_at=900.0, incident_id="external-incident",
                first_failure_at=950.0, budget_category="rate_limit", budget_limit=8,
            )
            flight["healthy_start"] = False
            changed_current = circuit._decision_reserved_state(changed_before, flight)
            # The modified incident/budget and its reservation are internally
            # coherent, but disagree with the immutable WAL-bound old baseline.
            atomic_json(health_path, changed_current)
            changed_bytes = health_path.read_bytes()
            return_response.set()
            with pytest.raises(SafetyStateError, match="pre-attempt health digest"):
                await task
            assert health_path.read_bytes() == changed_bytes
            assert len(calls) == 1
            assert lease.inspect().state == "response_received"
            with pytest.raises(SafetyStateError, match="pre-attempt health digest"):
                ProviderCircuit(client, health_path, clock=Clock())
        finally:
            return_response.set()
            await client.aclose()

    asyncio.run(exercise())


def test_authorized_recovery_v1_reproduction_reaches_wal_response_and_restart(tmp_path):
    """Exercise the original public-call failure before checking restart recovery."""
    from jev_factorio.operational_safety import atomic_json

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncMockJevClient()
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        circuit.state.update(
            phase="exhausted", incident_id=str(uuid4()), category="service_network",
            attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
            budget_category="service_network", budget_limit=8,
        )
        circuit._save()
        authorization = {
            "request_id": str(uuid4()),
            "incident_id": circuit.state["incident_id"],
            "evidence_sha256": "a" * 64,
        }
        atomic_json(tmp_path / "provider-authorization.json", authorization)
        request_identity = _identity("v1-authorized-recovery")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity,
        )

        evaluation_error = None
        result = None
        try:
            result = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=lease,
            )
        except SafetyStateError as error:
            evaluation_error = str(error)
        assert lease.inspect().state == "response_received"

        restart_error = None
        try:
            ProviderCircuit(client, health_path, clock=Clock())
        except SafetyStateError as error:
            restart_error = str(error)
        assert evaluation_error is None, (
            f"public evaluate failed after a durable WAL response: {evaluation_error}; "
            f"restart_error={restart_error}"
        )
        assert restart_error is None
        assert result is not None

    asyncio.run(exercise())


def test_authorized_recovery_binds_post_admission_health_and_replays_exact_result(tmp_path):
    from jev_factorio.operational_safety import atomic_json
    from jev_factorio.provider_decision_wal import canonical_sha256

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        circuit.state.update(
            phase="exhausted", incident_id=str(uuid4()), category="service_network",
            attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
            budget_category="service_network", budget_limit=8,
        )
        circuit._save()
        prior_incident = deepcopy(circuit.state)
        authorization = {
            "request_id": str(uuid4()),
            "incident_id": circuit.state["incident_id"],
            "evidence_sha256": "a" * 64,
        }
        atomic_json(tmp_path / "provider-authorization.json", authorization)
        request_identity = _identity("authorized-recovery")
        restarted_client = None

        try:
            lease = circuit.prepare_decision_lease(
                wal, STATE, QUESTIONS, identity=request_identity,
            )
            authorized_state = json.loads(health_path.read_text(encoding="utf-8"))
            history = json.loads((tmp_path / (
                "provider-authorized-" + authorization["request_id"] + ".json"
            )).read_text(encoding="utf-8"))
            assert history == {"request": authorization, "previous_state": prior_incident}
            assert authorized_state["incident_id"] == prior_incident["incident_id"]
            assert authorized_state["first_failure_at"] == prior_incident["first_failure_at"]
            assert authorized_state["category"] == prior_incident["category"]
            assert authorized_state["authorization_id"] == authorization["request_id"]
            assert authorized_state["attempts"] == 0
            assert authorized_state["budget_category"] is None
            assert authorized_state["budget_limit"] is None
            assert lease.health_state_sha256 == canonical_sha256(authorized_state)

            result = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=lease,
            )
            replay = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=lease,
            )
            assert result == replay
            assert lease.inspect().state == "response_received"
            assert circuit.state["in_flight"] is None
            assert circuit.state["previous_incident"]["incident_id"] == prior_incident["incident_id"]
            assert len(calls) == 1

            restarted_client = AsyncJevClient(
                api_key="offline", transport=httpx.MockTransport(handler),
            )
            restarted = ProviderCircuit(restarted_client, health_path, clock=Clock())
            restarted_lease = restarted.prepare_decision_lease(
                wal, STATE, QUESTIONS, identity=request_identity,
            )
            recovered = await restarted.evaluate_async(
                STATE, QUESTIONS, identity=request_identity,
                decision_lease=restarted_lease,
            )
            assert recovered == result
            assert restarted.state["authorization_id"] == authorization["request_id"]
            assert len(calls) == 1
        finally:
            await client.aclose()
            if restarted_client is not None:
                await restarted_client.aclose()

    asyncio.run(exercise())


def test_new_authorization_cannot_rebind_an_already_prepared_lease(tmp_path):
    from jev_factorio.operational_safety import atomic_json

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        circuit.state.update(
            phase="exhausted", incident_id=str(uuid4()), category="service_network",
            attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
            budget_category="service_network", budget_limit=8,
        )
        circuit._save()
        prior_incident = deepcopy(circuit.state)
        request_identity = _identity("authorization-after-lease")
        old_lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity,
        )
        old_health = health_path.read_bytes()
        authorization = {
            "request_id": str(uuid4()),
            "incident_id": prior_incident["incident_id"],
            "evidence_sha256": "b" * 64,
        }
        atomic_json(tmp_path / "provider-authorization.json", authorization)

        try:
            with pytest.raises(SafetyStateError, match="changed after decision lease"):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=old_lease,
                )
            assert old_lease.inspect_optional() is None
            assert health_path.read_bytes() == old_health
            assert json.loads(wal.path.read_text())["records"] == []
            assert calls == []

            retry = circuit.prepare_decision_lease(
                wal, STATE, QUESTIONS, identity=request_identity,
            )
            admitted = json.loads(health_path.read_text())
            assert retry.health_state_sha256 == wal_module.canonical_sha256(admitted)
            assert admitted["authorization_id"] == authorization["request_id"]
            result = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=retry,
            )
            assert result.answers["ready"]["choice"] == "yes"
            assert len(calls) == 1
            history = json.loads((tmp_path / (
                "provider-authorized-" + authorization["request_id"] + ".json"
            )).read_text())
            assert history == {"request": authorization, "previous_state": prior_incident}
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_authorization_history_write_failure_leaves_health_and_wal_untouched(
        tmp_path, monkeypatch):
    from jev_factorio import provider_health as health_module
    from jev_factorio.operational_safety import atomic_json

    health_path = tmp_path / "health.json"
    wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
    client = AsyncMockJevClient()
    circuit = ProviderCircuit(client, health_path, clock=Clock())
    circuit.state.update(
        phase="exhausted", incident_id=str(uuid4()), category="service_network",
        attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
        budget_category="service_network", budget_limit=8,
    )
    circuit._save()
    prior = deepcopy(circuit.state)
    health_before = health_path.read_bytes()
    authorization = {
        "request_id": str(uuid4()), "incident_id": prior["incident_id"],
        "evidence_sha256": "c" * 64,
    }
    atomic_json(tmp_path / "provider-authorization.json", authorization)
    history_path = tmp_path / ("provider-authorized-" + authorization["request_id"] + ".json")
    real_atomic_json = health_module.atomic_json

    def fail_history(path, value):
        if Path(path) == history_path:
            raise OSError("injected authorization-history write failure")
        real_atomic_json(path, value)

    monkeypatch.setattr(health_module, "atomic_json", fail_history)
    with pytest.raises(OSError, match="authorization-history write failure"):
        circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=_identity("auth-history-write-failure"),
        )
    assert health_path.read_bytes() == health_before
    assert not history_path.exists()
    assert json.loads(wal.path.read_text())["records"] == []
    assert circuit.state == prior


def test_authorization_history_survives_health_preinstall_failure_and_restart_retry(
        tmp_path, monkeypatch):
    from jev_factorio import provider_health as health_module
    from jev_factorio.operational_safety import atomic_json

    health_path = tmp_path / "health.json"
    wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
    client = AsyncMockJevClient()
    circuit = ProviderCircuit(client, health_path, clock=Clock())
    circuit.state.update(
        phase="exhausted", incident_id=str(uuid4()), category="service_network",
        attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
        budget_category="service_network", budget_limit=8,
    )
    circuit._save()
    prior = deepcopy(circuit.state)
    health_before = health_path.read_bytes()
    authorization = {
        "request_id": str(uuid4()), "incident_id": prior["incident_id"],
        "evidence_sha256": "d" * 64,
    }
    atomic_json(tmp_path / "provider-authorization.json", authorization)
    history_path = tmp_path / ("provider-authorized-" + authorization["request_id"] + ".json")
    real_atomic_json = health_module.atomic_json

    def fail_health_before_install(path, value):
        if Path(path) == health_path:
            raise OSError("injected health preinstall failure")
        real_atomic_json(path, value)

    monkeypatch.setattr(health_module, "atomic_json", fail_health_before_install)
    with pytest.raises(OSError, match="health preinstall failure"):
        circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=_identity("auth-preinstall-failure"),
        )
    assert health_path.read_bytes() == health_before
    assert history_path.exists()
    assert json.loads(history_path.read_text()) == {"request": authorization, "previous_state": prior}
    assert json.loads(wal.path.read_text())["records"] == []

    monkeypatch.setattr(health_module, "atomic_json", real_atomic_json)
    restarted = ProviderCircuit(client, health_path, clock=Clock())
    lease = restarted.prepare_decision_lease(
        wal, STATE, QUESTIONS, identity=_identity("auth-preinstall-retry"),
    )
    admitted = json.loads(health_path.read_text())
    assert admitted["authorization_id"] == authorization["request_id"]
    assert admitted["attempts"] == 0
    assert admitted["incident_id"] == prior["incident_id"]
    assert lease.health_state_sha256 == wal_module.canonical_sha256(admitted)
    assert json.loads(history_path.read_text()) == {"request": authorization, "previous_state": prior}
    assert json.loads(wal.path.read_text())["records"] == []


def test_authorization_postinstall_error_is_recovered_without_budget_reset(tmp_path, monkeypatch):
    from jev_factorio import provider_health as health_module
    from jev_factorio.operational_safety import atomic_json

    health_path = tmp_path / "health.json"
    wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
    client = AsyncMockJevClient()
    circuit = ProviderCircuit(client, health_path, clock=Clock())
    circuit.state.update(
        phase="exhausted", incident_id=str(uuid4()), category="service_network",
        attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
        budget_category="service_network", budget_limit=8,
    )
    circuit._save()
    prior = deepcopy(circuit.state)
    authorization = {
        "request_id": str(uuid4()), "incident_id": prior["incident_id"],
        "evidence_sha256": "e" * 64,
    }
    atomic_json(tmp_path / "provider-authorization.json", authorization)
    history_path = tmp_path / ("provider-authorized-" + authorization["request_id"] + ".json")
    real_atomic_json = health_module.atomic_json

    def install_then_raise(path, value):
        real_atomic_json(path, value)
        if Path(path) == health_path:
            raise OSError("injected postinstall health fsync ambiguity")

    monkeypatch.setattr(health_module, "atomic_json", install_then_raise)
    with pytest.raises(OSError, match="postinstall health fsync ambiguity"):
        circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=_identity("auth-postinstall-error"),
        )
    committed = health_path.read_bytes()
    history_bytes = history_path.read_bytes()
    committed_state = json.loads(committed)
    assert committed_state["authorization_id"] == authorization["request_id"]
    assert committed_state["attempts"] == 0
    assert json.loads(history_bytes) == {"request": authorization, "previous_state": prior}

    monkeypatch.setattr(health_module, "atomic_json", real_atomic_json)
    restarted = ProviderCircuit(client, health_path, clock=Clock())
    lease = restarted.prepare_decision_lease(
        wal, STATE, QUESTIONS, identity=_identity("auth-postinstall-restart"),
    )
    assert health_path.read_bytes() == committed
    assert history_path.read_bytes() == history_bytes
    assert lease.health_state_sha256 == wal_module.canonical_sha256(committed_state)
    assert json.loads(wal.path.read_text())["records"] == []


def test_authorized_not_sent_admission_restores_post_authorization_budget(tmp_path):
    from jev_factorio.operational_safety import atomic_json

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        circuit.state.update(
            phase="exhausted", incident_id=str(uuid4()), category="service_network",
            attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
            budget_category="service_network", budget_limit=8,
        )
        circuit._save()
        prior_incident = deepcopy(circuit.state)
        authorization = {
            "request_id": str(uuid4()), "incident_id": prior_incident["incident_id"],
            "evidence_sha256": "2" * 64,
        }
        atomic_json(tmp_path / "provider-authorization.json", authorization)
        request_identity = _identity("authorized-deadline")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity,
        )
        admitted = deepcopy(circuit.state)
        history_path = tmp_path / ("provider-authorized-" + authorization["request_id"] + ".json")
        history_bytes = history_path.read_bytes()
        try:
            with pytest.raises(AsyncProviderDeadlineExceeded):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease,
                    deadline=time.monotonic() - 1,
                )
            assert lease.inspect().state == "failed"
            assert lease.inspect().phase == "not_sent"
            assert lease.inspect().error_category == "local_admission"
            assert circuit.state == admitted
            assert json.loads(health_path.read_text()) == admitted
            assert history_path.read_bytes() == history_bytes
            assert calls == []
            with pytest.raises(AsyncProviderLocalError):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=request_identity, decision_lease=lease,
                )
            assert json.loads(health_path.read_text()) == admitted
            assert calls == []

            retry_identity = _identity("authorized-after-deadline")
            retry_lease = circuit.prepare_decision_lease(
                wal, STATE, QUESTIONS, identity=retry_identity,
            )
            assert json.loads(health_path.read_text()) == admitted
            result = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=retry_identity, decision_lease=retry_lease,
            )
            assert result.answers["ready"]["choice"] == "yes"
            assert retry_lease.inspect().state == "response_received"
            assert circuit.state["authorization_id"] == authorization["request_id"]
            assert history_path.read_bytes() == history_bytes
            assert json.loads(history_bytes) == {
                "request": authorization, "previous_state": prior_incident,
            }
            assert len(calls) == 1
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_new_authorization_does_not_supersede_may_have_been_sent_flight(tmp_path):
    from jev_factorio.operational_safety import atomic_json

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncMockJevClient()
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        circuit.state.update(
            phase="cooldown", incident_id=str(uuid4()), category="service_network",
            attempts=2, first_failure_at=900.0, next_probe_at=1005.0,
            budget_category="service_network", budget_limit=8,
        )
        circuit._save()
        identity = _identity("authorization-after-may")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=identity,
        )
        before = deepcopy(circuit.state)
        lease.reserve()
        flight = {
            "request_id": identity.request_id,
            "started_at": circuit.clock(),
            "healthy_start": False,
            "decision_binding": lease.binding(),
            "authorization_sha256": circuit._decision_auth_digest(),
        }
        circuit.state = circuit._decision_reserved_state(before, flight)
        circuit._save_decision_state(before)
        lease.mark_may_have_been_sent()
        request_bytes = health_path.read_bytes()
        wal_bytes = wal.path.read_bytes()
        authorization = {
            "request_id": str(uuid4()),
            "incident_id": before["incident_id"],
            "evidence_sha256": "3" * 64,
        }
        atomic_json(tmp_path / "provider-authorization.json", authorization)
        history_path = tmp_path / ("provider-authorized-" + authorization["request_id"] + ".json")

        restarted = ProviderCircuit(client, health_path, clock=Clock())
        resumed = restarted.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=identity,
        )
        try:
            with pytest.raises(SafetyStateError, match="authorization changed during a decision"):
                await restarted.evaluate_async(
                    STATE, QUESTIONS, identity=identity, decision_lease=resumed,
                )
            assert health_path.read_bytes() == request_bytes
            assert wal.path.read_bytes() == wal_bytes
            assert resumed.inspect().state == "may_have_been_sent"
            assert not history_path.exists()
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_authorized_response_survives_release_cancellation_and_replays_once(tmp_path):
    from jev_factorio.operational_safety import atomic_json

    calls = []

    async def exercise():
        entered, answer, release_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            entered.set()
            await answer.wait()
            return httpx.Response(200, json={"answers": _answers()}, request=request)

        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        circuit.state.update(
            phase="exhausted", incident_id=str(uuid4()), category="service_network",
            attempts=8, first_failure_at=900.0, next_probe_at=1100.0,
            budget_category="service_network", budget_limit=8,
        )
        circuit._save()
        prior_incident = deepcopy(circuit.state)
        authorization = {
            "request_id": str(uuid4()), "incident_id": prior_incident["incident_id"],
            "evidence_sha256": "f" * 64,
        }
        atomic_json(tmp_path / "provider-authorization.json", authorization)
        request_identity = _identity("authorized-release-cancel")
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity,
        )
        history_path = tmp_path / ("provider-authorized-" + authorization["request_id"] + ".json")
        history_bytes = history_path.read_bytes()
        original_release = client._release

        async def observe_release():
            release_started.set()
            return await original_release()

        client._release = observe_release
        task = asyncio.create_task(circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity, decision_lease=lease))
        await entered.wait()
        await client._condition.acquire()
        try:
            answer.set()
            await asyncio.wait_for(release_started.wait(), timeout=2)
            assert lease.inspect().state == "response_received"
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            client._condition.release()
        with pytest.raises(AsyncProviderCancelled) as cancelled:
            await task
        assert cancelled.value.delivery_phase == "response_received"
        assert lease.inspect().state == "response_received"
        assert json.loads(health_path.read_text())["in_flight"] is not None
        assert json.loads(health_path.read_text())["authorization_id"] == authorization["request_id"]
        assert json.loads(history_bytes) == {
            "request": authorization, "previous_state": prior_incident,
        }
        assert len(calls) == 1
        await client.aclose()

        replay_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler),
        )
        replay = ProviderCircuit(replay_client, health_path, clock=Clock())
        replay_lease = replay.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity,
        )
        try:
            result = await replay.evaluate_async(
                STATE, QUESTIONS, identity=request_identity,
                decision_lease=replay_lease,
            )
        finally:
            await replay_client.aclose()
        assert result.answers["ready"]["choice"] == "yes"
        assert replay.state["authorization_id"] == authorization["request_id"]
        assert replay.state["previous_incident"]["incident_id"] == prior_incident["incident_id"]
        assert history_path.read_bytes() == history_bytes
        assert len(calls) == 1

    asyncio.run(exercise())


def test_operator_authorization_archives_terminal_wal_failure_before_new_probe(tmp_path):
    from jev_factorio.operational_safety import atomic_json

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "offline rate limit"}, request=request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        failed_identity = _identity("before-operator-probe")
        failed_lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=failed_identity,
        )
        try:
            with pytest.raises(ProviderBlocked):
                await circuit.evaluate_async(
                    STATE, QUESTIONS, identity=failed_identity,
                    decision_lease=failed_lease,
                )
        finally:
            await client.aclose()

        assert failed_lease.inspect().state == "failed"
        assert circuit.state["decision_outcome"]["state"] == "failed"
        prior_failure = deepcopy(circuit.state)
        authorization = {
            "request_id": str(uuid4()),
            "incident_id": prior_failure["incident_id"],
            "evidence_sha256": "1" * 64,
        }
        atomic_json(tmp_path / "provider-authorization.json", authorization)

        retry_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler),
        )
        retry_circuit = ProviderCircuit(retry_client, health_path, clock=Clock())
        alternate_wal = ProviderDecisionWAL.initialize(tmp_path / "alternate-wal.json")
        before_wrong_wal = health_path.read_bytes()
        with pytest.raises(SafetyStateError, match="bound to another WAL"):
            retry_circuit.prepare_decision_lease(
                alternate_wal, STATE, QUESTIONS,
                identity=_identity("wrong-wal-operator-probe"),
            )
        assert health_path.read_bytes() == before_wrong_wal
        assert json.loads(alternate_wal.path.read_text())["records"] == []

        retry_identity = _identity("after-operator-probe")
        retry_lease = retry_circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=retry_identity,
        )
        authorized_state = json.loads(health_path.read_text())
        history_path = tmp_path / ("provider-authorized-" + authorization["request_id"] + ".json")
        history = json.loads(history_path.read_text())
        assert history == {"request": authorization, "previous_state": prior_failure}
        assert "decision_outcome" not in authorized_state
        assert authorized_state["incident_id"] == prior_failure["incident_id"]
        assert authorized_state["authorization_id"] == authorization["request_id"]
        assert retry_lease.health_state_sha256 == wal_module.canonical_sha256(authorized_state)
        try:
            result = await retry_circuit.evaluate_async(
                STATE, QUESTIONS, identity=retry_identity, decision_lease=retry_lease,
            )
        finally:
            await retry_client.aclose()
        assert result.answers["ready"]["choice"] == "yes"
        assert failed_lease.inspect().error_category == "rate_limit"
        assert len(calls) == 2

    asyncio.run(exercise())


def test_completed_health_outcome_detects_later_incident_or_cooldown_change(tmp_path):
    from jev_factorio.operational_safety import atomic_json, SafetyStateError

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        request_identity = _identity("post-completion-external-change")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path, clock=Clock())
        lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=request_identity)
        try:
            await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity, decision_lease=lease)
        finally:
            await client.aclose()
        assert len(calls) == 1

        changed = json.loads(health_path.read_text())
        changed.update(
            phase="exhausted", category="rate_limit", attempts=8,
            next_probe_at=5000.0, incident_id="independent-post-result-incident",
            first_failure_at=1200.0, budget_category="rate_limit", budget_limit=8,
        )
        atomic_json(health_path, changed)
        preserved = health_path.read_bytes()

        replay_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SafetyStateError, match="completed health state changed"):
                ProviderCircuit(replay_client, health_path, clock=Clock())
            assert health_path.read_bytes() == preserved
            assert len(calls) == 1
        finally:
            await replay_client.aclose()

    asyncio.run(exercise())


def test_unconsumed_response_blocks_a_new_identity_on_the_health_sidecar(tmp_path):
    from jev_factorio.provider_health import SafetyStateError

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": _answers()}, request=request)

    async def exercise():
        health_path = tmp_path / "health.json"
        wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        circuit = ProviderCircuit(client, health_path)
        first_identity = _identity("unconsumed-first")
        first_lease = circuit.prepare_decision_lease(
            wal, STATE, QUESTIONS, identity=first_identity)
        try:
            await circuit.evaluate_async(
                STATE, QUESTIONS, identity=first_identity, decision_lease=first_lease)
            preserved = health_path.read_bytes()
            with pytest.raises(SafetyStateError, match="must be replayed or consumed"):
                circuit.prepare_decision_lease(
                    wal, STATE, QUESTIONS, identity=_identity("unconsumed-second"))
            assert health_path.read_bytes() == preserved
            assert first_lease.inspect().state == "response_received"
            assert len(calls) == 1
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_two_circuit_instances_cannot_dispatch_against_one_health_owner(tmp_path):
    first_calls, second_calls = [], []

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def first_handler(request):
            first_calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"answers": _answers()}, request=request)

        def second_handler(request):
            second_calls.append(request)
            return httpx.Response(200, json={"answers": _answers()}, request=request)

        health_path = tmp_path / "health.json"
        first_wal = ProviderDecisionWAL.initialize(tmp_path / "first-wal.json")
        second_wal = ProviderDecisionWAL.initialize(tmp_path / "second-wal.json")
        first_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(first_handler))
        second_client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(second_handler))
        first = ProviderCircuit(first_client, health_path)
        second = ProviderCircuit(second_client, health_path)
        first_identity, second_identity = _identity("owner-first"), _identity("owner-second")
        first_lease = first.prepare_decision_lease(
            first_wal, STATE, QUESTIONS, identity=first_identity)
        second_lease = second.prepare_decision_lease(
            second_wal, STATE, QUESTIONS, identity=second_identity)
        first_task = asyncio.create_task(first.evaluate_async(
            STATE, QUESTIONS, identity=first_identity, decision_lease=first_lease))
        try:
            await entered.wait()
            owner_bytes = health_path.read_bytes()
            with pytest.raises(WALBusyError):
                await second.evaluate_async(
                    STATE, QUESTIONS, identity=second_identity,
                    decision_lease=second_lease)
            assert health_path.read_bytes() == owner_bytes
            assert first_lease.inspect().state == "may_have_been_sent"
            assert second_lease.inspect_optional() is None
            assert len(first_calls) == 1
            assert second_calls == []
            release.set()
            result = await first_task
            assert result.identity is first_identity
            assert first_lease.inspect().state == "response_received"
            assert first.state["decision_outcome"]["identity"]["request_id"] == (
                first_identity.request_id)
            assert len(first_calls) == 1 and second_calls == []
            with pytest.raises(SafetyStateError, match="another WAL"):
                await second.evaluate_async(
                    STATE, QUESTIONS, identity=second_identity,
                    decision_lease=second_lease)
            assert second_lease.inspect_optional() is None
            assert second_calls == []
        finally:
            release.set()
            if not first_task.done():
                await first_task
            await first_client.aclose()
            await second_client.aclose()

    asyncio.run(exercise())


def test_other_process_health_owner_lock_blocks_before_lease_mutation(tmp_path):
    calls = []
    health_path = tmp_path / "health.json"
    wal = ProviderDecisionWAL.initialize(tmp_path / "wal.json")
    client = AsyncJevClient(
        api_key="offline", transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(
                200, json={"answers": _answers()}, request=request)))
    circuit = ProviderCircuit(client, health_path)
    identity = _identity("other-process-owner")
    lease = circuit.prepare_decision_lease(wal, STATE, QUESTIONS, identity=identity)
    source = (
        "from pathlib import Path\n"
        "import sys\n"
        "from jev_factorio.provider_decision_wal import _writer_lock\n"
        "with _writer_lock(Path(sys.argv[1])):\n"
        " print('LOCKED', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", source, str(health_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        original_state = deepcopy(circuit.state)
        with pytest.raises(WALBusyError):
            asyncio.run(circuit.evaluate_async(
                STATE, QUESTIONS, identity=identity, decision_lease=lease))
        assert circuit.state == original_state
        assert not health_path.exists()
        assert lease.inspect_optional() is None
        assert calls == []
    finally:
        if process.poll() is None:
            process.stdin.write("release\n")
            process.stdin.flush()
        process.wait(timeout=5)
        asyncio.run(client.aclose())
