"""CLI: python -m jev_factorio --backend mock --steps 8"""
from __future__ import annotations

import argparse
import json
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


def make_backend(name: str, resume: bool = False, adopt_session: bool = False,
                 setup_timing=None, connector_witness_path=None):
    if name == "mock":
        return MockBackend()
    if name == "play_api":
        from .backends.play_api import PlayApiBackend
        return PlayApiBackend(factorio_user_dir=os.environ.get(
            "FACTORIO_USER_DIR", "~/.factorio"))
    if name == "fle":
        from .backends.fle import FleBackend
        b = FleBackend()
        if setup_timing is None:
            b.start(resume=resume, adopt_session=adopt_session,
                    connector_witness_path=connector_witness_path)
        else:
            b.start(resume=resume, adopt_session=adopt_session,
                    setup_timing=setup_timing,
                    connector_witness_path=connector_witness_path)
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
    if args.duration_hours is not None and (
        not 0 < args.duration_hours < float("inf")
    ):
        p.error("--duration-hours must be finite and positive")
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
            if (args.reevaluate_blocked_once
                    and (checkpoint_data.get("status") != "blocked"
                         or checkpoint_data.get("reason") not in {
                             "Candidate evidence insufficient", "low choice confidence"})):
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
    if args.controller == "flat":
        if args.mock_model or args.checkpoint or args.resume_controller or args.model or args.policy != "jev":
            p.error("Campaign options require --controller hierarchical")
    else:
        from .controller import HierarchicalLoop
        from .jev_client import MockJevClient, make_client

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
        # Resolve credentials before starting a backend that initializes a world.
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
            duration_seconds=args.duration_hours * 3600 if args.duration_hours is not None else None,
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
            if setup_timing:
                backend = make_backend(args.backend, resume=args.resume,
                                       adopt_session=args.adopt_session,
                                       setup_timing=setup_timing,
                                       connector_witness_path=connector_witness)
            else:
                backend = make_backend(args.backend, resume=args.resume,
                                       adopt_session=args.adopt_session,
                                       connector_witness_path=connector_witness)
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
            if args.profile_latency and setup_timing:
                initialized['initialization_timing'] = setup_timing.profile_result()
            research.emit("controller_initialized", initialized,
                          session_id=getattr(memory, "session_id", None))
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
            if args.reconcile_only:
                result = loop.reconcile_only()
                print(json.dumps({"reconciliation": result}, sort_keys=True), flush=True)
            elif args.until_complete:
                loop.run(until_complete=True)
            elif args.duration_hours is not None:
                loop.run(steps=None, duration_seconds=args.duration_hours * 3600)
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
