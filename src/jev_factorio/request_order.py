"""Bounded, explicit ordering claims for model-request evidence.

Canonical event JSON sorts object keys. These helpers carry the supported
behavioral collection order as arrays so replay can reconstruct the request
without changing the original request or relying on object member order.
"""
from __future__ import annotations

import json
from copy import deepcopy


ORDER_SCHEMA = "jev-factorio.request-order.v1"
MAX_ORDER_ITEMS = 4096
MAX_ORDER_TEXT_BYTES = 64 * 1024
MAX_ORDER_METADATA_BYTES = 128 * 1024


class RequestOrderError(ValueError):
    """An order claim is malformed, conflicts with the request, or exceeds bounds."""


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestOrderError("request contains duplicate JSON object keys")
        result[key] = value
    return result


def snapshot_request(state: object, questions: object) -> dict:
    """Return the bounded JSON request copy whose order is recorded and sent."""
    if type(state) is not dict or type(questions) is not dict:
        raise RequestOrderError("model request state and questions must be objects")
    try:
        request = {"state": deepcopy(state), "questions": deepcopy(questions)}
        encoded = json.dumps(
            request,
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )
        # Parse only to validate key normalization/duplicates. Keep the detached
        # Python values passed to the client (for example tuples) unchanged.
        json.loads(encoded, object_pairs_hook=_object_without_duplicates)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise RequestOrderError("model request is not finite, unambiguous JSON") from error
    if type(request) is not dict or type(request.get("state")) is not dict \
            or type(request.get("questions")) is not dict:
        raise RequestOrderError("model request state and questions must be objects")
    # Compute once here to reject unbounded metadata before the provider call.
    describe_request_order(request["state"], request["questions"])
    return request


class _OrderBudget:
    def __init__(self):
        self.items = 0
        self.text_bytes = 0

    def add(self, values, field: str, *, nonempty: bool = True) -> None:
        if self.items + len(values) > MAX_ORDER_ITEMS:
            raise RequestOrderError("request ordering exceeds the supported item count")
        added_bytes = 0
        for value in values:
            if type(value) is not str or (nonempty and not value):
                raise RequestOrderError(f"{field} contains an unsupported identifier")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeError as error:
                raise RequestOrderError(f"{field} contains invalid Unicode") from error
            if size > MAX_ORDER_TEXT_BYTES:
                raise RequestOrderError(f"{field} contains an unsupported identifier")
            added_bytes += size
        if self.text_bytes + added_bytes > MAX_ORDER_TEXT_BYTES:
            raise RequestOrderError("request ordering exceeds the supported text size")
        self.items += len(values)
        self.text_bytes += added_bytes


def _ordered_keys(value: dict, field: str, budget: _OrderBudget) -> list[str]:
    if type(value) is not dict:
        raise RequestOrderError(f"{field} must be an object")
    keys = list(value)
    budget.add(keys, field)
    return keys


def _map_collection(state: dict, name: str, budget: _OrderBudget) -> list[str] | None:
    if name not in state:
        return None
    return _ordered_keys(state[name], f"state.{name}", budget)


def describe_request_order(state: object, questions: object) -> dict:
    """Describe only the supported order-sensitive request collections."""
    if type(state) is not dict or type(questions) is not dict:
        raise RequestOrderError("model request state and questions must be objects")
    budget = _OrderBudget()
    question_ids = _ordered_keys(questions, "questions", budget)
    criteria_ids = {}
    score_criteria = {}
    for question_id in question_ids:
        question = questions[question_id]
        if type(question) is not dict:
            continue
        criteria = question.get("criteria")
        if type(criteria) is dict:
            budget.add([question_id], "criteria question ID")
            criteria_ids[question_id] = _ordered_keys(
                criteria, f"questions[{question_id!r}].criteria", budget)
        elif question.get("type") == "score":
            if type(criteria) not in (list, tuple):
                raise RequestOrderError("score criteria must be an ordered list")
            budget.add([question_id], "score question ID")
            budget.add(criteria, f"questions[{question_id!r}].criteria", nonempty=False)
            score_criteria[question_id] = list(criteria)
    order = {
        "schema": ORDER_SCHEMA,
        "question_ids": question_ids,
        "criteria_ids": criteria_ids,
        "score_criteria": score_criteria,
        "candidate_plan_ids": _map_collection(state, "candidate_plans", budget),
        "candidate_evidence_ids": _map_collection(state, "candidate_evidence", budget),
        "shared_plan_material_ids": _map_collection(state, "shared_plan_materials", budget),
    }
    try:
        encoded = json.dumps(order, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError) as error:
        raise RequestOrderError("request order metadata is not finite JSON") from error
    if len(encoded) > MAX_ORDER_METADATA_BYTES:
        raise RequestOrderError("request order metadata exceeds the supported size")
    return order


def _validate_order_list(value: object, expected: list[str], field: str) -> list[str]:
    if type(value) is not list:
        raise RequestOrderError(f"{field} must be an ordered list")
    _OrderBudget().add(value, field, nonempty=False)
    if len(value) != len(set(value)) or set(value) != set(expected):
        raise RequestOrderError(f"{field} conflicts with the captured request")
    return list(value)


def _restore_map(value: dict, order: object, expected: list[str], field: str) -> dict:
    ids = _validate_order_list(order, expected, field)
    return {key: value[key] for key in ids}


def reconstruct_request(state: object, questions: object, order: object) -> dict:
    """Validate an order claim and restore only the collections it names.

    Missing order metadata is handled by the replay caller as unavailable, not
    guessed from the sorted event object. A present but malformed claim fails.
    """
    if type(order) is not dict or set(order) != {
        "schema", "question_ids", "criteria_ids", "score_criteria",
        "candidate_plan_ids", "candidate_evidence_ids", "shared_plan_material_ids",
    } or order.get("schema") != ORDER_SCHEMA:
        raise RequestOrderError("request order metadata has an unsupported schema")
    try:
        metadata_size = len(json.dumps(
            order, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, OverflowError) as error:
        raise RequestOrderError("request order metadata is not finite JSON") from error
    if metadata_size > MAX_ORDER_METADATA_BYTES:
        raise RequestOrderError("request order metadata exceeds the supported size")
    expected = describe_request_order(state, questions)
    if type(order.get("criteria_ids")) is not dict \
            or set(order["criteria_ids"]) != set(expected["criteria_ids"]):
        raise RequestOrderError("criteria order metadata conflicts with the request")
    if type(order.get("score_criteria")) is not dict \
            or order["score_criteria"] != expected["score_criteria"]:
        raise RequestOrderError("score criteria order conflicts with the request")

    question_order = _validate_order_list(
        order.get("question_ids"), expected["question_ids"], "question_ids")
    detached = snapshot_request(state, questions)
    restored_questions = detached["questions"]
    for question_id, criteria_order in order["criteria_ids"].items():
        criteria = restored_questions[question_id].get("criteria")
        restored_questions[question_id]["criteria"] = _restore_map(
            criteria, criteria_order, expected["criteria_ids"][question_id],
            f"criteria_ids[{question_id!r}]",
        )
    detached["questions"] = {key: restored_questions[key] for key in question_order}

    for name, field in (
        ("candidate_plan_ids", "candidate_plans"),
        ("candidate_evidence_ids", "candidate_evidence"),
        ("shared_plan_material_ids", "shared_plan_materials"),
    ):
        expected_ids = expected[name]
        claimed_ids = order.get(name)
        if expected_ids is None:
            if claimed_ids is not None:
                raise RequestOrderError(f"{name} claims an absent request collection")
            continue
        if claimed_ids is None:
            raise RequestOrderError(f"{name} omits a present request collection")
        detached["state"][field] = _restore_map(
            detached["state"][field], claimed_ids, expected_ids, name)
    return detached
