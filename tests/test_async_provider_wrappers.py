"""Offline semantic parity tests for the explicit async health/trace adapters."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time

import httpx
import pytest

from jev_factorio.async_provider import (
    AsyncProviderCancelled,
    AsyncProviderQueueFull,
    AsyncProviderTimeout,
    MAY_HAVE_BEEN_SENT,
    NOT_SENT,
    RESPONSE_RECEIVED,
    RequestIdentity,
    make_result,
)
from jev_factorio.causal_trace import CausalTrace, TracedClient
from jev_factorio.jev_client import (
    AsyncCloudflareJevClient,
    AsyncJevClient,
    AsyncMockJevClient,
    AsyncTracedClient,
    MockJevClient,
)
from jev_factorio.provider_health import ProviderBlocked, ProviderCircuit
from jev_factorio.research_log import ResearchLogError


STATE = {"tick": 42, "inventory": {"coal": 3}}
QUESTIONS = {"ready": {"type": "choice", "criteria": {"yes": "ready"}}}


def identity(label: str = "one") -> RequestIdentity:
    return RequestIdentity(
        session_id="session-" + label,
        actor_id="actor-" + label,
        observation_id="observation-" + label,
        decision_id="decision-" + label,
        request_id="request-" + label,
    )


def run(coro):
    return asyncio.run(coro)


def answers():
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


class ControlledAsyncClient:
    model = "controlled-model"
    answer_quantum = 0
    is_mock = True

    def __init__(self, error=None, result=None):
        self.error, self.result = error, result
        self.calls = []

    async def evaluate(self, state, questions, *, identity, deadline=None):
        self.calls.append(identity)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return await AsyncMockJevClient().evaluate(
            state, questions, identity=identity, deadline=deadline)


@pytest.mark.parametrize("provider", ["direct", "cloudflare", "mock"])
def test_async_circuit_preserves_immutable_result_for_each_provider(tmp_path, provider):
    async def exercise():
        if provider == "direct":
            def handler(request):
                return httpx.Response(200, json={
                    "answers": answers(), "usage": {"input_tokens": 4},
                    "model": "direct-resolved",
                }, request=request)

            client = AsyncJevClient(
                api_key="offline-only", base_url="https://direct.invalid",
                model="direct-requested", transport=httpx.MockTransport(handler),
            )
        elif provider == "cloudflare":
            def handler(request):
                return httpx.Response(200, json={
                    "success": True,
                    "result": {"answers": answers(), "usage": {"input_tokens": 5},
                               "model": "cloudflare-resolved"},
                }, request=request)

            client = AsyncCloudflareJevClient(
                "offline-account", "offline-only", model="cloudflare-requested",
                transport=httpx.MockTransport(handler),
            )
        else:
            client = AsyncMockJevClient()

        circuit = ProviderCircuit(client, tmp_path / f"{provider}.json")
        request_identity = identity(provider)
        try:
            result = await circuit.evaluate_async(
                STATE, QUESTIONS, identity=request_identity,
                deadline=time.monotonic() + 5,
            )
        finally:
            await client.aclose()

        assert result.identity is request_identity
        assert result.answers["ready"]["choice"] == "yes"
        assert result.requested_model == client.model
        assert getattr(client, "answer_quantum", 0) == (0.01 if provider == "direct" else 0)
        assert circuit.state["phase"] == "healthy"
        assert circuit.state["in_flight"] is None
        assert not hasattr(client, "last_usage")
        assert not hasattr(client, "last_model")
        with pytest.raises(TypeError):
            result.answers["ready"]["choice"] = "no"

    run(exercise())


def test_async_circuit_classifies_rate_limit_and_does_not_replay_during_cooldown(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "12"}, request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline-only", transport=httpx.MockTransport(handler))
        clock = Clock()
        circuit = ProviderCircuit(client, tmp_path / "provider.json", clock=clock)
        request_identity = identity("rate-limit")
        try:
            with pytest.raises(ProviderBlocked) as blocked:
                await circuit.evaluate_async(STATE, QUESTIONS, identity=request_identity)
            assert blocked.value.called
            assert blocked.value.state["category"] == "rate_limit"
            assert blocked.value.identity is request_identity
            assert blocked.value.delivery_phase == RESPONSE_RECEIVED
            assert blocked.value.state["next_probe_at"] == 1012.0

            with pytest.raises(ProviderBlocked) as suppressed:
                await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("retry"))
            assert not suppressed.value.called
            assert suppressed.value.identity.request_id == "request-retry"
            assert suppressed.value.delivery_phase == NOT_SENT
            assert len(calls) == 1
        finally:
            await client.aclose()

    run(exercise())


def test_async_circuit_recovers_same_provider_and_retains_incident_history(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={
            "answers": answers(), "usage": {"input_tokens": 1},
            "model": "recovered-model",
        }, request=request)

    async def exercise():
        client = AsyncJevClient(api_key="offline-only", transport=httpx.MockTransport(handler))
        clock = Clock()
        circuit = ProviderCircuit(client, tmp_path / "recovery.json", clock=clock)
        try:
            with pytest.raises(ProviderBlocked) as failed:
                await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("first"))
            incident_id = failed.value.state["incident_id"]
            assert failed.value.state["category"] == "service_network"
            clock.now += 60
            result = await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("recovery"))
            assert result.identity == identity("recovery")
            assert circuit.state["phase"] == "healthy"
            assert circuit.state["previous_incident"]["incident_id"] == incident_id
            assert circuit.state["last_recovery_at"] == clock.now
            assert len(calls) == 2
        finally:
            await client.aclose()

    run(exercise())


def test_async_local_not_sent_failure_releases_reservation_without_health_charge(tmp_path):
    async def exercise():
        request_identity = identity("queue-full")
        client = ControlledAsyncClient(
            error=AsyncProviderQueueFull("bounded queue full", request_identity))
        path = tmp_path / "provider.json"
        circuit = ProviderCircuit(client, path)
        with pytest.raises(AsyncProviderQueueFull) as raised:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=request_identity)
        assert raised.value.identity is request_identity
        assert raised.value.delivery_phase == NOT_SENT
        assert client.calls == [request_identity]
        assert circuit.state["phase"] == "healthy"
        assert circuit.state["in_flight"] is None
        assert json.loads(path.read_text())["in_flight"] is None

        client.error = None
        result = await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("next"))
        assert result.identity == identity("next")
        assert circuit.state["phase"] == "healthy"
        assert circuit.state["in_flight"] is None

    run(exercise())


def test_async_circuit_rejects_overlapping_call_before_reserving_or_reconciling(tmp_path):
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        class GatedClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                started.set()
                await release.wait()
                return await AsyncMockJevClient().evaluate(
                    state, questions, identity=identity, deadline=deadline)

        client = GatedClient()
        circuit = ProviderCircuit(client, tmp_path / "one-flight.json")
        first_identity = identity("first-active")
        first = asyncio.create_task(circuit.evaluate_async(
            STATE, QUESTIONS, identity=first_identity))
        try:
            await started.wait()
            second_identity = identity("second-overlap")
            with pytest.raises(AsyncProviderQueueFull) as rejected:
                await circuit.evaluate_async(STATE, QUESTIONS, identity=second_identity)
            assert rejected.value.identity is second_identity
            assert rejected.value.delivery_phase == NOT_SENT
            assert len(client.calls) == 1
            assert circuit.state["in_flight"] is not None
            release.set()
            result = await first
            assert result.identity is first_identity
            assert circuit.state["phase"] == "healthy"
            assert circuit.state["in_flight"] is None
        finally:
            release.set()
            if not first.done():
                await asyncio.gather(first, return_exceptions=True)

    run(exercise())


def test_sync_entry_rejects_async_provider_before_metadata_or_state_changes(tmp_path):
    client = ControlledAsyncClient()
    client.last_usage, client.last_model = {"stale": 1}, "stale-model"
    path = tmp_path / "idle-async.json"
    seed = ProviderCircuit(client, path)
    seed.state["in_flight"] = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "started_at": 1000.0,
        "healthy_start": True,
    }
    path.write_text(json.dumps(seed.state))
    circuit = ProviderCircuit(client, path)
    before = deepcopy(circuit.state)
    stored_before = path.read_bytes()
    with pytest.raises(TypeError, match="use evaluate_async"):
        circuit.evaluate(STATE, QUESTIONS)
    assert circuit.state == before
    assert path.read_bytes() == stored_before
    assert client.last_usage == {"stale": 1}
    assert client.last_model == "stale-model"
    assert client.calls == []


def test_sync_entry_cannot_reconcile_active_async_reservation(tmp_path):
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        class GatedClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                started.set()
                await release.wait()
                return await AsyncMockJevClient().evaluate(
                    state, questions, identity=identity, deadline=deadline)

        client = GatedClient()
        client.last_usage, client.last_model = {"stale": 2}, "stale-model"
        path = tmp_path / "active-async.json"
        circuit = ProviderCircuit(client, path)
        request_identity = identity("active-async")
        task = asyncio.create_task(circuit.evaluate_async(
            STATE, QUESTIONS, identity=request_identity))
        try:
            await started.wait()
            before = deepcopy(circuit.state)
            path_before = path.read_bytes()
            with pytest.raises(RuntimeError, match="active async evaluation"):
                circuit.evaluate(STATE, QUESTIONS)
            assert circuit.state == before
            assert path.read_bytes() == path_before
            assert client.last_usage == {"stale": 2}
            assert client.last_model == "stale-model"
            assert client.calls == [request_identity]
            release.set()
            assert (await task).identity is request_identity
            assert circuit.state["phase"] == "healthy"
        finally:
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    run(exercise())


def test_async_not_sent_probe_preserves_existing_incident_budget(tmp_path):
    async def exercise():
        clock = Clock()
        first_identity = identity("probe-initial")
        client = ControlledAsyncClient(error=AsyncProviderTimeout(
            first_identity, MAY_HAVE_BEEN_SENT))
        circuit = ProviderCircuit(client, tmp_path / "probe.json", clock=clock)
        with pytest.raises(ProviderBlocked) as initial:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=first_identity)
        before = dict(circuit.state)
        clock.now += 60
        local_identity = identity("probe-queue-full")
        client.error = AsyncProviderQueueFull("bounded queue full", local_identity)
        with pytest.raises(AsyncProviderQueueFull):
            await circuit.evaluate_async(STATE, QUESTIONS, identity=local_identity)
        assert circuit.state == before
        assert circuit.state["incident_id"] == initial.value.state["incident_id"]
        assert circuit.state["attempts"] == 1
        assert circuit.state["in_flight"] is None

        client.error = None
        recovered = await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("probe-recovered"))
        assert recovered.identity.request_id == "request-probe-recovered"
        assert circuit.state["phase"] == "healthy"
        assert circuit.state["previous_incident"]["incident_id"] == before["incident_id"]

    run(exercise())


def test_async_not_sent_cancellation_releases_but_does_not_relabel_remote_health(tmp_path):
    async def exercise():
        request_identity = identity("cancel-before-send")
        client = ControlledAsyncClient(error=AsyncProviderCancelled(request_identity, NOT_SENT))
        path = tmp_path / "cancel-before-send.json"
        circuit = ProviderCircuit(client, path)
        with pytest.raises(AsyncProviderCancelled) as raised:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=request_identity)
        assert raised.value.delivery_phase == NOT_SENT
        assert circuit.state["phase"] == "healthy"
        assert circuit.state["in_flight"] is None
        assert json.loads(path.read_text())["in_flight"] is None

        client.error = None
        result = await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("after-cancel"))
        assert result.identity == identity("after-cancel")
        assert circuit.state["phase"] == "healthy"

    run(exercise())


def test_async_circuit_write_failures_never_duplicate_provider_request(tmp_path, monkeypatch):
    async def exercise():
        request_identity = identity("write-failure")
        client = ControlledAsyncClient()
        path = tmp_path / "write-failure.json"
        circuit = ProviderCircuit(client, path)
        monkeypatch.setattr(circuit, "_save", lambda: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(OSError, match="full"):
            await circuit.evaluate_async(STATE, QUESTIONS, identity=request_identity)
        assert client.calls == []
        assert circuit.state["in_flight"] is not None

        circuit = ProviderCircuit(client, path)
        save = circuit._save

        def fail_success_commit():
            if circuit.state["in_flight"] is None:
                raise OSError("full after response")
            save()

        monkeypatch.setattr(circuit, "_save", fail_success_commit)
        with pytest.raises(OSError, match="full after response"):
            await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("response"))
        assert len(client.calls) == 1
        assert circuit.state["in_flight"] == json.loads(path.read_text())["in_flight"]

        recovered = ProviderCircuit(client, path)
        with pytest.raises(ProviderBlocked) as blocked:
            await recovered.evaluate_async(STATE, QUESTIONS, identity=identity("no-replay"))
        assert not blocked.value.called
        assert blocked.value.state["category"] == "unknown_outcome"
        assert blocked.value.identity.request_id == "request-no-replay"
        assert blocked.value.delivery_phase == NOT_SENT
        assert len(client.calls) == 1

    run(exercise())


@pytest.mark.parametrize("phase", [MAY_HAVE_BEEN_SENT, RESPONSE_RECEIVED])
def test_async_ambiguous_cancellation_keeps_reservation_and_prevents_replay(tmp_path, phase):
    async def exercise():
        request_identity = identity("ambiguous-" + phase)
        client = ControlledAsyncClient(
            error=AsyncProviderCancelled(request_identity, phase))
        path = tmp_path / (phase + ".json")
        circuit = ProviderCircuit(client, path)
        with pytest.raises(AsyncProviderCancelled) as raised:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=request_identity)
        assert raised.value.identity is request_identity
        assert raised.value.delivery_phase == phase
        assert circuit.state["in_flight"] is not None
        assert json.loads(path.read_text())["in_flight"] == circuit.state["in_flight"]

        with pytest.raises(ProviderBlocked) as blocked:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("must-not-replay"))
        assert not blocked.value.called
        assert blocked.value.state["category"] == "unknown_outcome"
        assert blocked.value.identity.request_id == "request-must-not-replay"
        assert blocked.value.delivery_phase == NOT_SENT
        assert len(client.calls) == 1

    run(exercise())


def test_async_transport_timeout_uses_sync_health_budget_and_retains_phase(tmp_path):
    async def exercise():
        request_identity = identity("timeout")
        client = ControlledAsyncClient(
            error=AsyncProviderTimeout(request_identity, MAY_HAVE_BEEN_SENT))
        circuit = ProviderCircuit(client, tmp_path / "provider.json", clock=Clock())
        with pytest.raises(ProviderBlocked) as blocked:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=request_identity)
        assert blocked.value.called
        assert blocked.value.state["category"] == "service_network"
        assert blocked.value.identity is request_identity
        assert blocked.value.delivery_phase == MAY_HAVE_BEEN_SENT
        assert blocked.value.state["in_flight"] is None
        assert len(client.calls) == 1

    run(exercise())


def test_async_invalid_answer_is_schema_failure_not_a_healthy_result(tmp_path):
    async def exercise():
        request_identity = identity("bad-schema")
        invalid = make_result(request_identity, {"unexpected": {"type": "noul", "noul": 0}},
                              None, "controlled-model", "controlled-model", "a" * 64)
        client = ControlledAsyncClient(result=invalid)
        circuit = ProviderCircuit(client, tmp_path / "provider.json", clock=Clock())
        with pytest.raises(ProviderBlocked) as blocked:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=request_identity)
        assert blocked.value.state["category"] == "application_schema"
        assert blocked.value.identity is request_identity
        assert blocked.value.delivery_phase == RESPONSE_RECEIVED
        assert blocked.value.state["in_flight"] is None
        with pytest.raises(ProviderBlocked) as exhausted:
            await circuit.evaluate_async(STATE, QUESTIONS, identity=identity("exhausted"))
        assert not exhausted.value.called
        assert exhausted.value.identity.request_id == "request-exhausted"
        assert exhausted.value.delivery_phase == NOT_SENT
        assert len(client.calls) == 1

    run(exercise())


def _trace_for(request_identity: RequestIdentity):
    sink = Sink()
    trace = CausalTrace(sink, "async-test")
    trace.begin_step()
    trace.decision_id = request_identity.decision_id
    trace.observation_id = request_identity.observation_id
    trace._session_id = request_identity.session_id
    return trace, sink


def test_async_trace_binds_request_and_response_metadata_without_shared_last_fields():
    async def exercise():
        request_identity = identity("trace")
        trace, sink = _trace_for(request_identity)
        client = AsyncTracedClient(AsyncMockJevClient(), trace)
        result = await client.evaluate(STATE, QUESTIONS, identity=request_identity)
        request = next(payload for kind, payload in sink.events if kind == "model_request")
        response = next(payload for kind, payload in sink.events if kind == "model_response")
        expected_identity = {
            "session_id": request_identity.session_id,
            "actor_id": request_identity.actor_id,
            "observation_id": request_identity.observation_id,
            "decision_id": request_identity.decision_id,
            "request_id": request_identity.request_id,
        }
        assert request["provider_identity"] == expected_identity
        assert response["provider_identity"] == expected_identity
        assert response["resolved_model"] == result.resolved_model == result.requested_model
        assert response["usage"] is None
        assert response["answers"]["ready"]["choice"] == "yes"
        assert result.identity is request_identity
        assert not hasattr(client, "last_usage")
        assert not hasattr(client, "last_model")

    run(exercise())


def test_async_trace_failure_keeps_identity_and_delivery_phase():
    async def exercise():
        request_identity = identity("trace-timeout")
        trace, sink = _trace_for(request_identity)
        error = AsyncProviderTimeout(request_identity, MAY_HAVE_BEEN_SENT)
        client = AsyncTracedClient(ControlledAsyncClient(error=error), trace)
        with pytest.raises(AsyncProviderTimeout) as raised:
            await client.evaluate(STATE, QUESTIONS, identity=request_identity)
        response = next(payload for kind, payload in sink.events if kind == "model_response")
        assert raised.value is error
        assert response["status"] == "error"
        assert response["delivery_phase"] == MAY_HAVE_BEEN_SENT
        assert response["provider_identity"]["request_id"] == request_identity.request_id
        assert response["error"]["category"] == "timeout"

    run(exercise())


def test_async_trace_composes_with_persisted_provider_circuit(tmp_path):
    async def exercise():
        request_identity = identity("trace-circuit")
        trace, sink = _trace_for(request_identity)
        circuit = ProviderCircuit(AsyncMockJevClient(), tmp_path / "trace-circuit.json")
        wrapper = AsyncTracedClient(circuit, trace)
        result = await wrapper.evaluate(STATE, QUESTIONS, identity=request_identity)
        response = next(payload for kind, payload in sink.events if kind == "model_response")
        assert result.identity is request_identity
        assert response["provider_identity"]["request_id"] == request_identity.request_id
        assert circuit.state["phase"] == "healthy"
        assert circuit.state["in_flight"] is None

    run(exercise())


def test_async_trace_has_one_fail_fast_slot_per_actor_context():
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        request_identity = identity("trace-active")

        class GatedClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                started.set()
                await release.wait()
                return await AsyncMockJevClient().evaluate(
                    state, questions, identity=identity, deadline=deadline)

        trace, sink = _trace_for(request_identity)
        client = GatedClient()
        wrapper = AsyncTracedClient(client, trace)
        first = asyncio.create_task(wrapper.evaluate(
            STATE, QUESTIONS, identity=request_identity))
        try:
            await started.wait()
            overlapping_identity = RequestIdentity(
                session_id=request_identity.session_id,
                actor_id=request_identity.actor_id,
                observation_id=request_identity.observation_id,
                decision_id=request_identity.decision_id,
                request_id="request-overlap",
            )
            with pytest.raises(AsyncProviderQueueFull) as rejected:
                await wrapper.evaluate(STATE, QUESTIONS, identity=overlapping_identity)
            assert rejected.value.identity is overlapping_identity
            assert rejected.value.delivery_phase == NOT_SENT
            assert client.calls == [request_identity]
            assert sum(kind == "model_request" for kind, _ in sink.events) == 1
            release.set()
            assert (await first).identity is request_identity
            assert sum(kind == "model_response" for kind, _ in sink.events) == 1
        finally:
            release.set()
            if not first.done():
                await asyncio.gather(first, return_exceptions=True)

    run(exercise())


def test_overlapping_trace_contexts_keep_models_usage_and_identity_separate():
    def handler(request):
        payload = request.read()
        data = json.loads(payload)
        model, marker = data["model"], data["state"]["marker"]
        body = {"answers": answers(), "usage": {"marker": marker},
                "model": model + "-resolved"}
        return httpx.Response(200, json=body, request=request)

    async def exercise():
        transport = httpx.MockTransport(handler)
        clients = [
            AsyncJevClient(api_key="offline-only", model="model-a", transport=transport,
                           max_concurrency=2),
            AsyncJevClient(api_key="offline-only", model="model-b", transport=transport,
                           max_concurrency=2),
        ]
        identities = [identity("overlap-a"), identity("overlap-b")]
        traces = [_trace_for(item) for item in identities]
        wrappers = [AsyncTracedClient(client, trace[0])
                    for client, trace in zip(clients, traces)]
        try:
            results = await asyncio.gather(*(
                wrapper.evaluate({"marker": marker}, QUESTIONS, identity=request_identity)
                for wrapper, marker, request_identity in
                zip(wrappers, ("usage-a", "usage-b"), identities)
            ))
        finally:
            await asyncio.gather(*(client.aclose() for client in clients))

        assert [result.identity for result in results] == identities
        assert [result.requested_model for result in results] == ["model-a", "model-b"]
        assert [result.resolved_model for result in results] == ["model-a-resolved", "model-b-resolved"]
        assert [result.usage["marker"] for result in results] == ["usage-a", "usage-b"]
        for (_, sink), request_identity, result in zip(traces, identities, results):
            event = next(payload for kind, payload in sink.events if kind == "model_response")
            assert event["provider_identity"]["request_id"] == request_identity.request_id
            assert event["resolved_model"] == result.resolved_model
            assert event["usage"]["marker"] == result.usage["marker"]

    run(exercise())


def test_trace_rejects_wrong_active_identity_before_provider_call():
    async def exercise():
        expected = identity("right-context")
        wrong = identity("wrong-context")
        trace, sink = _trace_for(expected)
        client = ControlledAsyncClient()
        wrapper = AsyncTracedClient(client, trace)
        with pytest.raises(ValueError, match="active causal trace"):
            await wrapper.evaluate(STATE, QUESTIONS, identity=wrong)
        assert client.calls == []
        assert not any(kind == "model_request" for kind, _ in sink.events)

    run(exercise())


def test_trace_fails_closed_if_actor_context_changes_while_request_is_in_flight():
    async def exercise():
        request_identity = identity("context-drift")
        trace, sink = _trace_for(request_identity)

        class ContextChangingClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                result = await AsyncMockJevClient().evaluate(
                    state, questions, identity=identity, deadline=deadline)
                trace.observation_id = "different-observation"
                return result

        client = ContextChangingClient()
        wrapper = AsyncTracedClient(client, trace)
        with pytest.raises(ResearchLogError, match="context changed"):
            await wrapper.evaluate(STATE, QUESTIONS, identity=request_identity)
        assert client.calls == [request_identity]
        assert trace._failed
        assert not any(kind == "model_response" for kind, _ in sink.events)

    run(exercise())


def test_trace_does_not_emit_error_under_changed_context():
    async def exercise():
        request_identity = identity("error-context-drift")
        trace, sink = _trace_for(request_identity)
        error = AsyncProviderTimeout(request_identity, MAY_HAVE_BEEN_SENT)

        class ContextChangingErrorClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                trace.observation_id = "changed-before-error-capture"
                raise error

        client = ContextChangingErrorClient()
        wrapper = AsyncTracedClient(client, trace)
        with pytest.raises(AsyncProviderTimeout) as raised:
            await wrapper.evaluate(STATE, QUESTIONS, identity=request_identity)
        assert raised.value is error
        assert raised.value.delivery_phase == MAY_HAVE_BEEN_SENT
        assert trace._failed
        assert not any(kind == "model_response" for kind, _ in sink.events)

    run(exercise())


def test_sync_trace_entry_is_rejected_before_mutation_during_async_call():
    class CountingSyncClient(MockJevClient):
        def __init__(self):
            self.calls = []

        def evaluate(self, state, questions):
            self.calls.append((deepcopy(state), deepcopy(questions)))
            return super().evaluate(state, questions)

    async def exercise():
        request_identity = identity("mixed-sync-entry")
        trace, sink = _trace_for(request_identity)
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingAsyncClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                entered.set()
                await release.wait()
                return await AsyncMockJevClient().evaluate(
                    state, questions, identity=identity, deadline=deadline)

        async_client = BlockingAsyncClient()
        sync_client = CountingSyncClient()
        task = asyncio.create_task(AsyncTracedClient(async_client, trace).evaluate(
            STATE, QUESTIONS, identity=request_identity))
        await entered.wait()
        original_model_call_id = trace.model_call_id
        event_count = len(sink.events)

        with pytest.raises(ResearchLogError, match="during an async provider call"):
            TracedClient(sync_client, trace).evaluate(STATE, QUESTIONS)

        assert trace.model_call_id == original_model_call_id == "model:1"
        assert len(sink.events) == event_count
        assert sync_client.calls == []
        assert async_client.calls == [request_identity]

        release.set()
        result = await task
        assert result.identity == request_identity
        response = next(payload for kind, payload in sink.events
                        if kind == "model_response"
                        and payload.get("provider_identity", {}).get("request_id")
                        == request_identity.request_id)
        assert response["model_call_id"] == original_model_call_id
        assert not trace._failed
        sync_result = TracedClient(sync_client, trace).evaluate(STATE, QUESTIONS)
        assert sync_result["ready"]["choice"] == "yes"
        assert len(sync_client.calls) == 1
        assert trace.model_call_id == "model:2"

    run(exercise())


def test_idle_sync_trace_entry_still_records_one_model_call():
    class CountingSyncClient(MockJevClient):
        def __init__(self):
            self.calls = []

        def evaluate(self, state, questions):
            self.calls.append((deepcopy(state), deepcopy(questions)))
            return super().evaluate(state, questions)

    request_identity = identity("idle-sync-entry")
    trace, sink = _trace_for(request_identity)
    client = CountingSyncClient()

    answers_result = TracedClient(client, trace).evaluate(STATE, QUESTIONS)

    assert client.calls == [(STATE, QUESTIONS)]
    assert answers_result["ready"]["choice"] == "yes"
    assert trace.model_call_id == "model:1"
    assert [kind for kind, _ in sink.events].count("model_request") == 1
    assert [kind for kind, _ in sink.events].count("model_response") == 1
    assert all(payload["model_call_id"] == "model:1"
               for kind, payload in sink.events if kind.startswith("model_"))


def test_async_trace_fails_closed_if_model_call_id_changes_on_success():
    async def exercise():
        request_identity = identity("model-id-drift-success")
        trace, sink = _trace_for(request_identity)

        class ModelIdChangingClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                result = await AsyncMockJevClient().evaluate(
                    state, questions, identity=identity, deadline=deadline)
                trace.model_call_id = "model:unrelated"
                return result

        client = ModelIdChangingClient()
        with pytest.raises(ResearchLogError, match="context changed"):
            await AsyncTracedClient(client, trace).evaluate(
                STATE, QUESTIONS, identity=request_identity)

        assert client.calls == [request_identity]
        assert trace._failed
        assert not any(kind == "model_response" for kind, _ in sink.events)
        assert all(payload["model_call_id"] != "model:unrelated"
                   for _, payload in sink.events)

    run(exercise())


def test_async_trace_preserves_transport_error_if_model_call_id_changes():
    async def exercise():
        request_identity = identity("model-id-drift-error")
        trace, sink = _trace_for(request_identity)
        error = AsyncProviderTimeout(request_identity, MAY_HAVE_BEEN_SENT)

        class ModelIdChangingErrorClient(ControlledAsyncClient):
            async def evaluate(self, state, questions, *, identity, deadline=None):
                self.calls.append(identity)
                trace.model_call_id = "model:unrelated"
                raise error

        client = ModelIdChangingErrorClient()
        with pytest.raises(AsyncProviderTimeout) as raised:
            await AsyncTracedClient(client, trace).evaluate(
                STATE, QUESTIONS, identity=request_identity)

        assert raised.value is error
        assert raised.value.delivery_phase == MAY_HAVE_BEEN_SENT
        assert client.calls == [request_identity]
        assert trace._failed
        assert not any(kind == "model_response" for kind, _ in sink.events)
        assert all(payload["model_call_id"] != "model:unrelated"
                   for _, payload in sink.events)

    run(exercise())
