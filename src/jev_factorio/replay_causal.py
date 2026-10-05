"""Audit recorded producer references without manufacturing causal identities."""
from __future__ import annotations

from .request_order import RequestOrderError, reconstruct_request


def audit_producer(events: list[dict], report) -> None:
    observations, models, actions, plans, frames = {}, {}, {}, {}, {}
    plan_step_unknown = set()
    controller_initialized = False
    controller_stopped = False

    def lifecycle(event, payload, kind):
        """Keep the two supported controller boundary records run-scoped."""
        nonlocal controller_initialized, controller_stopped
        line = event["sequence"]
        decision = payload.get("decision_id")
        forbidden = {
            "trace_id", "decision_id", "observation_id", "model_call_id",
            "action_id", "attempt_id", "plan_id", "step_index",
            "candidate_set_id", "related_action_id",
        }
        if event.get("correlation") or forbidden.intersection(payload):
            report.add("error", "invalid_lifecycle_scope",
                       "Controller boundary evidence cannot claim decision-level identity",
                       line, decision)
            return
        if kind == "controller_initialized":
            allowed = {"requested_model", "model_is_mock", "initialization_timing",
                       "startup_window_timing"}
            valid = ({"requested_model", "model_is_mock"} <= set(payload) <= allowed
                     and (payload["requested_model"] is None
                          or (isinstance(payload["requested_model"], str)
                              and bool(payload["requested_model"].strip())))
                     and type(payload["model_is_mock"]) is bool
                     and all(name not in payload or isinstance(payload[name], dict)
                             for name in ("initialization_timing", "startup_window_timing")))
            if controller_initialized or controller_stopped or not valid:
                report.add("error", "invalid_lifecycle_payload" if not valid
                           else "duplicate_controller_initialized",
                           "Controller initialization evidence is malformed or out of order",
                           line)
                return
            controller_initialized = True
        else:
            allowed = {"terminal", "controller_status"}
            valid = (set(payload) == allowed and type(payload.get("terminal")) is bool
                     and (payload.get("controller_status") is None
                          or (isinstance(payload.get("controller_status"), str)
                              and bool(payload["controller_status"].strip()))))
            if controller_stopped or not controller_initialized or not valid:
                report.add("error", "invalid_lifecycle_payload" if not valid
                           else "invalid_lifecycle_scope" if not controller_initialized
                           else "duplicate_controller_stopped",
                           "Controller stop evidence is malformed or out of order", line)
                return
            controller_stopped = True
        report.run_evidence.append({
            "line": line, "event_type": kind, "payload": payload,
            "session_id": event.get("session_id"), "time": event.get("time"),
        })

    def poison_action(trace_id, action_id, *, returned=False):
        if not isinstance(trace_id, str) or not isinstance(action_id, str):
            return
        captured = actions.get((trace_id, action_id))
        if captured is None:
            return
        captured["verified"] = None
        captured["verification_conflicted"] = True
        if returned:
            captured["acknowledgment"] = "unknown"
            captured["result_conflicted"] = True

    def poison_action_id(action_id, *, returned=False):
        if not isinstance(action_id, str):
            return
        for (_, known_id), captured in actions.items():
            if known_id == action_id:
                captured["verified"] = None
                captured["verification_conflicted"] = True
                if returned:
                    captured["acknowledgment"] = "unknown"
                    captured["result_conflicted"] = True

    previous_sequence = 0
    for event in events:
        payload = event["payload"]
        kind = event["event_type"]
        sequence = event.get("sequence")
        if type(sequence) is not int or sequence <= previous_sequence:
            report.add("error", "invalid_causal_chronology",
                       "Producer events are not in strictly increasing sequence order",
                       sequence if type(sequence) is int else None,
                       payload.get("decision_id"))
            poison_action(payload.get("trace_id"), payload.get("action_id"),
                          returned=kind == "action_returned")
            previous_sequence = max(previous_sequence, sequence) if type(sequence) is int else previous_sequence
            continue
        previous_sequence = sequence
        if kind in {"controller_initialized", "controller_stopped"}:
            lifecycle(event, payload, kind)
            continue
        trace = payload.get("trace_id")
        decision = payload.get("decision_id")
        line = event["sequence"]

        def issue(code, message, severity="error"):
            report.add(severity, code, message, line, decision)

        if kind in {"run_started", "run_finished"}:
            continue
        if kind not in {
            "step_started", "step_finished", "step_failed", "observation",
            "observation_validated", "model_request", "model_response",
            "candidate_set_created", "candidate_set_filtered", "decision",
            "plan_committed", "plan_failed", "plan_progress", "precondition_checked",
            "action_prepared", "action_returned", "verification", "pending_expired",
            "connection_preflight_rejected",
            "checkpoint_written", "goal_checked", "goal_completed", "goal_activated",
        }:
            issue("unsupported_causal_event", "Event is preserved but its causal semantics are unknown", "gap")
        identities = ("trace_id", "decision_id", "observation_id", "model_call_id",
                      "action_id", "plan_id", "related_action_id", "attempt_id")
        if any(payload.get(name) is not None and (
            not isinstance(payload[name], str) or not payload[name]
        ) for name in identities):
            issue("invalid_causal_identity", "Captured causal identity is not nonempty text")
            poison_action(trace, payload.get("action_id"),
                          returned=kind == "action_returned")
            poison_action(trace, event.get("correlation", {}).get("action_id"),
                          returned=kind == "action_returned")
            continue
        if not isinstance(trace, str) or not trace:
            issue("unknown_causal_scope", "Event has no captured trace identity", "gap")
            poison_action(trace, payload.get("action_id"),
                          returned=kind == "action_returned")
            continue
        context_valid = True
        for captured, promoted in ((payload.get("session_id"), event["session_id"]),
                                   (payload.get("factorio_tick"), event["time"]["factorio_tick"])):
            if promoted is not None and captured != promoted:
                issue("context_conflict", "Envelope and captured session or tick disagree")
                context_valid = False
        for name in ("decision_id", "model_call_id", "action_id", "plan_id"):
            captured = payload.get(name)
            promoted = event["correlation"].get(name)
            if promoted is not None and promoted != captured:
                issue("correlation_conflict", "Envelope and captured identity disagree")
                context_valid = False
        if not isinstance(decision, str) or not decision:
            issue("unknown_decision", "Event has no captured decision identity", "gap")
            poison_action(trace, payload.get("action_id"),
                          returned=kind == "action_returned")
            continue
        if not context_valid:
            poison_action(trace, payload.get("action_id"),
                          returned=kind == "action_returned")
            poison_action(trace, event.get("correlation", {}).get("action_id"),
                          returned=kind == "action_returned")
            continue
        frame = frames.setdefault((trace, decision), {
            "trace_id": trace, "decision_id": decision, "evidence": [],
            "selection": None, "actions": [], "missing_evidence": [],
            "model_calls": [],
        })
        frame["evidence"].append(event)
        observation = payload.get("observation_id")
        observation_valid = True
        model = payload.get("model_call_id")
        action = payload.get("action_id")
        plan = payload.get("plan_id")
        related = payload.get("related_action_id")
        if related is not None and (trace, related) not in actions:
            issue("invalid_related_action", "Related action has no preceding preparation")
        if kind == "observation" and payload.get("status") == "ok":
            if not isinstance(observation, str) or not observation:
                issue("missing_observation_id", "Successful observation lacks identity", "gap")
            elif (trace, observation) in observations:
                issue("duplicate_observation", "Observation identity is reused")
            else:
                observations[trace, observation] = event
        elif observation is not None and (trace, observation) not in observations:
            issue("invalid_observation_reference", "Captured observation reference has no preceding observation")
            observation_valid = False
        if kind == "model_request":
            if not isinstance(model, str) or not model:
                issue("missing_model_id", "Model request lacks identity", "gap")
            elif (trace, model) in models:
                issue("duplicate_model_request", "Model call identity is reused")
            else:
                if "request_order" not in payload:
                    request_order_status = "unavailable"
                    reconstructed_request = None
                    issue("request_order_unavailable",
                          "Captured model request has no explicit supported ordering claim", "gap")
                else:
                    try:
                        reconstructed_request = reconstruct_request(
                            payload.get("state"), payload.get("questions"),
                            payload["request_order"],
                        )
                    except RequestOrderError:
                        request_order_status = "invalid"
                        reconstructed_request = None
                        issue("invalid_request_order",
                              "Captured model request ordering is malformed or conflicts with its request")
                    else:
                        request_order_status = "validated"
                model_call = {
                    "model_call_id": model,
                    "request": event,
                    "request_order_status": request_order_status,
                    "reconstructed_request": reconstructed_request,
                    "response": None,
                }
                frame["model_calls"].append(model_call)
                models[trace, model] = {
                    "request": event, "result": None, "report": model_call,
                }
        elif kind == "model_response":
            call = models.get((trace, model))
            if call is None:
                issue("invalid_model_reference", "Model response has no preceding request")
            elif call["result"] is not None:
                issue("duplicate_model_result", "Model call has multiple results")
            elif call["request"]["payload"].get("decision_id") != decision:
                issue("model_decision_conflict", "Model response belongs to another decision")
            else:
                call["result"] = event
                call["report"]["response"] = event
        elif kind == "decision":
            if frame["selection"] is not None:
                issue("duplicate_decision", "Decision identity has multiple selections")
            frame["selection"] = event
            if "model_called" in payload and type(payload["model_called"]) is not bool:
                issue("invalid_model_flag", "Captured model-called flag is not boolean")
            if payload.get("model_called"):
                call = models.get((trace, model))
                if call is None or call["result"] is None:
                    issue("invalid_model_reference", "Model-backed selection lacks preceding request/result")
                elif call["request"]["payload"].get("decision_id") != decision:
                    issue("model_decision_conflict", "Selection references another decision's model call")
            issue("unknown_candidate_reference", "Producer records no explicit candidate-set identity", "gap")
        elif kind == "plan_committed":
            definition = payload.get("plan")
            if not isinstance(plan, str) or not isinstance(definition, dict):
                issue("missing_plan_definition", "Plan commitment lacks identity or definition", "gap")
            elif definition.get("id") != plan:
                issue("plan_identity_conflict", "Committed plan identity disagrees with its definition")
            else:
                prior = plans.get((trace, plan))
                if prior is not None and prior["payload"].get("decision_id") == decision:
                    issue("duplicate_plan_commitment", "Decision repeats a plan commitment")
                plans[trace, plan] = event
            selection = frame["selection"]
            if selection is not None and selection["payload"].get("plan_id") != plan:
                issue("plan_selection_conflict", "Committed plan differs from recorded selection")
        elif kind == "action_prepared":
            prepared_valid = observation_valid
            selection = frame["selection"]
            if selection is not None and payload.get("role") != "mock_clock_advance":
                chosen_action = selection["payload"].get("action")
                if chosen_action is not None and chosen_action != payload.get("action"):
                    issue("action_selection_conflict", "Prepared action differs from recorded selection")
                    prepared_valid = False
                chosen_plan = selection["payload"].get("plan_id")
                if chosen_plan is not None and chosen_plan != plan:
                    issue("plan_selection_conflict", "Prepared action differs from recorded plan selection")
                    prepared_valid = False
            prepared_action = payload.get("action")
            if not isinstance(prepared_action, str) or not prepared_action.strip():
                issue("invalid_prepared_action", "Prepared action must be nonempty text")
                prepared_valid = False
            if type(payload.get("parameters")) is not dict:
                issue("invalid_prepared_parameters", "Prepared action parameters must be an object")
                prepared_valid = False
            if ((plan is None) != (payload.get("step_index") is None)
                    or (payload.get("step_index") is not None
                        and (type(payload.get("step_index")) is not int
                             or payload["step_index"] < 0))):
                issue("invalid_prepared_plan_step", "Prepared plan and step identities are incomplete")
                prepared_valid = False
            if not isinstance(action, str) or not action:
                issue("missing_action_id", "Prepared action lacks identity", "gap")
            elif (trace, action) in actions:
                issue("duplicate_action", "Action identity is reused")
                poison_action(trace, action, returned=True)
            else:
                captured = {"action_id": action, "prepared": event, "result": None,
                            "verifications": [], "expiries": [],
                            "acknowledgment": "unknown", "verified": None,
                            "preparation_valid": prepared_valid,
                            "verification_conflicted": False, "result_conflicted": False}
                actions[trace, action] = captured
                frame["actions"].append(captured)
            commitment = plans.get((trace, plan))
            if plan is not None and commitment is None:
                issue("unknown_plan_origin", "Plan definition may originate outside the captured trace", "gap")
                if isinstance(action, str) and (trace, action) in actions:
                    plan_step_unknown.add((trace, action))
            elif commitment is not None:
                steps = commitment["payload"]["plan"].get("steps")
                index = payload.get("step_index")
                if not isinstance(steps, list) or type(index) is not int:
                    issue("unknown_plan_step", "Captured plan step is unavailable", "gap")
                    if isinstance(action, str) and (trace, action) in actions:
                        plan_step_unknown.add((trace, action))
                elif not 0 <= index < len(steps):
                    issue("invalid_plan_step", "Action references an absent plan step")
                    if isinstance(action, str) and (trace, action) in actions:
                        actions[trace, action]["preparation_valid"] = False
                elif not isinstance(steps[index], dict):
                    issue("invalid_plan_step", "Captured plan step is not an object")
                    if isinstance(action, str) and (trace, action) in actions:
                        actions[trace, action]["preparation_valid"] = False
                elif (steps[index].get("action") != payload.get("action")
                      or (steps[index].get("parameters") or {}) != payload.get("parameters")):
                    issue("plan_step_mismatch", "Prepared action differs from the captured plan step")
                    if isinstance(action, str) and (trace, action) in actions:
                        actions[trace, action]["preparation_valid"] = False
        elif kind in {"action_returned", "verification", "pending_expired", "connection_preflight_rejected"}:
            captured = actions.get((trace, action))
            if action is None:
                issue("unknown_action_origin", "No action identity is captured for this evidence", "gap")
                continue
            if captured is None:
                issue("invalid_action_reference", "Captured action reference has no preceding preparation")
                poison_action_id(action, returned=kind == "action_returned")
                continue
            if captured.get("preflight_rejection") is not None:
                issue("conflicting_preflight_evidence", "Rejected connection has contradictory later action evidence")
                continue
            if not captured.get("preparation_valid", True):
                issue("invalid_action_reference", "Action evidence refers to an invalid preparation")
                poison_action(trace, action, returned=kind == "action_returned")
                if kind == "verification":
                    captured["verifications"].append(event)
                continue
            if kind == "connection_preflight_rejected":
                prepared = captured["prepared"]["payload"]
                result = captured["result"]
                valid = (
                    captured.get("preflight_rejection") is None
                    and not captured["verifications"]
                    and result is not None and result["payload"].get("status") == "error"
                    and payload.get("action") == prepared.get("action") == result["payload"].get("action") == "factory_connect"
                    and result["payload"].get("parameters") == prepared.get("parameters")
                    and payload.get("mutation_started") is False
                    and isinstance(payload.get("code"), str)
                    and payload.get("code") in {
                        "missing_fluid_port", "no_connection_route", "insufficient_connection_materials"}
                    and event.get("session_id") is not None
                    and event["session_id"] == captured["prepared"].get("session_id") == result.get("session_id")
                    and all(prepared.get(key) is not None
                            and payload.get(key) == prepared[key] == result["payload"].get(key)
                            for key in ("decision_id", "plan_id", "step_index", "attempt_id"))
                )
                if not valid:
                    issue("invalid_preflight_rejection", "Connection rejection lacks matching pre-mutation evidence")
                    continue
                captured["preflight_rejection"] = event
                captured["acknowledgment"] = "rejected_before_mutation"
                captured["verified"] = False
                continue
            if kind == "action_returned":
                prepared_event = captured["prepared"]
                prepared = prepared_event["payload"]
                valid = observation_valid and not captured["result_conflicted"]
                if captured["result"] is not None:
                    issue("duplicate_action_result", "Action has multiple recorded results")
                    valid = False
                if prepared.get("decision_id") != decision:
                    issue("action_decision_conflict", "Action result belongs to another decision")
                    valid = False
                origin_session = prepared_event.get("session_id")
                result_session = event.get("session_id")
                if origin_session is None or result_session is None:
                    issue("unknown_action_session", "Action result lacks known session continuity", "gap")
                    valid = False
                elif origin_session != result_session:
                    issue("action_session_conflict", "Action result belongs to another session")
                    valid = False
                if any(payload.get(key) != prepared.get(key)
                       for key in ("action", "parameters", "plan_id", "step_index",
                                   "attempt_id", "observation_id")):
                    issue("action_result_conflict", "Action result differs from preparation")
                    valid = False
                if (not isinstance(payload.get("status"), str)
                        or payload.get("status") not in {"ok", "error"}):
                    issue("invalid_action_result", "Action result lacks a supported status")
                    valid = False
                if not valid:
                    poison_action(trace, action, returned=True)
                    continue
                captured["result"] = event
                captured["acknowledgment"] = "returned" if payload.get("status") == "ok" else "ambiguous"
            elif kind == "verification":
                captured["verifications"].append(event)
                prepared_event = captured["prepared"]
                prepared = prepared_event["payload"]
                valid = observation_valid and not captured["verification_conflicted"]
                same_decision = prepared.get("decision_id") == decision
                if not same_decision:
                    cross_step = (
                        payload.get("action_origin") == "current_trace"
                        and isinstance(prepared.get("attempt_id"), str)
                        and prepared.get("attempt_id") == payload.get("attempt_id")
                        and isinstance(prepared.get("plan_id"), str)
                        and type(prepared.get("step_index")) is int
                    )
                    if not cross_step:
                        issue("verification_decision_conflict", "Verification belongs to another decision")
                        valid = False
                elif payload.get("action_origin") not in (None, "current_trace"):
                    issue("verification_origin_conflict", "Verification claims an external action origin")
                    valid = False
                if any(payload.get(key) != prepared.get(key)
                       for key in ("attempt_id", "plan_id", "step_index")):
                    issue("verification_attempt_conflict" if payload.get("attempt_id") != prepared.get("attempt_id")
                          else "verification_plan_conflict",
                          "Verification does not preserve action attempt and plan-step identity")
                    valid = False
                if not same_decision and (payload.get("action_origin") != "current_trace"
                                          or payload.get("attempt_id") is None
                                          or payload.get("plan_id") is None
                                          or payload.get("step_index") is None):
                    issue("verification_decision_conflict", "Cross-step verification lacks current-trace attempt binding")
                    valid = False
                verdict = payload.get("verified")
                if verdict is None:
                    issue("unknown_verification", "Producer has no postcondition verdict", "gap")
                elif type(verdict) is not bool:
                    issue("invalid_verification", "Captured verification is not boolean")
                    valid = False
                observed = observations.get((trace, observation))
                if observed is None:
                    issue("missing_verification_observation", "Verification has no captured observation", "gap")
                    valid = False
                elif (observed["sequence"] <= prepared_event["sequence"]
                      or observed["sequence"] >= event["sequence"]):
                    issue("stale_verification", "Verification observation precedes preparation")
                    valid = False
                if observed is not None and observed["payload"].get("decision_id") != decision:
                    issue("verification_decision_conflict", "Verification observation belongs to another decision")
                    valid = False
                sessions = [prepared_event.get("session_id"), event.get("session_id")]
                if observed is not None:
                    sessions.append(observed.get("session_id"))
                known_sessions = {session for session in sessions if session is not None}
                if len(known_sessions) > 1:
                    issue("verification_session_conflict", "Verification crosses captured session boundaries")
                    valid = False
                elif any(session is None for session in sessions):
                    issue("unknown_verification_session", "Verification lacks known session continuity", "gap")
                    valid = False
                if captured["result"] is None:
                    issue("missing_action_result", "Verification precedes or lacks a captured action result", "gap")
                    valid = False
                if not valid:
                    poison_action(trace, action)
                elif verdict is not None and not captured["verification_conflicted"]:
                    if (trace, action) not in plan_step_unknown:
                        captured["verified"] = verdict
            elif kind == "pending_expired":
                prepared_event = captured["prepared"]
                prepared = prepared_event["payload"]
                valid = observation_valid and not captured["verification_conflicted"]
                if payload.get("action_origin") != "current_trace":
                    issue("expiry_origin_conflict", "Expiry does not identify a current-trace action")
                    valid = False
                if prepared.get("decision_id") != decision:
                    if not (isinstance(prepared.get("attempt_id"), str)
                            and payload.get("attempt_id") == prepared.get("attempt_id")
                            and isinstance(prepared.get("plan_id"), str)
                            and type(prepared.get("step_index")) is int):
                        issue("expiry_decision_conflict", "Cross-step expiry lacks current-trace attempt binding")
                        valid = False
                if any(payload.get(key) != prepared.get(key)
                       for key in ("attempt_id", "plan_id", "step_index")):
                    issue("expiry_attempt_conflict" if payload.get("attempt_id") != prepared.get("attempt_id")
                          else "expiry_plan_conflict",
                          "Expiry does not preserve action attempt and plan-step identity")
                    valid = False
                origin_session = prepared_event.get("session_id")
                expiry_session = event.get("session_id")
                if origin_session is None or expiry_session is None:
                    issue("unknown_expiry_session", "Expiry lacks known session continuity", "gap")
                    valid = False
                elif origin_session != expiry_session:
                    issue("expiry_session_conflict", "Expiry crosses captured session boundaries")
                    valid = False
                pending = prepared.get("pending")
                if (isinstance(pending, dict) and "started_tick" in pending
                        and payload.get("started_tick") != pending.get("started_tick")):
                    issue("expiry_started_tick_conflict", "Expiry differs from the prepared pending attempt")
                    valid = False
                if not valid:
                    poison_action(trace, action)
                else:
                    captured["expiries"].append(event)
    for captured in actions.values():
        if captured["result"] is None:
            report.add("gap", "unknown_acknowledgment", "Prepared action has no recorded result")
        if not captured["verifications"] and not captured.get("preflight_rejection"):
            report.add("gap", "unverified_action", "Action has no captured verification")
    for call in models.values():
        if call["result"] is None:
            report.add("gap", "unfinished_model_call", "Model request has no recorded result")
    for frame in frames.values():
        starts = [event for event in frame["evidence"] if event["event_type"] == "step_started"]
        terminals = [event for event in frame["evidence"]
                     if event["event_type"] in {"step_finished", "step_failed"}]
        if not starts:
            report.add("gap", "missing_step_start", "Captured decision step has no start event",
                       decision_id=frame["decision_id"])
        if len(starts) > 1 or len(terminals) > 1:
            report.add("error", "duplicate_step_boundary", "Decision step has duplicate lifecycle boundaries",
                       decision_id=frame["decision_id"])
        if starts and starts[0] is not frame["evidence"][0]:
            report.add("error", "late_step_start", "Decision evidence precedes its start",
                       decision_id=frame["decision_id"])
        if terminals and terminals[-1] is not frame["evidence"][-1]:
            report.add("error", "post_terminal_evidence", "Decision evidence follows its terminal event",
                       decision_id=frame["decision_id"])
        if not terminals:
            report.add("gap", "unfinished_step", "Captured decision step has no terminal event",
                       decision_id=frame["decision_id"])
    report.decisions = list(frames.values())
