"""Read-only, offline reconstruction of captured controller evidence.

Only the standard library is imported. This is an evidence replay, never a
backend/action replay. See docs/REPLAY.md for the explicit event-v1 contract,
legacy limitations, hash canonicalization, and exit codes.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

EVENT_SCHEMA = "jev-factorio.event.v1"
REPORT_SCHEMA = "jev-factorio.replay.v1"
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_EVENTS = 100_000
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
RUN_EVENTS = frozenset({
    "run_started", "run_finished", "segment_started", "code_revision_changed",
    "checkpoint_written", "incident_started", "incident_finished",
    "operational_repair", "manual_intervention", "goal_completed",
})
CAUSAL_EVENTS = frozenset({
    "observation", "candidate_set_created", "model_request", "model_response",
    "provider_error", "decision", "plan_committed", "action_prepared",
    "action_dispatched", "action_returned", "dispatch_error", "verification", "decision_finished",
})


class ReplayInputError(ValueError):
    """An input cannot be read within the supported, bounded JSON contract."""


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ReplayInputError("Duplicate JSON object key")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise ReplayInputError("Non-finite JSON number")


def _integer(value: str) -> int:
    if len(value) > 128:
        raise ReplayInputError("JSON integer exceeds the supported precision")
    return int(value)


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ReplayInputError("Non-finite JSON number")
    return number


def _decode(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_nonfinite, parse_float=_float, parse_int=_integer)
        if not isinstance(value, dict):
            raise ReplayInputError("Expected a JSON object")
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        # Do not echo input fragments, exception text, paths, or credentials.
        raise ReplayInputError("Invalid or unsupported JSON object") from error


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _event_hash(event: dict) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    line: int | None = None
    decision_id: str | None = None


@dataclass
class ReplayReport:
    format: str = "unknown"
    run_id: str | None = None
    manifest: dict | None = None
    integrity: dict = field(default_factory=lambda: {"status": "unverified"})
    decisions: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    run_evidence: list[dict] = field(default_factory=list)

    def add(self, severity: str, code: str, message: str, line: int | None = None,
            decision_id: str | None = None) -> None:
        self.findings.append(Finding(severity, code, message, line, decision_id))

    @property
    def status(self) -> str:
        if any(item.severity == "error" for item in self.findings):
            return "invalid"
        if any(item.severity == "gap" for item in self.findings):
            return "incomplete"
        return "complete"

    def to_dict(self) -> dict:
        return {
            "schema": REPORT_SCHEMA, "status": self.status,
            "offline": True, "replay_authorized": False,
            "behavior_reexecuted": False, "authenticity_established": False,
            "audit_scope": (["structure", "hash_chain", "captured_causal_references", "plan_dispatch_consistency"]
                            if self.format == "research-v1" else
                            ["structure", "hash_chain", "causal_links", "candidate_membership", "plan_dispatch_consistency"]),
            "not_recomputed": ["policy_selection", "postcondition_truth", "world_evolution"],
            "format": self.format, "run_id": self.run_id,
            "manifest": self.manifest, "integrity": self.integrity,
            "segments": self.segments, "mixed_segments": len(self.segments) > 1,
            "decision_count": None if self.format == "legacy" else len(self.decisions),
            "legacy_record_count": len(self.decisions) if self.format == "legacy" else None,
            "decisions": self.decisions, "run_evidence": self.run_evidence,
            "events": self.events,
            "findings": [asdict(item) for item in self.findings],
        }


def _read(path: Path, report: ReplayReport, max_line_bytes: int,
          max_input_bytes: int, max_events: int) -> list[tuple[int, dict]]:
    rows = []
    size = 0
    digest = hashlib.sha256()
    consumed_all = True
    try:
        with path.open("rb") as stream:
            line = 0
            while True:
                raw = stream.readline(min(max_line_bytes, max_input_bytes - size) + 1)
                if not raw:
                    break
                line += 1
                size += len(raw)
                digest.update(raw)
                if size > max_input_bytes or len(raw) > max_line_bytes or line > max_events:
                    report.add("error", "input_limit", "Input exceeds the configured limit", line)
                    consumed_all = False
                    break
                if not raw.strip():
                    report.add("error", "blank_record", "Blank JSONL records are not permitted", line)
                    consumed_all = False
                    break
                try:
                    row = _decode(raw)
                    if row.get("schema") == EVENT_SCHEMA and "schema_version" in row:
                        canonical = json.dumps(row, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
                        if raw != canonical:
                            raise ReplayInputError("Noncanonical producer record")
                except ReplayInputError:
                    report.add("error", "invalid_json", "Invalid JSONL record; replay stopped", line)
                    consumed_all = False
                    break
                rows.append((line, row))
                if not raw.endswith(b"\n"):
                    report.add("gap", "unterminated_record", "Final record has no newline", line)
    except OSError as error:
        raise ReplayInputError("Cannot read the input log") from error
    report.integrity.update({
        "input_sha256": digest.hexdigest(), "digest_scope": "file" if consumed_all else "consumed_prefix",
        "bytes_read": size, "parsed_records": len(rows),
    })
    if not rows:
        report.add("gap", "empty_log", "No complete records are available")
    return rows


def _envelope(event: dict) -> bool:
    if ("line" in event or "prev_hash" not in event
            or event.get("schema") != EVENT_SCHEMA
            or type(event.get("sequence")) is not int or event["sequence"] < 1
            or not _identifier(event.get("run_id"))
            or not _identifier(event.get("segment_id"))
            or not _identifier(event.get("event_type"))
            or not isinstance(event.get("payload"), dict)
            or not isinstance(event.get("correlation"), dict)
            or not isinstance(event.get("time"), dict)):
        return False
    if any(value is not None and not _identifier(value)
           for value in event["correlation"].values()):
        return False
    stamp = event["time"]
    if type(stamp.get("monotonic_ns")) is not int or stamp["monotonic_ns"] < 0:
        return False
    tick = stamp.get("factorio_tick")
    if tick is not None and (type(tick) is not int or tick < 0):
        return False
    utc = stamp.get("utc")
    try:
        if not isinstance(utc, str):
            return False
        parsed = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        return parsed.utcoffset() == timedelta(0)
    except ValueError:
        return False


def _validated_events(rows: list[tuple[int, dict]], report: ReplayReport,
                      expected_head: str | None) -> list[dict]:
    accepted: list[dict] = []
    previous = None
    for line, event in rows:
        if not _envelope(event):
            report.add("error", "unsupported_envelope", "Unsupported or malformed event envelope", line)
            break
        if report.run_id is None:
            report.run_id = event["run_id"]
        if event["run_id"] != report.run_id:
            report.add("error", "mixed_run", "A single event stream cannot contain multiple run IDs", line)
            break
        if event["sequence"] != len(accepted) + 1:
            report.add("error", "sequence_mismatch", "Expected contiguous sequence numbers starting at one", line)
            break
        claimed = event.get("event_hash")
        try:
            valid_hash = (isinstance(claimed, str) and _HASH.fullmatch(claimed)
                          and claimed == _event_hash(event))
        except (ValueError, UnicodeError, RecursionError):
            valid_hash = False
        if event.get("prev_hash") != previous or not valid_hash:
            report.add("error", "hash_mismatch", "Hash chain verification failed; replay stopped", line)
            break
        accepted.append({"line": line, **event})
        previous = claimed
    report.integrity.update({
        "status": "valid_prefix", "verified_events": len(accepted), "head_hash": previous,
        "expected_head": expected_head, "anchored": False,
    })
    if expected_head is not None:
        if (previous != expected_head or len(accepted) != len(rows)
                or report.integrity.get("digest_scope") != "file"):
            report.add("error", "head_mismatch", "The captured chain does not match the supplied head")
        else:
            report.integrity["anchored"] = True
    if accepted and len(accepted) == len(rows) and not any(
            item.code in {"invalid_json", "input_limit", "blank_record"} for item in report.findings):
        report.integrity["status"] = "verified_anchored" if report.integrity["anchored"] else "verified_unanchored"
    return accepted


def _frame(segment: str, identity: str) -> dict:
    return {
        "segment_id": segment, "decision_id": identity, "selection": None,
        "observations": [], "candidate_sets": [], "model_calls": [],
        "plans": [], "actions": [], "verifications": [], "termination": None,
        "evidence": [], "missing_evidence": [],
    }


class _Audit:
    def __init__(self, report: ReplayReport):
        self.report = report
        self.index: dict[tuple[str, str, str | None, str], dict] = {}
        self.frames: dict[tuple[str, str], dict] = {}
        self.active_frame: dict | None = None
        self.observed_sequences: set[int] = set()

    def issue(self, event: dict, code: str, message: str, severity: str = "error") -> None:
        self.report.add(severity, code, message, event["line"],
                        event["correlation"].get("decision_id"))

    def identity(self, event: dict, field_name: str, value: Any = None) -> str | None:
        correlation = event["correlation"].get(field_name)
        payload = event["payload"].get(field_name) if value is None else value
        if correlation is not None and payload is not None and correlation != payload:
            self.issue(event, "identity_conflict", "Payload and correlation identities disagree")
        identity = correlation if correlation is not None else payload
        if not _identifier(identity):
            self.issue(event, "missing_identity", "Required evidence identity is missing", "gap")
            return None
        return identity

    def put(self, kind: str, identity: str | None, event: dict) -> None:
        if identity is None:
            return
        key = (event["segment_id"], kind,
               event["correlation"].get("decision_id") if kind == "plan" else None, identity)
        if key in self.index:
            self.issue(event, "duplicate_identity", "Evidence identities must be unique within a segment")
        else:
            self.index[key] = event

    def get(self, kind: str, identity: Any, owner: dict, *, same_decision: bool = False) -> dict | None:
        if not _identifier(identity):
            self.issue(owner, "missing_reference", "Required causal reference is missing", "gap")
            return None
        event = self.index.get((owner["segment_id"], kind,
                                owner["correlation"].get("decision_id") if kind == "plan" else None, identity))
        if event is None:
            self.issue(owner, "dangling_reference", "Referenced evidence is absent from this segment", "gap")
            return None
        if event["sequence"] >= owner["sequence"]:
            self.issue(owner, "noncausal_reference", "A causal dependency must precede its consumer")
        if same_decision and event["correlation"].get("decision_id") != owner["correlation"].get("decision_id"):
            self.issue(owner, "decision_mismatch", "Causal evidence belongs to a different decision")
        return event

    def prepare(self, events: list[dict]) -> None:
        previous_segment = None
        finished = False
        terminal_decisions: set[tuple[str, str]] = set()
        for event in events:
            kind, payload = event["event_type"], event["payload"]
            segment = event["segment_id"]
            if finished:
                self.issue(event, "after_run_finished", "Events follow the terminal run event")
            if kind == "run_finished":
                finished = True
            if kind == "run_started" and event["sequence"] != 1:
                self.issue(event, "duplicate_start", "run_started must occur only at sequence one")
            if segment != previous_segment:
                if segment in self.report.segments:
                    self.issue(event, "segment_reentry", "A previously closed segment was reentered")
                if previous_segment is not None and (
                    kind not in {"segment_started", "code_revision_changed"}
                    or payload.get("previous_segment_id") != previous_segment
                ):
                    self.issue(event, "unannounced_segment", "Segment transition lacks explicit predecessor evidence")
                self.report.segments.append(segment)
                previous_segment = segment
            elif kind == "code_revision_changed":
                self.issue(event, "revision_without_segment", "Code revision changes require a new segment")
            if kind not in RUN_EVENTS | CAUSAL_EVENTS:
                self.issue(event, "unsupported_event", "Unknown event type retained but not interpreted", "gap")
            for field_name, value in event["correlation"].items():
                if field_name in payload and payload[field_name] != value:
                    self.issue(event, "identity_conflict", "Payload and correlation identities disagree")
            decision_id = event["correlation"].get("decision_id")
            payload_decision = payload.get("decision_id")
            claimed_decisions = {value for value in (decision_id, payload_decision)
                                 if _identifier(value)}
            if any((segment, value) in terminal_decisions for value in claimed_decisions):
                self.issue(event, "post_terminal_evidence",
                           "Decision evidence follows its terminal record")
                continue
            if decision_id:
                key = (segment, decision_id)
                frame = self.frames.setdefault(key, _frame(*key))
                frame["evidence"].append(event)
                if kind == "decision_finished":
                    terminal_decisions.add(key)
            elif kind in CAUSAL_EVENTS - {"observation"}:
                self.issue(event, "unattributed_event", "Causal event has no decision identity", "gap")
                continue
            if kind == "observation":
                self.put("observation", self.identity(event, "observation_id"), event)
                if not isinstance(payload.get("state"), dict):
                    self.issue(event, "missing_state", "Observation lacks a captured state object", "gap")
            elif kind == "candidate_set_created":
                self.put("candidates", self.identity(event, "candidate_set_id"), event)
            elif kind == "model_request":
                self.put("request", self.identity(event, "model_call_id"), event)
            elif kind == "plan_committed":
                plan = payload.get("plan")
                plan_id = plan.get("id") if isinstance(plan, dict) else None
                self.put("plan", self.identity(event, "plan_id", plan_id), event)
                if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
                    self.issue(event, "missing_plan", "Committed plan has no captured step definition", "gap")
            elif kind == "action_prepared":
                self.put("action", self.identity(event, "action_id"), event)
        if not events or events[0]["event_type"] != "run_started":
            self.report.add("gap", "missing_run_start", "run_started is missing")
        if not events or events[-1]["event_type"] != "run_finished":
            self.report.add("gap", "missing_run_finish", "Run has no captured terminal boundary")

    def candidates(self, event: dict) -> list[str]:
        entries = event["payload"].get("candidates")
        if (not isinstance(entries, list) or any(
            not isinstance(entry, dict) or not _identifier(entry.get("id")) for entry in entries
        )):
            self.issue(event, "invalid_candidates", "Candidates must be objects with explicit IDs")
            return []
        identities = [entry["id"] for entry in entries]
        if len(identities) != len(set(identities)):
            self.issue(event, "duplicate_candidate", "Candidate IDs are not unique")
        return identities

    def include_observation(self, observed: dict) -> None:
        if self.active_frame is not None and observed["sequence"] not in self.observed_sequences:
            self.active_frame["observations"].append(observed)
            self.observed_sequences.add(observed["sequence"])

    def reference_observation(self, event: dict) -> dict | None:
        observed = self.get("observation", event["payload"].get("observation_id"), event)
        if observed is not None:
            self.include_observation(observed)
        return observed

    def reconstruct(self) -> None:
        for frame in self.frames.values():
            self.reconstruct_frame(frame)
            self.report.decisions.append(frame)

    def reconstruct_frame(self, frame: dict) -> None:
        self.active_frame = frame
        self.observed_sequences = set()
        calls: dict[str, dict] = {}
        actions: dict[str, dict] = {}
        selections = []
        for event in frame["evidence"]:
            kind, payload = event["event_type"], event["payload"]
            if kind == "observation":
                self.include_observation(event)
            elif kind == "candidate_set_created":
                self.reference_observation(event)
                self.candidates(event)
                frame["candidate_sets"].append(event)
            elif kind == "model_request":
                self.reference_observation(event)
                candidates = self.get("candidates", payload.get("candidate_set_id"), event, same_decision=True)
                if candidates and candidates["payload"].get("observation_id") != payload.get("observation_id"):
                    self.issue(event, "candidate_observation_mismatch", "Request and candidate set reference different observations")
                if not isinstance(payload.get("questions"), dict) or not isinstance(payload.get("state"), dict):
                    self.issue(event, "missing_request", "Exact model state/questions were not captured", "gap")
                call_id = self.identity(event, "model_call_id")
                if call_id:
                    calls[call_id] = {"model_call_id": call_id, "request": event, "responses": [], "errors": []}
            elif kind in {"model_response", "provider_error"}:
                call_id = self.identity(event, "model_call_id")
                self.get("request", call_id, event, same_decision=True)
                if call_id in calls:
                    slot = calls[call_id]
                    if slot["responses"] or slot["errors"]:
                        self.issue(event, "duplicate_model_result", "Each model attempt has one terminal result")
                    slot["responses" if kind == "model_response" else "errors"].append(event)
                if kind == "model_response" and "answers" not in payload:
                    self.issue(event, "missing_answers", "Model response has no captured answers", "gap")
            elif kind == "decision":
                selections.append(event)
                frame["selection"] = event
                self.reference_observation(event)
                candidate_set = self.get("candidates", payload.get("candidate_set_id"), event, same_decision=True)
                if candidate_set and candidate_set["payload"].get("observation_id") != payload.get("observation_id"):
                    self.issue(event, "candidate_observation_mismatch", "Selection and candidate set reference different observations")
                selected = payload.get("plan_id") or payload.get("action")
                if selected is not None and not _identifier(selected):
                    self.issue(event, "invalid_selection", "Selected identity must be a string or null")
                if selected and candidate_set and selected not in self.candidates(candidate_set):
                    self.issue(event, "selection_not_offered", "Selected plan/action was not in the captured candidate set")
                if type(payload.get("model_called")) is not bool:
                    self.issue(event, "unknown_model_attribution", "Decision lacks an explicit model_called flag", "gap")
                if payload.get("model_called") is True:
                    call_id = payload.get("model_call_id")
                    self.get("request", call_id, event, same_decision=True)
                    call = calls.get(call_id) if isinstance(call_id, str) else None
                    if call is None or not (call["responses"] or call["errors"]):
                        self.issue(event, "missing_model_result", "Decision has no preceding captured model result", "gap")
                elif payload.get("model_call_id") is not None:
                    self.issue(event, "model_attribution_conflict", "A non-model decision references a model call")
            elif kind == "plan_committed":
                frame["plans"].append(event)
                selection = frame["selection"]
                plan = payload.get("plan")
                if selection and isinstance(plan, dict) and plan.get("id") != selection["payload"].get("plan_id"):
                    self.issue(event, "plan_mismatch", "Committed plan differs from the selected plan")
                elif not selection:
                    self.issue(event, "missing_selection", "Plan commitment has no preceding decision", "gap")
                if selection and isinstance(plan, dict):
                    candidates = self.get("candidates", selection["payload"].get("candidate_set_id"), event, same_decision=True)
                    offered = candidates["payload"].get("candidates") if candidates else None
                    if isinstance(offered, list):
                        for candidate in offered:
                            if (isinstance(candidate, dict) and candidate.get("id") == plan.get("id")
                                    and "steps" in candidate and candidate["steps"] != plan.get("steps")):
                                self.issue(event, "committed_plan_changed", "Committed steps differ from the offered plan definition")
            elif kind == "action_prepared":
                self.reference_observation(event)
                action_id = self.identity(event, "action_id")
                plan = None
                if payload.get("plan_id") is not None:
                    plan = self.get("plan", payload["plan_id"], event)
                if not _identifier(payload.get("action")) or not isinstance(payload.get("parameters"), dict):
                    self.issue(event, "missing_dispatch", "Action name/parameters were not fully captured", "gap")
                selection = frame["selection"]
                if selection and selection["payload"].get("plan_id") != payload.get("plan_id"):
                    self.issue(event, "dispatch_plan_mismatch", "Prepared action differs from the selected plan")
                if selection and selection["payload"].get("action") not in (None, payload.get("action")):
                    self.issue(event, "dispatch_action_mismatch", "Prepared action differs from the selected action")
                if not selection:
                    self.issue(event, "missing_selection", "Prepared action has no preceding decision", "gap")
                if plan:
                    self.check_plan_step(plan, event)
                if action_id:
                    actions[action_id] = {
                        "action_id": action_id, "prepared": event,
                        "dispatches": [], "results": [], "verifications": [],
                        "acknowledgment": "unknown", "verified": None,
                    }
            elif kind == "decision_finished":
                if frame["termination"] is not None:
                    self.issue(event, "duplicate_decision_finish", "Decision has multiple terminal records")
                if payload.get("status") not in ("abstained", "aborted", "verified_without_dispatch", "dispatched"):
                    self.issue(event, "invalid_decision_finish", "Unsupported decision termination status")
                if not _identifier(payload.get("reason")):
                    self.issue(event, "missing_finish_reason", "Decision termination has no recorded reason", "gap")
                frame["termination"] = event
            elif kind == "verification" and payload.get("scope") in ("plan", "observation"):
                observation = self.reference_observation(event)
                if self.identity(event, "decision_id") is None:
                    self.issue(event, "missing_decision", "Non-action verification requires decision attribution", "gap")
                if event["correlation"].get("action_id") is not None:
                    self.issue(event, "verification_scope_conflict", "Non-action verification cannot claim an action identity")
                if type(payload.get("verified")) is not bool:
                    self.issue(event, "invalid_verification", "Verification must contain a boolean verdict")
                if payload["scope"] == "plan":
                    plan = self.get("plan", payload.get("plan_id"), event, same_decision=True)
                    if observation and plan and observation["sequence"] <= plan["sequence"]:
                        self.issue(event, "stale_verification", "Plan verification predates its commitment")
                frame["verifications"].append(event)
            elif kind in {"action_dispatched", "action_returned", "dispatch_error", "verification"}:
                action_id = self.identity(event, "action_id")
                prepared = self.get("action", action_id, event, same_decision=True)
                action = actions.get(action_id) if isinstance(action_id, str) else None
                if action is None:
                    # Preserve orphan evidence in the frame, never manufacture a dispatch.
                    continue
                if kind == "action_dispatched":
                    if action["results"]:
                        self.issue(event, "late_dispatch", "Dispatch evidence follows a terminal action result")
                    if action["dispatches"]:
                        self.issue(event, "duplicate_dispatch", "A retry must have a distinct action identity")
                    action["dispatches"].append(event)
                elif kind in {"action_returned", "dispatch_error"}:
                    if action["results"]:
                        self.issue(event, "duplicate_action_result", "An action attempt has multiple return/error events")
                    action["results"].append(event)
                    action["acknowledgment"] = "returned" if kind == "action_returned" else "ambiguous"
                else:
                    observation = self.reference_observation(event)
                    claimed = payload.get("verified")
                    if type(claimed) is not bool:
                        self.issue(event, "invalid_verification", "Verification must contain a boolean verdict")
                    if observation and prepared and observation["sequence"] <= prepared["sequence"]:
                        self.issue(event, "stale_verification", "Verification uses an observation preceding action preparation")
                    if action["verified"] is True and claimed is False:
                        self.issue(event, "conflicting_verification", "A verified attempt later has a conflicting verdict")
                    action["verifications"].append(event)
                    if type(claimed) is bool:
                        action["verified"] = claimed
        if len(selections) > 1:
            self.issue(selections[-1], "duplicate_decision", "Decision identity was reused for multiple selections")
        if not selections:
            frame["missing_evidence"].append("decision")
            self.issue(frame["evidence"][0], "missing_decision", "No selection event was captured for this decision", "gap")
        if len(selections) == 1:
            selected = selections[0]
            for caused in frame["plans"] + [action["prepared"] for action in actions.values()]:
                if caused["sequence"] < selected["sequence"]:
                    self.issue(caused, "noncausal_selection", "Plan/dispatch precedes its captured selection")
            has_target = selected["payload"].get("plan_id") or selected["payload"].get("action")
            selected_call = selected["payload"].get("model_call_id")
            call = calls.get(selected_call) if isinstance(selected_call, str) else None
            if call and any(result["sequence"] >= selected["sequence"] for result in call["responses"] + call["errors"]):
                self.issue(selected, "noncausal_model_result", "Selection precedes its recorded model result")
            if has_target and not actions and not frame["verifications"] and not frame["termination"]:
                self.issue(selected, "missing_execution_evidence", "Selected work has no dispatch, verification, or explicit termination", "gap")
        termination = frame["termination"]
        if termination:
            status = termination["payload"].get("status")
            if status == "dispatched" and not actions:
                self.issue(termination, "missing_dispatch", "Claimed dispatch has no prepared action evidence", "gap")
            if status == "verified_without_dispatch" and not frame["verifications"]:
                self.issue(termination, "missing_verification", "Claimed non-dispatch verification has no evidence", "gap")
            if status == "abstained" and actions:
                self.issue(termination, "abstention_dispatched", "An abstained decision also prepared an action")
            if selections and termination["sequence"] < selections[0]["sequence"]:
                self.issue(termination, "noncausal_termination", "Termination precedes the captured selection")
        for call in calls.values():
            if not call["responses"] and not call["errors"]:
                self.issue(call["request"], "unfinished_model_call", "Model request has no captured terminal result", "gap")
        for action in actions.values():
            if not action["results"]:
                self.issue(action["prepared"], "unknown_acknowledgment", "Action acknowledgment was not captured", "gap")
            if not action["verifications"]:
                self.issue(action["prepared"], "unverified_action", "No verification was captured for this action", "gap")
        frame["observations"].sort(key=lambda event: event["sequence"])
        frame["model_calls"] = list(calls.values())
        frame["actions"] = list(actions.values())

    def check_plan_step(self, plan_event: dict, prepared: dict) -> None:
        payload = prepared["payload"]
        plan = plan_event["payload"].get("plan")
        steps = plan.get("steps") if isinstance(plan, dict) else None
        index = payload.get("step_index")
        if not isinstance(steps, list) or type(index) is not int:
            self.issue(prepared, "unknown_plan_step", "Dispatch has no explicit captured plan-step index", "gap")
            return
        if not 0 <= index < len(steps) or not isinstance(steps[index], dict):
            self.issue(prepared, "invalid_plan_step", "Dispatch references a nonexistent plan step")
            return
        step = steps[index]
        if step.get("parameters") is not None and not isinstance(step["parameters"], dict):
            self.issue(prepared, "invalid_plan_parameters", "Plan step parameters are not an object or null")
            return
        if step.get("action") != payload.get("action") or (step.get("parameters") or {}) != payload.get("parameters"):
            self.issue(prepared, "plan_step_mismatch", "Dispatched action/parameters differ from the captured plan step")


def _legacy(rows: list[tuple[int, dict]], report: ReplayReport) -> None:
    report.format = "legacy"
    report.integrity["status"] = "unverified_legacy"
    report.add("gap", "legacy_evidence", "Legacy records lack a hash chain and complete causal event boundaries")
    for line, record in rows:
        if "schema" in record or "event_type" in record or record.get("controller") not in (None, "hierarchical"):
            report.add("error", "mixed_format", "Unsupported or mixed log formats", line)
            break
        if record.get("controller") == "hierarchical" and (
            type(record.get("schema_version")) is not int or record["schema_version"] not in (1, 2)
        ):
            report.add("error", "unsupported_legacy_schema", "Unsupported hierarchical record version", line)
            break
        if not isinstance(record.get("state"), dict) or not _identifier(record.get("action")):
            report.add("error", "invalid_legacy_record", "Not a supported captured controller record", line)
            break
        decision = record.get("decision")
        if decision is not None and not isinstance(decision, dict):
            report.add("error", "invalid_legacy_decision", "Decision must be an object or null", line)
            break
        hierarchical = record.get("controller") == "hierarchical"
        captured = decision or {}
        context = captured.get("state")
        context = context if isinstance(context, dict) else {}
        candidates = context.get("candidate_plans") if hierarchical else None
        questions = captured.get("questions") if hierarchical else record.get("questions")
        answers = captured.get("answers") if hierarchical else record.get("answers")
        if not hierarchical and isinstance(questions, dict):
            action_question = questions.get("next_action")
            if isinstance(action_question, dict):
                candidates = action_question.get("criteria")
        selected = captured.get("plan_id") if hierarchical else record["action"]
        source = captured.get("source") if hierarchical else record.get("source")
        if selected is not None and not _identifier(selected):
            report.add("error", "invalid_selection", "Selected identity must be a string or null", line)
            break
        if selected is not None and isinstance(candidates, dict) and selected not in candidates:
            # Hybrid can select from a larger pre-budget candidate set than the model saw.
            severity = "gap" if source in ("fallback", "deterministic-fallback") else "error"
            report.add(severity, "selection_not_captured", "Selected candidate is absent from the logged offered set", line)
        verified = record.get("verified")
        if verified is not None and type(verified) is not bool:
            report.add("error", "invalid_verification", "Legacy verification is not a boolean", line)
        frame = {
            "record_locator": {"line": line}, "decision_id": None,
            "session_id": record.get("session_id"), "controller": "hierarchical" if hierarchical else "flat",
            "selection_recorded": decision is not None if hierarchical else True,
            "selection": decision if hierarchical else {"action": selected, "source": source},
            "observations": {"before": record["state"], "after": record.get("after_state")},
            "candidate_set": candidates, "questions": questions, "responses": answers,
            "selected_plan": candidates.get(selected) if hierarchical and isinstance(candidates, dict) and isinstance(selected, str) else None,
            "dispatch": {"action": record["action"], "outcome": record.get("outcome"),
                         "pending": record.get("pending"), "prepared_event": None},
            "verification": {"recorded_verdict": verified, "independently_recomputed": False},
            "missing_evidence": ["durable_preparation", "explicit_causal_ids", "run_provenance"],
            "record": record,
        }
        if candidates is None:
            frame["missing_evidence"].append("candidate_set")
        report.decisions.append(frame)
        report.events.append({"line": line, "record": record})


def replay_log(path: str | Path, *, expected_head: str | None = None,
               max_line_bytes: int = MAX_LINE_BYTES, max_input_bytes: int = MAX_INPUT_BYTES,
               max_events: int = MAX_EVENTS, format: str = "auto") -> ReplayReport:
    """Reconstruct captured evidence without executing or importing agent code.

A directory means exactly events.jsonl plus optional manifest.json. Standalone
JSONL does not auto-discover adjacent files. Hashes verify bytes/ordering, not
truth or authorship. Missing evidence remains unknown and appears as a gap.
"""
    if any(type(value) is not int or value < 1 for value in (max_line_bytes, max_input_bytes, max_events)):
        raise ReplayInputError("Input limits must be positive integers")
    if format not in {"auto", "research-v1", "proposed-v1", "legacy"}:
        raise ReplayInputError("Unsupported replay format")
    if expected_head is not None and (not isinstance(expected_head, str) or not _HASH.fullmatch(expected_head)):
        raise ReplayInputError("Expected head must be a sha256-prefixed lowercase digest")
    source = Path(path)
    report = ReplayReport()
    seal = None
    if source.is_dir():
        seal_path = source / "integrity.json"
        if seal_path.exists():
            with seal_path.open("rb") as stream:
                raw = stream.read(min(max_line_bytes, 1024 * 1024) + 1)
            if len(raw) > min(max_line_bytes, 1024 * 1024):
                raise ReplayInputError("Integrity seal exceeds the supported limit")
            seal = _decode(raw)
            if raw != json.dumps(seal, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n":
                report.add("error", "noncanonical_seal", "Producer seal bytes are not canonical")
        manifest_path = source / "manifest.json"
        if manifest_path.exists():
            try:
                with manifest_path.open("rb") as stream:
                    raw = stream.read(min(max_line_bytes, 1024 * 1024) + 1)
                if len(raw) > min(max_line_bytes, 1024 * 1024):
                    raise ReplayInputError("Manifest exceeds the supported limit")
                report.manifest = _decode(raw)
                if report.manifest.get("schema") == "jev-factorio.manifest.v1":
                    if raw != json.dumps(report.manifest, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n":
                        raise ReplayInputError("Research manifest bytes are not canonical")
            except OSError as error:
                raise ReplayInputError("Cannot read the run manifest") from error
        source = source / "events.jsonl"
    rows = _read(source, report, max_line_bytes, max_input_bytes, max_events)
    actual = rows and rows[0][1].get("schema") == EVENT_SCHEMA and "schema_version" in rows[0][1]
    if format == "research-v1" or (format == "auto" and actual):
        from .research_log import digest as research_digest, validate_manifest
        from .replay_source import verify_source
        from .replay_causal import audit_producer

        report.format = "research-v1"
        report.events = [{"line": line, **event} for line, event in rows]
        report.run_id = rows[0][1].get("run_id") if rows else None
        if report.manifest is not None:
            try:
                validate_manifest(report.manifest)
            except (ValueError, TypeError, RecursionError) as error:
                raise ReplayInputError("Invalid research manifest") from error
            if rows:
                first = rows[0][1]
                first_payload = first.get("payload")
                claimed_manifest = (first_payload.get("manifest_hash")
                                    if isinstance(first_payload, dict) else None)
                if (first.get("schema") == EVENT_SCHEMA
                        and type(first.get("schema_version")) is int
                        and first.get("schema_version") == 1
                        and first.get("event_type") == "run_started"
                        and isinstance(first.get("run_id"), str)
                        and claimed_manifest == research_digest(report.manifest)
                        and report.manifest.get("run_id") != rows[0][1].get("run_id")):
                    raise ReplayInputError("Manifest run identity does not match source")
        try:
            checked = verify_source(rows, report.manifest, seal, expected_head)
        except (ValueError, TypeError, RecursionError):
            report.add("error", "invalid_source_evidence", "Original producer envelope, chain, manifest, or seal is invalid")
            report.integrity["status"] = "invalid"
            return report
        report.integrity.update({key: value for key, value in checked.items() if key != "gaps"})
        report.integrity["status"] = "verified_source" if checked["complete"] else "incomplete_source"
        for gap in checked["gaps"]:
            report.add("gap", gap, "Original producer evidence is incomplete")
        if report.status == "invalid":
            report.integrity["status"] = "invalid"
            return report
        audit_producer([event for _, event in rows], report)
        return report
    if format == "legacy" and rows and ("schema" in rows[0][1] or "event_type" in rows[0][1]):
        report.add("error", "mixed_format", "Input does not match the selected legacy format")
        return report
    if rows and "schema" not in rows[0][1] and "event_type" not in rows[0][1]:
        if format == "proposed-v1":
            report.add("error", "mixed_format", "Input does not match the selected proposed format")
            return report
        _legacy(rows, report)
        if expected_head is not None:
            report.add("error", "unverifiable_head", "Legacy logs cannot satisfy a chain-head assertion")
        return report
    report.format = EVENT_SCHEMA
    events = _validated_events(rows, report, expected_head)
    report.events = events
    if report.manifest is None:
        report.add("gap", "missing_manifest", "No run manifest was supplied; provenance is incomplete")
    elif (type(report.manifest.get("schema_version")) is not int
          or report.manifest["schema_version"] != 1
          or report.manifest.get("run_id") != report.run_id):
        report.add("error", "manifest_mismatch", "Manifest version or run identity is incompatible")
    if report.manifest is not None and events:
        claimed_manifest = events[0]["payload"].get("manifest_sha256")
        if claimed_manifest is None:
            report.add("gap", "unbound_manifest", "No manifest digest is bound into run_started")
        else:
            try:
                actual_manifest = "sha256:" + hashlib.sha256(_canonical(report.manifest)).hexdigest()
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ReplayInputError("Unsupported manifest encoding") from error
            if claimed_manifest != actual_manifest:
                report.add("error", "manifest_hash_mismatch", "Manifest digest does not match run_started")
    audit = _Audit(report)
    audit.prepare(events)
    audit.reconstruct()
    return report


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Captured JSONL or run directory; never an endpoint")
    parser.add_argument("--output", type=Path, help="Create a new report file (never overwrite)")
    parser.add_argument("--max-line-bytes", type=int, default=MAX_LINE_BYTES)
    parser.add_argument("--max-input-bytes", type=int, default=MAX_INPUT_BYTES)
    parser.add_argument("--max-events", type=int, default=MAX_EVENTS)
    parser.add_argument("--expected-head", help="Externally retained sha256:<digest> chain head")
    parser.add_argument("--format", choices=("auto", "research-v1", "proposed-v1", "legacy"), default="auto")
    parser.add_argument("--allow-incomplete", action="store_true", help="Exit zero for gaps only; never masks invalid evidence")
    args = parser.parse_args(argv)
    try:
        report = replay_log(args.path, expected_head=args.expected_head,
                            max_line_bytes=args.max_line_bytes,
                            max_input_bytes=args.max_input_bytes, max_events=args.max_events, format=args.format)
        result = json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output:
            # Exclusive creation protects inputs, checkpoints, symlinks and previous reports.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(result)
        else:
            sys.stdout.write(result)
    except (ReplayInputError, OSError, UnicodeError, ValueError, RecursionError):
        sys.stderr.write("Replay input/output error; inputs were not modified.\n")
        return 2
    if report.status == "invalid":
        return 1
    if report.status == "incomplete" and not args.allow_incomplete:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
