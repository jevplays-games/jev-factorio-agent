"""Focused offline contract tests for the explicit asynchronous provider API."""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest
import requests

from jev_factorio.async_provider import (
    AsyncProviderCancelled,
    AsyncProviderClosed,
    AsyncProviderConnectionError,
    AsyncProviderDeadlineExceeded,
    AsyncProviderHTTPError,
    AsyncProviderPayloadError,
    AsyncProviderQueueFull,
    AsyncProviderTimeout,
    RequestIdentity,
)
from jev_factorio.jev_client import (
    AsyncCloudflareJevClient,
    AsyncJevClient,
    AsyncMockJevClient,
    CloudflareJevClient,
    JevClient,
    MockJevClient,
    make_async_client,
    make_client,
)
from jev_factorio.provider_health import category


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


def response_for(request: httpx.Request, body: dict) -> httpx.Response:
    return httpx.Response(200, json=body, request=request)


def test_direct_async_matches_sync_schema_and_returns_immutable_per_call_metadata(monkeypatch):
    body = {
        "answers": {"ready": {"type": "choice", "choice": "yes"}},
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "model": "jev-resolved-7",
    }
    sync_requests = []

    def sync_post(url, **kwargs):
        sync_requests.append((url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(requests, "post", sync_post)
    sync = JevClient(api_key="offline", base_url="https://direct.invalid", model="jev-requested")
    sync_answers = sync.evaluate(STATE, QUESTIONS)

    async_requests = []

    def handler(request):
        async_requests.append(request)
        return response_for(request, body)

    async def exercise():
        client = AsyncJevClient(
            api_key="offline", base_url="https://direct.invalid", model="jev-requested",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.evaluate(STATE, QUESTIONS, identity=identity())
            return result
        finally:
            await client.aclose()

    result = run(exercise())
    assert result.answers == sync_answers
    assert result.usage == sync.last_usage == body["usage"]
    assert result.requested_model == sync.model
    assert result.resolved_model == sync.last_model == body["model"]
    assert result.identity == identity()
    assert json.loads(async_requests[0].content) == sync_requests[0][1]["json"]
    assert async_requests[0].headers["Authorization"] == "Bearer offline"
    assert len(result.request_payload_sha256) == 64
    with pytest.raises(TypeError):
        result.answers["ready"] = {}
    with pytest.raises(TypeError):
        result.answers["ready"]["choice"] = "no"
    with pytest.raises(TypeError):
        result.usage["input_tokens"] = 99


def test_async_direct_client_preserves_requests_redirect_following_for_post():
    async def exercise():
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/systemone":
                return httpx.Response(307, headers={"Location": "/v2/systemone"},
                                      request=request)
            return response_for(request, {"answers": {"ready": "yes"}})

        client = AsyncJevClient(
            api_key="offline", base_url="https://direct.invalid/systemone",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.evaluate(STATE, QUESTIONS, identity=identity("redirect"))
        finally:
            await client.aclose()
        return calls, result

    calls, result = run(exercise())
    assert calls == [("POST", "/systemone"), ("POST", "/v2/systemone")]
    assert result.answers == {"ready": "yes"}


def test_cloudflare_async_matches_sync_nested_input_and_metadata(monkeypatch):
    body = {"success": True, "result": {
        "answers": {"ready": {"type": "choice", "choice": "yes"}},
        "usage": {"input_tokens": 8}, "model": "typesafe/jev-v2",
    }}
    sync_capture = []

    def sync_post(url, **kwargs):
        sync_capture.append((url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(requests, "post", sync_post)
    sync = CloudflareJevClient("account", "offline", model="typesafe/jev")
    sync_answers = sync.evaluate(STATE, QUESTIONS)

    async_capture = []

    def handler(request):
        async_capture.append(request)
        return response_for(request, body)

    async def exercise():
        client = AsyncCloudflareJevClient(
            "account", "offline", model="typesafe/jev", transport=httpx.MockTransport(handler)
        )
        try:
            return await client.evaluate(STATE, QUESTIONS, identity=identity("cf"))
        finally:
            await client.aclose()

    result = run(exercise())
    assert result.answers == sync_answers
    assert result.usage == sync.last_usage == body["result"]["usage"]
    assert result.resolved_model == sync.last_model == body["result"]["model"]
    assert async_capture[0].url.path.endswith("/accounts/account/ai/run")
    sent = json.loads(async_capture[0].content)
    assert sent == sync_capture[0][1]["json"]
    assert async_capture[0].headers["Authorization"] == "Bearer offline"
    assert sent["model"] == "typesafe/jev"
    assert sent["input"] == {"state": STATE, "questions": QUESTIONS}


@pytest.mark.parametrize("provider", ["direct", "cloudflare"])
def test_inflight_result_keeps_the_model_snapshotted_into_its_request(provider):
    async def exercise():
        request_entered = asyncio.Event()
        release_response = asyncio.Event()
        observed = []

        async def handler(request):
            observed.append(json.loads(request.content))
            request_entered.set()
            await release_response.wait()
            if provider == "direct":
                return response_for(request, {
                    "answers": {"ready": "yes"},
                    "usage": {"model_sent": "model-A"},
                    "model": "resolved-A",
                })
            return response_for(request, {"success": True, "result": {
                "answers": {"ready": "yes"},
                "usage": {"model_sent": "model-A"},
                "model": "resolved-A",
            }})

        if provider == "direct":
            client = AsyncJevClient(
                api_key="offline", model="model-A", transport=httpx.MockTransport(handler)
            )
        else:
            client = AsyncCloudflareJevClient(
                "account", "offline", model="model-A", transport=httpx.MockTransport(handler)
            )
        try:
            pending = asyncio.create_task(
                client.evaluate(STATE, QUESTIONS, identity=identity("model-binding"))
            )
            await request_entered.wait()
            client.model = "model-B"
            release_response.set()
            result = await pending
        finally:
            release_response.set()
            await client.aclose()
        return observed[0], result

    submitted, result = run(exercise())
    assert submitted["model"] == "model-A"
    assert result.requested_model == "model-A"
    assert result.resolved_model == "resolved-A"
    assert result.usage == {"model_sent": "model-A"}


def test_async_mock_has_same_answers_without_provider_transport():
    async def exercise():
        client = AsyncMockJevClient()
        result = await client.evaluate(STATE, QUESTIONS, identity=identity("mock"))
        await client.aclose()
        return result

    result = run(exercise())
    assert result.answers == MockJevClient().evaluate(STATE, QUESTIONS)
    assert result.requested_model == result.resolved_model == "mock-rule-based"
    assert result.usage is None
    assert result.identity == identity("mock")


def test_request_identity_is_immutable_and_each_out_of_order_response_stays_bound():
    async def exercise():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def handler(request):
            payload = json.loads(request.content)
            label = payload["state"]["label"]
            if label == "slow":
                first_entered.set()
                await release_first.wait()
            return response_for(request, {
                "answers": {"result": {"type": "choice", "choice": label}},
                "usage": {"label": label}, "model": "resolved-" + label,
            })

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler), max_concurrency=2
        )
        slow = asyncio.create_task(client.evaluate(
            {"label": "slow"}, QUESTIONS, identity=identity("slow")
        ))
        await first_entered.wait()
        fast = await client.evaluate({"label": "fast"}, QUESTIONS, identity=identity("fast"))
        release_first.set()
        slow_result = await slow
        await client.aclose()
        return slow_result, fast

    slow, fast = run(exercise())
    assert slow.identity == identity("slow")
    assert slow.usage == {"label": "slow"}
    assert slow.resolved_model == "resolved-slow"
    assert fast.identity == identity("fast")
    assert fast.usage == {"label": "fast"}
    assert fast.resolved_model == "resolved-fast"
    with pytest.raises((AttributeError, TypeError)):
        slow.identity.actor_id = "other"


def test_concurrency_queue_limit_and_overflow_rejects_before_transport():
    async def exercise():
        active = maximum_active = sent = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            nonlocal active, maximum_active, sent
            active += 1
            sent += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                both_entered.set()
            await release.wait()
            active -= 1
            return response_for(request, {"answers": {"ok": True}})

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler),
            max_concurrency=2, max_queue=1,
        )
        first = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("1")))
        second = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("2")))
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        third = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("3")))
        await asyncio.sleep(0)
        with pytest.raises(AsyncProviderQueueFull) as raised:
            await client.evaluate(STATE, QUESTIONS, identity=identity("4"))
        assert raised.value.delivery_phase == "not_sent"
        assert sent == 2
        release.set()
        results = await asyncio.gather(first, second, third)
        await client.aclose()
        return maximum_active, sent, results

    maximum_active, sent, results = run(exercise())
    assert maximum_active == 2
    assert sent == 3
    assert {item.identity.request_id for item in results} == {"request-1", "request-2", "request-3"}


def test_deadline_expired_in_queue_is_classified_not_sent():
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        sent = []

        async def handler(request):
            sent.append(request)
            entered.set()
            await release.wait()
            return response_for(request, {"answers": {"ok": True}})

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler),
            max_concurrency=1, max_queue=1,
        )
        first = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("held")))
        await entered.wait()
        queued = asyncio.create_task(client.evaluate(
            STATE, QUESTIONS, identity=identity("expired"), deadline=time.monotonic() + 0.03
        ))
        for _ in range(100):
            if client._waiting == 1:
                break
            await asyncio.sleep(0)
        assert client._waiting == 1
        with pytest.raises(AsyncProviderDeadlineExceeded) as raised:
            await queued
        assert raised.value.delivery_phase == "not_sent"
        assert len(sent) == 1
        release.set()
        await first
        await client.aclose()

    run(exercise())


def test_cancellation_before_send_is_distinct_from_cancel_after_transport_entry():
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        sent = []

        async def handler(request):
            sent.append(request)
            entered.set()
            await release.wait()
            return response_for(request, {"answers": {"ok": True}})

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler),
            max_concurrency=1, max_queue=1,
        )
        active = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("active")))
        await entered.wait()
        queued = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("queued")))
        await asyncio.sleep(0)
        assert client._waiting == 1
        queued.cancel()
        with pytest.raises(AsyncProviderCancelled) as before_send:
            await queued
        assert before_send.value.delivery_phase == "not_sent"

        active.cancel()
        with pytest.raises(AsyncProviderCancelled) as after_entry:
            await active
        assert after_entry.value.delivery_phase == "may_have_been_sent"
        assert len(sent) == 1
        await client.aclose()

    run(exercise())


@pytest.mark.parametrize("status,expected", [
    (400, "application_schema"), (401, "authentication_authorization"),
    (402, "account_quota"), (403, "authentication_authorization"),
    (408, "service_network"), (429, "rate_limit"), (503, "service_network"),
])
def test_http_status_error_category_and_retry_after_match_sync_circuit(status, expected):
    async def exercise():
        async def handler(request):
            return httpx.Response(status, headers={"Retry-After": "120"},
                                   text="secret response body", request=request)

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler)
        )
        try:
            await client.evaluate(STATE, QUESTIONS, identity=identity("error"))
        finally:
            await client.aclose()

    with pytest.raises(AsyncProviderHTTPError) as raised:
        run(exercise())
    assert isinstance(raised.value, requests.HTTPError)
    assert raised.value.response.status_code == status
    assert raised.value.response.headers["Retry-After"] == "120"
    assert "secret response body" not in str(raised.value)
    assert category(raised.value) == expected
    assert raised.value.delivery_phase == "response_received"


def test_transport_deadline_after_entry_is_timeout_with_ambiguous_delivery():
    async def exercise():
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler), timeout=1.0
        )
        try:
            with pytest.raises(AsyncProviderTimeout) as raised:
                await client.evaluate(
                    STATE, QUESTIONS, identity=identity("timeout"),
                    deadline=time.monotonic() + 0.03,
                )
            assert entered.is_set()
            assert raised.value.delivery_phase == "may_have_been_sent"
            assert isinstance(raised.value, requests.Timeout)
            assert category(raised.value) == "service_network"
        finally:
            await client.aclose()

    run(exercise())


@pytest.mark.parametrize("body", [
    [], {"answers": []}, {"success": False}, "non-finite-json",
])
def test_malformed_direct_response_is_provider_payload_error(body):
    async def exercise():
        async def handler(request):
            if body == "non-finite-json":
                return httpx.Response(200, content=b'{"answers":{"bad":NaN}}',
                                      headers={"Content-Type": "application/json"},
                                      request=request)
            return response_for(request, body)

        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        try:
            await client.evaluate(STATE, QUESTIONS, identity=identity("bad-payload"))
        finally:
            await client.aclose()

    with pytest.raises(AsyncProviderPayloadError) as raised:
        run(exercise())
    assert category(raised.value) == "application_schema"
    assert raised.value.delivery_phase == "response_received"


def test_shared_transport_is_not_closed_and_owned_transport_is_closed():
    async def exercise():
        shared = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: response_for(request, {"answers": {"ok": True}})
        ))
        borrowed = AsyncJevClient(api_key="offline", http_client=shared, owns_client=False)
        await borrowed.evaluate(STATE, QUESTIONS, identity=identity("borrowed"))
        await borrowed.aclose()
        shared_remains_open = not shared.is_closed
        await shared.aclose()

        owned = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(
                lambda request: response_for(request, {"answers": {"ok": True}})
            )
        )
        owned_http = owned.http_client
        await owned.evaluate(STATE, QUESTIONS, identity=identity("owned"))
        await asyncio.gather(owned.aclose(), owned.aclose())
        return shared_remains_open, owned_http.is_closed

    assert run(exercise()) == (True, True)


@pytest.mark.parametrize("error,expected_type,phase", [
    (httpx.ConnectTimeout("offline"), AsyncProviderTimeout, "may_have_been_sent"),
    (httpx.ConnectError("offline"), AsyncProviderConnectionError, "may_have_been_sent"),
    (httpx.ReadTimeout("offline"), AsyncProviderTimeout, "may_have_been_sent"),
    (httpx.ReadError("offline"), AsyncProviderConnectionError, "may_have_been_sent"),
])
def test_transport_errors_preserve_sync_provider_category_and_delivery_phase(
        error, expected_type, phase):
    async def exercise():
        def handler(request):
            raise error

        client = AsyncJevClient(api_key="offline", transport=httpx.MockTransport(handler))
        try:
            await client.evaluate(STATE, QUESTIONS, identity=identity("transport-error"))
        finally:
            await client.aclose()

    with pytest.raises(expected_type) as raised:
        run(exercise())
    assert isinstance(raised.value, (requests.Timeout, requests.ConnectionError))
    assert category(raised.value) == "service_network"
    assert raised.value.delivery_phase == phase


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
def test_redirect_followup_transport_failure_remains_ambiguous(error_type):
    async def exercise():
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/first":
                return httpx.Response(
                    307, headers={"Location": "/second"}, request=request
                )
            raise error_type("offline after redirect", request=request)

        client = AsyncJevClient(
            api_key="offline", base_url="https://direct.invalid/first",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises((AsyncProviderConnectionError, AsyncProviderTimeout)) as raised:
                await client.evaluate(STATE, QUESTIONS, identity=identity("redirect-failure"))
        finally:
            await client.aclose()
        return calls, raised.value

    calls, raised = run(exercise())
    assert calls == [("POST", "/first"), ("POST", "/second")]
    assert raised.delivery_phase == "may_have_been_sent"


def test_explicit_factory_matches_existing_provider_priority_and_keeps_sync_factory():
    async def close(client):
        await client.aclose()

    saved = {
        name: os.environ.get(name)
        for name in ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
    }
    try:
        os.environ["TYPESAFE_API_KEY"] = "direct-offline"
        os.environ["CLOUDFLARE_API_TOKEN"] = "cloudflare-offline"
        os.environ["CLOUDFLARE_ACCOUNT_ID"] = "account-offline"
        direct = make_async_client()
        assert isinstance(direct, AsyncJevClient)
        assert isinstance(make_client(), JevClient)
        run(close(direct))

        os.environ.pop("TYPESAFE_API_KEY")
        cloudflare = make_async_client()
        assert isinstance(cloudflare, AsyncCloudflareJevClient)
        assert isinstance(make_client(), CloudflareJevClient)
        run(close(cloudflare))

        os.environ.pop("CLOUDFLARE_API_TOKEN")
        os.environ.pop("CLOUDFLARE_ACCOUNT_ID")
        offline = make_async_client()
        assert isinstance(offline, AsyncMockJevClient)
        assert isinstance(make_client(), MockJevClient)
        run(close(offline))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_client_close_rejects_new_admission_and_drains_active_request():
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return response_for(request, {"answers": {"ok": True}})

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler), timeout=2.0
        )
        active = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("active")))
        await entered.wait()
        closing = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        with pytest.raises(AsyncProviderClosed) as rejected:
            await client.evaluate(STATE, QUESTIONS, identity=identity("after-close"))
        assert getattr(rejected.value, "delivery_phase", None) == "not_sent"
        assert not closing.done()
        release.set()
        await active
        await closing
        assert client.http_client.is_closed

    run(exercise())


def test_client_close_rejects_queued_waiter_without_cancelling_active_request():
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return response_for(request, {"answers": {"ok": True}})

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler),
            max_concurrency=1, max_queue=1, timeout=2.0,
        )
        active = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("active")))
        await entered.wait()
        queued = asyncio.create_task(client.evaluate(STATE, QUESTIONS, identity=identity("queued")))
        for _ in range(100):
            if client._waiting == 1:
                break
            await asyncio.sleep(0)
        assert client._waiting == 1

        closing = asyncio.create_task(client.aclose())
        for _ in range(100):
            if client._closing:
                break
            await asyncio.sleep(0)
        with pytest.raises(AsyncProviderClosed) as rejected:
            await queued
        assert rejected.value.delivery_phase == "not_sent"
        assert not active.done()
        release.set()
        await active
        await closing

    run(exercise())


def test_waiting_requests_enter_the_transport_in_fifo_order():
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        send_order = []

        async def handler(request):
            label = json.loads(request.content)["state"]["label"]
            send_order.append(label)
            if label == "first":
                entered.set()
                await release.wait()
            return response_for(request, {"answers": {"label": label}})

        client = AsyncJevClient(
            api_key="offline", transport=httpx.MockTransport(handler),
            max_concurrency=1, max_queue=2,
        )
        active = asyncio.create_task(client.evaluate(
            {"label": "first"}, QUESTIONS, identity=identity("first")
        ))
        await entered.wait()
        second = asyncio.create_task(client.evaluate(
            {"label": "second"}, QUESTIONS, identity=identity("second")
        ))
        third = asyncio.create_task(client.evaluate(
            {"label": "third"}, QUESTIONS, identity=identity("third")
        ))
        for _ in range(100):
            if client._waiting == 2:
                break
            await asyncio.sleep(0)
        assert client._waiting == 2
        release.set()
        await asyncio.gather(active, second, third)
        await client.aclose()
        return send_order

    assert run(exercise()) == ["first", "second", "third"]
