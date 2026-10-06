"""Run-level replicated/paired summaries and reproducible columnar projections."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import tempfile
from typing import Any

from .research_evaluation import RunEvaluation
from .research_events import EvidenceError, MixedTreatmentError, canonical

METRICS = ("target_achieved", "native_victory_event_observed", "verified_actions",
           "model_calls", "provider_errors", "input_tokens", "output_tokens", "wall_elapsed_seconds")
TABLE_SCHEMAS: dict[str, dict[str, str]] = {
    "runs": {
        "run_id": "VARCHAR", "experiment_id": "VARCHAR", "trial_id": "VARCHAR",
        "condition": "VARCHAR", "replicate": "BIGINT", "pair_id": "VARCHAR",
        "treatment_id": "VARCHAR", "session_id": "VARCHAR", "world_kind": "VARCHAR",
        "world_seed": "VARCHAR", "target": "VARCHAR", "policy": "VARCHAR",
        "evidence_class": "VARCHAR", "complete": "BOOLEAN", "benchmark_eligible": "BOOLEAN",
        "mixed_treatments": "BOOLEAN", "terminal_status": "VARCHAR", "target_achieved": "BOOLEAN",
        "native_victory_event_observed": "BOOLEAN", "prepared_actions": "BIGINT",
        "returned_actions": "BIGINT", "verified_actions": "BIGINT",
        "unverified_actions": "BIGINT", "preflight_rejected_actions": "BIGINT",
        "model_calls": "BIGINT", "provider_errors": "BIGINT", "input_tokens": "BIGINT",
        "output_tokens": "BIGINT", "input_tokens_recorded": "BIGINT", "output_tokens_recorded": "BIGINT",
        "token_usage_complete": "BOOLEAN", "wall_elapsed_seconds": "DOUBLE",
        "event_head_hash": "VARCHAR", "warnings_json": "VARCHAR", "models_json": "VARCHAR",
    },
    "events": {
        "run_id": "VARCHAR", "sequence": "BIGINT", "segment_id": "VARCHAR", "event_type": "VARCHAR",
        "utc": "VARCHAR", "monotonic_ns": "BIGINT", "factorio_tick": "BIGINT",
        "decision_id": "VARCHAR", "model_call_id": "VARCHAR", "action_id": "VARCHAR",
        "duration_ms": "DOUBLE", "event_hash": "VARCHAR",
    },
    "decisions": {
        "run_id": "VARCHAR", "decision_id": "VARCHAR", "sequence": "BIGINT", "segment_id": "VARCHAR",
        "model_call_id": "VARCHAR", "source": "VARCHAR", "action": "VARCHAR",
    },
    "model_calls": {
        "run_id": "VARCHAR", "model_call_id": "VARCHAR", "segment_id": "VARCHAR", "decision_id": "VARCHAR",
        "request_sequence": "BIGINT", "response_sequence": "BIGINT", "requested_model": "VARCHAR",
        "resolved_model": "VARCHAR", "status": "VARCHAR", "duration_ms": "DOUBLE",
        "input_tokens": "BIGINT", "output_tokens": "BIGINT",
    },
    "actions": {
        "run_id": "VARCHAR", "action_id": "VARCHAR", "decision_id": "VARCHAR", "segment_id": "VARCHAR",
        "action": "VARCHAR", "prepared_sequence": "BIGINT", "returned_sequence": "BIGINT",
        "acknowledged": "BOOLEAN", "verified": "BOOLEAN",
        "preflight_rejected": "BOOLEAN", "preflight_rejection_code": "VARCHAR",
        "verification_count": "BIGINT",
        "verified_sequence": "BIGINT", "duration_ms": "DOUBLE",
    },
    "milestones": {
        "run_id": "VARCHAR", "goal": "VARCHAR", "sequence": "BIGINT", "segment_id": "VARCHAR",
        "observation_id": "VARCHAR", "factorio_tick": "BIGINT",
    },
    "interventions": {
        "run_id": "VARCHAR", "sequence": "BIGINT", "segment_id": "VARCHAR",
        "kind": "VARCHAR", "incident_id": "VARCHAR",
    },
    "pairs": {
        "experiment_id": "VARCHAR", "pair_id": "VARCHAR", "baseline_run_id": "VARCHAR",
        "treatment_run_id": "VARCHAR", "metric": "VARCHAR",
        "baseline": "DOUBLE", "treatment": "DOUBLE", "delta": "DOUBLE",
    },
}


def _stats(values: list[Any]) -> dict:
    known = [float(value) for value in values if value is not None]
    return {
        "n_total": len(values), "n_observed": len(known), "n_missing": len(values) - len(known),
        "mean": statistics.mean(known) if known else None,
        "sample_sd": statistics.stdev(known) if len(known) > 1 else None,
        "min": min(known) if known else None, "max": max(known) if known else None,
    }


def _metadata_issues(summary: dict) -> list[str]:
    issues = []
    for key in ("experiment_id", "condition", "trial_id"):
        if not isinstance(summary.get(key), str) or not summary[key].strip():
            issues.append(f"missing:{key}")
    if type(summary.get("replicate")) is not int or summary["replicate"] < 0:
        issues.append("missing:replicate")
    if type(summary.get("world_seed")) not in {int, str} or summary["world_seed"] == "":
        issues.append("missing:world_seed")
    return issues


def _pair_key(summary: dict) -> str:
    return (summary["pair_id"] if summary.get("pair_id") else
            canonical([summary["world_seed"], summary["replicate"]]).decode())


def experiment_summary(evaluations: list[RunEvaluation], *, pair: tuple[str, str] | None = None,
                       allow_mixed_treatments: bool = False) -> dict:
    """Describe independent runs, never decisions as independent replicates.

    No inferential significance claim is made. Incomplete, intervened, or otherwise
    ineligible runs stay visible in attrition/denominators, not silently discarded.
    """
    if not evaluations:
        raise EvidenceError("No runs to evaluate")
    summaries = sorted((item.summary for item in evaluations), key=lambda s: s["run_id"])
    if len({s["run_id"] for s in summaries}) != len(summaries):
        raise EvidenceError("Duplicate run_id/input; a run is one experimental unit")
    known_sessions = [s["session_id"] for s in summaries if s["session_id"] is not None]
    if len(set(known_sessions)) != len(known_sessions):
        raise EvidenceError("Repeated world session; independent resets cannot be inferred from new run IDs")
    cells: set[bytes] = set()
    trials: set[tuple] = set()
    groups: dict[tuple, list[dict]] = defaultdict(list)
    conditions: dict[tuple, list[dict]] = defaultdict(list)
    exclusions = []
    indexed: dict[tuple, dict] = {}
    for summary in summaries:
        missing = _metadata_issues(summary)
        if missing:
            exclusions.append({"run_id": summary["run_id"], "reasons": missing})
            continue
        exp, condition = summary["experiment_id"], summary["condition"]
        cell = canonical([exp, condition, summary["world_seed"], summary["replicate"]])
        trial = (exp, condition, summary["trial_id"])
        if cell in cells or trial in trials:
            raise EvidenceError("Duplicate condition/seed/replicate or condition/trial cell")
        cells.add(cell)
        trials.add(trial)
        key = (exp, condition, _pair_key(summary))
        if key in indexed:
            raise EvidenceError("Ambiguous duplicate pairing key")
        indexed[key] = summary
        conditions[(exp, condition)].append(summary)
        groups[(exp, condition, summary["treatment_id"])].append(summary)
        if not summary["benchmark_eligible"]:
            exclusions.append({"run_id": summary["run_id"],
                               "reasons": summary["treatment_issues"] + summary["warnings"]})
    drift = []
    for key, members in sorted(conditions.items()):
        if (len({s["treatment_id"] for s in members}) > 1
                or len({model for s in members for model in s["models"]}) > 1):
            drift.append({"experiment_id": key[0], "condition": key[1]})
    if drift and not allow_mixed_treatments:
        raise MixedTreatmentError("A condition label maps to multiple code/config/model treatments")
    drift_keys = {(d["experiment_id"], d["condition"]) for d in drift}
    replicated = []
    for (exp, condition, treatment), members in sorted(groups.items()):
        eligible = [s for s in members if s["benchmark_eligible"] and (exp, condition) not in drift_keys]
        replicated.append({
            "experiment_id": exp, "condition": condition, "treatment_id": treatment,
            "runs_total": len(members), "runs_eligible": len(eligible),
            "run_ids": [s["run_id"] for s in members],
            "metrics": {metric: _stats([s[metric] for s in eligible]) for metric in METRICS},
        })
    comparison = None
    if pair:
        baseline, treatment = pair
        if baseline == treatment:
            raise EvidenceError("Paired comparison requires two distinct condition labels")
        matched, unmatched = [], []
        pair_keys = sorted({(exp, key) for exp, condition, key in indexed if condition in pair})
        if not pair_keys:
            raise EvidenceError("No runs with the requested comparison labels and complete pairing metadata")
        for exp, key in pair_keys:
            left, right = indexed.get((exp, baseline, key)), indexed.get((exp, treatment, key))
            reasons = []
            if left is None or right is None:
                reasons.append("missing_counterpart")
            else:
                if not left["benchmark_eligible"] or not right["benchmark_eligible"]:
                    reasons.append("ineligible_run")
                if (exp, baseline) in drift_keys or (exp, treatment) in drift_keys:
                    reasons.append("condition_treatment_drift")
                for field in ("world_seed", "replicate", "world_settings_sha256", "initial_save_sha256",
                              "target", "world_kind"):
                    if left.get(field) is None or right.get(field) is None:
                        reasons.append(f"missing_pair_provenance:{field}")
                    elif canonical(left[field]) != canonical(right[field]):
                        reasons.append(f"mismatched_pair_provenance:{field}")
                for field in ("runtime", "factorio_version", "fle_version", "backend"):
                    a, b = left["treatment"].get(field), right["treatment"].get(field)
                    if field == "runtime" and (not a or not b):
                        reasons.append("missing_pair_provenance:runtime")
                    elif canonical(a) != canonical(b):
                        reasons.append(f"mismatched_pair_provenance:{field}")
            if reasons:
                unmatched.append({"experiment_id": exp, "pair_id": key,
                                  "baseline_run_id": left["run_id"] if left else None,
                                  "treatment_run_id": right["run_id"] if right else None,
                                  "reasons": sorted(set(reasons))})
                continue
            for metric in METRICS:
                a, b = left[metric], right[metric]
                matched.append({"experiment_id": exp, "pair_id": key,
                                "baseline_run_id": left["run_id"], "treatment_run_id": right["run_id"],
                                "metric": metric, "baseline": float(a) if a is not None else None,
                                "treatment": float(b) if b is not None else None,
                                "delta": float(b) - float(a) if a is not None and b is not None else None})
        comparison = {
            "baseline": baseline, "treatment": treatment, "delta_definition": "treatment - baseline",
            "matched_pairs": len(matched) // len(METRICS), "unmatched": unmatched, "rows": matched,
            "metrics": {metric: _stats([row["delta"] for row in matched if row["metric"] == metric])
                        for metric in METRICS},
        }
    return {
        "schema": "jev-factorio.evaluation.v1", "experimental_unit": "run",
        "statistics": "Descriptive only; decisions are not replicates; no significance or confidence interval claimed.",
        "runs_total": len(summaries), "runs": summaries, "replicated": replicated,
        "exclusions": exclusions, "condition_drift": drift, "paired": comparison,
    }


def _run_row(summary: dict) -> dict:
    row = {name: summary.get(name) for name in TABLE_SCHEMAS["runs"]}
    row["world_seed"] = None if summary["world_seed"] is None else canonical(summary["world_seed"]).decode()
    for field in ("warnings", "models"):
        row[field + "_json"] = canonical(summary[field]).decode()
    return row


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                               allow_nan=False).encode("utf-8") + b"\n")


def write_report(evaluations: list[RunEvaluation], output_dir: Path, *, parquet: bool = False,
                 pair: tuple[str, str] | None = None, allow_mixed_treatments: bool = False) -> dict:
    """Publish a complete new report directory; never overwrite inputs or a report."""
    report = experiment_summary(evaluations, pair=pair, allow_mixed_treatments=allow_mixed_treatments)
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise EvidenceError("Output directory already exists; choose a new report directory")
    pa = pq = None
    if parquet:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise EvidenceError("Parquet export requires: pip install 'jev-factorio[evaluation]'") from exc
    tables = {name: [] for name in TABLE_SCHEMAS}
    for item in sorted(evaluations, key=lambda item: item.summary["run_id"]):
        tables["runs"].append(_run_row(item.summary))
        for name, rows in item.tables.items():
            tables[name].extend(rows)
    tables["pairs"] = report["paired"]["rows"] if report["paired"] else []
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".research-report-", dir=output_dir.parent))
    try:
        (temporary / "tables").mkdir()
        _write_json(temporary / "summary.json", report)
        _write_json(temporary / "integrity.json", {"runs": [e.integrity for e in sorted(evaluations, key=lambda item: item.summary["run_id"])]})
        _write_json(temporary / "table_schema.json", {"schema": "jev-factorio.tables.v2", "tables": TABLE_SCHEMAS})
        statements = ["-- Run from the report directory. All projections are derived; JSONL inputs remain evidence."]
        for name, columns in TABLE_SCHEMAS.items():
            rows = [{key: row.get(key) for key in columns} for row in tables[name]]
            with (temporary / "tables" / f"{name}.jsonl").open("wb") as stream:
                for row in rows:
                    stream.write(canonical(row) + b"\n")
            if rows:
                sql_types = ", ".join(f"{_sql_literal(key)}: {_sql_literal(value)}" for key, value in columns.items())
                statements.append(f'CREATE OR REPLACE VIEW "{name}" AS SELECT * FROM read_json('
                                  f"'tables/{name}.jsonl', format='newline_delimited', "
                                  f"auto_detect=false, columns={{{sql_types}}});")
            else:
                empty = ", ".join(f'CAST(NULL AS {kind}) AS "{key}"' for key, kind in columns.items())
                statements.append(f'CREATE OR REPLACE VIEW "{name}" AS SELECT {empty} WHERE false;')
            if parquet:
                types = {"VARCHAR": pa.string(), "BIGINT": pa.int64(), "BOOLEAN": pa.bool_(), "DOUBLE": pa.float64()}
                schema = pa.schema([(key, types[kind]) for key, kind in columns.items()])
                pq.write_table(pa.Table.from_pylist(rows, schema=schema),
                               temporary / "tables" / f"{name}.parquet", compression="zstd")
        (temporary / "views.sql").write_text("\n".join(statements) + "\n", encoding="utf-8")
        artifacts = {p.relative_to(temporary).as_posix(): "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(temporary.rglob("*")) if p.is_file()}
        _write_json(temporary / "artifacts.json", {"schema": "jev-factorio.artifacts.v1", "sha256": artifacts})
        if output_dir.exists():
            raise EvidenceError("Output directory appeared during evaluation")
        os.rename(temporary, output_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return report
