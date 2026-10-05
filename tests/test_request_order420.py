"""Behavioral ordering controls for durable model-request evidence."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.async_provider import RequestIdentity
from jev_factorio.causal_trace import CausalTrace
from jev_factorio.jev_client import AsyncMockJevClient, AsyncTracedClient, MockJevClient
from jev_factorio.replay import replay_log
from jev_factorio.research_log import ResearchLog, RunConfiguration, verify_run


REPO = Path(__file__).resolve().parents[1]
STATE = {
    "candidate_plans": {
        "z-candidate": {"description": "first"},
        "a-candidate": {"description": "second"},
    },
    "candidate_evidence": {
        "z-candidate": {"local_target": "first"},
        "a-candidate": {"local_target": "second"},
    },
    "shared_plan_materials": {"z-material": {"count": 1}, "a-material": {"count": 2}},
}
QUESTIONS = {
    "z-question": {
        "type": "choice",
        "criteria": {"z-candidate": "first", "a-candidate": "second"},
    },
    "a-score/benefit": {
        "type": "score",
        "criteria": [f"level-{index}" for index in range(12)],
    },
}


def _config():
    return RunConfiguration(
        backend="mock", controller="hierarchical", policy="jev",
        target="rocket_launch", mock_model=True, steps=1,
    )


def _frame(report):
    return next(frame for frame in report.decisions if frame["model_calls"])


class RecordingMock(MockJevClient):
    def __init__(self):
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((deepcopy(state), deepcopy(questions)))
        return super().evaluate(state, questions)


def test_sync_request_order_survives_canonical_writer_and_public_replay(tmp_path):
    run_dir = tmp_path / "sync-run"
    state, questions = deepcopy(STATE), deepcopy(QUESTIONS)
    before = (deepcopy(state), deepcopy(questions))
    client = RecordingMock()
    with ResearchLog(run_dir, _config(), repo_dir=REPO, environ={}) as log:
        trace = CausalTrace(log, "hierarchical")
        trace.begin_step()
        answers = trace.client(client).evaluate(state, questions)

    integrity = verify_run(run_dir)
    assert integrity["complete"] is True
    report = replay_log(run_dir, format="research-v1")
    assert report.integrity["status"] == "verified_source"
    call = _frame(report)["model_calls"][0]
    reconstructed = call["reconstructed_request"]
    assert call["request"]["correlation"]["model_call_id"] == call["model_call_id"]

    assert list(client.calls[0][1]) == ["z-question", "a-score/benefit"]
    assert list(client.calls[0][1]["z-question"]["criteria"]) == [
        "z-candidate", "a-candidate"
    ]
    assert list(client.calls[0][0]["candidate_plans"]) == [
        "z-candidate", "a-candidate"
    ]
    assert list(reconstructed["questions"]) == list(client.calls[0][1])
    assert list(reconstructed["questions"]["z-question"]["criteria"]) == [
        "z-candidate", "a-candidate"
    ]
    assert list(reconstructed["state"]["candidate_plans"]) == [
        "z-candidate", "a-candidate"
    ]
    assert list(reconstructed["state"]["candidate_evidence"]) == [
        "z-candidate", "a-candidate"
    ]
    assert list(reconstructed["state"]["shared_plan_materials"]) == [
        "z-material", "a-material"
    ]
    replayed_answers = MockJevClient().evaluate(
        reconstructed["state"], reconstructed["questions"]
    )
    assert replayed_answers["z-question"]["choice"] == answers["z-question"]["choice"] == "z-candidate"
    assert list(answers["a-score/benefit"]["legend"]) == [str(i) for i in range(12)]
    assert list(replayed_answers["a-score/benefit"]["legend"]) == [str(i) for i in range(12)]
    assert (state, questions) == before


def test_async_request_order_uses_same_canonical_binding(tmp_path):
    class RecordingAsyncMock(AsyncMockJevClient):
        def __init__(self):
            self.calls = []

        async def evaluate(self, state, questions, *, identity, deadline=None,
                           decision_lease=None):
            self.calls.append((deepcopy(state), deepcopy(questions)))
            return await super().evaluate(
                state, questions, identity=identity, deadline=deadline,
                decision_lease=decision_lease,
            )

    async def exercise(run_dir):
        client = RecordingAsyncMock()
        supplied_state, supplied_questions = deepcopy(STATE), deepcopy(QUESTIONS)
        original = deepcopy(supplied_state), deepcopy(supplied_questions)
        with ResearchLog(run_dir, _config(), repo_dir=REPO, environ={}) as log:
            trace = CausalTrace(log, "hierarchical")
            trace.begin_step()
            identity = RequestIdentity(
                session_id="session-420", actor_id="actor-420",
                observation_id="observation-420", decision_id=trace.decision_id,
                request_id="request-420",
            )
            trace._session_id = identity.session_id
            trace.observation_id = identity.observation_id
            result = await AsyncTracedClient(client, trace).evaluate(
                supplied_state, supplied_questions, identity=identity
            )
        return client, result, identity, (supplied_state, supplied_questions), original

    client, result, identity, supplied, original = asyncio.run(exercise(tmp_path / "async-run"))
    integrity = verify_run(tmp_path / "async-run")
    assert integrity["complete"] is True
    report = replay_log(tmp_path / "async-run", format="research-v1")
    call = _frame(report)["model_calls"][0]
    reconstructed = call["reconstructed_request"]
    assert result.identity is identity
    assert call["request"]["correlation"]["model_call_id"] == call["model_call_id"]
    assert call["request"]["payload"]["provider_identity"] == {
        "session_id": identity.session_id,
        "actor_id": identity.actor_id,
        "observation_id": identity.observation_id,
        "decision_id": identity.decision_id,
        "request_id": identity.request_id,
    }
    assert list(client.calls[0][1]) == ["z-question", "a-score/benefit"]
    assert list(reconstructed["questions"]) == list(client.calls[0][1])
    assert list(reconstructed["questions"]["z-question"]["criteria"]) == [
        "z-candidate", "a-candidate"
    ]
    assert list(reconstructed["state"]["candidate_plans"]) == [
        "z-candidate", "a-candidate"
    ]
    assert list(reconstructed["state"]["candidate_evidence"]) == [
        "z-candidate", "a-candidate"
    ]
    replayed_answers = MockJevClient().evaluate(
        reconstructed["state"], reconstructed["questions"]
    )
    assert result.answers["z-question"]["choice"] == "z-candidate"
    assert replayed_answers["z-question"]["choice"] == result.answers["z-question"]["choice"]
    assert list(result.answers["a-score/benefit"]["legend"]) == [str(i) for i in range(12)]
    assert list(replayed_answers["a-score/benefit"]["legend"]) == [str(i) for i in range(12)]
    assert supplied == original


def _manual_request_run(run_dir, *, request_order_marker="missing"):
    request_state, request_questions = deepcopy(STATE), deepcopy(QUESTIONS)
    with ResearchLog(run_dir, _config(), repo_dir=REPO, environ={}) as log:
        trace = CausalTrace(log, "hierarchical")
        trace.begin_step()
        payload = {
            "trace_id": trace.trace_id,
            "decision_id": trace.decision_id,
            "model_call_id": "model:legacy",
            "state": request_state,
            "questions": request_questions,
            "requested_model": "mock-rule-based",
            "is_mock": True,
            "dispatch": "prepared",
        }
        if request_order_marker != "missing":
            payload["request_order"] = request_order_marker
        log.emit("model_request", payload)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate-question", "missing-criterion", "candidate-conflict", "score-conflict", "schema"],
)
def test_present_but_conflicting_order_claim_is_invalid(tmp_path, mutation):
    from jev_factorio.request_order import describe_request_order

    marker = describe_request_order(deepcopy(STATE), deepcopy(QUESTIONS))
    if mutation == "duplicate-question":
        marker["question_ids"].append("z-question")
    elif mutation == "missing-criterion":
        marker["criteria_ids"]["z-question"].remove("a-candidate")
    elif mutation == "candidate-conflict":
        marker["candidate_plan_ids"].remove("a-candidate")
    elif mutation == "score-conflict":
        marker["score_criteria"]["a-score/benefit"][0] = "unexpected"
    else:
        marker["schema"] = "jev-factorio.request-order.v999"

    run_dir = tmp_path / f"invalid-{mutation}"
    _manual_request_run(run_dir, request_order_marker=marker)
    report = replay_log(run_dir, format="research-v1")
    assert report.integrity["status"] == "verified_source"
    assert report.status == "invalid"
    assert any(finding.code == "invalid_request_order" for finding in report.findings)
    assert _frame(report)["model_calls"][0]["reconstructed_request"] is None


def test_orderless_historical_model_request_is_reported_without_invented_order(tmp_path):
    run_dir = tmp_path / "historical-orderless"
    _manual_request_run(run_dir)
    report = replay_log(run_dir, format="research-v1")
    call = _frame(report)["model_calls"][0]
    assert call["request_order_status"] == "unavailable"
    assert call["reconstructed_request"] is None
    assert report.status == "incomplete"
    assert any(finding.code == "request_order_unavailable" for finding in report.findings)


def test_order_binding_is_detached_and_redacted_before_persistence(tmp_path):
    run_dir = tmp_path / "redaction-run"
    state, questions = deepcopy(STATE), deepcopy(QUESTIONS)
    questions["z-question"]["instructions"] = "do not persist offline-secret-value"
    before = (deepcopy(state), deepcopy(questions))

    class MutatingMock(MockJevClient):
        def evaluate(self, received_state, received_questions):
            received_questions["z-question"]["criteria"].clear()
            received_state["candidate_plans"].clear()
            return {"z-question": {"type": "choice", "choice": "z-candidate"}}

    with ResearchLog(
        run_dir, _config(), repo_dir=REPO,
        environ={"TYPESAFE_API_KEY": "offline-secret-value"},
    ) as log:
        trace = CausalTrace(log, "hierarchical", client=MutatingMock())
        trace.begin_step()
        trace.client(MutatingMock()).evaluate(state, questions)

    assert (state, questions) == before
    raw = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "offline-secret-value" not in raw
    report = replay_log(run_dir, format="research-v1")
    request = _frame(report)["model_calls"][0]["reconstructed_request"]
    assert list(request["questions"]["z-question"]["criteria"]) == [
        "z-candidate", "a-candidate"
    ]


def test_lexical_order_remains_a_valid_explicit_control(tmp_path):
    state = {"candidate_plans": {"a-plan": {}, "z-plan": {}}}
    questions = {"a-question": {"type": "choice", "criteria": {"a": "first", "z": "last"}}}
    run_dir = tmp_path / "lexical-run"
    with ResearchLog(run_dir, _config(), repo_dir=REPO, environ={}) as log:
        trace = CausalTrace(log, "hierarchical")
        trace.begin_step()
        trace.client(MockJevClient()).evaluate(state, questions)
    report = replay_log(run_dir, format="research-v1")
    call = _frame(report)["model_calls"][0]
    assert call["request_order_status"] == "validated"
    assert call["reconstructed_request"] == {"state": state, "questions": questions}


def test_legacy_jsonl_remains_readable_without_invented_order(tmp_path):
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({"state": {}, "action": "idle", "source": "fallback"}) + "\n",
                    encoding="utf-8")
    report = replay_log(path, format="legacy")
    assert report.format == "legacy"
    assert not any("request_order" in finding.code for finding in report.findings)


def test_order_metadata_bounds_stop_sync_dispatch_before_client_entry(tmp_path):
    from jev_factorio.research_log import ResearchLogError

    state, questions = deepcopy(STATE), {
        f"question-{index:04d}": {"type": "noul", "criteria": {}}
        for index in range(4097)
    }
    client = RecordingMock()
    run_dir = tmp_path / "bounded-run"
    with ResearchLog(run_dir, _config(), repo_dir=REPO, environ={}) as log:
        trace = CausalTrace(log, "hierarchical")
        trace.begin_step()
        with pytest.raises(ResearchLogError, match="bounded model request"):
            trace.client(client).evaluate(state, questions)
    assert client.calls == []
