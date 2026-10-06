"""Strict JEV assessment then choice, with checkpointed provider boundaries.

The campaign's existing attempt ledger owns deduplication and batch limits.
This record is committed together with that attempt. A pending phase is never
sent again; only a saved response permits continuing to the next phase.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

import requests

from .judgments import (Decision, InvalidJudgment, candidate_assessments,
                        question_batch, validate_answers)
from .provider_health import ProviderBlocked
from .skills import Plan

PROTOCOL = "jev-assess-then-choose-v1"
MAX_RECORD_BYTES = 1_048_576
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_PHASES = {"assessment_ready", "assessment_pending", "assessment_received",
           "choice_ready", "choice_pending", "choice_received", "settled"}
_OUTCOMES = {"selected", "all_candidates_rejected", "low_choice_confidence",
             "model_abstention", "stale_evidence", "invalid_answer",
             "request_rejected", "provider_blocked"}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def native_digest(snapshot):
    """Use the established semantic clock normalization, never elapsed time."""
    from .blocked_persistence import _stable
    facts = snapshot.for_jev()
    # These are observation diagnostics, already excluded from model facts.
    for key in ("acceptance_runtime", "consumed", "observation_snapshot_schema",
                "observation_query_bounds", "inventory_insertable_evidence"):
        facts.get("factory", {}).pop(key, None)
    return digest(_stable({"facts": facts}, current_tick=snapshot.tick))


def prepare(*, binding, context, questions, offered, input_candidate_ids,
            answer_quantum=0):
    record = {
        "schema": 1, "protocol": PROTOCOL, "binding": deepcopy(binding),
        "prepared": {"context": deepcopy(context), "questions": deepcopy(questions),
                     "plans": [json.loads(encoded(p.to_dict())) for p in offered],
                     "input_candidate_ids": list(input_candidate_ids),
                     "answer_quantum": answer_quantum},
        "phase": "assessment_ready", "assessment": None,
        "choice_request": None, "choice": None, "outcome": None,
        "provider": None,
    }
    record["prepared_sha256"] = digest({"binding": record["binding"],
                                       "prepared": record["prepared"]})
    validate(record, binding["session_id"], binding["target"])
    return record


def assessment_request(record):
    context = deepcopy(record["prepared"]["context"])
    context["decision_phase"] = {
        "protocol": PROTOCOL, "phase": "assessment",
        "instruction": "Assess each supplied action independently. These assessments "
                       "do not select an action or authorize execution. A separate "
                       "JEV choice will receive the validated assessments.",
    }
    questions = {key: value for key, value in record["prepared"]["questions"].items()
                 if key != "candidate"}
    return context, deepcopy(questions)


def choice_request(record):
    prepared, binding = record["prepared"], record["binding"]
    plans = [Plan.from_dict(p) for p in prepared["plans"]]
    gates = candidate_assessments(record["assessment"], plans, binding["confidence_floor"])
    qualified = [p for p in plans if p.id not in gates["candidate_rejections"]]
    if not qualified:
        return None
    # Do not silently prune an approved candidate to produce an easier choice.
    # Build the complete qualified set, then bound the actual choice-only wire.
    context, questions, offered = question_batch(
        prepared["context"], qualified, max_bytes=MAX_RECORD_BYTES,
        max_candidates=len(qualified))
    if len(offered) != len(qualified):
        raise ValueError("Qualified choice cannot preserve every approved candidate")
    assessments = {}
    for plan in plans:
        assessments[plan.id] = {
            "qualified": plan in qualified,
            "rejections": gates["candidate_rejections"].get(plan.id, []),
            "answers": {suffix: {key: value for key, value in
                       record["assessment"][plan.id + "/" + suffix].items()
                       if key != "legend"}
                       for suffix in ("useful_progress", "benefit", "disruption",
                                      "needs_observation")},
        }
    context["validated_assessments"] = assessments
    context["decision_phase"] = {
        "protocol": PROTOCOL, "phase": "choice",
        "assessment_sha256": digest(record["assessment"]),
        "confidence_floor": binding["confidence_floor"],
        "instruction": "The assessments are retained JEV judgments validated under "
                       "the existing candidate gates, not observed success. Choose "
                       "one of the qualified supplied candidates or observe. Use the "
                       "assessments, current facts and scheduling preferences together. "
                       "Code will execute only your sufficiently confident selected "
                       "candidate after fresh native checks. No scheduler fallback exists.",
    }
    question = deepcopy(questions["candidate"])
    # The second request explicitly supplies the previous assessments. The old
    # instruction about independent questions is inapplicable to this phase.
    question["instructions"] = question["instructions"].replace(
        "Do not assume other questions' answers are available.", "").replace(
        "do not assume answers to the separate candidate questions.",
        "use the explicitly supplied validated assessments.")
    result = {"state": context, "questions": {"candidate": question}}
    _bound(result, binding["max_request_bytes"])
    return result


def _bound(request, limit):
    # Match the established request-size accounting, including normal spaces.
    size = len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if size > limit:
        raise ValueError("Two-stage provider request exceeds the configured byte budget")
    return size


def validate(record, session_id, target):
    keys = {"schema", "protocol", "binding", "prepared", "prepared_sha256",
            "phase", "assessment", "choice_request", "choice", "outcome", "provider"}
    if (not isinstance(record, dict) or set(record) != keys
            or type(record["schema"]) is not int or record["schema"] != 1
            or record["protocol"] != PROTOCOL or record["phase"] not in _PHASES
            or len(encoded(record)) > MAX_RECORD_BYTES):
        raise ValueError("Invalid two-stage decision record")
    binding, prepared = record["binding"], record["prepared"]
    binding_keys = {"session_id", "target", "source_revision", "input_sha256",
                    "state_sha256", "frontier_sha256", "native_sha256",
                    "confidence_floor", "max_request_bytes"}
    if (not isinstance(binding, dict) or set(binding) != binding_keys
            or binding["session_id"] != session_id or binding["target"] != target
            or type(binding["confidence_floor"]) not in (int, float)
            or not 0 <= binding["confidence_floor"] <= 1
            or type(binding["max_request_bytes"]) is not int
            or not 1 <= binding["max_request_bytes"] <= 262144):
        raise ValueError("Invalid two-stage decision binding")
    from .blocked_persistence import _source
    _source(binding["source_revision"])
    for key in ("input_sha256", "state_sha256", "frontier_sha256", "native_sha256"):
        if type(binding[key]) is not str or not _SHA.fullmatch(binding[key]):
            raise ValueError("Invalid two-stage decision digest")
    if (not isinstance(prepared, dict) or set(prepared) != {
            "context", "questions", "plans", "input_candidate_ids", "answer_quantum"}
            or prepared["answer_quantum"] not in (0, 0.01)
            or not isinstance(prepared["plans"], list) or not 1 <= len(prepared["plans"]) <= 254
            or record["prepared_sha256"] != digest({"binding": binding, "prepared": prepared})):
        raise ValueError("Two-stage prepared request identity mismatch")
    plans = [Plan.from_dict(p) for p in prepared["plans"]]
    ids = [p.id for p in plans]
    inputs = prepared["input_candidate_ids"]
    if (len(set(ids)) != len(ids) or not isinstance(inputs, list)
            or any(type(key) is not str for key in inputs) or len(set(inputs)) != len(inputs)
            or not set(ids) <= set(inputs)):
        raise ValueError("Invalid two-stage offered candidates")
    context, questions, offered = question_batch(
        prepared["context"], plans, max_bytes=binding["max_request_bytes"],
        max_candidates=len(plans))
    if (len(offered) != len(plans) or encoded(context) != encoded(prepared["context"])
            or encoded(questions) != encoded(prepared["questions"])):
        raise ValueError("Two-stage request differs from its complete candidate evidence")
    assess_context, assess_questions = assessment_request(record)
    _bound({"state": assess_context, "questions": assess_questions}, binding["max_request_bytes"])
    if record["assessment"] is not None:
        validate_answers(assess_questions, record["assessment"], prepared["answer_quantum"])
    if record["choice_request"] is not None:
        if record["assessment"] is None or encoded(record["choice_request"]) != encoded(choice_request(record)):
            raise ValueError("Two-stage choice is not derived from the saved assessment")
    if record["choice"] is not None:
        if record["choice_request"] is None:
            raise ValueError("Two-stage answer lacks its request")
        validate_answers(record["choice_request"]["questions"], record["choice"],
                         prepared["answer_quantum"])
    phase = record["phase"]
    if (phase in {"assessment_ready", "assessment_pending"}
            and any(record[key] is not None for key in ("assessment", "choice_request", "choice"))):
        raise ValueError("Premature two-stage response")
    if (phase in {"assessment_received", "choice_ready", "choice_pending", "choice_received"}
            and record["assessment"] is None):
        raise ValueError("Missing durable assessment response")
    if phase in {"choice_ready", "choice_pending", "choice_received"} and record["choice_request"] is None:
        raise ValueError("Missing durable choice request")
    if phase == "choice_received" and record["choice"] is None:
        raise ValueError("Missing durable choice response")
    if (phase == "assessment_received" and record["choice_request"] is not None
            or phase not in {"choice_received", "settled"} and record["choice"] is not None):
        raise ValueError("Premature two-stage choice")
    if (record["outcome"] is not None and record["outcome"] not in _OUTCOMES
            or (phase == "settled") != (record["outcome"] is not None)):
        raise ValueError("Invalid two-stage disposition")
    if record["provider"] is not None and not isinstance(record["provider"], dict):
        raise ValueError("Invalid two-stage provider disposition")
    if record["outcome"] == "selected":
        if (record["choice"] is None or record["choice"]["candidate"]["choice"] == "observe"
                or record["choice"]["candidate"]["confidence"] < binding["confidence_floor"]):
            raise ValueError("Selected two-stage decision lacks a confident JEV choice")
    if record["outcome"] in {"low_choice_confidence", "model_abstention"}:
        choice = (record["choice"] or {}).get("candidate")
        if not choice or (record["outcome"] == "model_abstention") != (choice["choice"] == "observe"):
            raise ValueError("Two-stage rejection lacks its JEV choice")
        if record["outcome"] == "low_choice_confidence" and choice["confidence"] >= binding["confidence_floor"]:
            raise ValueError("Two-stage confidence disposition disagrees with its answer")
    if record["outcome"] == "all_candidates_rejected":
        if record["assessment"] is None or choice_request(record) is not None or record["choice"] is not None:
            raise ValueError("Two-stage candidate rejection disagrees with its assessment")


def advance(record, client, *, commit, fresh_native_digest):
    """Continue one saved workflow, at most one call per phase, never reroll.

    commit persists a detached record through the controller checkpoint; a
    failure must propagate before any next request or returned action authority.
    fresh_native_digest obtains a new validated observation under the owner.
    """
    record = deepcopy(record)
    binding = record["binding"]
    called = False

    def result():
        return decision(record, model_called=called)
    validate(record, binding["session_id"], binding["target"])

    def save():
        validate(record, binding["session_id"], binding["target"])
        commit(deepcopy(record))

    def finish(outcome):
        record["phase"], record["outcome"] = "settled", outcome
        save()

    def fresh():
        if fresh_native_digest() != binding["native_sha256"]:
            finish("stale_evidence")
            return False
        return True

    def request(phase, state, questions):
        nonlocal called
        record["phase"] = phase + "_pending"
        save()  # This is the provider dispatch boundary, not receipt of a reply.
        try:
            called = True
            answer = client.evaluate(deepcopy(state), deepcopy(questions))
        except ProviderBlocked as error:
            record["provider"] = deepcopy(error.state)
            finish("provider_blocked")
            return False
        except (requests.RequestException, ValueError):
            # Delivery may have occurred. Keep the pending state; restarting
            # must not resend this phase or advance to another batch.
            return False
        try:
            validate_answers(questions, answer, record["prepared"]["answer_quantum"])
        except InvalidJudgment:
            finish("invalid_answer")
            return False
        record["assessment" if phase == "assessment" else "choice"] = deepcopy(answer)
        record["phase"] = phase + "_received"
        save()
        return True

    if record["phase"].endswith("_pending"):
        return result()
    if record["phase"] == "settled":
        # A saved approval may be resumed only while the outer WAL remains
        # pending. Its ordinary ledger outcome fences all later redispatch.
        if record["outcome"] == "selected":
            fresh()
        return result()
    if record["phase"] == "assessment_ready":
        if not fresh() or not request("assessment", *assessment_request(record)):
            return result()
    if record["phase"] == "assessment_received":
        if not fresh():
            return result()
        try:
            record["choice_request"] = choice_request(record)
        except ValueError:
            finish("request_rejected")
            return result()
        if record["choice_request"] is None:
            finish("all_candidates_rejected")
            return result()
        record["phase"] = "choice_ready"
        save()
    if record["phase"] == "choice_ready":
        if not fresh() or not request("choice", record["choice_request"]["state"],
                                      record["choice_request"]["questions"]):
            return result()
    if record["phase"] == "choice_received":
        if not fresh():
            return result()
        choice = record["choice"]["candidate"]
        finish("model_abstention" if choice["choice"] == "observe" else
               "low_choice_confidence" if choice["confidence"] < binding["confidence_floor"]
               else "selected")
    return result()


def decision(record, *, model_called=False):
    prepared, binding = record["prepared"], record["binding"]
    plans = [Plan.from_dict(p) for p in prepared["plans"]]
    outcome = record["outcome"] or "provider_blocked"
    reasons = {"selected": "", "all_candidates_rejected": "Candidate evidence insufficient",
               "stale_evidence": "Candidate evidence insufficient",
               "low_choice_confidence": "low choice confidence",
               "model_abstention": "model abstention",
               "invalid_answer": "Invalid two-stage provider answer",
               "request_rejected": "Two-stage choice request exceeds its budget",
               "provider_blocked": "Two-stage provider phase unresolved; no replay is authorized"}
    choice = record["choice"] or {}
    selected = choice["candidate"]["choice"] if outcome == "selected" else None
    diagnostics = {
        "schema": 1, "protocol": PROTOCOL, "phase": record["phase"], "outcome": outcome,
        "input_candidates": len(prepared["input_candidate_ids"]),
        "offered_candidates": len(plans),
        "pruned_candidate_ids": [key for key in prepared["input_candidate_ids"]
                                 if key not in {p.id for p in plans}],
        "max_request_bytes": binding["max_request_bytes"],
        "request_bytes": len(encoded(record["choice_request"] or {})),
        "candidate_rejections": {}, "prepared_sha256": record["prepared_sha256"],
    }
    if record["assessment"] is not None:
        diagnostics.update(candidate_assessments(record["assessment"], plans,
                                                 binding["confidence_floor"]))
    if outcome == "provider_blocked":
        diagnostics["provider"] = record["provider"] or {
            "category": "unknown_outcome", "phase": "blocked"}
    request = record["choice_request"] or {
        "state": prepared["context"], "questions": prepared["questions"]}
    return Decision(selected, "jev" if selected else "observe", reasons[outcome],
                    state=request["state"], questions=request["questions"],
                    answers={**(record["assessment"] or {}), **choice},
                    model_called=model_called, diagnostics=diagnostics)
