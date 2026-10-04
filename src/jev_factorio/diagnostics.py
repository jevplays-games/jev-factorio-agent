"""Offline reconciliation evidence. Never starts a backend, writes a checkpoint, or replays an action."""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path

from .evaluation import read_records
from .memory import CampaignMemory, load_checkpoint
from .skills import Plan
from .state import GameSnapshot


def captured_snapshot(data: dict) -> GameSnapshot:
    if (not isinstance(data, dict)
            or not {"session_id", "world_kind", "tick", "inventory", "factory"} <= data.keys()
            or not isinstance(data["inventory"], dict) or not isinstance(data["factory"], dict)
            or type(data["tick"]) is not int or data["tick"] < 0
            or data["world_kind"] not in {"mock", "fle"}):
        raise ValueError("Invalid captured snapshot")
    if any(type(value) is not int or value < 0 for value in data["inventory"].values()):
        raise ValueError("Invalid captured inventory")
    json.dumps(data, allow_nan=False)
    return GameSnapshot(**data)


def reconciliation_report(memory: CampaignMemory, snapshot: GameSnapshot | None = None) -> dict:
    report = {
        "read_only": True, "replay_authorized": False,
        "session_id": memory.session_id, "target": memory.target,
        "checkpoint_status": memory.status, "checkpoint_tick": memory.last_tick,
        "pending": deepcopy(memory.pending), "attempt": deepcopy(memory.attempt),
        "assessment": "no_pending_action" if memory.pending is None else "observation_required",
        "evidence_class": "no_observation",
        "guidance": "Preserve pending intent. Captured evidence is not authorization to retry, clear, or resume a live campaign.",
    }
    for key in ("background_schema", "background_job", "background_attempt", "background_step",
                "output_buffers_schema", "output_commitments",
                "input_routes_schema", "input_commitments", "outposts_schema", "outpost_commitments"):
        if hasattr(memory, key):
            report[key] = deepcopy(getattr(memory, key))
    if getattr(memory, "background_job", None) is not None and memory.pending is None:
        report["assessment"] = "background_observation_required"
    if snapshot is None:
        return report
    snapshot = captured_snapshot(snapshot.for_jev())
    if snapshot.session_id != memory.session_id:
        raise ValueError("Captured observation session mismatch")
    report["evidence_class"] = "synthetic" if snapshot.world_kind == "mock" else "captured-tool-assisted"
    report["observation_tick"] = snapshot.tick
    if snapshot.tick < memory.last_tick:
        report["assessment"] = "stale_observation"
        return report
    if memory.pending is None:
        return report
    step = Plan.from_dict(memory.active_plan).steps[memory.step_index]
    if step.action not in {"factory_insert", "factory_extract"}:
        report["assessment"] = "pending_nontransfer_action"
        return report
    parameters = step.parameters
    entities, receipts = snapshot.factory.get("entities", {}), snapshot.factory.get("receipts", {})
    if not isinstance(entities, dict) or not isinstance(receipts, dict):
        raise ValueError("Invalid captured factory evidence")
    machine = entities.get(parameters["role"], {})
    receipt = receipts.get(parameters["receipt"])
    if not isinstance(machine, dict) or (receipt is not None and not isinstance(receipt, dict)):
        raise ValueError("Invalid captured transfer evidence")
    report["carried_quantity"] = snapshot.inventory.get(parameters["item"])
    report["requested_quantity"] = parameters["quantity"]
    report["assessment"] = "missing_receipt_unresolved"
    if receipt is None:
        return report
    unit = machine.get("unit_number")
    expected = memory.attempt["expected_unit_number"]
    matching = (
        type(unit) is int and unit > 0 and type(receipt.get("unit_number")) is int
        and receipt["unit_number"] == unit
        and (expected is None or expected == unit)
        and receipt.get("role") == parameters["role"]
        and receipt.get("item") == parameters["item"]
        and receipt.get("extracting") is (step.action == "factory_extract")
    )
    quantity = receipt.get("quantity")
    # Return only contract facts, not arbitrary receipt strings or error messages.
    report["receipt_quantity"] = quantity if type(quantity) is int else None
    report["assessment"] = "mismatched_receipt_unresolved"
    if matching and type(quantity) is int:
        if quantity == parameters["quantity"]:
            report["assessment"] = "full_matching_receipt_in_capture"
        elif 0 <= quantity < parameters["quantity"]:
            report["assessment"] = "partial_receipt_unresolved"
    return report


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--target", default="rocket_launch")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--snapshot", type=Path, help="Previously captured GameSnapshot JSON")
    source.add_argument("--log", type=Path, help="Previously captured single-session decision JSONL")
    args = parser.parse_args()
    try:
        memory = load_checkpoint(args.checkpoint, args.session_id, args.target)
        snapshot = None
        if args.snapshot:
            snapshot = captured_snapshot(json.loads(args.snapshot.read_text(encoding="utf-8")))
        elif args.log:
            records = read_records(args.log)
            if records[-1]["session_id"] != memory.session_id or records[-1]["target"] != memory.target:
                raise ValueError("Captured log identity mismatch")
            snapshot = captured_snapshot(records[-1]["after_state"])
        print(json.dumps(reconciliation_report(memory, snapshot), indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        parser.exit(2, "Invalid or incompatible captured evidence; no files or game state were changed.\n")


if __name__ == "__main__":
    cli()
