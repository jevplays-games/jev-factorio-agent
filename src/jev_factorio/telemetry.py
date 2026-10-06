"""Bounded diagnostic evidence. These records never authorize game actions."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator
from uuid import uuid4
from .iteration_timing import span
from .preflight_codes import CONNECTION_PREFLIGHT_CODES

DISPATCH_STAGES = {"dispatch", "entity_lookup", "approach", "transfer_rpc"}
STAGES = DISPATCH_STAGES | {"observe", "reconcile", "pre_dispatch_observe", "post_dispatch_observe",
                            "selection", "verification", "planning"}
ERROR_CODES = ({"timeout", "connection", "http", "invalid_data", "io", "interrupted", "execution",
                "storage_preflight_rejected", "maintenance_preflight_rejected",
                "cancelled_before_entry", "transfer_preflight_rejected"}
               | {"connection_preflight:" + code for code in CONNECTION_PREFLIGHT_CODES})
WAIT_ACTIONS = {"idle", "factory_wait"}
Trace = Callable[[dict], None]


class DispatchCancelledBeforeEntry(Exception):
    """Exact internal proof that the async dispatch gate suppressed operation entry."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_code(error: BaseException) -> str:
    """Use a fixed vocabulary; never serialize messages, URLs, or class names."""
    import requests
    from .backends.errors import ConnectionPreflightRejected
    from .operational_safety import MaintenanceAdmissionClosed, StoragePressure
    from .backends.native_factory import TransferPreflightRejected

    if type(error) is DispatchCancelledBeforeEntry:
        return "cancelled_before_entry"
    if type(error) is TransferPreflightRejected:
        validate_transfer_preflight_proof(error.proof)
        return "transfer_preflight_rejected"

    # Mirror the controller's exact rejection contracts. Generic errors and a
    # ConnectionPreflightRejected subclass remain ambiguous, not local proof.
    if type(error) is ConnectionPreflightRejected and error.code in CONNECTION_PREFLIGHT_CODES:
        return "connection_preflight:" + error.code
    if isinstance(error, MaintenanceAdmissionClosed):
        return "maintenance_preflight_rejected"
    if isinstance(error, StoragePressure):
        return "storage_preflight_rejected"

    for types, code in (
        ((KeyboardInterrupt, SystemExit), "interrupted"),
        ((TimeoutError, requests.Timeout), "timeout"),
        ((ConnectionError, requests.ConnectionError), "connection"),
        ((requests.HTTPError,), "http"),
        ((ValueError, KeyError, TypeError), "invalid_data"),
        ((OSError,), "io"),
    ):
        if isinstance(error, types):
            return code
    return "execution"


@contextmanager
def phase(stage: str, trace: Trace | None = None) -> Iterator[None]:
    if stage not in STAGES:
        raise ValueError("Unknown diagnostic stage")
    with span(stage):
        if trace is None:
            yield
            return
        event = {"stage": stage, "status": "started", "at_utc": utc_now(),
                 "seconds": None, "error_code": None}
        trace(dict(event))  # A failed write prevents entering the operation.
        start = time.perf_counter()
        try:
            yield
        except BaseException as error:
            try:
                failed = {**event, "status": "failed", "seconds": time.perf_counter() - start,
                          "error_code": error_code(error)}
                from .backends.native_factory import TransferPreflightRejected
                if type(error) is TransferPreflightRejected:
                    failed["proof"] = error.proof
                trace(failed)
            except BaseException:
                pass
            raise
        else:
            trace({**event, "status": "returned", "seconds": time.perf_counter() - start})


def fingerprint(step: dict) -> str:
    return hashlib.sha256(json.dumps(step, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


TRANSFER_PREFLIGHT_SCHEMA = "jev.transfer-capacity-preflight.v1"


def validate_transfer_preflight_context(context: object) -> None:
    """Validate the controller-created identity sent to the read-only Lua gate."""
    from .bootstrap_output import ROLE as BOOTSTRAP_ROLE
    fields = {
        "schema", "attempt_id", "session_id", "plan_id", "step_index", "step_sha256",
        "action", "started_tick", "observed_tick", "receipt", "item", "quantity",
        "direction", "role", "machine_unit_number", "machine_name", "actor_unit_number",
        "actor_player_index", "surface_index", "force_index",
    }
    if type(context) is not dict or set(context) != fields:
        raise ValueError("Invalid transfer preflight request fields")
    if (type(context["schema"]) is not int or context["schema"] != 1
            or type(context["attempt_id"]) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", context["attempt_id"])
            or type(context["session_id"]) is not str
            or not 1 <= len(context["session_id"]) <= 128
            or type(context["plan_id"]) is not str or not 1 <= len(context["plan_id"]) <= 128
            or type(context["step_index"]) is not int or not 0 <= context["step_index"] < 32
            or type(context["step_sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", context["step_sha256"])
            or context["action"] not in {"factory_insert", "factory_extract"}
            or type(context["started_tick"]) is not int or context["started_tick"] < 0
            or type(context["observed_tick"]) is not int
            or context["observed_tick"] < context["started_tick"]
            or type(context["receipt"]) is not str or not 1 <= len(context["receipt"]) <= 128
            or type(context["item"]) is not str or not 1 <= len(context["item"]) <= 128
            or type(context["quantity"]) is not int or not 1 <= context["quantity"] <= 1_000_000
            or context["direction"] != ("extract" if context["action"] == "factory_extract" else "insert")
            or type(context["role"]) is not str or not 1 <= len(context["role"]) <= 128
            or context["role"] == BOOTSTRAP_ROLE
            or type(context["machine_name"]) is not str
            or not 1 <= len(context["machine_name"]) <= 128):
        raise ValueError("Invalid transfer preflight request identity")
    for name in ("machine_unit_number", "actor_unit_number", "actor_player_index",
                 "surface_index", "force_index"):
        if type(context[name]) is not int or context[name] < 1:
            raise ValueError("Invalid transfer preflight actor or entity identity")


def validate_transfer_preflight_proof(proof: object, expected_context: dict | None = None) -> None:
    """Validate a complete, exact Lua pre-mutation capacity rejection envelope."""
    fields = {"schema", "result", "request", "tick", "actor", "machine", "source", "target", "checks"}
    if type(proof) is not dict or set(proof) != fields:
        raise ValueError("Invalid native transfer preflight proof fields")
    if proof["schema"] != TRANSFER_PREFLIGHT_SCHEMA or proof["result"] != "destination_capacity_short":
        raise ValueError("Invalid native transfer preflight proof type")
    request = proof["request"]
    validate_transfer_preflight_context(request)
    if expected_context is not None:
        validate_transfer_preflight_context(expected_context)
        if request != expected_context:
            raise ValueError("Native transfer preflight request identity mismatch")
    if type(proof["tick"]) is not int or proof["tick"] < request["observed_tick"]:
        raise ValueError("Invalid native transfer preflight tick")

    actor = proof["actor"]
    if (type(actor) is not dict
            or set(actor) != {"name", "unit_number", "player_index", "surface_index", "force_index"}
            or type(actor.get("name")) is not str or not actor["name"]
            or type(actor.get("unit_number")) is not int
            or actor["unit_number"] != request["actor_unit_number"]
            or type(actor.get("player_index")) is not int
            or actor["player_index"] != request["actor_player_index"]
            or type(actor.get("surface_index")) is not int
            or actor["surface_index"] != request["surface_index"]
            or type(actor.get("force_index")) is not int
            or actor["force_index"] != request["force_index"]):
        raise ValueError("Native transfer preflight actor binding mismatch")
    machine = proof["machine"]
    if (type(machine) is not dict
            or set(machine) != {"role", "name", "unit_number", "surface_index", "force_index"}
            or machine.get("role") != request["role"]
            or machine.get("name") != request["machine_name"]
            or type(machine.get("unit_number")) is not int
            or machine["unit_number"] != request["machine_unit_number"]
            or type(machine.get("surface_index")) is not int
            or machine["surface_index"] != request["surface_index"]
            or type(machine.get("force_index")) is not int
            or machine["force_index"] != request["force_index"]):
        raise ValueError("Native transfer preflight machine binding mismatch")

    source, target = proof["source"], proof["target"]
    endpoint_fields = {"kind", "role", "name", "unit_number", "quantity"}
    capacity_fields = {"kind", "role", "name", "unit_number", "insertable_count"}
    if type(source) is not dict or set(source) != endpoint_fields:
        raise ValueError("Invalid native transfer preflight source")
    if type(target) is not dict or set(target) != capacity_fields:
        raise ValueError("Invalid native transfer preflight destination")
    for endpoint in (source, target):
        if (type(endpoint.get("kind")) is not str or not endpoint["kind"]
                or type(endpoint.get("role")) is not str or not endpoint["role"]
                or type(endpoint.get("name")) is not str or not endpoint["name"]
                or type(endpoint.get("unit_number")) is not int
                or endpoint["unit_number"] < 1):
            raise ValueError("Invalid native transfer preflight endpoint identity")
    if (type(source.get("quantity")) is not int
            or source["quantity"] < request["quantity"]
            or type(target.get("insertable_count")) is not int
            or not 0 <= target["insertable_count"] < request["quantity"]):
        raise ValueError("Native transfer preflight checks do not prove capacity rejection")
    actor_source = request["action"] == "factory_insert"
    if actor_source:
        source_expected = ("actor_main", "@agent", actor["name"], actor["unit_number"])
        target_matches = (
            target["kind"] in {"machine_fuel", "furnace_source", "lab_input",
                                "assembling_machine_input"}
            and (target["role"], target["name"], target["unit_number"])
            == (request["role"], machine["name"], machine["unit_number"])
        )
    else:
        source_expected = ("machine_output_or_chest", request["role"],
                           machine["name"], machine["unit_number"])
        target_matches = ((target["kind"], target["role"], target["name"], target["unit_number"])
                          == ("actor_main", "@agent", actor["name"], actor["unit_number"]))
    if ((source["kind"], source["role"], source["name"], source["unit_number"]) != source_expected
            or not target_matches):
        raise ValueError("Native transfer preflight source or destination binding mismatch")
    checks = proof["checks"]
    if (type(checks) is not dict
            or set(checks) != {"reachable", "receipt_absent", "source_sufficient", "capacity_short"}
            or any(type(checks.get(name)) is not bool or checks[name] is not True
                   for name in checks)):
        raise ValueError("Native transfer preflight lacks its exact successful checks")


def make_attempt(session: str, target: str, plan: dict, index: int, pending: dict,
                 *, process_id: str | None = None, unit_number: int | None = None) -> dict:
    step = plan["steps"][index]
    legacy = process_id is None
    identity = {"session": session, "target": target, "plan": plan,
                "index": index, "started_tick": pending["started_tick"]}
    return {
        "id": "legacy:" + fingerprint(identity) if legacy else uuid4().hex,
        "origin": "legacy" if legacy else "new", "action": step["action"],
        "plan_id": plan["id"], "step_index": index, "step_sha256": fingerprint(step),
        "started_tick": pending["started_tick"],
        "started_at_utc": None if legacy else utc_now(), "process_id": process_id,
        "expected_unit_number": unit_number,
        "receipt": (step.get("parameters") or {}).get("receipt"),
        "dispatch_phases": {}, "observation_error": None,
    }


def _seconds(value: object) -> bool:
    return (value is None or (type(value) in {int, float}
                             and math.isfinite(value) and value >= 0))


def _utc(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    except ValueError:
        return False


def validate_phase(event: dict) -> None:
    if (not isinstance(event, dict)
            or set(event) not in ({"stage", "status", "at_utc", "seconds", "error_code"},
                                  {"stage", "status", "at_utc", "seconds", "error_code", "proof"})
            or event["stage"] not in STAGES
            or event["status"] not in {"started", "returned", "failed"}
            or not _utc(event["at_utc"]) or not _seconds(event["seconds"])
            or (event["status"] == "started") != (event["seconds"] is None)
            or (event["status"] == "failed" and event["error_code"] not in ERROR_CODES)
            or (event["status"] != "failed" and event["error_code"] is not None)):
        raise ValueError("Invalid diagnostic phase")
    has_proof = "proof" in event
    if (has_proof != (event["error_code"] == "transfer_preflight_rejected")
            or (has_proof and (event["stage"] != "transfer_rpc"
                               or event["status"] != "failed"))):
        raise ValueError("Transfer preflight proof is not bound to a failed transfer RPC")
    if has_proof:
        validate_transfer_preflight_proof(event["proof"])


def validate_attempt(attempt: dict, *, finished: bool = False) -> None:
    from .skills import ACTIONS
    from .factory_contract import COMMAND_FIELDS

    fields = {"id", "origin", "action", "plan_id", "step_index", "step_sha256",
              "started_tick", "started_at_utc", "process_id", "expected_unit_number",
              "receipt", "dispatch_phases", "observation_error"}
    if finished:
        fields |= {"outcome", "finished_tick", "finished_at_utc", "latency_seconds"}
    if not isinstance(attempt, dict) or set(attempt) != fields:
        raise ValueError("Invalid attempt fields")
    legacy = attempt["origin"] == "legacy"
    pattern = r"legacy:[0-9a-f]{64}" if legacy else r"[0-9a-f]{32}"
    if (attempt["origin"] not in {"legacy", "new"}
            or not isinstance(attempt["id"], str) or not re.fullmatch(pattern, attempt["id"])
            or attempt["action"] not in ACTIONS | COMMAND_FIELDS.keys()
            or not isinstance(attempt["plan_id"], str) or not attempt["plan_id"]
            or type(attempt["step_index"]) is not int or not 0 <= attempt["step_index"] < 32
            or not isinstance(attempt["step_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", attempt["step_sha256"])
            or type(attempt["started_tick"]) is not int or attempt["started_tick"] < 0):
        raise ValueError("Invalid attempt identity")
    if legacy:
        if attempt["started_at_utc"] is not None or attempt["process_id"] is not None:
            raise ValueError("Legacy attempt start timing is unknown")
    elif (not _utc(attempt["started_at_utc"])
          or not isinstance(attempt["process_id"], str)
          or not re.fullmatch(r"[0-9a-f]{32}", attempt["process_id"])):
        raise ValueError("Invalid attempt start provenance")
    unit, receipt = attempt["expected_unit_number"], attempt["receipt"]
    if ((unit is not None and (type(unit) is not int or unit < 1))
            or (receipt is not None and (not isinstance(receipt, str) or not 1 <= len(receipt) <= 128))):
        raise ValueError("Invalid attempt entity or receipt")
    stages = attempt["dispatch_phases"]
    if not isinstance(stages, dict) or set(stages) - DISPATCH_STAGES:
        raise ValueError("Invalid dispatch diagnostics")
    for stage, event in stages.items():
        validate_phase(event)
        if stage != event["stage"]:
            raise ValueError("Dispatch stage mismatch")
        if stage == "transfer_rpc" and event.get("error_code") == "transfer_preflight_rejected":
            proof = event["proof"]
            request = proof["request"]
            if (request["attempt_id"] != attempt["id"]
                    or request["action"] != attempt["action"]
                    or request["plan_id"] != attempt["plan_id"]
                    or request["step_index"] != attempt["step_index"]
                    or request["step_sha256"] != attempt["step_sha256"]
                    or request["started_tick"] != attempt["started_tick"]
                    or request["receipt"] != attempt["receipt"]
                    or request["machine_unit_number"] != attempt["expected_unit_number"]):
                raise ValueError("Transfer preflight proof does not match its attempt")
    if attempt["observation_error"] is not None:
        event = attempt["observation_error"]
        validate_phase(event)
        if event["status"] != "failed" or event["stage"] in DISPATCH_STAGES:
            raise ValueError("Invalid observation error")
    if finished and (
        attempt["outcome"] not in {
            "verified", "wait_replanned", "wait_expired", "partial_transfer_reconciled",
            "zero_effect_transfer_reconciled", "rejected_transfer_reconciled",
            "connection_preflight_rejected", "storage_preflight_rejected", "maintenance_preflight_rejected",
            "cancelled_before_dispatch", "transfer_preflight_rejected",
        }
        or (attempt["outcome"] not in {"verified", "partial_transfer_reconciled",
                                        "zero_effect_transfer_reconciled",
                                        "rejected_transfer_reconciled", "connection_preflight_rejected",
                                        "storage_preflight_rejected", "maintenance_preflight_rejected",
                                        "cancelled_before_dispatch", "transfer_preflight_rejected"}
            and attempt["action"] not in WAIT_ACTIONS)
        or (attempt["outcome"] == "cancelled_before_dispatch"
            and (set(stages) != {"dispatch"}
                 or stages["dispatch"]["status"] != "failed"
                 or stages["dispatch"]["error_code"] != "cancelled_before_entry"))
        or (attempt["outcome"] in {"partial_transfer_reconciled", "zero_effect_transfer_reconciled"}
            and attempt["action"] not in {"factory_insert", "factory_extract"})
        or (attempt["outcome"] == "rejected_transfer_reconciled"
            and attempt["action"] != "factory_insert")
        or (attempt["outcome"] == "connection_preflight_rejected"
            and attempt["action"] != "factory_connect")
        or (attempt["outcome"] == "transfer_preflight_rejected"
            and (attempt["action"] not in {"factory_insert", "factory_extract"}
                 or stages.get("transfer_rpc", {}).get("error_code") != "transfer_preflight_rejected"))
        or (attempt["outcome"] != "transfer_preflight_rejected"
            and any(event.get("error_code") == "transfer_preflight_rejected"
                    for event in stages.values()))
        or type(attempt["finished_tick"]) is not int
        or attempt["finished_tick"] < attempt["started_tick"]
        or not _utc(attempt["finished_at_utc"]) or not _seconds(attempt["latency_seconds"])
        or (legacy and attempt["latency_seconds"] is not None)
    ):
        raise ValueError("Invalid attempt outcome")
