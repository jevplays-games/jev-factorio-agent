"""CLI: python -m jev_factorio --backend mock --steps 8"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from contextlib import ExitStack
from pathlib import Path

from dotenv import load_dotenv

from .backends.mock import MockBackend
from .judgments import (DEFAULT_MAX_REQUEST_BYTES, MAX_MAX_REQUEST_BYTES,
                        MIN_MAX_REQUEST_BYTES)
from .blocked_persistence import DEFAULT_IDLE_OBSERVATIONS, MAX_IDLE_OBSERVATIONS
from .loop import AgentLoop
from .research_log import ResearchLog, RunConfiguration, validate_output_paths


class _ReconcileOnlyDecisionClient:
    """Fail closed if reconciliation ever reaches a model decision path."""

    is_mock = False

    def evaluate(self, *args, **kwargs):
        raise RuntimeError("Model calls are disabled in reconcile-only mode")


async def _run_async_controller_lifecycle(controller, close_state: dict, **run_options):
    """Run and close an async controller on its one owning event loop."""
    try:
        return await controller.run_async(**run_options)
    finally:
        # The surrounding ExitStack owns a fallback close for failures before
        # this coroutine starts. Once closure is attempted here, never retry it
        # on a different event loop during stack unwinding.
        close_state["attempted"] = True
        await controller.aclose_async_provider()


def make_backend(name: str, resume: bool = False, adopt_session: bool = False,
                 setup_timing=None, connector_witness_path=None, connector_binding=None,
                 completed_craft=None, background_craft=None):
    if name == "mock":
        return MockBackend()
    if name == "play_api":
        from .backends.play_api import PlayApiBackend
        return PlayApiBackend(factorio_user_dir=os.environ.get(
            "FACTORIO_USER_DIR", "~/.factorio"))
    if name == "fle":
        from .backends.fle import FleBackend
        b = FleBackend()
        attachment = ({'connector_binding': connector_binding}
                      if connector_binding is not None else {})
        if completed_craft is not None:
            attachment['completed_craft'] = completed_craft
        if background_craft is not None:
            attachment['background_craft'] = background_craft
        if setup_timing is None:
            b.start(resume=resume, adopt_session=adopt_session,
                    connector_witness_path=connector_witness_path, **attachment)
        else:
            b.start(resume=resume, adopt_session=adopt_session,
                    setup_timing=setup_timing,
                    connector_witness_path=connector_witness_path, **attachment)
        return b
    raise SystemExit(f"unknown backend: {name}")


def _preflight_selected_checkpoint(path: Path, memory_type, target: str) -> bytes:
    """Validate one stable capture through the controller CLI actually selected."""
    path = Path(path)
    captured = path.read_bytes()
    try:
        data = json.loads(captured.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid resumed controller checkpoint") from error
    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not data["session_id"]:
        raise ValueError("Invalid resumed controller checkpoint identity")

    from .memory import load_checkpoint_bytes

    memory = load_checkpoint_bytes(
        captured, data["session_id"], target,
        checkpoint_path=path, memory_type=memory_type)
    archive_index = getattr(memory, "_blocked_recovery_archive_index", None)
    try:
        if path.read_bytes() != captured:
            raise ValueError("Checkpoint changed during composed resume preflight")
    finally:
        if archive_index is not None:
            archive_index.close()
    return captured


def _checkpoint_capture_matches(path: Path, captured: bytes) -> bool:
    """Recheck the preflighted bytes immediately before backend attachment."""
    return Path(path).read_bytes() == captured


def _preflight_sync_async_rollback(path: Path, target: str) -> bytes:
    """Reject sync resume while this checkpoint still owns async decision work.

    Sidecar existence alone is not an ownership signal: empty WAL/archive files
    and fully settled archive history are valid. The decision is based on the
    exact composed checkpoint, validated WAL records, archive settlements, and
    the matching provider-health reservation/outcome.
    """
    from .memory import checkpoint_memory_type, load_checkpoint_bytes
    from .operational_safety import read_json, safety_dir

    path = Path(path)
    captured = path.read_bytes()
    try:
        document = json.loads(captured.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot roll back an invalid async controller checkpoint") from error
    if (type(document) is not dict or type(document.get("session_id")) is not str
            or not document["session_id"]):
        raise ValueError("Cannot roll back a checkpoint without its session identity")
    session_id = document["session_id"]
    history = document.get("history", [])
    if type(history) is not list or any(type(row) is not dict for row in history):
        raise ValueError("Invalid checkpoint history during async rollback preflight")
    owns_async = document.get("async_decision") is not None or any(
        row.get("kind") in {"async_decision_settled", "async_plan_lineage"}
        for row in history)
    safety = safety_dir(path)
    if safety.is_symlink() or (safety.exists() and not safety.is_dir()):
        raise ValueError("Async rollback safety path is not a real directory")
    for name in ("provider-decisions.json", "decision-archives"):
        candidate = safety / name
        owns_async = owns_async or candidate.exists() or candidate.is_symlink()
    health_path = safety / "provider.json"
    if health_path.exists() or health_path.is_symlink():
        if health_path.is_symlink():
            raise ValueError("Provider health state is not a real file")
        health = read_json(health_path)
        if type(health) is not dict:
            raise ValueError("Provider health state disappeared during rollback preflight")
        flight = health.get("in_flight")
        owns_async = owns_async or "decision_outcome" in health or (
            isinstance(flight, dict) and "decision_binding" in flight)
    if not owns_async:
        # Ordinary checkpoints retain their established composed validation and
        # authorized migration order. This extra guard only owns async evidence;
        # loading every legacy checkpoint here would preempt those migrations.
        if path.read_bytes() != captured:
            raise ValueError("Checkpoint changed during async rollback preflight")
        return captured

    memory = load_checkpoint_bytes(
        captured, session_id, target, checkpoint_path=path,
        memory_type=checkpoint_memory_type(document))
    try:
        return _validate_sync_async_rollback(path, captured, memory)
    finally:
        archive_index = getattr(memory, "_blocked_recovery_archive_index", None)
        if archive_index is not None:
            archive_index.close()


def _validate_sync_async_rollback(path: Path, captured: bytes, memory) -> bytes:
    """Validate retained async evidence while the caller owns loader resources."""
    from .async_decision_archive import AsyncDecisionArchive
    from .controller import HierarchicalLoop
    from .operational_safety import read_json, safety_dir
    from .provider_decision_wal import ProviderDecisionWAL, _read_document, _request_sha256
    from .provider_health import ProviderCircuit

    session_id = memory.session_id
    if memory.async_decision is not None:
        raise ValueError("Async provider decision is still selected in the checkpoint")

    safety = safety_dir(path)
    if safety.is_symlink():
        raise ValueError("Async rollback safety directory is not a real directory")
    if not safety.exists():
        if any(row.get("kind") in {
                "async_decision_settled", "async_plan_lineage"}
                for row in memory.history):
            raise ValueError("Async checkpoint lineage has no retained safety archive")
        if path.read_bytes() != captured:
            raise ValueError("Checkpoint changed during async rollback preflight")
        return captured
    if not safety.is_dir():
        raise ValueError("Async rollback safety path is not a directory")

    wal_path = safety / "provider-decisions.json"
    wal_document = None
    wal_by_request = {}
    wal_id = hashlib.sha256(str(wal_path.resolve()).encode("utf-8")).hexdigest()
    if wal_path.exists() or wal_path.is_symlink():
        wal_document = _read_document(wal_path)
        for raw_record in wal_document["records"]:
            view = ProviderDecisionWAL._view(raw_record)
            identity = raw_record["identity"]
            if identity["session_id"] != session_id:
                continue
            request_id = identity["request_id"]
            wal_by_request[request_id] = (raw_record, view)
            if view.state in {
                    "reserved", "may_have_been_sent", "response_received", "ambiguous"}:
                raise ValueError("Async provider WAL still owns an unresolved request")

    archive_directory = safety / "decision-archives"
    archives_by_request = {}
    archive_by_id = {}
    archive = None
    if archive_directory.is_symlink():
        raise ValueError("Async decision archive directory is not a real directory")
    if archive_directory.exists():
        if not archive_directory.is_dir():
            raise ValueError("Async decision archive path is not a directory")
        archive = AsyncDecisionArchive(archive_directory)
        for record in archive.records():
            identity = record["identity"]
            if identity["session_id"] != session_id:
                continue
            request_id = record["request_id"]
            if request_id in archives_by_request:
                raise ValueError("Async archive request identity is duplicated")
            archives_by_request[request_id] = record
            archive_by_id[record["archive_id"]] = record

    # These are the same strict digest/shape validators used by the async
    # controller. They are read-only and bind each settled selection to its
    # own retained attempts and archive, rather than accepting a sidecar marker
    # or historical WAL row by itself.
    validator = object.__new__(HierarchicalLoop)
    validator.memory = memory
    settlements = validator._async_settlement_entries()
    lineage = validator._async_plan_lineage_entries()
    settlement_by_id = {entry["archive_id"]: entry for entry in settlements}
    lineage_by_id = {entry["archive_id"]: entry for entry in lineage}
    if len(settlement_by_id) != len(settlements) or len(lineage_by_id) != len(lineage):
        raise ValueError("Async checkpoint contains duplicate settlement identities")
    if any(entry["disposition"] == "selected" and archive_id not in lineage_by_id
           for archive_id, entry in settlement_by_id.items()):
        raise ValueError("Selected async settlement lacks its checkpoint plan lineage")
    if any(archive_id not in settlement_by_id
           or settlement_by_id[archive_id]["disposition"] != "selected"
           for archive_id in lineage_by_id):
        raise ValueError("Async plan lineage lacks its selected checkpoint settlement")

    if archive is not None:
        for request_id, record in archives_by_request.items():
            wal_pair = wal_by_request.get(request_id)
            wal_view = wal_pair[1] if wal_pair is not None else None
            provider = record["provider"]
            if provider["wal_id"] != wal_id:
                raise ValueError("Async archive is bound to another provider WAL")
            if wal_pair is not None:
                raw_wal = wal_pair[0]
                if (raw_wal["identity"] != record["identity"]
                        or raw_wal["request_sha256"] != _request_sha256(provider["wal_request"])
                        ):
                    raise ValueError("Async archive and provider WAL identities differ")

            entry = settlement_by_id.get(record["archive_id"])
            marker = archive.load_settlement(record)
            if entry is not None:
                if (entry["archive_sha256"] != record["record_sha256"]
                        or entry["request_id"] != request_id
                        or entry["wal_state"] != (wal_view.state if wal_view else None)
                        or entry["wal_phase"] != (wal_view.phase if wal_view else None)
                        or entry["wal_record_sha256"]
                        != HierarchicalLoop._async_wal_record_sha256(wal_view)):
                    raise ValueError("Async checkpoint settlement no longer matches its archive/WAL")
                if marker is not None and marker != entry:
                    raise ValueError("Async archive settlement marker differs from checkpoint history")
                if entry["disposition"] == "selected":
                    validator._async_validate_settled_plan(
                        record, entry, lineage_by_id[record["archive_id"]])
                continue

            if marker is not None:
                raise ValueError("Async archive marker has no retained checkpoint disposition")
            if wal_view is None:
                # Archive creation precedes checkpoint pointer and WAL reserve;
                # this precise orphan proves no provider request was submitted.
                continue
            if (wal_view.state == "failed" and wal_view.phase == "not_sent"
                    and wal_view.error_category == "local_admission"):
                continue
            raise ValueError("Async archive has no checkpoint disposition for its WAL outcome")

    if set(settlement_by_id) - set(archive_by_id):
        raise ValueError("Async checkpoint settlement refers to a missing archive")
    if wal_document is not None:
        for request_id, (_, view) in wal_by_request.items():
            if request_id not in archives_by_request:
                raise ValueError("Async provider WAL row has no matching request archive")
            if view.state == "failed" and view.phase == "not_sent" \
                    and view.error_category == "local_admission":
                continue
            if archives_by_request[request_id]["archive_id"] not in settlement_by_id:
                raise ValueError("Async provider WAL row has no retained checkpoint disposition")

    health_path = safety / "provider.json"
    if health_path.exists() or health_path.is_symlink():
        if health_path.is_symlink():
            raise ValueError("Provider health state is not a real file")
        health = read_json(health_path)
        if health is None:
            raise ValueError("Provider health state disappeared during rollback preflight")
        health_identity = health.get("identity")
        if (type(health_identity) is not str or len(health_identity) != 64
                or any(character not in "0123456789abcdef" for character in health_identity)):
            raise ValueError("Provider health identity is malformed")
        # Match ProviderCircuit's legacy defaults before applying its canonical
        # validator without constructing a provider client or mutating the file.
        health.setdefault("in_flight", None)
        health.setdefault("budget_category", health.get("category"))
        health.setdefault("budget_limit", (
            ProviderCircuit._limit(health.get("category"))
            if health.get("category") else None))
        circuit = object.__new__(ProviderCircuit)
        circuit.identity = health_identity
        circuit._validate(health)

        flight = health.get("in_flight")
        if isinstance(flight, dict) and "decision_binding" in flight:
            binding = flight["decision_binding"]
            bound_identity = binding["identity"]
            if binding["wal_id"] != wal_id:
                raise ValueError("Provider health reservation is bound to another WAL")
            pair = wal_by_request.get(bound_identity["request_id"])
            if (pair is None or pair[0]["identity"] != bound_identity
                    or pair[0]["request_sha256"] != binding["request_sha256"]
                    or pair[1].state not in {
                        "reserved", "may_have_been_sent", "response_received", "ambiguous"}):
                raise ValueError("Provider health reservation has no matching unresolved WAL request")
            raise ValueError("Provider health still owns an async decision reservation")

        outcome = health.get("decision_outcome")
        if isinstance(outcome, dict) and outcome["identity"]["session_id"] == session_id:
            if outcome["wal_id"] != wal_id:
                raise ValueError("Provider health outcome is bound to another WAL")
            pair = wal_by_request.get(outcome["identity"]["request_id"])
            if pair is None:
                raise ValueError("Provider health outcome has no matching provider WAL record")
            view = pair[1]
            if (pair[0]["identity"] != outcome["identity"]
                    or pair[0]["request_sha256"] != outcome["request_sha256"]
                    or view.state != outcome["state"] or view.phase != outcome["phase"]
                    or view.result_sha256 != outcome["result_sha256"]
                    or view.error_category != outcome["error_category"]
                    or view.http_status != outcome["http_status"]):
                raise ValueError("Provider health outcome differs from its provider WAL record")
            if view.state in {
                    "reserved", "may_have_been_sent", "response_received", "ambiguous"}:
                raise ValueError("Provider health retains an unresolved async outcome")
            archive_record = archives_by_request.get(outcome["identity"]["request_id"])
            if (archive_record is None
                    or archive_record["archive_id"] not in settlement_by_id):
                if not (view.state == "failed" and view.phase == "not_sent"
                        and view.error_category == "local_admission"):
                    raise ValueError("Provider health outcome lacks retained async disposition")

    if path.read_bytes() != captured:
        raise ValueError("Checkpoint changed during async rollback preflight")
    return captured


def _validate_treatment_checkpoint(saved: object, treatment: dict) -> None:
    """Keep the selected treatment bound to the exact checkpoint capture."""
    if not isinstance(saved, dict):
        raise ValueError("Treatment differs from checkpoint")
    if (saved.get('solid_intents') != treatment['solid_intents']
            or saved.get('solid_science_policy') is not treatment['solid_science_policy']
            or (treatment['coal_targets'] and (
                saved.get('coal_targets') != treatment['coal_targets']
                or saved.get('coal_kit_policy') is not treatment['coal_kit_policy']
                or saved.get('coal_economic_admission', False) is not treatment.get('coal_economic_admission', False)
                or treatment.get('coal_economic_admission', False) and saved.get('coal_supply_schema') != 2))
            or (not treatment['coal_targets'] and 'coal_targets' in saved)):
        raise ValueError("Treatment differs from checkpoint")


def cli() -> None:
    # ``cli`` has several branch-local imports for compatibility; bind the
    # module before any new async-status output uses the shared JSON encoder.
    import json

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    p = argparse.ArgumentParser(prog="jev-factorio")
    p.add_argument("--backend", default=os.environ.get("JEV_BACKEND", "mock"))
    limits = p.add_mutually_exclusive_group()
    limits.add_argument("--steps", type=int)
    limits.add_argument("--duration-hours", type=float)
    limits.add_argument("--until-complete", action="store_true",
                        help="Run hierarchical decisions until the configured target is terminal")
    limits.add_argument("--reconcile-only", action="store_true",
                        help="Observe and durably settle resumed background work without acting")
    p.add_argument("--resume", action="store_true",
                   help="Resume an existing live FLE session without resetting its world")
    p.add_argument("--tick-seconds", type=float,
                   default=float(os.environ.get("JEV_TICK_SECONDS", "0")))  # 0 in mock
    p.add_argument("--confidence-floor", type=float,
                   default=float(os.environ.get("JEV_CONFIDENCE_FLOOR", "0.45")))
    p.add_argument("--max-request-bytes", type=int,
                   default=int(os.environ.get("JEV_MAX_REQUEST_BYTES",
                                              str(DEFAULT_MAX_REQUEST_BYTES))),
                   help="Serialized decision-request byte budget (not provider tokens); "
                        "candidates that do not fit are pruned and reported")
    p.add_argument("--persistent-idle-observations", type=int,
                   default=None,
                   help="With --persist-recoverable-blocks, stop the invocation after this many "
                        "consecutive maximum-delay waits with unchanged decision evidence "
                        f"(default {DEFAULT_IDLE_OBSERVATIONS}; 0 disables the bound); "
                        "the blocked checkpoint is preserved")
    p.add_argument("--log-file", default=os.environ.get("JEV_LOG_FILE"))
    p.add_argument("--run-dir", default=os.environ.get("JEV_RUN_DIR"),
                   help="Create a new, exclusive research evidence directory (never append/resume)")
    p.add_argument("--dashboard-events", default=os.environ.get("JEV_DASHBOARD_EVENTS"),
                   help="Optional best-effort live dashboard JSONL; hierarchical controller only")
    p.add_argument("--controller", choices=("flat", "hierarchical"), default="flat")
    p.add_argument("--factory-scheduling", choices=("serial", "ready-work"), default="serial",
                   help="Opt-in bounded production choices; does not enable concurrent mutations or belts")
    p.add_argument("--campaign-diagnostics", action="store_true",
                   help="30-minute progress, host pressure, eligibility and blocked-investment evidence")
    p.add_argument("--profile-latency", action="store_true",
                   help="Content-free startup and inter-request wall/CPU timing in a hierarchical research run")
    p.add_argument("--setup-timing-file", type=Path,
                   help="Exclusive content-free one-use initialization timing result")
    p.add_argument("--owner-step-gate-dir", type=Path,
                   help="Opt-in exclusive private owner grants between verified live steps")
    p.add_argument("--owner-step-lock-path", type=Path)
    p.add_argument("--owner-step-lock-fd", type=int)
    p.add_argument("--owner-step-wait-seconds", type=float, default=120)
    p.add_argument("--profile-observations", action="store_true",
                   help="Content-free observation RPC timing; hierarchical FLE only")
    p.add_argument("--consolidated-observations", action="store_true",
                   help="DEV PILOT: batch native discovery with identity-checked cache; implies profiling")
    p.add_argument("--lead-time-supply", action="store_true",
                   help="DEV PILOT: bounded current-science replenishment reserves; requires ready-work")
    p.add_argument("--coverage-margin-lookahead", action="store_true",
                   help="DEV PILOT: measured coverage permits only already-produced future science collection")
    p.add_argument("--furnace-output-buffers", action="store_true",
                   help="Opt-in paid burner-inserter output buffers; requires ready-work FLE")
    p.add_argument("--furnace-input-belts", action="store_true",
                   help="Opt-in owned drill/belt input routes; requires furnace output buffers")
    p.add_argument("--ore-side-successors", action="store_true",
                   help="Opt-in additive ore-side producers; requires background work and input belts")
    p.add_argument("--mining-outposts", action="store_true",
                   help="Opt-in paid mining outposts for existing manual cells; requires furnace input belts")
    p.add_argument("--background-work", action="store_true",
                   help="Opt-in receipt-tracked crafting and research prefetch; requires ready-work FLE")
    p.add_argument("--production-treatment", type=Path,
                   help="Immutable v1 solid/coal treatment JSON; opt-in ready-work FLE only")
    p.add_argument("--target", choices=("bootstrap_mining", "iron_smelting", "steam_power",
                                       "automation_science", "rocket_launch"), default="rocket_launch")
    p.add_argument("--policy", choices=("jev", "deterministic", "hybrid"), default="jev")
    p.add_argument("--mock-model", action="store_true", help="Explicit offline model (mock backend only)")
    p.add_argument("--model", help="Provider-specific model ID; pin it for reproducible evaluation")
    p.add_argument("--async-decisions", action="store_true",
                   help="Explicitly opt in to durable asynchronous decisions (hierarchical controller only)")
    p.add_argument("--async-decision-timeout-seconds", type=float, default=None,
                   help="Async provider deadline in (0, 30] seconds; requires --async-decisions (default: 30)")
    p.add_argument("--checkpoint", help="Session-bound controller checkpoint, not a game save")
    p.add_argument("--resume-controller", action="store_true")
    p.add_argument("--reevaluate-blocked-once", action="store_true",
                   help="Re-evaluate one exact blocked decision after the decision contract changes")
    p.add_argument("--compatible-source-authorization", type=Path,
                   help="One-use supervisor authority for equal-contract source recovery")
    p.add_argument("--compatible-source-authorization-sha256")
    p.add_argument("--compatible-source-lock-fd", type=int,
                   help="Inherited original supervisor writer-lock descriptor")
    p.add_argument("--exact-checkpoint-sha256",
                   help="Exact starting controller checkpoint SHA-256 for blocked re-evaluation")
    p.add_argument("--blocked-source-revision",
                   help="Full Git commit supplied by the checkpoint owner for the blocked decision")
    p.add_argument("--persist-recoverable-blocks", action="store_true",
                   help="Opt in to observation-only waits and changed-evidence retries for exact recoverable blocks")
    p.add_argument("--initialize-persistent-campaign", action="store_true",
                   help="Explicit new dedicated campaign with persistent recovery; requires a new checkpoint and research directory")
    p.add_argument("--adopt-session", action="store_true",
                   help="Explicitly identify an older live FLE session without resetting it")
    args = p.parse_args()
    duration_seconds = None
    if args.duration_hours is not None:
        if not math.isfinite(args.duration_hours) or args.duration_hours <= 0:
            p.error("--duration-hours must be finite and positive")
        duration_seconds = args.duration_hours * 3600
        if not math.isfinite(duration_seconds):
            p.error("--duration-hours must convert to finite seconds")
    async_provider_mode = None
    async_decision_timeout = None
    if args.async_decision_timeout_seconds is not None and not args.async_decisions:
        p.error("--async-decision-timeout-seconds requires --async-decisions")
    if args.async_decisions:
        if args.controller != "hierarchical":
            p.error("--async-decisions requires --controller hierarchical")
        if args.policy == "deterministic":
            p.error("--async-decisions requires a model-based policy")
        if args.reconcile_only:
            p.error("--async-decisions cannot be combined with --reconcile-only")
        if not args.checkpoint:
            p.error("--async-decisions requires --checkpoint")
        if args.model is not None and (
                not args.model.strip() or args.model != args.model.strip()):
            p.error("--model must be a non-empty provider model ID without surrounding whitespace")
        if args.mock_model:
            if args.model is not None:
                p.error("--model cannot be combined with --mock-model")
            async_provider_mode = "mock"
        elif os.environ.get("TYPESAFE_API_KEY"):
            async_provider_mode = "typesafe"
        elif (os.environ.get("CLOUDFLARE_API_TOKEN")
              and os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
            async_provider_mode = "cloudflare"
        else:
            p.error("Async decisions require live provider credentials or explicit --mock-model")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            p.error("The async CLI must be started from a synchronous process, not a running event loop")
        async_decision_timeout = (
            30.0 if args.async_decision_timeout_seconds is None
            else args.async_decision_timeout_seconds)
        if (not math.isfinite(async_decision_timeout)
                or not 0 < async_decision_timeout <= 30):
            p.error("--async-decision-timeout-seconds must be finite and in (0, 30]")
    sync_resume_async_capture = None
    if (args.resume_controller and args.controller == "hierarchical"
            and not args.async_decisions and args.checkpoint):
        try:
            # Do this before constructing either provider client or backend.
            # A sync reducer cannot safely consume an async-selected plan or
            # bypass a paid/ambiguous provider request retained by this run.
            sync_resume_async_capture = _preflight_sync_async_rollback(
                Path(args.checkpoint), args.target)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError,
                RuntimeError, AttributeError) as error:
            p.error("Synchronous resume is blocked by unresolved or invalid async decision state; "
                    f"checkpoint and provider evidence were preserved ({type(error).__name__})")
    if (args.persistent_idle_observations is not None
            and not args.persist_recoverable_blocks):
        p.error("--persistent-idle-observations requires --persist-recoverable-blocks")
    if (args.persistent_idle_observations is not None
            and not 0 <= args.persistent_idle_observations <= MAX_IDLE_OBSERVATIONS):
        p.error(f"--persistent-idle-observations must be in [0, {MAX_IDLE_OBSERVATIONS}]")
    persistent_idle_observations = (
        (DEFAULT_IDLE_OBSERVATIONS if args.persistent_idle_observations is None
         else args.persistent_idle_observations)
        if args.persist_recoverable_blocks else None
    )
    if args.profile_latency and (args.controller != 'hierarchical' or not args.run_dir):
        p.error("--profile-latency requires hierarchical control and --run-dir")
    if args.consolidated_observations:
        args.profile_observations = True
    if args.profile_observations or args.lead_time_supply or args.coverage_margin_lookahead:
        args.campaign_diagnostics = True
    if args.campaign_diagnostics and args.controller != "hierarchical":
        p.error("--campaign-diagnostics requires --controller hierarchical")
    if args.profile_observations and args.backend != "fle":
        p.error("Observation profiling requires --backend fle")
    if args.lead_time_supply and args.factory_scheduling != "ready-work":
        p.error("--lead-time-supply requires --factory-scheduling ready-work")
    if args.coverage_margin_lookahead and not args.lead_time_supply:
        p.error("--coverage-margin-lookahead requires --lead-time-supply")
    if args.until_complete and args.controller != "hierarchical":
        p.error("--until-complete requires --controller hierarchical")
    if args.reconcile_only and (
        args.controller != "hierarchical" or args.backend != "fle"
        or not args.resume or not args.resume_controller or not args.checkpoint
        or not args.background_work or args.factory_scheduling != "ready-work"
        or args.target == "bootstrap_mining"
    ):
        p.error("--reconcile-only requires resumed hierarchical FLE background-work, "
                "ready-work scheduling, and a checkpoint")
    persistent_checkpoint_capture = None
    compatible_authorization = None
    if args.initialize_persistent_campaign:
        if (not args.persist_recoverable_blocks or args.resume or args.resume_controller
                or args.adopt_session or args.reevaluate_blocked_once
                or args.compatible_source_authorization is not None or not args.run_dir
                or not args.checkpoint):
            p.error("New persistent campaign requires fresh checkpoint/research paths and no resume or migration flags")
        from .new_campaign import require_unused
        try:
            require_unused(Path(args.checkpoint))
        except ValueError as error:
            p.error(str(error))
    if args.compatible_source_authorization is not None:
        if (not args.persist_recoverable_blocks or args.reevaluate_blocked_once
                or args.compatible_source_authorization_sha256 is None
                or args.compatible_source_lock_fd is None):
            p.error("Compatible-source recovery requires persistent recovery, exact authority pin "
                    "and original lock descriptor; changed-contract reevaluation is separate")
        try:
            from .compatible_recovery import read_authorization, require_writer_lock
            compatible_authorization = read_authorization(
                args.compatible_source_authorization, args.compatible_source_authorization_sha256)
            require_writer_lock(args.compatible_source_lock_fd, compatible_authorization["lock_path"])
        except (OSError, ValueError, KeyError) as error:
            p.error(f"Compatible-source authority preflight failed: {error}")
    elif (args.compatible_source_authorization_sha256 is not None
          or args.compatible_source_lock_fd is not None):
        p.error("Compatible-source pins require an explicit authorization file")
    if args.persist_recoverable_blocks:
        if (not args.until_complete or args.backend != "fle" or args.controller != "hierarchical"
                or args.policy != "jev" or args.mock_model
                or (not args.initialize_persistent_campaign and (not args.resume or not args.resume_controller))
                or not args.checkpoint or args.reconcile_only
                or args.owner_step_gate_dir is not None or args.owner_step_lock_path is not None
                or args.owner_step_lock_fd is not None):
            p.error("--persist-recoverable-blocks requires until-complete resumed live FLE Jev control, "
                    "a controller checkpoint, and no reconcile-only or owner-step gate")
        if not args.reevaluate_blocked_once and not args.initialize_persistent_campaign:
            # A resumed block with no persistent ledger can only enter this mode
            # through the existing exact-checkpoint, changed-contract gate.
            try:
                import json
                from .blocked_persistence import is_recoverable_reason
                raw = Path(args.checkpoint).read_bytes()
                checkpoint_data = json.loads(raw.decode("utf-8"))
                if (not isinstance(checkpoint_data, dict)
                        or checkpoint_data.get("status") == "blocked"
                        and is_recoverable_reason(checkpoint_data.get("reason"))
                        and checkpoint_data.get("blocked_recovery") is None):
                    raise ValueError("first blocked recovery requires --reevaluate-blocked-once")
            except (OSError, UnicodeError, ValueError, TypeError) as error:
                p.error(f"Persistent recovery checkpoint preflight failed: {error}")
        try:
            import json
            from .blocked_persistence import validate_checkpoint_metadata
            from .provenance import gameplay_context
            owner_context = gameplay_context()
            revision = owner_context.get("code_revision")
            if not isinstance(revision, dict):
                raise ValueError("a supervisor-pinned source revision is required")
            if not args.initialize_persistent_campaign:
                persistent_checkpoint_capture = Path(args.checkpoint).read_bytes()
                checkpoint_data = json.loads(persistent_checkpoint_capture.decode("utf-8"))
                validate_checkpoint_metadata(
                    checkpoint_data, revision,
                    allow_source_change=(args.reevaluate_blocked_once
                                         or compatible_authorization is not None),
                    owner_context=owner_context)
            from .memory import _BLOCKED_REEVALUATION_REASONS
            if (args.reevaluate_blocked_once
                    and (checkpoint_data.get("status") != "blocked"
                         or checkpoint_data.get("reason") not in _BLOCKED_REEVALUATION_REASONS)):
                raise ValueError("source authorization only applies to an eligible blocked checkpoint")
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            p.error(f"Persistent recovery source/checkpoint preflight failed: {error}")
    blocked_reevaluation_source = None
    blocked_checkpoint_capture = None
    if args.reevaluate_blocked_once:
        if (args.backend != "fle" or args.controller != "hierarchical" or args.policy != "jev"
                or args.mock_model or not args.resume or not args.resume_controller
                or not args.checkpoint or args.steps == 0
                or args.reconcile_only or args.owner_step_gate_dir is not None
                or args.owner_step_lock_path is not None or args.owner_step_lock_fd is not None
                or not args.exact_checkpoint_sha256 or not args.blocked_source_revision):
            p.error("--reevaluate-blocked-once requires resumed hierarchical FLE Jev control, "
                    "its exact checkpoint SHA-256, and the blocked source revision; "
                    "it cannot use reconcile-only or owner-step gating")
        try:
            from .blocked_reevaluation import validate_checkpoint_digest, validate_source_revision
            from .provenance import gameplay_context
            blocked_reevaluation_source = validate_source_revision(args.blocked_source_revision)
            supervised_revision = gameplay_context().get("code_revision")
            if (supervised_revision is not None
                    and (not isinstance(supervised_revision, dict)
                         or supervised_revision.get("commit") != blocked_reevaluation_source["source_head"])):
                raise ValueError("Current Git HEAD differs from supervised source provenance")
            blocked_checkpoint_capture = Path(args.checkpoint).read_bytes()
            validate_checkpoint_digest(blocked_checkpoint_capture, args.exact_checkpoint_sha256)
        except (OSError, ValueError) as error:
            p.error(f"Blocked decision re-evaluation preflight failed: {error}")
    elif args.exact_checkpoint_sha256 is not None or args.blocked_source_revision is not None:
        p.error("Exact checkpoint and blocked source pins require --reevaluate-blocked-once")
    if args.tick_seconds < 0 or not args.tick_seconds < float("inf"):
        p.error("--tick-seconds must be finite and nonnegative")
    if args.resume and args.backend != "fle":
        p.error("--resume requires --backend fle")
    if args.adopt_session and (
        args.controller != "hierarchical" or args.backend != "fle"
        or not args.resume or args.resume_controller
    ):
        p.error("--adopt-session requires hierarchical FLE --resume and a new checkpoint")
    if args.steps is not None and args.steps < 0:
        p.error("--steps must be nonnegative")
    if args.setup_timing_file and (args.steps != 1 or args.duration_hours is not None
                                   or args.controller != 'hierarchical'):
        p.error("--setup-timing-file requires one hierarchical step")
    if args.owner_step_gate_dir is not None:
        if (args.backend != 'fle' or args.controller != 'hierarchical'
                or not args.resume or not args.resume_controller
                or args.until_complete or args.reconcile_only
                or args.duration_hours is not None or args.steps is None
                or not 2 <= args.steps <= 10 or args.setup_timing_file
                or args.owner_step_lock_path is None or args.owner_step_lock_fd is None
                or not os.environ.get('JEV_NATIVE_ATTACHMENT_RECEIPT')
                or not 0 < args.owner_step_wait_seconds <= 120):
            p.error("Owner step gate requires resumed hierarchical FLE, 2-10 steps, "
                    "original inherited lock, attachment receipt, and bounded wait")
    elif args.owner_step_lock_path is not None or args.owner_step_lock_fd is not None:
        p.error("Owner lock options require --owner-step-gate-dir")
    if not 0 <= args.confidence_floor <= 1:
        p.error("--confidence-floor must be finite and in [0, 1]")
    if not MIN_MAX_REQUEST_BYTES <= args.max_request_bytes <= MAX_MAX_REQUEST_BYTES:
        p.error(f"--max-request-bytes must be in [{MIN_MAX_REQUEST_BYTES}, {MAX_MAX_REQUEST_BYTES}]")
    if args.backend not in {"mock", "play_api", "fle"}:
        p.error(f"unknown backend: {args.backend}")
    if args.dashboard_events and args.controller != "hierarchical":
        p.error("--dashboard-events requires --controller hierarchical")
    if args.factory_scheduling != "serial" and args.controller != "hierarchical":
        p.error("--factory-scheduling requires --controller hierarchical")
    if args.background_work and (
        args.controller != "hierarchical" or args.factory_scheduling != "ready-work"
        or args.backend != "fle" or args.target == "bootstrap_mining"
    ):
        p.error("--background-work requires hierarchical FLE ready-work and a native production target")
    if args.ore_side_successors and (not args.furnace_input_belts or not args.background_work
                                     or args.mining_outposts or args.target != 'rocket_launch'
                                     or not args.resume or not args.resume_controller):
        p.error('--ore-side-successors requires background-work input belts, rocket goal, existing resumed campaign, and no mining outposts')
    if args.mining_outposts and (not args.furnace_input_belts or args.target != 'rocket_launch'):
        p.error('--mining-outposts requires --furnace-input-belts and --target rocket_launch')
    if args.furnace_input_belts and not args.furnace_output_buffers:
        p.error("--furnace-input-belts requires --furnace-output-buffers")
    if args.furnace_output_buffers and (
        args.controller != "hierarchical" or args.factory_scheduling != "ready-work"
        or args.backend != "fle" or args.target == "bootstrap_mining"
    ):
        p.error("--furnace-output-buffers requires hierarchical FLE ready-work and a native production target")
    treatment = None
    treatment_digest = None
    if args.production_treatment:
        if args.resume and not args.resume_controller:
            p.error('--production-treatment cannot attach to an existing world without its controller checkpoint')
        if (args.controller != 'hierarchical' or args.backend != 'fle'
                or args.factory_scheduling != 'ready-work' or args.target == 'bootstrap_mining'):
            p.error('--production-treatment requires hierarchical FLE ready-work production')
        try:
            from .treatment import load
            treatment, treatment_digest = load(args.production_treatment)
        except (OSError, ValueError, TypeError) as error:
            p.error(f'Invalid production treatment: {error}')
        if args.resume_controller:
            try:
                import json
                saved = json.loads(Path(args.checkpoint).read_bytes())
                _validate_treatment_checkpoint(saved, treatment)
            except (OSError, ValueError, TypeError, AttributeError) as error:
                p.error(f'Production treatment checkpoint preflight failed: {error}')
    elif args.resume_controller:
        try:
            import json
            saved = json.loads(Path(args.checkpoint).read_bytes())
            if isinstance(saved, dict) and ({'solid_intents', 'coal_targets'} & saved.keys()):
                p.error('Existing solid/coal checkpoint requires its immutable production treatment')
        except (OSError, ValueError, TypeError):
            # Ordinary checkpoint validation owns malformed or missing legacy input.
            pass
    options = dict(confidence_floor=args.confidence_floor,
                   tick_seconds=args.tick_seconds, log_file=args.log_file)
    if args.async_decisions:
        options.update(async_decisions=True,
                       async_decision_timeout=async_decision_timeout)
    if args.controller == "flat":
        if args.mock_model or args.checkpoint or args.resume_controller or args.model or args.policy != "jev":
            p.error("Campaign options require --controller hierarchical")
    else:
        from .controller import HierarchicalLoop

        if args.backend not in {"mock", "fle"}:
            p.error("Hierarchical control currently supports mock and FLE backends")
        if args.mock_model and (args.backend != "mock" or args.policy == "deterministic"):
            p.error("--mock-model requires --backend mock and a model-based policy")
        if args.backend != "mock" and (not args.checkpoint or args.tick_seconds <= 0):
            p.error("Live hierarchical control requires --checkpoint and a positive --tick-seconds")
        if args.checkpoint and Path(args.checkpoint).exists() and not args.resume_controller:
            p.error("Checkpoint exists; explicitly resume or use a new path")
        if args.resume_controller:
            if not args.checkpoint or not Path(args.checkpoint).is_file():
                p.error("--resume-controller requires an existing --checkpoint")
            if args.backend == "fle" and not args.resume:
                p.error("Resuming live controller memory requires --resume to preserve the world")
        if args.ore_side_successors:
            try:
                import json
                from .background import BackgroundWorkLoop
                from .buffer_controller import buffered_loop_type
                from .input_controller import input_loop_type
                from .successor_controller import successor_loop_type
                path = Path(args.checkpoint)
                identity = json.loads(path.read_text(encoding='utf-8'))
                kind = successor_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
                kind.memory_type.load(path, identity.get('session_id'), args.target)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                p.error('Successor checkpoint preflight failed; backend not started')
        # Resolve synchronous clients here as before. Async clients are built
        # inside ExitStack below, immediately before backend attachment, so
        # startup failures can still close their owned pool deterministically.
        if args.async_decisions:
            client = None
        else:
            from .jev_client import MockJevClient, make_client
            try:
                client = (_ReconcileOnlyDecisionClient() if args.reconcile_only else
                          None if args.policy == "deterministic" else
                          MockJevClient() if args.mock_model else
                          make_client(allow_mock=False, model=args.model))
            except ValueError as error:
                p.error(str(error))

    if args.dashboard_events:
        dashboard_path = Path(args.dashboard_events)
        if dashboard_path.is_symlink():
            p.error("Dashboard output must not be a symlink")
        for other in (args.checkpoint, args.log_file):
            if other and (
                str(dashboard_path.resolve()).casefold() == str(Path(other).resolve()).casefold()
                or (dashboard_path.exists() and Path(other).exists() and dashboard_path.samefile(other))
            ):
                p.error("Dashboard output must be separate from logs and checkpoints")
    run_dir = None
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
        try:
            validate_output_paths(run_dir, args.log_file, args.checkpoint, args.dashboard_events)
        except ValueError as error:
            p.error(str(error))
        configuration = RunConfiguration(
            backend=args.backend, controller=args.controller, policy=args.policy,
            target=args.target if args.controller == "hierarchical" else None,
            requested_model=args.model,
            steps=(args.steps if args.steps is not None else 8)
            if args.duration_hours is None and not (args.until_complete or args.reconcile_only) else None,
            duration_seconds=duration_seconds,
            until_complete=args.until_complete, reconcile_only=args.reconcile_only,
            reevaluate_blocked_once=args.reevaluate_blocked_once,
            persist_recoverable_blocks=args.persist_recoverable_blocks,
            initialize_persistent_campaign=args.initialize_persistent_campaign,
            persistent_idle_observations=persistent_idle_observations,
            exact_checkpoint_sha256=args.exact_checkpoint_sha256,
            blocked_source_revision=args.blocked_source_revision,
            tick_seconds=args.tick_seconds, confidence_floor=args.confidence_floor,
            resume=args.resume, resume_controller=args.resume_controller,
            adopt_session=args.adopt_session, mock_model=args.mock_model,
            legacy_log_enabled=bool(args.log_file), checkpoint_enabled=bool(args.checkpoint),
            factory_scheduling=args.factory_scheduling, background_work=args.background_work,
            furnace_output_buffers=args.furnace_output_buffers,
            furnace_input_belts=args.furnace_input_belts, mining_outposts=args.mining_outposts,
            ore_side_successors=args.ore_side_successors,
            campaign_diagnostics=args.campaign_diagnostics,
            profile_observations=args.profile_observations,
            profile_latency=args.profile_latency,
            consolidated_observations=args.consolidated_observations,
            lead_time_supply=args.lead_time_supply,
            coverage_margin_lookahead=args.coverage_margin_lookahead,
            solid_routes=treatment is not None,
            solid_science_policy=treatment['solid_science_policy'] if treatment else False,
            coal_supply=bool(treatment and treatment['coal_targets']),
            coal_kit_policy=treatment['coal_kit_policy'] if treatment else False,
            coal_economic_admission=treatment.get('coal_economic_admission', False) if treatment else False,
            treatment_sha256=treatment_digest,
        )
    setup_timing = None
    if args.setup_timing_file or args.profile_latency:
        target = args.setup_timing_file.absolute() if args.setup_timing_file else None
        if target is not None and (
                target.exists() or target.is_symlink() or not target.parent.is_dir()
                or any(other and target == Path(other).absolute()
                       for other in (args.checkpoint, args.log_file, args.dashboard_events))):
            p.error("Setup timing output must be a new separate file in an existing directory")
        from .setup_timing import SetupTiming
        if target is not None:
            import atexit
        setup_timing = SetupTiming(target, backend_expected=args.backend == 'fle')
        if target is not None:
            atexit.register(setup_timing.write)
        setup_timing.mark('setup_start')
    with ExitStack() as cleanup:
        research = None
        if run_dir is not None:
            try:
                research = cleanup.enter_context(ResearchLog(run_dir, configuration))
            except (OSError, ValueError) as error:
                p.error(f"Cannot initialize research evidence ({type(error).__name__}); backend not started")
        if setup_timing:
            setup_timing.mark('research_ready')
        writer = None
        if args.dashboard_events:
            from .dashboard import EventWriter
            try:
                writer = cleanup.enter_context(EventWriter(
                    args.dashboard_events, forbidden=(args.checkpoint, args.log_file)))
            except (OSError, ValueError) as error:
                p.error(str(error))
        if setup_timing:
            setup_timing.mark('dashboard_ready')
        async_close_state = None
        if args.async_decisions:
            from .jev_client import AsyncMockJevClient, make_async_client
            try:
                if args.mock_model:
                    # Explicit offline selection outranks ambient credentials.
                    # The general factory intentionally prefers configured live
                    # providers, so do not route this opt-in through that factory.
                    client = AsyncMockJevClient()
                else:
                    client = make_async_client(
                        allow_mock=False, model=args.model,
                        max_concurrency=1, max_queue=0,
                        timeout=async_decision_timeout)
            except ValueError as error:
                p.error(str(error))
            async_close_state = {"attempted": False}

            def close_async_provider_before_runner() -> None:
                if not async_close_state["attempted"]:
                    async_close_state["attempted"] = True
                    asyncio.run(client.aclose())

            cleanup.callback(close_async_provider_before_runner)
        if args.controller == "flat":
            options["research_log"] = research
            connector_witness = (Path(args.checkpoint).with_name(
                'native-connector-observer-v1.witness.jsonl') if args.checkpoint else None)
            loop = AgentLoop(make_backend(args.backend, resume=args.resume,
                                          connector_witness_path=connector_witness), **options)
        else:
            options["research_log"] = research
            options["max_request_bytes"] = args.max_request_bytes
            if args.reevaluate_blocked_once:
                options.update(
                    reevaluate_blocked_once=True,
                    exact_checkpoint_sha256=args.exact_checkpoint_sha256,
                    blocked_source_revision=args.blocked_source_revision,
                )
            if args.persist_recoverable_blocks:
                options["persist_recoverable_blocks"] = True
                options["persistent_idle_observations"] = persistent_idle_observations
                if args.initialize_persistent_campaign:
                    options["initialize_persistent_campaign"] = True
            loop_type = HierarchicalLoop
            if args.background_work:
                from .background import BackgroundWorkLoop

                loop_type = BackgroundWorkLoop
            if args.furnace_output_buffers:
                from .buffer_controller import buffered_loop_type

                loop_type = buffered_loop_type(loop_type)
            if args.furnace_input_belts:
                from .input_controller import input_loop_type

                loop_type = input_loop_type(loop_type)
            if args.ore_side_successors:
                from .successor_controller import successor_loop_type
                loop_type = successor_loop_type(loop_type)
            if args.mining_outposts:
                from .outpost_controller import outpost_loop_type
                loop_type = outpost_loop_type(loop_type)
            if treatment:
                from .solid_controller import solid_loop_type
                loop_type = solid_loop_type(loop_type)
                options.update(solid_intents=treatment['solid_intents'],
                               solid_science_policy=treatment['solid_science_policy'])
                if treatment['coal_targets']:
                    from .coal_controller import coal_loop_type
                    loop_type = coal_loop_type(loop_type)
                    options.update(coal_targets=treatment['coal_targets'],
                                   coal_kit_policy=treatment['coal_kit_policy'],
                                   coal_economic_admission=treatment.get('coal_economic_admission', False))
            if args.campaign_diagnostics:
                from .campaign_controller import campaign_loop_type
                loop_type = campaign_loop_type(loop_type)
                options.update(lead_time_supply=args.lead_time_supply,
                               coverage_margin_lookahead=args.coverage_margin_lookahead)
            selected_resume_checkpoint_capture = None
            if args.checkpoint and Path(args.checkpoint).is_file():
                if compatible_authorization is not None:
                    try:
                        from .compatible_recovery import migrate_checkpoint
                        from .provenance import gameplay_context
                        context = gameplay_context()
                        provider_identity = None
                        if getattr(client, "uses_http_provider", False):
                            from .provider_health import ProviderCircuit
                            from .operational_safety import safety_dir
                            # Validate the retained circuit before backend
                            # attachment; its constructor does not dispatch.
                            circuit = ProviderCircuit(client, safety_dir(Path(args.checkpoint)) / "provider.json")
                            provider_identity = circuit.identity
                        migrate_checkpoint(
                            Path(args.checkpoint), compatible_authorization,
                            loop_type.memory_type, context["code_revision"],
                            {key: context[key] for key in ("run_id", "segment_id", "execution_id")},
                            lock_fd=args.compatible_source_lock_fd,
                            provider_identity_sha256=provider_identity)
                        # The authorization is consumed before make_backend or
                        # any native observation. Every later guard binds the
                        # durably migrated bytes, not the old capture.
                        persistent_checkpoint_capture = Path(args.checkpoint).read_bytes()
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        p.error(f"Compatible-source migration failed; reconcile checkpoint before retry: {error}")
                try:
                    import json
                    path = Path(args.checkpoint)
                    captured = path.read_bytes()
                    checkpoint_data = json.loads(captured.decode("utf-8"))
                    if (isinstance(checkpoint_data, dict)
                            and checkpoint_data.get("blocked_recovery_archive") is not None):
                        if not args.resume_controller:
                            raise ValueError("An archived blocked-recovery checkpoint must be resumed")
                        from .memory import load_checkpoint
                        verified = load_checkpoint(path, checkpoint_data.get("session_id"), args.target)
                        index = getattr(verified, "_blocked_recovery_archive_index", None)
                        if index is not None:
                            index.close()
                        if path.read_bytes() != captured:
                            raise ValueError("Checkpoint or blocked-recovery archive changed during preflight")
                except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError) as error:
                    p.error(f"Blocked-recovery archive preflight failed: {error}")
            if args.backend == "fle":
                from .operational_safety import storage_ready
                output_roots = [Path(args.checkpoint).parent]
                if args.log_file:
                    output_roots.append(Path(args.log_file).parent)
                if run_dir:
                    output_roots.append(run_dir)
                if args.dashboard_events:
                    output_roots.append(Path(args.dashboard_events).parent)
                if not storage_ready(output_roots):
                    p.error("Storage reserve unavailable; live backend was not attached")
            if treatment:
                from .treatment import load
                _, current_digest = load(args.production_treatment)
                if current_digest != treatment_digest:
                    p.error('Production treatment changed before backend attachment')
                if args.resume_controller:
                    try:
                        import json
                        path = Path(args.checkpoint)
                        captured = path.read_bytes()
                        identity = json.loads(captured)
                        loop_type.memory_type.from_bytes(captured, identity['session_id'], args.target)
                        if path.read_bytes() != captured:
                            raise ValueError('Checkpoint changed during composed preflight')
                    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                        p.error(f'Composed treatment checkpoint preflight failed: {error}')
            if args.reevaluate_blocked_once:
                try:
                    from .blocked_reevaluation import validate_checkpoint_capture
                    preflight_memory = validate_checkpoint_capture(
                        blocked_checkpoint_capture, args.exact_checkpoint_sha256,
                        loop_type.memory_type, args.target, 4,
                        blocked_reevaluation_source["decision_contract_sha256"],
                        checkpoint_path=Path(args.checkpoint))
                    # The CLI preflight only needs to validate the archive. The
                    # live controller builds its own bounded index after restore.
                    index = getattr(preflight_memory, "_blocked_recovery_archive_index", None)
                    if index is not None:
                        index.close()
                    if Path(args.checkpoint).read_bytes() != blocked_checkpoint_capture:
                        raise ValueError('Checkpoint changed during blocked-decision preflight')
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                    p.error(f'Blocked decision checkpoint preflight failed: {error}')
            if args.initialize_persistent_campaign:
                from .new_campaign import reserve
                from .provenance import gameplay_context
                try:
                    reserve(Path(args.checkpoint), gameplay_context())
                except (OSError, ValueError) as error:
                    p.error(f"New campaign admission failed; backend not started: {error}")
            if (args.persist_recoverable_blocks and not args.initialize_persistent_campaign
                    and Path(args.checkpoint).read_bytes() != persistent_checkpoint_capture):
                p.error("Persistent recovery checkpoint changed during pre-backend preflight")
            if args.resume_controller:
                try:
                    selected_resume_checkpoint_capture = _preflight_selected_checkpoint(
                        Path(args.checkpoint), loop_type.memory_type, args.target)
                except Exception as error:
                    p.error("Composed resume checkpoint preflight failed; backend not started "
                            f"({type(error).__name__})")
                if (persistent_checkpoint_capture is not None
                        and selected_resume_checkpoint_capture != persistent_checkpoint_capture):
                    p.error("Checkpoint changed after recovery source preflight; backend not started")
                if (blocked_checkpoint_capture is not None
                        and selected_resume_checkpoint_capture != blocked_checkpoint_capture):
                    p.error("Checkpoint changed after blocked-decision preflight; backend not started")
                if sync_resume_async_capture is not None:
                    try:
                        rollback_capture = _preflight_sync_async_rollback(
                            Path(args.checkpoint), args.target)
                    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
                            RuntimeError, AttributeError) as error:
                        p.error("Synchronous rollback state changed during preflight; backend not started "
                                f"({type(error).__name__})")
                    if rollback_capture != selected_resume_checkpoint_capture:
                        p.error("Checkpoint changed during synchronous rollback preflight; backend not started")
                if treatment is not None:
                    try:
                        saved = json.loads(selected_resume_checkpoint_capture.decode("utf-8"))
                        _validate_treatment_checkpoint(saved, treatment)
                    except (UnicodeError, ValueError, TypeError, AttributeError) as error:
                        p.error("Production treatment no longer matches the preflighted checkpoint; "
                                f"backend not started ({type(error).__name__})")
            if setup_timing:
                setup_timing.mark('preflight_ready')
            if selected_resume_checkpoint_capture is not None:
                try:
                    unchanged = _checkpoint_capture_matches(
                        Path(args.checkpoint), selected_resume_checkpoint_capture)
                except OSError:
                    unchanged = False
                if not unchanged:
                    p.error("Checkpoint changed after composed preflight; backend not started")
            connector_witness = (Path(args.checkpoint).with_name(
                'native-connector-observer-v1.witness.jsonl') if args.checkpoint else None)
            attachment = {}
            if args.backend == 'fle' and selected_resume_checkpoint_capture is not None:
                binding = json.loads(selected_resume_checkpoint_capture).get('connector_ownership')
                if isinstance(binding, dict) and binding.get('routes'):
                    attachment['connector_binding'] = binding
                    from .backends.native_completed_craft import (
                        checkpoint_completed_craft, checkpoint_background_craft)
                    craft = checkpoint_completed_craft(json.loads(selected_resume_checkpoint_capture))
                    if craft is not None:
                        attachment['completed_craft'] = craft
                    tracked = checkpoint_background_craft(json.loads(selected_resume_checkpoint_capture))
                    if tracked is not None:
                        attachment['background_craft'] = tracked
            if setup_timing:
                backend = make_backend(args.backend, resume=args.resume,
                                       adopt_session=args.adopt_session,
                                       setup_timing=setup_timing,
                                       connector_witness_path=connector_witness, **attachment)
            else:
                backend = make_backend(args.backend, resume=args.resume,
                                       adopt_session=args.adopt_session,
                                       connector_witness_path=connector_witness, **attachment)
            if setup_timing:
                setup_timing.mark('backend_ready')
            if args.backend == "fle" and args.profile_observations:
                backend.profile_observations = True
                backend.consolidated_observations = args.consolidated_observations
            loop = loop_type(backend, jev=client,
                                    target=args.target, policy=args.policy, checkpoint=args.checkpoint,
                                    resume_controller=args.resume_controller,
                                    factory_scheduling=args.factory_scheduling, **options)
        if setup_timing:
            setup_timing.mark('controller_ready')
        if writer is not None:
            from .dashboard import attach
            attach(loop, writer)
            if getattr(loop, "_safety", None) is not None:
                loop._safety.outputs = (*loop._safety.outputs, Path(args.dashboard_events).parent)
        if args.backend == "fle" and (args.log_file or args.dashboard_events):
            from .research_catalog import export_sidecar
            export_sidecar(getattr(loop, "backend", None), Path(args.log_file or args.dashboard_events).parent)
        if setup_timing:
            setup_timing.mark('outputs_ready')
        if setup_timing:
            setup_timing.mark('initialized')
        if research is not None:
            memory = getattr(loop, "memory", None)
            initialized = {
                "requested_model": getattr(getattr(loop, "jev", None), "model", None),
                "model_is_mock": bool(getattr(getattr(loop, "jev", None), "is_mock", False)),
            }
            if args.async_decisions:
                initialized["async_decisions"] = {
                    "enabled": True,
                    "provider": async_provider_mode,
                    "decision_deadline_seconds": async_decision_timeout,
                    "transport_timeout_seconds": (
                        None if async_provider_mode == "mock" else async_decision_timeout),
                    "max_client_concurrency": 1,
                    "max_queued_requests": 0,
                }
            if args.profile_latency and setup_timing:
                initialized['initialization_timing'] = setup_timing.profile_result()
            research.emit("controller_initialized", initialized,
                          session_id=getattr(memory, "session_id", None))
        if args.async_decisions:
            print(json.dumps({"async_decisions": {
                "enabled": True,
                "provider": async_provider_mode,
                "decision_deadline_seconds": async_decision_timeout,
                "transport_timeout_seconds": (
                    None if async_provider_mode == "mock" else async_decision_timeout),
                "max_client_concurrency": 1,
                "max_queued_requests": 0,
            }}, sort_keys=True), flush=True)
        if args.profile_latency:
            loop.profile_latency = True
            trace = getattr(loop, '_trace', None)
            if trace is not None:
                trace.profile_latency = True
        step_gate = None
        if args.owner_step_gate_dir is not None:
            from .owner_step_gate import OwnerStepGate
            step_gate = OwnerStepGate(
                args.owner_step_gate_dir, Path(args.checkpoint),
                Path(os.environ['JEV_NATIVE_ATTACHMENT_RECEIPT']),
                Path(__file__).resolve().parents[2], args.owner_step_lock_path,
                args.owner_step_lock_fd, wait_seconds=args.owner_step_wait_seconds)
        try:
            if args.async_decisions:
                if args.until_complete:
                    run_options = {"until_complete": True}
                elif args.duration_hours is not None:
                    run_options = {"steps": None,
                                   "duration_seconds": duration_seconds}
                elif step_gate is None:
                    run_options = {"steps": args.steps if args.steps is not None else 8}
                else:
                    run_options = {"steps": args.steps, "after_step": step_gate}
                asyncio.run(_run_async_controller_lifecycle(
                    loop, async_close_state, **run_options))
            elif args.reconcile_only:
                result = loop.reconcile_only()
                print(json.dumps({"reconciliation": result}, sort_keys=True), flush=True)
            elif args.until_complete:
                loop.run(until_complete=True)
            elif args.duration_hours is not None:
                loop.run(steps=None, duration_seconds=duration_seconds)
            else:
                if step_gate is None:
                    loop.run(steps=args.steps if args.steps is not None else 8)
                else:
                    loop.run(steps=args.steps, after_step=step_gate)
        except BaseException as error:
            from .recovery_policy import record_exit
            record_exit(loop, error)
            raise
        finally:
            if setup_timing:
                setup_timing.capture_final_iteration_safely(loop)
        if research is not None:
            research.emit("controller_stopped", {
                "terminal": bool(getattr(loop, "terminal", False)),
                "controller_status": getattr(getattr(loop, "memory", None), "status", None),
            })


if __name__ == "__main__":
    cli()
