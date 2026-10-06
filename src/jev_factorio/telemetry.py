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
                "cancelled_before_entry"}
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

    if type(error) is DispatchCancelledBeforeEntry:
        return "cancelled_before_entry"

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
                trace({**event, "status": "failed", "seconds": time.perf_counter() - start,
                       "error_code": error_code(error)})
            except BaseException:
                pass
            raise
        else:
            trace({**event, "status": "returned", "seconds": time.perf_counter() - start})


def fingerprint(step: dict) -> str:
    return hashlib.sha256(json.dumps(step, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


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
            or set(event) != {"stage", "status", "at_utc", "seconds", "error_code"}
            or event["stage"] not in STAGES
            or event["status"] not in {"started", "returned", "failed"}
            or not _utc(event["at_utc"]) or not _seconds(event["seconds"])
            or (event["status"] == "started") != (event["seconds"] is None)
            or (event["status"] == "failed" and event["error_code"] not in ERROR_CODES)
            or (event["status"] != "failed" and event["error_code"] is not None)):
        raise ValueError("Invalid diagnostic phase")


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
            "cancelled_before_dispatch",
        }
        or (attempt["outcome"] not in {"verified", "partial_transfer_reconciled",
                                        "zero_effect_transfer_reconciled",
                                        "rejected_transfer_reconciled", "connection_preflight_rejected",
                                        "storage_preflight_rejected", "maintenance_preflight_rejected",
                                        "cancelled_before_dispatch"}
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
        or type(attempt["finished_tick"]) is not int
        or attempt["finished_tick"] < attempt["started_tick"]
        or not _utc(attempt["finished_at_utc"]) or not _seconds(attempt["latency_seconds"])
        or (legacy and attempt["latency_seconds"] is not None)
    ):
        raise ValueError("Invalid attempt outcome")
