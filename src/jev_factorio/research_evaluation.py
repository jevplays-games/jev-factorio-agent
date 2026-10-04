"""Offline causal reduction of verified research events into run-level evidence."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .research_events import (
    EvidenceError, MixedTreatmentError, VerifiedRun, canonical, digest,
    nonnegative_int, read_run, text, utc,
)

SUPPORTED_EVENTS = {
    "run_started", "run_finished", "segment_started", "observation",
    "candidate_set_created", "model_request", "model_response", "provider_error",
    "decision", "plan_committed", "action_prepared", "action_returned",
    "verification", "goal_completed", "checkpoint_written", "incident_started",
    "operational_repair", "code_revision_changed", "manual_intervention",
}
NON_WORK_ACTIONS = {"observe", "verify", "wait", "wait_for_production", "wait_for_research"}
TREATMENT_FIELDS = (
    "controller", "policy", "target", "backend", "requested_model", "resolved_model", "confidence_floor",
    "git", "runtime", "factorio_version", "fle_version", "configuration", "treatment",
)


@dataclass
class RunEvaluation:
    summary: dict
    tables: dict[str, list[dict]]
    integrity: dict
    sources: tuple[Path, Path]


def _ref(event: dict, key: str) -> str:
    return text(event["correlation"].get(key), f"{event['event_type']}.{key}")


def _duration(payload: dict) -> float | None:
    value = payload.get("duration_ns")
    return None if value is None else nonnegative_int(value, "duration_ns") / 1_000_000


def _usage(payload: dict, key: str) -> int | None:
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise EvidenceError("usage must be an object or null")
    value = (usage or {}).get(key)
    return None if value is None else nonnegative_int(value, f"usage.{key}")


def _same(left: Any, right: Any) -> bool:
    return canonical(left) == canonical(right)


def evaluate_run(path: Path, *, manifest_path: Path | None = None,
                 allow_mixed_treatments: bool = False) -> RunEvaluation:
    return reduce_run(read_run(path, manifest_path=manifest_path),
                      allow_mixed_treatments=allow_mixed_treatments)


def reduce_run(run: VerifiedRun, *, allow_mixed_treatments: bool = False) -> RunEvaluation:
    if run.integrity.get("source_format") == "logging-core-v1":
        from .research_core_evaluation import reduce_core_run
        return reduce_core_run(run, allow_mixed_treatments=allow_mixed_treatments)
    manifest, events = run.manifest, run.events
    run_id = manifest["run_id"]
    problems: set[str] = set()
    warnings: set[str] = set()
    observations: dict[str, dict] = {}
    observation_sequences: dict[str, int] = {}
    decisions: dict[str, dict] = {}
    calls: dict[str, dict] = {}
    actions: dict[str, dict] = {}
    milestones: dict[str, dict] = {}
    model_responses: dict[str, dict] = {}
    completion_decisions: dict[str, str] = {}
    native_victory_observations: set[str] = set()
    native_victory_milestones: set[str] = set()
    returns: dict[str, dict] = {}
    rows: list[dict] = []
    interventions: list[dict] = []
    model_names: set[str] = set()
    segments: set[str] = {events[0]["segment_id"]}
    current_segment = events[0]["segment_id"]
    world_kind = manifest.get("world_kind", manifest["backend"])
    if not isinstance(world_kind, str):
        raise EvidenceError("world_kind must be a string")
    evidence_class = ("synthetic" if world_kind == "mock" or manifest["backend"] == "mock" else
                      "tool-assisted" if world_kind in {"fle", "native", "play_api"} else "unknown")
    native_victory = False
    terminal: dict = {}

    def provenance(values: dict) -> None:
        for field in (*TREATMENT_FIELDS, "condition", "experiment_id", "trial_id", "replicate"):
            if field in values and not _same(values[field], manifest.get(field)):
                problems.add(f"changed:{field}")
        if "session_id" in values and values["session_id"] != manifest["session_id"]:
            # A different world/session is not made acceptable by a treatment override.
            raise EvidenceError("Mixed world sessions")
        if "world_kind" in values and values["world_kind"] != world_kind:
            raise EvidenceError("Mixed world kinds")

    for event in events:
        kind, payload = event["event_type"], event["payload"]
        seq, segment = event["sequence"], event["segment_id"]
        if kind not in SUPPORTED_EVENTS:
            raise EvidenceError("Unsupported event type")
        if segment != current_segment:
            required = {"git", "controller", "policy", "target", "requested_model"}
            declared = payload.get("provenance")
            if (segment in segments or kind != "segment_started" or not isinstance(declared, dict)
                    or not required.issubset(declared)):
                raise EvidenceError("Segment transition lacks fresh, explicit code/config provenance")
            segments.add(segment)
            current_segment = segment
        elif kind == "segment_started":
            raise EvidenceError("Duplicate segment declaration")
        provenance(event)
        if kind == "segment_started":
            provenance(payload)
        if "provenance" in payload:
            if not isinstance(payload["provenance"], dict):
                raise EvidenceError("provenance must be an object")
            provenance(payload["provenance"])
        rows.append({
            "run_id": run_id, "sequence": seq, "segment_id": segment,
            "event_type": kind, "utc": event["time"]["utc"],
            "monotonic_ns": event["time"]["monotonic_ns"],
            "factorio_tick": event["time"].get("factorio_tick"),
            "decision_id": event["correlation"].get("decision_id"),
            "model_call_id": event["correlation"].get("model_call_id"),
            "action_id": event["correlation"].get("action_id"),
            "duration_ms": _duration(payload), "event_hash": event["event_hash"],
        })
        if kind == "observation":
            observation_id = _ref(event, "observation_id")
            if observation_id in observations or not isinstance(payload.get("state"), dict):
                raise EvidenceError("Duplicate observation ID or missing state")
            state = payload["state"]
            provenance(state)
            observations[observation_id] = state
            observation_sequences[observation_id] = seq
            if (evidence_class == "tool-assisted" and state.get("victory") is True
                    and state.get("victory_source") == "native:base-game-rocket-launch"):
                native_victory = True
                native_victory_observations.add(observation_id)
        elif kind == "model_request":
            call_id = _ref(event, "model_call_id")
            if call_id in calls:
                raise EvidenceError("Duplicate model_call_id; retries need fresh IDs")
            requested = text(payload.get("requested_model"), "requested_model")
            if requested != manifest.get("requested_model"):
                problems.add("changed:requested_model")
            calls[call_id] = {
                "run_id": run_id, "model_call_id": call_id, "segment_id": segment,
                "decision_id": event["correlation"].get("decision_id"),
                "request_sequence": seq, "response_sequence": None,
                "requested_model": requested, "resolved_model": None,
                "status": "pending", "duration_ms": None,
                "input_tokens": None, "output_tokens": None,
            }
        elif kind in {"model_response", "provider_error"}:
            call_id = _ref(event, "model_call_id")
            if call_id not in calls:
                raise EvidenceError("Model response/error lacks a preceding request")
            if call_id in model_responses:
                raise EvidenceError("Duplicate model completion; retries need fresh IDs")
            request_decision = calls[call_id]["decision_id"]
            completion_decision = event["correlation"].get("decision_id")
            if (request_decision is not None and completion_decision is not None
                    and completion_decision != request_decision):
                raise EvidenceError("Model completion decision differs from its request")
            if completion_decision is not None:
                completion_decisions[call_id] = completion_decision
            model_responses[call_id] = payload
            resolved = payload.get("resolved_model")
            if resolved is not None:
                model_names.add(text(resolved, "resolved_model"))
            calls[call_id].update({
                "response_sequence": seq, "status": "error" if kind == "provider_error" else "ok",
                "resolved_model": resolved, "duration_ms": _duration(payload),
                "input_tokens": _usage(payload, "input_tokens"),
                "output_tokens": _usage(payload, "output_tokens"),
            })
        elif kind == "decision":
            decision_id = _ref(event, "decision_id")
            if decision_id in decisions:
                raise EvidenceError("Duplicate decision_id")
            call_id = event["correlation"].get("model_call_id")
            if call_id is not None and call_id not in model_responses:
                raise EvidenceError("Decision references an unfinished/unknown model call")
            if call_id is not None and calls[call_id]["decision_id"] not in {None, decision_id}:
                raise EvidenceError("Decision and model request correlation disagree")
            if call_id is not None and completion_decisions.get(call_id) not in {None, decision_id}:
                raise EvidenceError("Decision and model completion correlation disagree")
            decisions[decision_id] = {
                "run_id": run_id, "decision_id": decision_id, "sequence": seq,
                "segment_id": segment, "model_call_id": call_id,
                "source": text(payload.get("source"), "decision.source"),
                "action": text(payload.get("action"), "decision.action"),
            }
        elif kind == "action_prepared":
            action_id = _ref(event, "action_id")
            if action_id in actions:
                raise EvidenceError("Duplicate action_id; each dispatch preparation needs a fresh ID")
            decision_id = _ref(event, "decision_id")
            if decision_id not in decisions:
                raise EvidenceError("Action preparation lacks a preceding decision")
            action = text(payload.get("action"), "action")
            if action != decisions[decision_id]["action"]:
                raise EvidenceError("Prepared action differs from its decision")
            actions[action_id] = {
                "run_id": run_id, "action_id": action_id, "decision_id": decision_id,
                "segment_id": segment, "action": action, "prepared_sequence": seq,
                "returned_sequence": None, "acknowledged": None,
                "verified": False, "verification_count": 0, "verified_sequence": None,
                "duration_ms": None,
            }
        elif kind == "action_returned":
            action_id = _ref(event, "action_id")
            if action_id not in actions:
                raise EvidenceError("Action result lacks a preceding preparation")
            if action_id in returns:
                raise EvidenceError("Duplicate action result")
            if type(payload.get("ok")) is not bool:
                raise EvidenceError("Action result ok must be a boolean")
            returns[action_id] = payload
            actions[action_id].update({"returned_sequence": seq, "acknowledged": payload["ok"],
                                       "duration_ms": _duration(payload)})
        elif kind == "verification":
            action_id = _ref(event, "action_id")
            if action_id not in actions:
                raise EvidenceError("Verification lacks a preceding preparation")
            observation_id = _ref(event, "observation_id")
            if observation_id not in observations:
                raise EvidenceError("Verification lacks an existing observation")
            if observation_sequences[observation_id] <= actions[action_id]["prepared_sequence"]:
                raise EvidenceError("Verification observation predates the action preparation")
            if type(payload.get("verified")) is not bool:
                raise EvidenceError("verified must be a boolean")
            row = actions[action_id]
            row["verification_count"] += 1
            if payload["verified"] and not row["verified"]:
                row.update(verified=True, verified_sequence=seq)
            # False may precede true (delayed verification); never recount a completed attempt.
        elif kind == "goal_completed":
            goal = text(payload.get("goal"), "goal")
            observation_id = _ref(event, "observation_id")
            if observation_id not in observations or payload.get("verified") is not True:
                raise EvidenceError("Milestone lacks verified observational evidence")
            if goal == "rocket_launch" and observation_id in native_victory_observations:
                native_victory_milestones.add(goal)
            if goal not in milestones:
                milestones[goal] = {"run_id": run_id, "goal": goal, "sequence": seq,
                                    "segment_id": segment, "observation_id": observation_id,
                                    "factorio_tick": event["time"].get("factorio_tick")}
        elif kind in {"incident_started", "operational_repair", "code_revision_changed", "manual_intervention"}:
            interventions.append({"run_id": run_id, "sequence": seq, "segment_id": segment,
                                  "kind": kind, "incident_id": event["correlation"].get("incident_id")})
            if kind in {"code_revision_changed", "manual_intervention"}:
                problems.add(kind)
        elif kind == "run_finished":
            status = payload.get("status")
            if status not in {"completed", "failed", "blocked", "timeout", "cancelled", "stopped"}:
                raise EvidenceError("Invalid run_finished status")
            if payload.get("reason") is not None and not isinstance(payload["reason"], str):
                raise EvidenceError("Terminal reason must be a string or null")
            terminal = payload
    if len(model_names) > 1:
        problems.add("changed:resolved_model")
    if manifest.get("resolved_model") and model_names - {manifest["resolved_model"]}:
        problems.add("changed:resolved_model")
    if problems and not allow_mixed_treatments:
        raise MixedTreatmentError("Mixed/intervened treatment: " + ", ".join(sorted(problems)))
    if evidence_class == "unknown":
        warnings.add("unknown_world_kind")
    if not run.integrity["complete"]:
        warnings.add("incomplete_run")
    if not (manifest.get("runtime") or {}).get("packages_sha256"):
        warnings.add("unknown_environment")
    if not manifest["world"].get("initial_save_sha256") or not manifest["world"].get("settings_sha256"):
        warnings.add("missing_initial_world_hashes")
    if manifest["git"]["dirty"] and not manifest["git"].get("patch_sha256"):
        warnings.add("dirty_code_without_patch_hash")
    if any(row["status"] == "pending" for row in calls.values()):
        warnings.add("unresolved_model_requests")
    if any(row["resolved_model"] is None and row["status"] == "ok" for row in calls.values()):
        warnings.add("unknown_resolved_model")
    elapsed = (utc(events[-1]["time"]["utc"]) - utc(events[0]["time"]["utc"])).total_seconds()
    if any(utc(b["time"]["utc"]) < utc(a["time"]["utc"]) for a, b in zip(events, events[1:])):
        warnings.add("wall_clock_regressed")
        elapsed = None
    input_complete = all(row["input_tokens"] is not None for row in calls.values())
    output_complete = all(row["output_tokens"] is not None for row in calls.values())
    input_total = sum(row["input_tokens"] or 0 for row in calls.values())
    output_total = sum(row["output_tokens"] or 0 for row in calls.values())
    work = [row for row in actions.values() if row["action"] not in NON_WORK_ACTIONS]
    # Terminal status alone is not proof of success, especially for rocket launch.
    achieved = ("rocket_launch" in native_victory_milestones
                if manifest["target"] == "rocket_launch" and evidence_class != "synthetic" else
                manifest["target"] in milestones)
    target_achieved = True if achieved else (False if terminal.get("status") in {"failed", "timeout"} else None)
    if terminal.get("status") == "completed" and not achieved:
        warnings.add("completion_without_target_evidence")
    treatment = {key: manifest.get(key) for key in TREATMENT_FIELDS}
    summary = {
        "schema": "jev-factorio.summary.v1", "source_format": "evaluator-proposed-v1",
        "run_id": run_id,
        **{key: manifest.get(key) for key in ("experiment_id", "trial_id", "condition", "replicate", "pair_id")},
        "session_id": manifest["session_id"], "world_kind": world_kind,
        "world_seed": manifest["world"].get("seed"),
        "initial_save_sha256": manifest["world"].get("initial_save_sha256"),
        "world_settings_sha256": manifest["world"].get("settings_sha256"),
        "controller": manifest["controller"], "policy": manifest["policy"], "target": manifest["target"],
        "evidence_class": evidence_class, "treatment_id": digest(treatment),
        "treatment": treatment, "mixed_treatments": bool(problems),
        "treatment_issues": sorted(problems), "warnings": sorted(warnings),
        "benchmark_eligible": not problems and not warnings and run.integrity["complete"],
        "complete": run.integrity["complete"], "terminal_status": terminal.get("status", "incomplete"),
        "terminal_reason": terminal.get("reason"), "target_achieved": target_achieved,
        "native_victory_event_observed": native_victory,
        "events": len(events), "segments": len(segments), "observations": len(observations),
        "decisions": len(decisions), "model_calls": len(calls),
        "provider_errors": sum(row["status"] == "error" for row in calls.values()),
        "prepared_actions": len(actions), "returned_actions": len(returns),
        "verified_actions": sum(row["verified"] for row in work),
        "verified_waits": sum(row["verified"] for row in actions.values() if row["action"] in NON_WORK_ACTIONS),
        "unverified_actions": sum(not row["verified"] for row in actions.values()),
        "input_tokens": input_total if input_complete else None, "input_tokens_recorded": input_total,
        "output_tokens": output_total if output_complete else None, "output_tokens_recorded": output_total,
        "token_usage_complete": input_complete and output_complete,
        "input_token_usage_complete": input_complete, "output_token_usage_complete": output_complete,
        "models": sorted(model_names), "milestones": sorted(milestones),
        "interventions": dict(sorted(Counter(row["kind"] for row in interventions).items())),
        "wall_elapsed_seconds": elapsed, "event_head_hash": run.integrity["head_hash"],
    }
    return RunEvaluation(summary, {
        "events": rows, "decisions": list(decisions.values()), "model_calls": list(calls.values()),
        "actions": list(actions.values()), "milestones": list(milestones.values()),
        "interventions": interventions,
    }, run.integrity, run.sources)
