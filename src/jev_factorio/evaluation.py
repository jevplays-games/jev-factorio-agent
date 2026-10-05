"""Summarize evidence logs without relabeling mock runs as live benchmarks."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .research_events import EvidenceError, load_json
from .research_evaluation import evaluate_run
from .research_reports import experiment_summary, write_report
from .telemetry import WAIT_ACTIONS, validate_attempt


def read_records(path: Path) -> list[dict]:
    from .wait_record_codec import iter_stream
    with path.open("rb") as stream:
        records = list(iter_stream(stream, "gameplay", max_records=None, skip_blank=True))
    if not records:
        raise ValueError("Empty evaluation log")
    if any(not isinstance(r, dict) or r.get("controller") != "hierarchical" for r in records):
        raise ValueError("Do not mix sessions, policies, targets, or log schemas")
    identity = {(r.get("session_id"), r.get("world_kind"), r.get("target"),
                 r.get("policy"), r.get("requested_model")) for r in records}
    if len(identity) != 1:
        raise ValueError("Do not mix sessions, policies, targets, or log schemas")
    return records


def _attempt_counts(records: list[dict]) -> dict:
    identities, finished = {}, {}
    legacy = False
    for record in records:
        version = record.get("schema_version", 1)
        if type(version) is not int or version not in {1, 2}:
            raise ValueError("Unsupported evaluation schema")
        if version == 1:
            legacy = True
            if record.get("background_attempt") is not None:
                raise ValueError("Legacy evaluation schema has unsupported background attempt")
            continue
        if "attempt" not in record or not isinstance(record.get("attempt_outcomes"), list):
            raise ValueError("Missing version 2 attempt evidence")
        if len(record["attempt_outcomes"]) > 64:
            raise ValueError("Unbounded attempt outcome history")
        evidence = [(a, True) for a in record["attempt_outcomes"]]
        if record["attempt"] is not None:
            evidence.append((record["attempt"], False))
        background_attempt = record.get("background_attempt")
        if background_attempt is not None:
            background_schema = record.get("background_schema")
            if type(background_schema) is not int or background_schema not in {2, 3}:
                raise ValueError("Unsupported background attempt schema")
            evidence.append((background_attempt, False))
        for attempt, complete in evidence:
            validate_attempt(attempt, finished=complete)
            key = attempt["id"]
            identity = {k: attempt[k] for k in (
                "origin", "action", "plan_id", "step_index", "step_sha256", "started_tick",
                "started_at_utc", "process_id", "expected_unit_number", "receipt",
            )}
            if key in identities and identities[key] != identity:
                raise ValueError("Conflicting attempt identity")
            identities[key] = identity
            if complete:
                if key in finished and finished[key] != attempt:
                    raise ValueError("Conflicting attempt outcome")
                finished[key] = attempt
    verified = [a for a in finished.values() if a["outcome"] == "verified"]
    actions = sum(a["action"] not in WAIT_ACTIONS for a in verified)
    return {
        "verified_actions": None if legacy else actions,
        "identified_verified_actions": actions,
        "verified_waits": sum(a["action"] in WAIT_ACTIONS for a in verified),
        "identified_attempts": len(identities),
        "legacy_records_present": legacy,
        "attempt_count_scope": "Unique IDs present in log, including carried checkpoint outcomes; not a full-campaign total",
        "legacy_verified_action_records": sum(
            r.get("schema_version", 1) == 1 and r.get("verified") is True
            and r.get("action") not in {"observe", "verify", *WAIT_ACTIONS} for r in records
        ),
        "verification_latency_seconds": [a["latency_seconds"] for a in verified
                                          if a["latency_seconds"] is not None],
        "unknown_verification_latencies": sum(a["latency_seconds"] is None for a in verified),
    }


def _production_delta(records: list[dict]) -> dict | None:
    """Return an endpoint delta only when every captured counter snapshot is usable.

    Missing maps make the measurement unknown. Missing item keys in an available
    sparse map mean zero, and malformed counter values are rejected.
    """
    def counters(record: dict, field: str) -> dict | None:
        state = record.get(field)
        factory = state.get("factory") if isinstance(state, dict) else None
        if not isinstance(factory, dict) or "produced" not in factory or factory["produced"] is None:
            return None
        produced = factory["produced"]
        if not isinstance(produced, dict):
            raise ValueError("Invalid production counter map")
        for value in produced.values():
            if type(value) not in {int, float}:
                raise ValueError("Invalid production counter")
            try:
                valid = math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("Invalid production counter")
        return produced

    snapshots, missing_snapshot = [], False
    for record in records:
        before = counters(record, "state")
        after = counters(record, "after_state")
        snapshots.extend((before, after))
        missing_snapshot |= before is None or after is None
    if missing_snapshot:
        return None
    for before, after in zip(snapshots, snapshots[1:]):
        keys = before.keys() | after.keys()
        if any(after.get(key, 0) < before.get(key, 0) for key in keys):
            return None
    before, after = snapshots[0], snapshots[-1]
    delta = {key: after.get(key, 0) - before.get(key, 0) for key in before.keys() | after.keys()}
    return delta


def _summarize_legacy(path: Path) -> dict:
    records = read_records(path)
    last = records[-1]
    calls = [r for r in records if r.get("model_call")]
    observed_victory = any(
        r["after_state"].get("victory") is True
        and r["after_state"].get("victory_source") == "native:base-game-rocket-launch"
        for r in records
    )
    return {
        "session_id": last["session_id"], "world_kind": last["world_kind"],
        "target": last["target"], "policy": last["policy"],
        "evidence_class": "synthetic" if last["world_kind"] == "mock" else "tool-assisted",
        "terminal_status": last["status"], "terminal_reason": last.get("reason"),
        "records": len(records), "model_calls": len(calls),
        **_attempt_counts(records),
        "observed_production_delta": _production_delta(records),
        "milestones": last.get("completed_goals", {}),
        "native_victory_event_observed": observed_victory and last["world_kind"] != "mock",
        "input_tokens": sum((r.get("usage") or {}).get("input_tokens", 0) for r in calls),
        "token_usage_complete": all(
            type((r.get("usage") or {}).get("input_tokens")) is int for r in calls
        ),
        "models": sorted({r["resolved_model"] for r in calls if r.get("resolved_model")}),
    }


def _is_research(path: Path) -> bool:
    if path.is_dir():
        return True
    with path.open("rb") as stream:
        for line in stream:
            if line.strip():
                first = load_json(line)
                return any(key in first for key in ("schema", "event_type", "event_hash"))
    raise EvidenceError("Empty evaluation log")


def summarize(path: Path, *, manifest_path: Path | None = None,
              allow_mixed_treatments: bool = False) -> dict:
    """Keep the legacy API; event runs additionally require a bound manifest."""
    path = Path(path)
    if _is_research(path):
        return evaluate_run(path, manifest_path=manifest_path,
                            allow_mixed_treatments=allow_mixed_treatments).summary
    if manifest_path or allow_mixed_treatments:
        raise EvidenceError("Research options cannot authenticate a legacy log")
    return _summarize_legacy(path)


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="Legacy JSONL, event JSONL, or run directories")
    parser.add_argument("--manifest", type=Path, help="Manifest for a single event input")
    parser.add_argument("--output-dir", type=Path, help="New report directory; existing paths are never overwritten")
    parser.add_argument("--parquet", action="store_true", help="Also export typed Parquet (requires evaluation extra)")
    parser.add_argument("--pair", nargs=2, metavar=("BASELINE", "TREATMENT"), help="Compare matched condition labels")
    parser.add_argument("--allow-mixed-treatments", action="store_true",
                        help="Inspect mixed treatments; never pool them or bypass integrity validation")
    args = parser.parse_args(argv)
    try:
        kinds = [_is_research(path) for path in args.logs]
        if any(kinds) and not all(kinds):
            raise EvidenceError("Do not mix legacy and research event inputs")
        if args.manifest and len(args.logs) != 1:
            raise EvidenceError("--manifest requires exactly one event input")
        if args.parquet and not args.output_dir:
            raise EvidenceError("--parquet requires --output-dir")
        if not any(kinds):
            if args.manifest or args.output_dir or args.parquet or args.pair or args.allow_mixed_treatments:
                raise EvidenceError("Research reports require event inputs; legacy logs remain unauthenticated")
            result = [_summarize_legacy(path) for path in args.logs]
        else:
            evaluations = [evaluate_run(path, manifest_path=args.manifest,
                                       allow_mixed_treatments=args.allow_mixed_treatments) for path in args.logs]
            options = {"pair": tuple(args.pair) if args.pair else None,
                       "allow_mixed_treatments": args.allow_mixed_treatments}
            result = (write_report(evaluations, args.output_dir, parquet=args.parquet, **options)
                      if args.output_dir else experiment_summary(evaluations, **options))
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    cli()
