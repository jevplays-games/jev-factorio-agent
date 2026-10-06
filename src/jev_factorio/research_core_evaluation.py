"""Analytic projection of verified logging-core and causal-controller evidence."""
from __future__ import annotations

from .research_events import EvidenceError, MixedTreatmentError, VerifiedRun, canonical, digest, text, utc
from .research_evaluation import NON_WORK_ACTIONS, RunEvaluation, _duration, _usage
from .preflight_codes import CONNECTION_PREFLIGHT_CODES
from .telemetry import WAIT_ACTIONS


def reduce_core_run(run: VerifiedRun, *, allow_mixed_treatments: bool = False) -> RunEvaluation:
    manifest = run.manifest
    config = manifest["configuration"]
    non_work_actions = NON_WORK_ACTIONS | WAIT_ACTIONS
    run_id = manifest["run_id"]
    tables = {name: [] for name in ("events", "decisions", "model_calls", "actions",
                                    "milestones", "interventions")}
    observations, decisions, calls, actions, milestones = {}, {}, {}, {}, {}
    native_victory_observations, native_victory_milestones = set(), set()
    goal_checks = {}
    observation_decisions, action_payloads, action_returns = {}, {}, {}
    action_return_payloads, action_prepared_sessions, action_return_sessions = {}, {}, {}
    action_prepared_ticks, action_return_ticks, steps = {}, {}, {}
    finished_steps = set()
    sessions, worlds, traces, models, requested_models = set(), set(), set(), set(), set()
    problems = set()
    warnings = {"missing_initial_world_hashes", "missing_experiment_metadata"}
    terminal = None
    native_victory = False
    unknown_events = set()
    passive = {"run_started", "run_finished", "step_started", "step_finished", "step_failed",
               "candidate_set_created", "candidate_set_filtered", "plan_committed", "plan_failed",
               "plan_progress", "precondition_checked", "pending_expired", "checkpoint_written",
               "goal_activated", "observation_validated"}

    def identity(payload, field):
        value = payload.get(field)
        return (text(payload.get("trace_id"), "trace_id"), text(value, field))

    def reference(payload, field):
        value = payload.get(field)
        return None if value is None else canonical(identity(payload, field)).decode("utf-8")

    for event in run.events:
        payload, kind, sequence = event["payload"], event["event_type"], event["sequence"]
        trace = payload.get("trace_id")
        if trace is not None:
            traces.add(text(trace, "trace_id"))
        for field, values in (("session_id", sessions), ("world_kind", worlds)):
            if payload.get(field) is not None:
                values.add(text(payload[field], field))
        if event.get("session_id") is not None:
            sessions.add(event["session_id"])
            if payload.get("session_id") not in (None, event["session_id"]):
                raise EvidenceError("Core envelope and causal session disagree")
        for field, value in event["correlation"].items():
            if payload.get(field) is not None and payload[field] != value:
                raise EvidenceError("Core envelope and causal correlation disagree")
        for field in ("controller", "policy", "confidence_floor"):
            if field in payload and payload[field] != config.get(field):
                problems.add("changed:" + field)
        row = {
            "run_id": run_id, "sequence": sequence, "segment_id": trace,
            "event_type": kind, **event["time"],
            "decision_id": reference(payload, "decision_id"),
            "model_call_id": reference(payload, "model_call_id"),
            "action_id": reference(payload, "action_id"),
            "duration_ms": _duration(payload), "event_hash": event["event_hash"],
        }
        tables["events"].append(row)
        if kind in {"observation", "observation_validated", "model_request", "model_response",
                    "decision", "action_prepared", "action_returned", "verification",
                    "connection_preflight_rejected", "goal_checked", "goal_completed"}:
            step_key = identity(payload, "decision_id")
            if step_key not in steps or step_key in finished_steps:
                raise EvidenceError("Causal event lacks an active controller step")
        if kind == "step_started":
            key = identity(payload, "decision_id")
            if key in steps:
                raise EvidenceError("Duplicate causal step identity")
            steps[key] = sequence
        elif kind in {"step_finished", "step_failed"}:
            key = identity(payload, "decision_id")
            if key not in steps or key in finished_steps:
                raise EvidenceError("Causal step completion lacks its unique start")
            finished_steps.add(key)
        elif kind == "observation":
            if payload.get("status") == "error":
                warnings.add("failed_observation")
                continue
            key = identity(payload, "observation_id")
            snapshot = payload.get("snapshot")
            if key in observations or not isinstance(snapshot, dict):
                raise EvidenceError("Duplicate causal observation or missing snapshot")
            for field in ("session_id", "world_kind"):
                if snapshot.get(field) != payload.get(field):
                    raise EvidenceError("Observation identity differs from causal envelope")
            observations[key] = (sequence, snapshot)
            observation_decisions[key] = row["decision_id"]
            if (snapshot.get("world_kind") in {"native", "fle", "play_api"}
                    and snapshot.get("victory") is True
                    and snapshot.get("victory_source") == "native:base-game-rocket-launch"):
                native_victory = True
                native_victory_observations.add(key)
        elif kind == "model_request":
            key = identity(payload, "model_call_id")
            if key in calls:
                raise EvidenceError("Duplicate causal model request")
            requested = payload.get("requested_model")
            if requested is not None:
                requested_models.add(text(requested, "requested_model"))
            if config.get("requested_model") is not None and requested != config["requested_model"]:
                problems.add("changed:requested_model")
            calls[key] = {
                "run_id": run_id, "model_call_id": reference(payload, "model_call_id"),
                "segment_id": trace, "decision_id": row["decision_id"],
                "request_sequence": sequence, "response_sequence": None,
                "requested_model": requested, "resolved_model": None, "status": "pending",
                "duration_ms": None, "input_tokens": None, "output_tokens": None,
            }
        elif kind == "model_response":
            key = identity(payload, "model_call_id")
            if key not in calls or calls[key]["response_sequence"] is not None:
                raise EvidenceError("Unmatched or duplicate causal model response")
            if calls[key]["decision_id"] != row["decision_id"]:
                raise EvidenceError("Causal model response changes decision identity")
            if payload.get("status") not in {"ok", "error"}:
                raise EvidenceError("Invalid causal model response status")
            resolved = payload.get("resolved_model")
            if resolved is not None:
                models.add(text(resolved, "resolved_model"))
            calls[key].update(response_sequence=sequence, status=payload["status"],
                              resolved_model=resolved, duration_ms=_duration(payload),
                              input_tokens=_usage(payload, "input_tokens"),
                              output_tokens=_usage(payload, "output_tokens"))
        elif kind == "decision":
            key = identity(payload, "decision_id")
            if key in decisions:
                raise EvidenceError("Duplicate causal decision")
            if payload.get("model_call_id") is not None:
                call = calls.get(identity(payload, "model_call_id"))
                if call is None or call["response_sequence"] is None:
                    raise EvidenceError("Decision references unfinished model request")
                if call["decision_id"] != row["decision_id"]:
                    raise EvidenceError("Decision and causal model request disagree")
            decisions[key] = {
                "run_id": run_id, "decision_id": row["decision_id"], "sequence": sequence,
                "segment_id": trace, "model_call_id": row["model_call_id"],
                "source": text(payload.get("source"), "decision.source"),
                "action": payload.get("action"),
            }
        elif kind == "action_prepared":
            key = identity(payload, "action_id")
            if key in actions:
                raise EvidenceError("Duplicate causal action")
            if identity(payload, "decision_id") not in decisions:
                warnings.add("dispatch_without_new_decision")
            if identity(payload, "decision_id") not in steps:
                raise EvidenceError("Causal action lacks its controller step")
            action_payloads[key] = payload
            actions[key] = {
                "run_id": run_id, "action_id": row["action_id"],
                "decision_id": row["decision_id"], "segment_id": trace,
                "action": text(payload.get("action"), "action"),
                "prepared_sequence": sequence, "returned_sequence": None,
                "acknowledged": None, "verified": False, "verification_count": 0,
                "verified_sequence": None, "duration_ms": None,
                "preflight_rejected": False, "preflight_rejection_code": None,
            }
            action_prepared_sessions[key] = event.get("session_id")
            action_prepared_ticks[key] = event["time"].get("factorio_tick")
        elif kind == "action_returned":
            key = identity(payload, "action_id")
            if key not in actions or actions[key]["returned_sequence"] is not None:
                raise EvidenceError("Unmatched or duplicate causal action return")
            if payload.get("status") not in {"ok", "error"}:
                raise EvidenceError("Invalid causal action return status")
            if payload.get("action") != actions[key]["action"]:
                raise EvidenceError("Causal action return changes action identity")
            if row["decision_id"] != actions[key]["decision_id"]:
                raise EvidenceError("Causal action return changes decision identity")
            if any(payload.get(field) != action_payloads[key].get(field)
                   for field in ("plan_id", "step_index", "attempt_id", "role")):
                raise EvidenceError("Causal action return changes preparation identity")
            action_returns[key] = payload["status"]
            action_return_payloads[key] = payload
            action_return_sessions[key] = event.get("session_id")
            action_return_ticks[key] = event["time"].get("factorio_tick")
            actions[key].update(returned_sequence=sequence,
                                duration_ms=_duration(payload))
            warnings.add("backend_acknowledgment_unavailable")
        elif kind == "verification":
            verified = payload.get("verified")
            if verified is not None and type(verified) is not bool:
                raise EvidenceError("Invalid causal verification value")
            if payload.get("action_id") is None:
                warnings.add("unattributed_verification")
                continue
            key = identity(payload, "action_id")
            if key not in actions:
                raise EvidenceError("Verification references unknown causal action")
            if actions[key]["preflight_rejected"]:
                raise EvidenceError("Preflight-rejected action has contradictory verification")
            observation_key = identity(payload, "observation_id")
            observation = observations.get(observation_key)
            returned = actions[key]["returned_sequence"]
            if returned is None:
                raise EvidenceError("Verification precedes causal action return")
            if observation is None or observation[0] <= returned:
                raise EvidenceError("Verification lacks post-dispatch observation")
            if observation_decisions[observation_key] != row["decision_id"]:
                raise EvidenceError("Verification changes observation decision identity")
            if identity(payload, "decision_id") not in steps:
                raise EvidenceError("Verification lacks its controller step")
            if verified is None:
                warnings.add("verification_predicate_unavailable")
                continue
            prepared = action_payloads[key]
            predicate, pending = payload.get("predicate"), prepared.get("pending")
            if (payload.get("status") != "ok" or not isinstance(predicate, dict)
                    or predicate.get("action") != actions[key]["action"]
                    or payload.get("action_origin") != "current_trace"
                    or not isinstance(pending, dict)
                    or payload.get("started_tick") != pending.get("started_tick")
                    or any(payload.get(field) != prepared.get(field)
                           for field in ("plan_id", "step_index", "attempt_id"))):
                raise EvidenceError("Verification lacks matching explicit action predicate")
            if row["decision_id"] != actions[key]["decision_id"] and payload.get("phase") != "pending_poll":
                raise EvidenceError("Verification changes decision outside pending polling")
            if action_returns[key] == "error":
                if payload.get("phase") != "pending_poll":
                    raise EvidenceError("Ambiguous action return requires pending predicate verification")
                warnings.add("verification_after_ambiguous_return")
            actions[key]["verification_count"] += 1
            if verified and not actions[key]["verified"]:
                actions[key].update(verified=True, verified_sequence=sequence)
        elif kind == "goal_checked":
            if payload.get("status") == "ok" and payload.get("completed") is True:
                goal_checks[(identity(payload, "observation_id"),
                             text(payload.get("goal"), "goal"))] = sequence
        elif kind == "goal_completed":
            observation = observations.get(identity(payload, "observation_id"))
            goal = text(payload.get("goal"), "goal")
            checked = goal_checks.get((identity(payload, "observation_id"), goal))
            if (observation is None or checked is None or checked < observation[0]
                    or payload.get("verification_source") != "existing_goal_predicate"):
                raise EvidenceError("Causal milestone lacks its predicate observation")
            if goal not in milestones:
                milestones[goal] = {
                    "run_id": run_id, "goal": goal, "sequence": sequence,
                    "segment_id": trace, "observation_id": reference(payload, "observation_id"),
                    "factorio_tick": payload.get("factorio_tick"),
                }
            if goal == "rocket_launch" and identity(payload, "observation_id") in native_victory_observations:
                native_victory_milestones.add(goal)
        elif kind == "connection_preflight_rejected":
            key = identity(payload, "action_id")
            action = actions.get(key)
            prepared = action_payloads.get(key)
            returned = action_return_payloads.get(key)
            event_session = event.get("session_id")
            identity_fields = ("decision_id", "plan_id", "step_index", "attempt_id")
            valid = (
                action is not None and prepared is not None and returned is not None
                and action["action"] == "factory_connect"
                and config["controller"] == "hierarchical"
                and prepared.get("controller") == returned.get("controller")
                == payload.get("controller") == "hierarchical"
                and prepared.get("role") == returned.get("role") == "plan"
                and prepared.get("dispatch") == "prepared"
                and action_returns.get(key) == "error"
                and action["returned_sequence"] is not None
                and sequence > action["returned_sequence"]
                and action["verification_count"] == 0
                and not action["verified"] and not action["preflight_rejected"]
                and event_session is not None
                and action_prepared_sessions.get(key) == event_session
                and action_return_sessions.get(key) == event_session
                and prepared.get("session_id") == returned.get("session_id")
                == payload.get("session_id") == event_session
                and prepared.get("world_kind") == returned.get("world_kind")
                == payload.get("world_kind")
                and prepared.get("action") == returned.get("action")
                == payload.get("action") == "factory_connect"
                and type(prepared.get("parameters")) is dict
                and returned.get("parameters") == prepared.get("parameters")
                and prepared.get("observation_id") is not None
                and returned.get("observation_id") == payload.get("observation_id")
                == prepared.get("observation_id")
                and identity(payload, "decision_id") in decisions
                and type(prepared.get("plan_id")) is str and bool(prepared["plan_id"])
                and type(prepared.get("attempt_id")) is str and bool(prepared["attempt_id"])
                and type(prepared.get("step_index")) is int
                and prepared["step_index"] >= 0
                and all(prepared.get(field) is not None
                        and payload.get(field) == prepared[field] == returned.get(field)
                        for field in identity_fields)
                and type(event["time"].get("factorio_tick")) is int
                and event["time"]["factorio_tick"] >= 0
                and event["time"].get("factorio_tick") == payload.get("factorio_tick")
                == action_prepared_ticks.get(key) == action_return_ticks.get(key)
                and type(payload.get("action_origin")) is str
                and payload["action_origin"] == "current_trace"
                and payload.get("mutation_started") is False
                and type(payload.get("code")) is str
                and payload["code"] in CONNECTION_PREFLIGHT_CODES
                and type(returned.get("error")) is dict
                and returned["error"] == {"category": "invalid_data", "http_status": None}
            )
            if not valid:
                raise EvidenceError(
                    "Connection preflight rejection lacks matching returned pre-mutation evidence")
            action.update(preflight_rejected=True,
                          preflight_rejection_code=payload["code"])
        elif kind not in passive:
            unknown_events.add(kind)
        if kind == "run_finished":
            terminal = payload["outcome"]
    if len(sessions) > 1 or len(worlds) > 1:
        raise EvidenceError("Mixed causal world sessions or kinds")
    if len(models) > 1:
        problems.add("changed:resolved_model")
    if len(requested_models) > 1:
        problems.add("changed:requested_model")
    if len(traces) > 1:
        problems.add("multiple_controller_traces")
    if problems and not allow_mixed_treatments:
        raise MixedTreatmentError("Mixed causal treatment: " + ", ".join(sorted(problems)))
    world = next(iter(worlds), None)
    if config["backend"] == "mock" and world not in {None, "mock"}:
        raise EvidenceError("Mock configuration conflicts with observed world")
    evidence_class = "synthetic" if world == "mock" else (
        "tool-assisted" if world in {"native", "fle", "play_api"} else "unknown")
    if not sessions:
        warnings.add("unknown_session")
    if world is None:
        warnings.add("unknown_world_kind")
    if not steps:
        warnings.add("lifecycle_only")
    if steps.keys() - finished_steps:
        warnings.add("unfinished_causal_steps")
    if unknown_events:
        warnings.add("uninterpreted_event_types")
    if not run.integrity["complete"]:
        warnings.add("incomplete_run")
    if manifest["provenance"]["git"]["commit"] is None:
        warnings.add("unknown_code_revision")
    if manifest["provenance"]["git"]["dirty"] is not False:
        warnings.add("unattested_clean_code")
    if any(call["status"] == "pending" for call in calls.values()):
        warnings.add("unresolved_model_requests")
    if any(call["resolved_model"] is None for call in calls.values()):
        warnings.add("unknown_resolved_model")
    target = config["target"]
    achieved = ("rocket_launch" in native_victory_milestones
                if target == "rocket_launch" and world != "mock" else target in milestones)
    elapsed = (utc(run.events[-1]["time"]["utc"]) - utc(run.events[0]["time"]["utc"])).total_seconds()
    if any(utc(right["time"]["utc"]) < utc(left["time"]["utc"])
           for left, right in zip(run.events, run.events[1:])):
        elapsed = None
        warnings.add("wall_clock_regressed")
    treatment = {**config, **manifest["provenance"]}
    summary = {
        "schema": "jev-factorio.summary.v1", "source_format": "logging-core-v1",
        "run_id": run_id, "session_id": next(iter(sessions), None), "world_kind": world,
        **{key: None for key in ("experiment_id", "trial_id", "condition", "replicate", "pair_id",
                                 "world_seed", "initial_save_sha256", "world_settings_sha256")},
        "controller": config["controller"], "policy": config["policy"], "target": target,
        "evidence_class": evidence_class, "treatment_id": digest(treatment), "treatment": treatment,
        "mixed_treatments": bool(problems), "treatment_issues": sorted(problems),
        "warnings": sorted(warnings), "benchmark_eligible": False,
        "complete": run.integrity["complete"], "terminal_status": terminal or "incomplete",
        "terminal_reason": None, "target_achieved": True if achieved else None,
        "native_victory_event_observed": native_victory,
        "events": len(run.events), "segments": len(traces), "observations": len(observations),
        "decisions": len(decisions), "model_calls": len(calls),
        "model_call_count_scope": "Recorded causal requests only; lifecycle logs do not capture calls.",
        "provider_errors": sum(call["status"] == "error" for call in calls.values()),
        "prepared_actions": len(actions),
        "returned_actions": sum(action["returned_sequence"] is not None for action in actions.values()),
        "verified_actions": sum(action["verified"] and action["action"] not in non_work_actions
                                for action in actions.values()),
        "verified_waits": sum(action["verified"] and action["action"] in non_work_actions
                              for action in actions.values()),
        "unverified_actions": sum(not action["verified"] and not action["preflight_rejected"]
                                   for action in actions.values()),
        "preflight_rejected_actions": sum(action["preflight_rejected"]
                                           for action in actions.values()),
        "models": sorted(models), "milestones": sorted(milestones), "interventions": {},
        "requested_models": sorted(requested_models),
        "wall_elapsed_seconds": elapsed, "event_head_hash": run.integrity["head_hash"],
        "uninterpreted_event_types": sorted(unknown_events),
    }
    for kind in ("input", "output"):
        field = kind + "_tokens"
        complete = bool(steps) and all(call[field] is not None for call in calls.values())
        total = sum(call[field] or 0 for call in calls.values())
        summary[field] = total if complete else None
        summary[field + "_recorded"] = total
        summary[kind + "_token_usage_complete"] = complete
    summary["token_usage_complete"] = (summary["input_token_usage_complete"]
                                        and summary["output_token_usage_complete"])
    tables.update(decisions=list(decisions.values()), model_calls=list(calls.values()),
                  actions=list(actions.values()), milestones=list(milestones.values()))
    return RunEvaluation(summary, tables, run.integrity, run.sources)
