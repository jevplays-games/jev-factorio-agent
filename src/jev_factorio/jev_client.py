"""Thin Jev client: TypeSafe SDK if available, raw HTTP otherwise, mock if no key.

Direct API:  POST https://api.typesafe.ai/v1/systemone  (Bearer TYPESAFE_API_KEY)
             model alias "jev-latest"; docs: https://docs.typesafe.ai/api.md
Gateway:     Vercel AI Gateway, model id "typesafe-ai/jev",
             $0.042 / 1M input tokens, zero output-token charge.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

import requests

from .async_provider import (
    AsyncProviderClient,
    AsyncProviderDeadlineExceeded,
    AsyncProviderPayloadError,
    AsyncProviderQueueFull,
    AsyncProviderResult,
    NOT_SENT,
    RESPONSE_RECEIVED,
    RequestIdentity,
    make_result,
    request_payload_sha256,
    snapshot_json,
)
from .provider_health import ProviderPayloadError

API_URL = "https://api.typesafe.ai/v1/systemone"
GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/systemone"  # confirm path against Gateway docs


def _decision_provider_id(provider: str, endpoint: str) -> str:
    """Fingerprint only the non-secret endpoint; credentials never enter WAL."""
    if type(endpoint) is not str or not endpoint:
        raise ValueError("Provider endpoint is not configured")
    parsed = urlsplit(endpoint)
    if parsed.username is not None or parsed.password is not None or parsed.query:
        raise ValueError("Decision leases do not accept credential-bearing endpoints")
    safe_endpoint = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                                parsed.path, "", ""))
    value = json.dumps([provider, safe_endpoint], ensure_ascii=False,
                       separators=(",", ":"))
    return provider + ":sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validated_async_result(identity, answers, usage, requested_model,
                            resolved_model, payload_sha256, questions, quantum):
    if not isinstance(answers, dict):
        raise AsyncProviderPayloadError(identity)
    from .judgments import InvalidJudgment, validate_answers
    try:
        validate_answers(questions, answers, quantum=quantum)
    except InvalidJudgment as error:
        raise AsyncProviderPayloadError(identity, "Provider answer schema rejected") from error
    return make_result(identity, answers, usage, requested_model,
                       resolved_model, payload_sha256)


class JevClient:
    uses_http_provider = True
    answer_quantum = 0.01

    def __init__(self, api_key: str | None = None, base_url: str = API_URL,
                 model: str = "jev-latest"):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        self.base_url = base_url
        self.model = model

    def evaluate(self, state: dict, questions: dict) -> dict:
        """One system-one call. Returns the `answers` map keyed by question id."""
        self.last_usage = None
        self.last_model = None
        resp = requests.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"state": state, "model": self.model, "questions": questions},
            timeout=10,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
            if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
                raise ProviderPayloadError("Invalid provider response envelope")
        except (ValueError, TypeError) as error:
            raise ProviderPayloadError("Invalid provider response envelope") from error
        self.last_usage = body.get("usage")
        self.last_model = body.get("model")
        return body["answers"]


class MockJevClient:
    """Offline stand-in with the same interface: deterministic, rule-based answers.

    Lets the whole loop run (and tests pass) with no API key and no spend.
    """

    is_mock = True
    model = "mock-rule-based"

    def evaluate(self, state: dict, questions: dict) -> dict:
        answers = {}
        for qid, q in questions.items():
            if q["type"] == "choice":
                pick = next(iter(q["criteria"]))
                # tiny heuristic so the mock loop makes visible progress
                if qid == "next_action":
                    for preferred in ("fuel_drill", "place_burner_drill",
                                      "mine_coal", "mine_iron",
                                      "walk_to_coal", "walk_to_iron"):
                        if preferred in q["criteria"]:
                            pick = preferred
                            break
                answers[qid] = {"type": "choice", "choice": pick,
                                "probabilities": {k: (1.0 if k == pick else 0.0)
                                                  for k in q["criteria"]},
                                "confidence": 0.9}
            elif q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.0}
            else:
                legend = {str(i): lvl for i, lvl in enumerate(q["criteria"])}
                level = len(legend) - 1 if qid.endswith("/benefit") else 0
                answers[qid] = {"type": "score", "score": float(level), "legend": legend,
                                "probabilities": {k: float(k == str(level)) for k in legend},
                                "confidence": 1.0}
        return answers


class CloudflareJevClient:
    """Jev via Cloudflare Workers AI / AI Gateway.

    POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run
    Body: {"model": "typesafe/jev", "input": {state, questions}}
    - different envelope from api.typesafe.ai (state/questions nest under
      `input`, model id is "typesafe/jev"); answers come back under
      result.result (verified against the Cloudflare model docs and a live
      account, Sept 2026).
    - BILLING: third-party provider models like typesafe/jev are billed via
      AI Gateway Unified Billing (prepaid credits, +5% load fee), NOT the
      10,000 free Neurons/day (those cover first-party @cf/ models only).
      Error code 2021 = insufficient gateway credits.
    Docs: https://developers.cloudflare.com/ai/models/typesafe/jev/
          https://developers.cloudflare.com/ai-gateway/features/unified-billing/
    """

    uses_http_provider = True

    def __init__(self, account_id: str, api_token: str,
                 model: str = "typesafe/jev"):
        self.account_id = account_id
        self.api_token = api_token
        self.model = model
        self.url = (f"https://api.cloudflare.com/client/v4/accounts/"
                    f"{account_id}/ai/run")

    def evaluate(self, state: dict, questions: dict) -> dict:
        self.last_usage = None
        self.last_model = None
        resp = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_token}"},
            json={"model": self.model,
                  "input": {"state": state, "questions": questions}},
            timeout=10,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                raise ProviderPayloadError("Provider rejected application request")
            result = body.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise ProviderPayloadError("Invalid provider response envelope")
        except (ValueError, TypeError) as error:
            raise ProviderPayloadError("Invalid provider response envelope") from error
        self.last_usage = result.get("usage")
        self.last_model = result.get("model")
        return result["answers"]


class AsyncJevClient(AsyncProviderClient):
    """Explicit async counterpart to :class:`JevClient`.

    Each call returns immutable response data and its own identity. It does not
    update shared ``last_usage``/``last_model`` attributes, so concurrent
    completions cannot overwrite one another. The existing synchronous client
    and factory remain unchanged.
    """

    uses_http_provider = True
    answer_quantum = 0.01

    def __init__(self, api_key: str | None = None, base_url: str = API_URL,
                 model: str = "jev-latest", **transport_options):
        super().__init__(**transport_options)
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        self.base_url = base_url
        self.model = model

    def decision_provider_id(self) -> str:
        return _decision_provider_id("typesafe", self.base_url)

    def prepare_decision_payload(self, state: dict, questions: dict) -> dict:
        return snapshot_json({"state": state, "model": self.model,
                              "questions": questions})

    async def evaluate(self, state: dict, questions: dict, *, identity: RequestIdentity,
                       deadline: float | None = None,
                       decision_lease=None) -> AsyncProviderResult:
        requested_model = self.model
        payload = self.prepare_decision_payload(state, questions)

        def build_result(body, payload_sha256):
            return _validated_async_result(
                identity, body.get("answers"), body.get("usage"), requested_model,
                body.get("model"), payload_sha256, questions, self.answer_quantum,
            )

        posted = await self.post_json(
            identity=identity,
            url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload=payload,
            deadline=deadline,
            decision_lease=decision_lease,
            response_builder=build_result if decision_lease is not None else None,
        )
        if decision_lease is not None:
            return posted
        body, payload_sha256 = posted
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise AsyncProviderPayloadError(identity)
        return make_result(identity, answers, body.get("usage"), requested_model,
                           body.get("model"), payload_sha256)


class AsyncCloudflareJevClient(AsyncProviderClient):
    """Explicit async client for the currently supported Cloudflare AI route."""

    uses_http_provider = True

    def __init__(self, account_id: str, api_token: str,
                 model: str = "typesafe/jev", **transport_options):
        super().__init__(**transport_options)
        self.account_id = account_id
        self.api_token = api_token
        self.model = model
        self.url = (f"https://api.cloudflare.com/client/v4/accounts/"
                    f"{account_id}/ai/run")

    def decision_provider_id(self) -> str:
        return _decision_provider_id("cloudflare", self.url)

    def prepare_decision_payload(self, state: dict, questions: dict) -> dict:
        return snapshot_json({"model": self.model,
                              "input": {"state": state, "questions": questions}})

    async def evaluate(self, state: dict, questions: dict, *, identity: RequestIdentity,
                       deadline: float | None = None,
                       decision_lease=None) -> AsyncProviderResult:
        requested_model = self.model
        payload = self.prepare_decision_payload(state, questions)

        def build_result(body, payload_sha256):
            result = body.get("result")
            if body.get("success") is not True or not isinstance(result, dict):
                raise AsyncProviderPayloadError(
                    identity, "Provider rejected application request")
            return _validated_async_result(
                identity, result.get("answers"), result.get("usage"), requested_model,
                result.get("model"), payload_sha256, questions,
                getattr(self, "answer_quantum", 0),
            )

        posted = await self.post_json(
            identity=identity,
            url=self.url,
            headers={"Authorization": f"Bearer {self.api_token}"},
            payload=payload,
            deadline=deadline,
            decision_lease=decision_lease,
            response_builder=build_result if decision_lease is not None else None,
        )
        if decision_lease is not None:
            return posted
        body, payload_sha256 = posted
        result = body.get("result")
        if body.get("success") is not True or not isinstance(result, dict):
            raise AsyncProviderPayloadError(
                identity, "Provider rejected application request")
        answers = result.get("answers")
        if not isinstance(answers, dict):
            raise AsyncProviderPayloadError(identity)
        return make_result(identity, answers, result.get("usage"), requested_model,
                           result.get("model"), payload_sha256)


class AsyncMockJevClient:
    """Offline async-shaped adapter that preserves the synchronous mock rules."""

    is_mock = True
    model = MockJevClient.model

    def decision_provider_id(self) -> str:
        return "mock:sha256:" + hashlib.sha256(b"jev-factorio-mock-v1").hexdigest()

    def prepare_decision_payload(self, state: dict, questions: dict) -> dict:
        return snapshot_json({"state": state, "model": self.model,
                              "questions": questions})

    async def evaluate(self, state: dict, questions: dict, *, identity: RequestIdentity,
                       deadline: float | None = None,
                       decision_lease=None) -> AsyncProviderResult:
        if deadline is not None:
            if (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                    or not math.isfinite(deadline)):
                raise ValueError("deadline must be an absolute finite monotonic timestamp")
            if deadline <= time.monotonic():
                error = AsyncProviderDeadlineExceeded(
                    "provider request deadline expired", identity)
                if decision_lease is not None:
                    decision_lease.record_failure(error, NOT_SENT)
                raise error
        snapshot = self.prepare_decision_payload(state, questions)
        if decision_lease is not None:
            decision_lease.mark_may_have_been_sent()
        answers = MockJevClient().evaluate(snapshot["state"], snapshot["questions"])
        if decision_lease is None:
            return make_result(identity, answers, None, self.model, self.model,
                               request_payload_sha256(snapshot))
        try:
            result = _validated_async_result(
                identity, answers, None, self.model, self.model,
                request_payload_sha256(snapshot), questions, 0,
            )
        except AsyncProviderPayloadError as error:
            if decision_lease is not None:
                decision_lease.record_failure(error, RESPONSE_RECEIVED)
            raise
        if decision_lease is not None:
            decision_lease.save_response(result)
        return result

    async def aclose(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()


class AsyncTracedClient:
    """Explicit async counterpart to the synchronous causal trace wrapper.

    The caller supplies the immutable provider identity for each request. A
    trace is one serialized actor context; separate traces may run concurrently
    without sharing model, usage, or identity metadata.
    """

    def __init__(self, client, trace):
        self._client, self._trace = client, trace
        self._trace_lock = None

    def __getattr__(self, name):
        return getattr(self._client, name)

    def prepare_decision_trace_binding(self, identity: RequestIdentity):
        trace = self._trace
        if not trace.enabled:
            return None
        if trace._failed:
            from .research_log import ResearchLogError

            raise ResearchLogError("Causal trace has failed")
        self._require_context(identity)
        if trace._async_provider_call is not None:
            from .research_log import ResearchLogError

            raise ResearchLogError("Causal trace already has an async provider call")
        # This deterministic model-call identity survives restart and is kept
        # separate from the trace's transient event counter.
        return {
            "session_id": identity.session_id,
            "actor_id": identity.actor_id,
            "observation_id": identity.observation_id,
            "decision_id": identity.decision_id,
            "model_call_id": "model-request:" + identity.request_id,
        }

    async def evaluate(self, state: dict, questions: dict, *, identity: RequestIdentity,
                       deadline: float | None = None,
                       decision_lease=None) -> AsyncProviderResult:
        if not isinstance(identity, RequestIdentity):
            raise TypeError("identity must be a RequestIdentity")
        if (deadline is not None
                and (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                     or not math.isfinite(deadline))):
            raise ValueError("deadline must be an absolute finite monotonic timestamp")
        if not isinstance(state, dict) or not isinstance(questions, dict):
            raise TypeError("async provider state and questions must be JSON objects")
        request = snapshot_json({"state": state, "questions": questions})
        state, questions = request["state"], request["questions"]
        trace = self._trace
        if not trace.enabled:
            result = await self._evaluate(
                state, questions, identity, deadline, decision_lease)
            if (not isinstance(result, AsyncProviderResult)
                    or result.identity != identity):
                raise AsyncProviderPayloadError(
                    identity, "Provider result identity does not match request")
            return result
        if self._trace_lock is None:
            self._trace_lock = getattr(trace, "_async_provider_trace_lock", None)
            if self._trace_lock is None:
                self._trace_lock = asyncio.Lock()
                trace._async_provider_trace_lock = self._trace_lock
        if self._trace_lock.locked():
            raise AsyncProviderQueueFull("causal trace is already evaluating a provider", identity)
        async with self._trace_lock:
            if trace._failed:
                from .research_log import ResearchLogError

                raise ResearchLogError("Causal trace has failed")
            self._require_context(identity)
            reservation = trace._reserve_async_provider_call()
            model_call_id = None
            try:
                if decision_lease is not None and decision_lease.trace_binding is not None:
                    trace.model_call_id = decision_lease.trace_binding["model_call_id"]
                else:
                    trace.model_call_id = trace.identity("model")
                model_call_id = trace.model_call_id
                provider_identity = self._identity_payload(identity)
                requested_model = getattr(self._client, "model", None)
                trace.emit("model_request", {
                    "state": state,
                    "questions": questions,
                    "requested_model": requested_model,
                    "is_mock": getattr(self._client, "is_mock", False),
                    "dispatch": "prepared",
                    "provider_identity": provider_identity,
                })
                if not self._context_matches(identity, model_call_id):
                    from .research_log import ResearchLogError

                    trace._failed = True
                    raise ResearchLogError(
                        "Causal context changed before an async provider request")
                try:
                    result = await self._evaluate(
                        state, questions, identity, deadline, decision_lease)
                    if not self._context_matches(identity, model_call_id):
                        from .research_log import ResearchLogError

                        trace._failed = True
                        raise ResearchLogError(
                            "Causal context changed during an async provider request")
                    if (not isinstance(result, AsyncProviderResult)
                            or result.identity != identity):
                        raise AsyncProviderPayloadError(
                            identity, "Provider result identity does not match request")
                    trace.emit("model_response", {
                        "status": "ok",
                        "answers": _mutable_provider_json(result.answers),
                        "requested_model": result.requested_model,
                        "resolved_model": _mutable_provider_json(result.resolved_model),
                        "usage": _mutable_provider_json(result.usage),
                        "request_payload_sha256": result.request_payload_sha256,
                        "provider_identity": provider_identity,
                    })
                except BaseException as error:
                    if not self._context_matches(identity, model_call_id):
                        # The response/error belongs to the original request,
                        # but the shared trace envelope has moved. Poison the
                        # trace rather than writing those facts under new IDs.
                        trace._failed = True
                        raise
                    trace.error("model_response", error,
                                requested_model=requested_model,
                                provider_identity=provider_identity,
                                delivery_phase=getattr(error, "delivery_phase", None))
                    raise
                return result
            finally:
                trace._release_async_provider_call(reservation)

    async def _evaluate(self, state, questions, identity, deadline, decision_lease=None):
        async_method = getattr(self._client, "evaluate_async", None)
        if callable(async_method):
            kwargs = {"identity": identity, "deadline": deadline}
            if decision_lease is not None:
                kwargs["decision_lease"] = decision_lease
            return await async_method(state, questions, **kwargs)
        kwargs = {"identity": identity, "deadline": deadline}
        if decision_lease is not None:
            kwargs["decision_lease"] = decision_lease
        return await self._client.evaluate(state, questions, **kwargs)

    def _require_context(self, identity: RequestIdentity) -> None:
        trace = self._trace
        if (trace.decision_id != identity.decision_id
                or trace.observation_id != identity.observation_id
                or trace._session_id != identity.session_id):
            raise ValueError("provider identity does not match the active causal trace")

    def _context_matches(self, identity: RequestIdentity, model_call_id) -> bool:
        trace = self._trace
        return (trace.decision_id == identity.decision_id
                and trace.observation_id == identity.observation_id
                and trace._session_id == identity.session_id
                and trace.model_call_id == model_call_id)

    @staticmethod
    def _identity_payload(identity: RequestIdentity) -> dict:
        return {"session_id": identity.session_id,
                "actor_id": identity.actor_id,
                "observation_id": identity.observation_id,
                "decision_id": identity.decision_id,
                "request_id": identity.request_id}


def _mutable_provider_json(value):
    if isinstance(value, Mapping):
        return {key: _mutable_provider_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_provider_json(item) for item in value]
    return value


def make_client(*, allow_mock: bool = True, model: str | None = None) -> object:
    """Real client when a key exists, mock otherwise.

    Priority: TypeSafe direct key, then Cloudflare Workers AI token.
    """
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return JevClient(api_key=key, model=model or "jev-latest")
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cf_acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if cf_token and cf_acct:
        return CloudflareJevClient(account_id=cf_acct, api_token=cf_token,
                                   model=model or "typesafe/jev")
    if not allow_mock:
        raise ValueError("Live Jev credentials are required; use an explicit offline mock")
    return MockJevClient()


def make_async_client(*, allow_mock: bool = True, model: str | None = None,
                      **transport_options) -> object:
    """Construct an explicit async client using the same provider priority.

    This factory is opt-in; the production controller and synchronous
    ``make_client`` path are not changed by adding async transport support.
    """
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return AsyncJevClient(api_key=key, model=model or "jev-latest",
                              **transport_options)
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cf_acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if cf_token and cf_acct:
        return AsyncCloudflareJevClient(account_id=cf_acct, api_token=cf_token,
                                        model=model or "typesafe/jev",
                                        **transport_options)
    if not allow_mock:
        raise ValueError("Live Jev credentials are required; use an explicit offline mock")
    if transport_options:
        raise ValueError("async mock does not accept HTTP transport options")
    return AsyncMockJevClient()
