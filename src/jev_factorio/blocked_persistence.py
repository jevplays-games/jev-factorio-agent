"""Explicit, fail-closed polling for two recoverable blocked decisions.

This module never chooses a plan or grants dispatch authority. It records a
content fingerprint before a provider request so an unchanged decision input
cannot be billed again after a restart.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy


RECOVERABLE_REASONS = frozenset({
    "Candidate evidence insufficient", "low choice confidence",
})
_PROVIDER_OPERATOR_EVENT = "provider_circuit_operator_recovery_required"
_PROVIDER_CATEGORIES = frozenset({
    "application_schema", "service_network", "authentication_authorization",
    "account_quota", "rate_limit", "unknown_outcome", "unknown",
})
MAX_ATTEMPTS = 1024
MAX_WAIT_LEVEL = 9
MAX_WAIT_SECONDS = 300.0
# Both wait paths reach this delay (2 ** 8) before the 300 s cap applies to one of them.
IDLE_DELAY_SECONDS = 256.0
DEFAULT_IDLE_OBSERVATIONS = 0
MAX_IDLE_OBSERVATIONS = 1000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_CLOCK_FACTORY_RECEIPT = re.compile(r"[0-9]+:(factory_insert|factory_extract):([^:]+:.+)\Z")
_CLOCK_RECEIPT = re.compile(r"[0-9]+:(.+)\Z")
_BACKGROUND_WAIT_ID = re.compile(r"background-wait:(.+)\Z")
_PLANNED_RECEIPT_KEYS = {"receipt", "planned_native_receipt_id"}
_MAX_SELECTION_BATCH_CANDIDATES = 254
MAX_SELECTION_BATCHES_PER_STATE = 3
_ROUTE_DIAGNOSTIC_CLOCK_KEYS = frozenset({"cached", "survey_tick", "next_survey_tick"})
_VOLATILE_KEYS = frozenset({
    "tick", "observed_tick", "checked_tick", "last_tick", "started_tick", "finished_tick",
    "stalled_decisions", "recorded_at_utc", "timestamp", "provider_clock", "monotonic_ns",
    "duration_ms", "duration_ns", "wall_duration_ns", "process_cpu_ns", "thread_cpu_ns",
    "trace_id", "event_id", "decision_id", "model_call_id", "request_id",
})
_SYSTEM_HISTORY_EVENTS = frozenset({
    "blocked_decision_reevaluation_consumed", "blocked_recovery_attempt", "blocked_recovery_wait",
    "blocked_recovery_archive_committed", "blocked_recovery_alternatives_exhausted",
    "blocked_recovery_alternative_batch_limit",
})


def _source(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {"commit", "source_sha256"}
            or type(value.get("commit")) is not str or not _COMMIT.fullmatch(value["commit"])
            or type(value.get("source_sha256")) is not str
            or not _SHA256.fullmatch(value["source_sha256"])):
        raise ValueError("Persistent blocked recovery requires pinned source provenance")
    return {"commit": value["commit"], "source_sha256": value["source_sha256"]}


def _canonical_receipt(value: str) -> str:
    """Remove only the current-tick component of known planned receipt forms.

    Native dispatch still receives the original receipt. The decision fingerprint
    retains action, role, item, layout, part, and other receipt identity fields.
    """
    match = _CLOCK_FACTORY_RECEIPT.fullmatch(value)
    if match is not None:
        return "<current-tick>:" + match.group(1) + ":" + match.group(2)
    match = _CLOCK_RECEIPT.fullmatch(value)
    if match is not None:
        # Planner-owned construction receipts use `tick:layout-or-target:part`.
        return "<current-tick>:" + match.group(1)
    match = re.fullmatch(r"(launch:(?:pad|load|fish):)[0-9]+", value)
    if match is not None:
        return match.group(1) + "<current-tick>"
    match = re.fullmatch(r"buffer:[0-9]+:(.+)", value)
    if match is not None:
        return "buffer:<current-tick>:" + match.group(1)
    return value


def _canonical_planned_id(value: str) -> str:
    match = _BACKGROUND_WAIT_ID.fullmatch(value)
    if match is None:
        return value
    return "background-wait:" + _canonical_receipt(match.group(1))


def _candidate_id_map(candidate_ids: list[str]) -> dict[str, str]:
    mapping = {}
    canonical_ids = set()
    for candidate_id in candidate_ids:
        if type(candidate_id) is not str or not candidate_id or len(candidate_id) > 512:
            raise ValueError("Invalid persistent selection candidate ID")
        canonical = _canonical_planned_id(candidate_id)
        if canonical in canonical_ids:
            raise ValueError("Persistent selection candidate IDs collide after clock normalization")
        mapping[candidate_id] = canonical
        canonical_ids.add(canonical)
    return mapping


def _validate_selection_batch(value: object) -> dict:
    required = {"schema", "state_sha256", "frontier_sha256", "request_sha256", "offered"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value.get("schema")) is not int or value["schema"] != 1
            or any(type(value.get(key)) is not str or not _SHA256.fullmatch(value[key])
                   for key in ("state_sha256", "frontier_sha256", "request_sha256"))
            or not isinstance(value.get("offered"), list)
            or not 1 <= len(value["offered"]) <= _MAX_SELECTION_BATCH_CANDIDATES):
        raise ValueError("Invalid persistent selection batch metadata")
    seen_ids = set()
    seen_candidates = set()
    for row in value["offered"]:
        if (not isinstance(row, dict) or set(row) != {"plan_id", "candidate_sha256"}
                or type(row.get("plan_id")) is not str or not row["plan_id"]
                or len(row["plan_id"]) > 512
                or type(row.get("candidate_sha256")) is not str
                or not _SHA256.fullmatch(row["candidate_sha256"])):
            raise ValueError("Invalid persistent offered candidate metadata")
        if row["plan_id"] in seen_ids or row["candidate_sha256"] in seen_candidates:
            raise ValueError("Duplicate persistent offered candidate metadata")
        seen_ids.add(row["plan_id"])
        seen_candidates.add(row["candidate_sha256"])
    if value["offered"] != sorted(value["offered"], key=lambda item: (item["plan_id"], item["candidate_sha256"])):
        raise ValueError("Persistent offered candidate metadata is not canonical")
    return value


def _replace_candidate_ids(value, mapping: dict[str, str]):
    """Canonicalize dynamic plan IDs in request keys and explanatory text."""
    ordered = sorted(mapping.items(), key=lambda pair: len(pair[0]), reverse=True)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Selection request contains a non-string key")
            new_key = mapping.get(key, key)
            if key not in mapping:
                for old, new in ordered:
                    if new_key.startswith(old + "/"):
                        new_key = new + new_key[len(old):]
                        break
            if new_key in result:
                raise ValueError("Selection request candidate keys collide after normalization")
            result[new_key] = _replace_candidate_ids(item, mapping)
        return result
    if isinstance(value, list):
        return [_replace_candidate_ids(item, mapping) for item in value]
    if isinstance(value, tuple):
        return [_replace_candidate_ids(item, mapping) for item in value]
    if type(value) is str:
        result = value
        for old, new in ordered:
            if old == new:
                continue
            # IDs appear as question keys and as candidate_plans pointers.
            # Replace only a delimited identifier, never an arbitrary substring.
            pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old)
                                 + r"(?![A-Za-z0-9_])")
            result = pattern.sub(lambda _match, replacement=new: replacement, result)
        return result
    return value


def _selection_request_sha256(context: dict, questions: dict, plans: list[dict], *,
                              current_tick: int) -> str:
    raw_ids = [plan.get("id") for plan in plans if isinstance(plan, dict)]
    if len(raw_ids) != len(plans) or any(type(value) is not str for value in raw_ids):
        raise ValueError("Invalid plans in persistent selection request")
    mapping = _candidate_id_map(raw_ids)
    # Walk the prompt context as its own root so the existing stable-value
    # rules see candidate_plans/candidate_evidence at their normal paths. In
    # particular, planned craft-job UUIDs must not create a fresh fingerprint
    # when the compiler emits the same candidate again on the next tick.
    canonical_context = _replace_candidate_ids(deepcopy(context), mapping)
    canonical_questions = _replace_candidate_ids(deepcopy(questions), mapping)
    stable_body = {
        "state": _stable(canonical_context, current_tick=current_tick),
        "questions": _stable(canonical_questions, current_tick=current_tick),
    }
    encoded = json.dumps(stable_body, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_semantic_sha256(plan: dict, evidence: object, *, current_tick: int) -> str:
    if not isinstance(plan, dict) or type(plan.get("id")) is not str:
        raise ValueError("Invalid persistent selection candidate")
    payload = _stable({"plans": [deepcopy(plan)],
                       "candidate_evidence": {"candidate": deepcopy(evidence)}},
                      current_tick=current_tick)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def selection_state_sha256(state: dict, plans: list[dict], *, session_id: str,
                           source_revision: dict, target: str, policy: str,
                           confidence_floor: float, current_tick: int) -> str:
    """Bind recovery batches to unchanged native decision facts, not plan order.

    Candidate-specific evidence is bound by each candidate digest; the state
    key deliberately excludes that derived map so a newly surfaced candidate
    cannot make an already offered candidate look unseen. Native facts, active
    objective/history, source contract, session, and policy remain in the key.
    """
    if not isinstance(state, dict) or not isinstance(plans, list):
        raise ValueError("Invalid persistent selection frontier")
    plan_ids = [plan.get("id") for plan in plans if isinstance(plan, dict)]
    if len(plan_ids) != len(plans):
        raise ValueError("Invalid persistent selection candidate")
    _candidate_id_map(plan_ids)
    stable_state = deepcopy(state)
    stable_state.pop("candidate_evidence", None)
    stable_state.pop("candidate_plans", None)
    stable_state.pop("deterministic_ranking", None)
    payload = {
        "schema": 1, "kind": "persistent_selection_state", "session_id": session_id,
        "source_revision": _source(source_revision), "target": target, "policy": policy,
        "confidence_floor": confidence_floor,
        "state": _stable(stable_state, current_tick=current_tick),
    }
    if (type(session_id) is not str or not session_id or type(target) is not str or not target
            or policy != "jev" or type(current_tick) is not int or current_tick < 0
            or type(confidence_floor) not in {int, float} or not 0 <= confidence_floor <= 1):
        raise ValueError("Invalid persistent selection state identity")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def selection_frontier_sha256(state: dict, plans: list[dict], *, session_id: str,
                              source_revision: dict, target: str, policy: str,
                              confidence_floor: float, current_tick: int) -> str:
    """Bind a candidate frontier independent of candidate ordering."""
    state_sha256 = selection_state_sha256(
        state, plans, session_id=session_id, source_revision=source_revision,
        target=target, policy=policy, confidence_floor=confidence_floor,
        current_tick=current_tick)
    evidence = state.get("candidate_evidence", {}) if isinstance(state, dict) else {}
    if not isinstance(evidence, dict):
        raise ValueError("Invalid persistent candidate evidence map")
    candidates = []
    for plan in plans:
        if not isinstance(plan, dict) or type(plan.get("id")) is not str:
            raise ValueError("Invalid persistent selection candidate")
        canonical_id = _canonical_planned_id(plan["id"])
        candidates.append({
            "plan_id": canonical_id,
            "candidate_sha256": _candidate_semantic_sha256(
                plan, evidence.get(plan["id"]), current_tick=current_tick),
        })
    if len({row["plan_id"] for row in candidates}) != len(candidates):
        raise ValueError("Persistent selection candidate IDs collide after clock normalization")
    candidates.sort(key=lambda row: (row["plan_id"], row["candidate_sha256"]))
    payload = {"schema": 1, "kind": "persistent_selection_frontier",
               "state_sha256": state_sha256, "candidates": candidates}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _selection_semantic_evidence(context: dict) -> dict:
    """Resolve lossless wire recipe references before candidate identity hashing.

    The exact prepared request keeps its original representation and digest.
    Candidate identity instead matches the compiler's complete evidence records.
    """
    evidence = context.get("candidate_evidence", {})
    if not isinstance(evidence, dict):
        raise ValueError("Invalid prepared candidate evidence")
    if "shared_recipes" not in context:
        return evidence
    shared = context["shared_recipes"]
    if not isinstance(shared, dict) or not shared:
        raise ValueError("Invalid shared recipe records")
    for key, recipe in shared.items():
        if (type(key) is not str or not isinstance(recipe, dict)
                or recipe.get("name") != key
                or not {"name", "category", "ingredients", "products", "enabled"} <= set(recipe)):
            raise ValueError("Invalid complete shared recipe")
    def restore(value):
        if isinstance(value, dict):
            if "shared_recipe_key" in value:
                key = value["shared_recipe_key"]
                if set(value) != {"shared_recipe_key"} or type(key) is not str or key not in shared:
                    raise ValueError("Invalid shared recipe reference")
                recipe = shared[key]
                # Factoring never creates nested references inside complete recipes.
                def reserved(record):
                    if isinstance(record, dict):
                        return "shared_recipe_key" in record or any(reserved(v) for v in record.values())
                    return isinstance(record, list) and any(reserved(v) for v in record)
                if reserved(recipe):
                    raise ValueError("Nested shared recipe reference")
                return deepcopy(recipe)
            return {key: restore(child) for key, child in value.items()}
        if isinstance(value, list):
            return [restore(child) for child in value]
        return deepcopy(value)
    return restore(evidence)


def selection_batch_metadata(context: dict, questions: dict, offered: list,
                             *, state_sha256: str, frontier_sha256: str,
                             current_tick: int) -> dict:
    """Create a bounded, stable identity for the exact prepared request batch."""
    if (not isinstance(context, dict) or not isinstance(questions, dict)
            or not isinstance(offered, list) or not 1 <= len(offered) <= _MAX_SELECTION_BATCH_CANDIDATES
            or type(current_tick) is not int or current_tick < 0
            or type(state_sha256) is not str or not _SHA256.fullmatch(state_sha256)
            or type(frontier_sha256) is not str or not _SHA256.fullmatch(frontier_sha256)):
        raise ValueError("Invalid persistent selection batch")
    plan_rows = []
    for plan in offered:
        row = plan.to_dict() if hasattr(plan, "to_dict") else plan
        if not isinstance(row, dict) or type(row.get("id")) is not str:
            raise ValueError("Invalid offered persistent selection plan")
        plan_rows.append(deepcopy(row))
    raw_ids = [row["id"] for row in plan_rows]
    if len(raw_ids) != len(set(raw_ids)):
        raise ValueError("Duplicate offered persistent selection candidate IDs")
    mapping = _candidate_id_map(raw_ids)
    evidence = _selection_semantic_evidence(context)
    request_sha256 = _selection_request_sha256(
        context, questions, plan_rows, current_tick=current_tick)
    offered_rows = []
    for row in plan_rows:
        plan_id = row["id"]
        offered_rows.append({
            "plan_id": mapping[plan_id],
            "candidate_sha256": _candidate_semantic_sha256(
                row, evidence.get(plan_id), current_tick=current_tick),
        })
    offered_rows.sort(key=lambda item: (item["plan_id"], item["candidate_sha256"]))
    metadata = {"schema": 1, "state_sha256": state_sha256,
                "frontier_sha256": frontier_sha256, "request_sha256": request_sha256,
                "offered": offered_rows}
    return _validate_selection_batch(metadata)


def _stable(value, *, path: tuple = (), current_tick: int | None = None,
            background_wait: bool = False):
    if isinstance(value, dict):
        candidate_step = (
            (len(path) == 4 and path[0] == "plans" and type(path[1]) is int
             and path[2] == "steps" and type(path[3]) is int)
            or (len(path) == 4 and path[0] == "candidate_plans"
                and type(path[1]) is str and path[2] == "steps"
                and type(path[3]) is int)
        )
        parameters = value.get("parameters")
        if (candidate_step and value.get("action") == "factory_craft_job"
                and value.get("effect") == "craft_job_complete"
                and isinstance(parameters, dict)
                and type(parameters.get("receipt")) is str
                and re.fullmatch(r"[0-9a-f]{32}", parameters["receipt"])):
            # Background compilation allocates a fresh UUID before selection.
            # Its random value is not new evidence for a rejected candidate.
            # Keep the actual dispatched/native receipt and pending jobs intact.
            value = {**value, "parameters": {
                **parameters, "receipt": "<planned-craft-job-receipt>"}}
        route_diagnostic = (
            (len(path) >= 4 and path[0] == "candidate_plans"
             and type(path[1]) is str
             and path[2:4] == ("materials", "route_diagnostics"))
            or (len(path) >= 4 and path[0] == "plans"
                and type(path[1]) is int
                and path[2:4] == ("materials", "route_diagnostics"))
            or tuple(path[:4]) == ("facts", "factory", "input_routes", "diagnostics")
        )
        background_wait = background_wait or (
            type(value.get("id")) is str and value["id"].startswith("background-wait:")
        )
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Decision input contains a non-string key")
            normalized_key = key.casefold()
            if route_diagnostic and normalized_key in _ROUTE_DIAGNOSTIC_CLOCK_KEYS:
                # Input-route cache age is planner refresh bookkeeping. A due
                # refresh can change the substantive route evidence below, but
                # the cached bit and survey timestamps alone must not authorize
                # another model request for an unchanged native decision.
                continue
            if normalized_key in _VOLATILE_KEYS or normalized_key.endswith("_duration_ms"):
                continue
            if path == () and key == "history" and isinstance(item, list):
                # These controller-only events report polling/authorization; they
                # are intentionally not part of the Jev decision question.
                item = [event for event in item if not (
                    isinstance(event, dict) and event.get("kind") in _SYSTEM_HISTORY_EVENTS)]
            if key == "research_deadline_tick":
                if current_tick is not None and type(item) is int:
                    # This planner field is a projected deadline, computed as
                    # now plus the estimated refill horizon. Preserve that
                    # estimate rather than letting the clock alone change it.
                    result["research_horizon_ticks"] = item - current_tick
                    continue
            if (key in {"next_check_tick", "deadline_tick", "timeout_tick"}
                    or normalized_key.endswith("_deadline_tick")):
                if (current_tick is not None and type(item) is int):
                    # These are absolute scheduled horizons. Keep the horizon
                    # itself; remaining-tick encoding would change every poll.
                    canonical_key = (key if normalized_key.endswith("_deadline_tick")
                                     else key.removesuffix("_tick") + "_deadline_tick")
                    result[canonical_key] = item
                    continue
            if key == "timeout_ticks" and current_tick is not None and type(item) is int:
                # The planner expresses this wait as remaining time to the
                # durable native job deadline. Preserve that absolute horizon.
                if background_wait:
                    result["wait_absolute_deadline_tick"] = current_tick + item
                    continue
            if key in {"id", "plan_id"} and type(item) is str:
                item = _canonical_planned_id(item)
            if (key in _PLANNED_RECEIPT_KEYS and type(item) is str
                    and ("steps" in path or key == "planned_native_receipt_id")):
                item = _canonical_receipt(item)
            if (type(item) is str and path[:1] == ("candidate_evidence",)
                    and ((key == "planned_native_receipt"
                          and "utility_power_prerequisite_start_evidence" in path)
                         or (key == "native_receipt"
                             and path[-1:] == ("fuel_transfer_start_evidence",)))):
                # These witnesses describe a future paid transfer, including
                # the same witness nested under a power prerequisite. Keep
                # observed receipt journals intact; only planned clock prefixes
                # are irrelevant to an unchanged blocked decision.
                item = _canonical_receipt(item)
            result[key] = _stable(item, path=(*path, key), current_tick=current_tick,
                                  background_wait=background_wait)
        return result
    if isinstance(value, (list, tuple)):
        return [_stable(item, path=(*path, index), current_tick=current_tick,
                        background_wait=background_wait)
                for index, item in enumerate(value)]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and value == value and abs(value) != float("inf"):
        return value
    raise ValueError("Decision input contains an unsupported value")


def decision_input_sha256(state: dict, plans: list[dict], *, session_id: str,
                          source_revision: dict, target: str, policy: str,
                          confidence_floor: float, current_tick: int,
                          questions: dict | None = None,
                          selection_batch: dict | None = None) -> str:
    """Hash the actual decision facts/candidates while excluding known clocks."""
    if (type(session_id) is not str or not session_id
            or type(target) is not str or not target or policy != "jev"
            or type(current_tick) is not int or current_tick < 0
            or type(confidence_floor) not in {int, float} or not 0 <= confidence_floor <= 1
            or not isinstance(state, dict) or not isinstance(plans, list)):
        raise ValueError("Invalid persistent blocked decision input")
    payload = {
        "schema": 1, "session_id": session_id, "source_revision": _source(source_revision),
        "target": target, "policy": policy, "confidence_floor": confidence_floor,
        "state": _stable(deepcopy(state), current_tick=current_tick),
        "plans": _stable(deepcopy(plans), path=("plans",), current_tick=current_tick),
    }
    if questions is not None or selection_batch is not None:
        if not isinstance(questions, dict) or selection_batch is None:
            raise ValueError("A persistent prepared request requires its batch metadata")
        batch = _validate_selection_batch(selection_batch)
        expected = selection_batch_metadata(
            state, questions, plans, state_sha256=batch["state_sha256"],
            frontier_sha256=batch["frontier_sha256"], current_tick=current_tick)
        if expected != batch:
            raise ValueError("Persistent selection batch does not match its prepared request")
        payload["selection_batch"] = batch
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def planner_input_sha256(snapshot, plans: list[dict], blocker: str, *,
                         source_revision: dict, target: str) -> str:
    """Fingerprint a no-candidate frontier without authorizing a model call."""
    payload = {"schema": 1, "kind": "candidate_frontier", "session_id": snapshot.session_id,
               "source_revision": _source(source_revision), "target": target,
               "state": snapshot.for_jev(), "plans": plans, "blocker": blocker}
    encoded = json.dumps(_stable(payload, current_tick=snapshot.tick), sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_state(value: object, session_id: str) -> dict:
    keys = {"schema", "session_id", "source_revision", "attempts", "last_input_sha256", "wait_level"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["session_id"] != session_id):
        raise ValueError("Invalid persistent blocked-recovery state")
    _source(value["source_revision"])
    attempts = value["attempts"]
    if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
        raise ValueError("Persistent blocked-recovery attempt ledger is invalid or full")
    seen = set()
    selection_counts = {}
    selection_candidates = {}
    for row in attempts:
        legacy_required = {"source_revision", "decision_input_sha256", "reason", "tick"}
        required = legacy_required | {"outcome"}
        batched_required = required | {"selection_batch"}
        if not isinstance(row, dict) or frozenset(row) not in {
                frozenset(legacy_required), frozenset(required), frozenset(batched_required)}:
            raise ValueError("Invalid persistent blocked-recovery attempt")
        # Older local prototypes did not distinguish an in-flight call from a
        # completed rejection. Treat those rows as ambiguous and never replay.
        outcome = row.get("outcome", "pending")
        if (type(outcome) is not str
                or outcome not in {"pending", "rejected", "selected", "provider_blocked", "failed", "frontier"}
                or row["reason"] is not None and (
                    type(row["reason"]) is not str or not is_recoverable_reason(row["reason"]))
                or type(row["tick"]) is not int or row["tick"] < 0
                or type(row["decision_input_sha256"]) is not str
                or not _SHA256.fullmatch(row["decision_input_sha256"])):
            raise ValueError("Invalid persistent blocked-recovery attempt")
        if "selection_batch" in row:
            batch = _validate_selection_batch(row["selection_batch"])
            batch_source = _source(row["source_revision"])
            state_key = (batch_source["commit"], batch_source["source_sha256"],
                         batch["state_sha256"])
            selection_counts[state_key] = selection_counts.get(state_key, 0) + 1
            if selection_counts[state_key] > MAX_SELECTION_BATCHES_PER_STATE:
                raise ValueError("Persistent selection batch limit is exceeded for one state")
            observed = selection_candidates.setdefault(state_key, set())
            offered_digests = {item["candidate_sha256"] for item in batch["offered"]}
            if observed.intersection(offered_digests):
                raise ValueError("Persistent selection candidate was already offered for this state")
            observed.update(offered_digests)
        row_source = _source(row["source_revision"])
        key = (row_source["commit"], row_source["source_sha256"], row["decision_input_sha256"])
        if key in seen:
            raise ValueError("Duplicate persistent blocked-recovery attempt")
        seen.add(key)
    if (value["last_input_sha256"] is not None
            and (type(value["last_input_sha256"]) is not str
                 or not _SHA256.fullmatch(value["last_input_sha256"]))):
        raise ValueError("Invalid persistent blocked-recovery fingerprint")
    if (type(value["wait_level"]) is not int
            or not 0 <= value["wait_level"] <= MAX_WAIT_LEVEL):
        raise ValueError("Invalid persistent blocked-recovery backoff")
    return value



def is_recoverable_reason(reason: object) -> bool:
    """Classify refusal for observation; never grant selection or another bill.

    The immutable fingerprint protocol's legacy reason set remains unchanged.
    Abstention extends runtime/ledger recovery only; all input identities and
    selection limits are still validated by the original protocol.
    """
    return type(reason) is str and (reason in RECOVERABLE_REASONS or reason == "model abstention")


def _has_provider_operator_handoff(reason: object, history: object, attempts: list) -> bool:
    """Validate the terminal provider handoff without making it auto-recoverable."""
    if not isinstance(reason, str) or not isinstance(history, list):
        return False
    provider_inputs = {
        row.get("decision_input_sha256") for row in attempts
        if isinstance(row, dict) and row.get("outcome") == "provider_blocked"
    }
    for event in history:
        if (not isinstance(event, dict)
                or event.get("kind") != _PROVIDER_OPERATOR_EVENT
                or event.get("decision_input_sha256") not in provider_inputs
                or type(event.get("model_called")) is not bool
                or event.get("category") not in _PROVIDER_CATEGORIES
                or event.get("provider_phase") not in {"cooldown", "exhausted", "blocked"}):
            continue
        attempts_count = event.get("attempts")
        budget_limit = event.get("budget_limit")
        if (attempts_count is not None
                and (type(attempts_count) is not int or attempts_count < 0)):
            continue
        if (budget_limit is not None
                and (type(budget_limit) is not int or budget_limit < 1)):
            continue
        attempts_text = "unknown" if attempts_count is None else str(attempts_count)
        budget_text = "unknown" if budget_limit is None else str(budget_limit)
        expected_reason = (
            "Provider circuit requires operator recovery: "
            f"{event['category']} ({event['provider_phase']}, probes "
            f"{attempts_text}/{budget_text})")
        if event.get("reason") == reason == expected_reason:
            return True
    return False


def validate_memory_state(memory, current_source: dict, *, allow_source_change: bool = False) -> None:
    current = _source(current_source)
    if memory.blocked_recovery is None:
        if memory.status == "blocked" and is_recoverable_reason(memory.reason) and not allow_source_change:
            raise ValueError("Blocked resume requires a prior durable recovery attempt or source authorization")
        return
    state = _validate_state(memory.blocked_recovery, memory.session_id)
    if (memory.status == "blocked" and not state["attempts"]
            and not allow_source_change):
        raise ValueError("First blocked recovery requires explicit changed-contract authorization")
    if state["source_revision"] != current and not (
            allow_source_change and memory.status == "blocked"
            and is_recoverable_reason(memory.reason)):
        raise ValueError("Persistent blocked-recovery source changed; explicit source authorization is required")
    if (memory.status == "blocked" and not is_recoverable_reason(memory.reason)
            and not _has_provider_operator_handoff(
                memory.reason, memory.history, state["attempts"])):
        raise ValueError("Persistent recovery does not admit this blocked reason")


def validate_checkpoint_metadata(data: object, current_source: dict, *,
                                 allow_source_change: bool = False,
                                 owner_context: dict | None = None) -> None:
    """Pre-backend CLI guard for a persistent recovery checkpoint."""
    if not isinstance(data, dict):
        raise ValueError("Persistent blocked-recovery checkpoint is malformed")
    from types import SimpleNamespace
    from .compatible_recovery import validate_current_owner
    validate_current_owner(SimpleNamespace(
        session_id=data.get("session_id"), target=data.get("target"),
        compatible_source_recoveries=data.get("compatible_source_recoveries", []),
        blocked_reevaluations=data.get("blocked_reevaluations"),
        history=data.get("history")), owner_context)
    status, reason = data.get("status"), data.get("reason")
    state = data.get("blocked_recovery")
    session_id = data.get("session_id")
    if type(session_id) is not str or not session_id:
        raise ValueError("Persistent blocked-recovery session identity is missing")
    if state is None:
        if status == "blocked" and not is_recoverable_reason(reason):
            raise ValueError("Persistent recovery does not admit this blocked reason")
        if status == "blocked" and not allow_source_change:
            raise ValueError("Blocked resume requires source-authorized first recovery")
        return
    state = _validate_state(state, session_id)
    if status == "blocked" and not state["attempts"] and not allow_source_change:
        raise ValueError("First blocked recovery requires explicit changed-contract authorization")
    if state["source_revision"] != _source(current_source) and not (
            allow_source_change and status == "blocked"
            and is_recoverable_reason(reason)):
        raise ValueError("Persistent blocked-recovery source changed; explicit source authorization is required")
    if (status == "blocked" and not is_recoverable_reason(reason)
            and not _has_provider_operator_handoff(
                reason, data.get("history"), state["attempts"])):
        raise ValueError("Persistent recovery does not admit this blocked reason")


def ensure_state(memory, source_revision: dict, *, allow_source_change: bool = False) -> dict:
    source = _source(source_revision)
    if memory.blocked_recovery is None:
        memory.blocked_recovery = {
            "schema": 1, "session_id": memory.session_id, "source_revision": source,
            "attempts": [], "last_input_sha256": None, "wait_level": 0,
        }
    state = _validate_state(memory.blocked_recovery, memory.session_id)
    if state["source_revision"] != source:
        if not (allow_source_change and memory.status == "blocked"
                and is_recoverable_reason(memory.reason)):
            raise ValueError("Persistent blocked-recovery source changed")
        state["source_revision"] = source
    return state


def was_attempted(memory, source_revision: dict, input_sha256: str, *,
                  allow_source_change: bool = False, archive_index=None) -> bool:
    source = _source(source_revision)
    if memory.blocked_recovery_archive is not None and archive_index is None:
        raise ValueError("Blocked-recovery archive index is required for fingerprint lookup")
    if archive_index is not None and archive_index.find(
            source, input_sha256, memory=memory) is not None:
        return True
    current = memory.blocked_recovery
    if (allow_source_change and current is not None
            and current.get("source_revision") != source):
        # Do not mutate source lineage merely while checking a fingerprint; the
        # source authorization and first attempt are consumed together later.
        return False
    state = ensure_state(memory, source, allow_source_change=allow_source_change)
    return any(row["source_revision"] == source
               and row["decision_input_sha256"] == input_sha256 for row in state["attempts"])


def find_attempt(memory, source_revision: dict, input_sha256: str, *, archive_index=None) -> dict | None:
    """Return a copy of one exact source-bound ledger row, if present."""
    source = _source(source_revision)
    if memory.blocked_recovery_archive is not None and archive_index is None:
        raise ValueError("Blocked-recovery archive index is required for fingerprint lookup")
    # Consult the verified archive index first so its checkpoint binding and
    # immutable files are revalidated on every lookup. The index also contains
    # a startup snapshot of the active tail for duplicate detection, but those
    # rows are mutable: their pending outcome may have been finalized since
    # index construction. Prefer the current checkpoint memory for active rows.
    archived = (archive_index.find(source, input_sha256, memory=memory)
                if archive_index is not None else None)
    if memory.blocked_recovery is not None:
        state = _validate_state(memory.blocked_recovery, memory.session_id)
        for row in state["attempts"]:
            if row["source_revision"] == source and row["decision_input_sha256"] == input_sha256:
                result = deepcopy(row)
                result.setdefault("outcome", "pending")
                return result
    return archived


def selection_attempts_for_state(memory, source_revision: dict, state_sha256: str,
                                 *, archive_index=None, compatible_state_hashes=None) -> list[dict]:
    """Return verified prepared-batch WAL rows for one source-bound game state."""
    source = _source(source_revision)
    if type(state_sha256) is not str or not _SHA256.fullmatch(state_sha256):
        raise ValueError("Invalid persistent selection state fingerprint")
    if memory.blocked_recovery_archive is not None and archive_index is None:
        raise ValueError("Blocked-recovery archive index is required for selection history")
    from .compatible_recovery import approved_sources
    approved = approved_sources(memory, source)
    aliases = [(source, state_sha256)]
    if compatible_state_hashes is not None:
        if (not isinstance(compatible_state_hashes, list)
                or any(not isinstance(row, tuple) or len(row) != 2
                       for row in compatible_state_hashes)
                or [row[0] for row in compatible_state_hashes] != approved
                or any(type(row[1]) is not str or not _SHA256.fullmatch(row[1])
                       for row in compatible_state_hashes)
                or compatible_state_hashes[-1] != (source, state_sha256)):
            raise ValueError("Persistent selection aliases are not authorized compatible lineage")
        aliases = compatible_state_hashes
    elif len(approved) > 1:
        raise ValueError("Compatible-source selection requires complete source-bound state aliases")
    rows = {}
    if archive_index is not None:
        for alias_source, alias_hash in aliases:
            for row in archive_index.selection_attempts(
                    alias_source, alias_hash, memory=memory):
                key = (row["source_revision"]["commit"], row["source_revision"]["source_sha256"],
                       row["decision_input_sha256"])
                rows[key] = deepcopy(row)
    if memory.blocked_recovery is not None:
        state = _validate_state(memory.blocked_recovery, memory.session_id)
        for row in state["attempts"]:
            alias_hash = next((digest for revision, digest in aliases
                               if row["source_revision"] == revision), None)
            if alias_hash is None:
                continue
            batch = row.get("selection_batch")
            if isinstance(batch, dict) and batch["state_sha256"] == alias_hash:
                key = (row["source_revision"]["commit"], row["source_revision"]["source_sha256"],
                       row["decision_input_sha256"])
                rows[key] = deepcopy(row)
    from .paid_selection_reconciliation import scoped_representation_budget_rows
    carried, _ = scoped_representation_budget_rows(memory,aliases,archive_index=archive_index)
    for row in carried:
        key=(row["source_revision"]["commit"],row["source_revision"]["source_sha256"],row["decision_input_sha256"])
        rows[key]=deepcopy(row)
    result = [rows[key] for key in sorted(rows)]
    if len(result) > MAX_SELECTION_BATCHES_PER_STATE:
        raise ValueError("Persistent selection batch limit is exceeded for one state")
    seen_candidates = set()
    for row in result:
        batch = _validate_selection_batch(row.get("selection_batch"))
        for offered in batch["offered"]:
            candidate_sha256 = offered["candidate_sha256"]
            if candidate_sha256 in seen_candidates:
                raise ValueError("Persistent selection candidate was already offered for this state")
            seen_candidates.add(candidate_sha256)
    return result


def record_attempt(memory, source_revision: dict, input_sha256: str,
                   reason: str | None, tick: int, *, allow_source_change: bool = False,
                   archive_index=None, selection_batch: dict | None = None) -> None:
    if (reason is not None and (type(reason) is not str or not is_recoverable_reason(reason))
            or not _SHA256.fullmatch(input_sha256)
            or type(tick) is not int or tick < 0):
        raise ValueError("Invalid persistent blocked-recovery attempt input")
    if selection_batch is not None:
        selection_batch = deepcopy(_validate_selection_batch(selection_batch))
    state = ensure_state(memory, source_revision, allow_source_change=allow_source_change)
    if len(state["attempts"]) >= MAX_ATTEMPTS:
        raise ValueError("Persistent blocked-recovery attempt ledger is full")
    if was_attempted(memory, source_revision, input_sha256,
                     allow_source_change=allow_source_change, archive_index=archive_index):
        raise ValueError("Persistent blocked-recovery input was already attempted")
    attempt = {"source_revision": _source(source_revision),
               "decision_input_sha256": input_sha256,
               "reason": reason, "tick": tick, "outcome": "pending"}
    if selection_batch is not None:
        attempt["selection_batch"] = selection_batch
    # Validate the proposed WAL before changing memory or letting a caller
    # durably prepare a provider request. A distinct request fingerprint does
    # not authorize offering the same candidate again for this source/state.
    proposed = deepcopy(state)
    proposed["attempts"].append(attempt)
    proposed["last_input_sha256"] = input_sha256
    proposed["wait_level"] = 1
    _validate_state(proposed, memory.session_id)
    state["attempts"].append(attempt)
    state["last_input_sha256"] = input_sha256
    state["wait_level"] = 1


def finish_attempt(memory, source_revision: dict, input_sha256: str, outcome: str,
                   reason: str | None = None, *, archive_index=None) -> None:
    if (outcome not in {"rejected", "selected", "provider_blocked", "failed", "frontier"}
            or reason is not None and (type(reason) is not str or not is_recoverable_reason(reason))):
        raise ValueError("Invalid persistent blocked-recovery attempt outcome")
    row = find_attempt(memory, source_revision, input_sha256,
                       archive_index=archive_index)
    if row is None or row.get("outcome") != "pending":
        raise ValueError("Persistent blocked-recovery attempt is not pending")
    for stored in memory.blocked_recovery["attempts"]:
        if (stored["source_revision"] == _source(source_revision)
                and stored["decision_input_sha256"] == input_sha256):
            stored["outcome"] = outcome
            stored["reason"] = reason if reason is not None else stored["reason"]
            return
    raise ValueError("Persistent blocked-recovery attempt disappeared")


def record_wait(memory, source_revision: dict, input_sha256: str, *,
                allow_source_change: bool = False) -> float:
    if not _SHA256.fullmatch(input_sha256):
        raise ValueError("Invalid persistent blocked-recovery observation fingerprint")
    state = ensure_state(memory, source_revision, allow_source_change=allow_source_change)
    if state["last_input_sha256"] == input_sha256:
        state["wait_level"] = min(MAX_WAIT_LEVEL, state["wait_level"] + 1)
    else:
        state["wait_level"] = 1
    state["last_input_sha256"] = input_sha256
    return wait_seconds(memory)


def wait_seconds(memory) -> float:
    state = _validate_state(memory.blocked_recovery, memory.session_id)
    exponent = max(0, state["wait_level"] - 1)
    return min(MAX_WAIT_SECONDS, float(2 ** min(exponent + 1, 9)))
