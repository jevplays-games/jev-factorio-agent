"""Durable, session-preserving supervision for an autonomous campaign."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from typing import Callable

from .provenance import CONTEXT_ENV, append_audit, digest_json, identifier, source_revision
from .operational_safety import SafetyStateError, read_json, safety_dir
from .recovery_policy import classify, current_exit, repair_quota_exhausted


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass
class SupervisorConfig:
    state_dir: Path
    checkpoint: Path
    session_id: str
    started_at: float
    repair_command: list[str]
    cwd: Path
    python: str = sys.executable
    duration_hours: float = 12
    poll_seconds: float = 5
    hang_seconds: float = 600
    repair_seconds: float = 1800
    backoff_seconds: float = 30
    tick_seconds: float = 1
    run_id: str | None = None
    run_manifest: Path | None = None
    research_dir: Path | None = None
    factory_scheduling: str = "serial"
    background_work: bool = False
    furnace_output_buffers: bool = False
    furnace_input_belts: bool = False
    mining_outposts: bool = False
    ore_side_successors: bool = False
    campaign_diagnostics: bool = False
    profile_observations: bool = False
    consolidated_observations: bool = False
    lead_time_supply: bool = False
    coverage_margin_lookahead: bool = False
    production_treatment: Path | None = None
    max_repair_attempts: int = 3

    def validate(self) -> None:
        if type(self.max_repair_attempts) is not int or not 1 <= self.max_repair_attempts <= 10:
            raise ValueError("max_repair_attempts must be an integer in [1, 10]")
        for name in ("campaign_diagnostics", "profile_observations", "consolidated_observations",
                     "lead_time_supply", "coverage_margin_lookahead"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("Campaign options must be boolean")
        if self.consolidated_observations:
            self.profile_observations = True
        if self.profile_observations or self.lead_time_supply or self.coverage_margin_lookahead:
            self.campaign_diagnostics = True
        if self.lead_time_supply and self.factory_scheduling != "ready-work":
            raise ValueError("Lead-time supply requires ready-work scheduling")
        if self.coverage_margin_lookahead and not self.lead_time_supply:
            raise ValueError("Coverage-margin lookahead requires lead-time supply")
        if self.factory_scheduling not in {"serial", "ready-work"}:
            raise ValueError("Unknown factory scheduling mode")
        if (self.background_work or self.furnace_output_buffers or self.furnace_input_belts
                ) and self.factory_scheduling != "ready-work":
            raise ValueError("Production extensions require ready-work scheduling")
        if self.ore_side_successors and (not self.background_work or not self.furnace_input_belts or self.mining_outposts):
            raise ValueError('Successors require background-work input belts without mining outposts')
        if self.mining_outposts and not self.furnace_input_belts:
            raise ValueError('Mining outposts require furnace input belts')
        if self.furnace_input_belts and not self.furnace_output_buffers:
            raise ValueError("Furnace input belts require furnace output buffers")
        if self.production_treatment is not None:
            from .treatment import load
            if self.factory_scheduling != 'ready-work':
                raise ValueError('Production treatment requires ready-work scheduling')
            load(self.production_treatment)
        if self.run_id is not None:
            identifier(self.run_id)
        for name in ("started_at", "duration_hours", "poll_seconds", "hang_seconds",
                     "repair_seconds", "backoff_seconds", "tick_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not self.session_id or not isinstance(self.repair_command, list) or not self.repair_command or not all(
            isinstance(part, str) and part for part in self.repair_command
        ):
            raise ValueError("A session ID and nonempty repair argv are required")


class Supervisor:
    def __init__(self, config: SupervisorConfig, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 popen: Callable = subprocess.Popen) -> None:
        config.validate()
        self.config = config
        self.clock, self.sleep, self.popen = clock, sleep, popen
        self.state: dict = {}
        self.process = None
        self.output = None
        self.stop_requested = False
        self.audit_failed = False

    @property
    def state_path(self) -> Path:
        return self.config.state_dir / "supervisor.json"

    def remaining(self) -> float:
        return max(0, self.state["cutoff"] - self.clock())

    def _flush_audit(self) -> None:
        pending = self.state.get("audit_pending")
        if pending is not None:
            if pending.get("run_id") != self.state["run_id"]:
                raise ValueError("Pending audit belongs to a different run")
            append_audit(self.config.state_dir / "events.jsonl", pending)
            self.save(audit_pending=None)

    def transition(self, kind: str, updates: dict | None = None, **fields) -> bool:
        """Atomically persist state changes with a replayable audit outbox.

        Never launch another child after an audit failure. Replaying the outbox
        on restart closes both the before-append and after-fsync crash windows.
        """
        try:
            self._flush_audit()
            next_state = {**self.state, **(updates or {})}
            # Arbitrary exception text can contain URLs/credentials. Keep a
            # recognizable reason code and a fingerprint, not raw diagnostics.
            for key in ("reason", "error"):
                if key in fields:
                    text = str(fields[key])
                    fields[key + "_sha256"] = hashlib.sha256(text.encode()).hexdigest()
                    code = text.split(":", 1)[0]
                    allowed = {"blocked", "uncertain", "completed", "cutoff", "stopped",
                               "process_exit", "checkpoint_heartbeat_timeout", "checkpoint_invalid",
                               "checkpoint_invalid_on_restart", "gameplay_error", "execution_failed",
                               "checkpoint_reconciliation", "game_intervention", "infrastructure_change",
                               "code_change", "other"}
                    fields[key] = code if key == "reason" and code in allowed else "redacted"
            now = self.clock()
            incident = next_state.get("incident") or {}
            record = {
                **fields, "schema": "jev-factorio.supervisor-event.v1",
                "at": now, "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "monotonic_ns": time.monotonic_ns(), "writer_pid": os.getpid(),
                "event": kind, "event_id": str(uuid4()),
                "run_id": next_state["run_id"], "session_id": self.config.session_id,
                "segment_id": next_state["segment_id"],
                "sequence": next_state.get("audit_sequence", 0) + 1,
                "incident_id": fields.get("incident_id", incident.get("incident_id")),
            }
            self.save(**{**(updates or {}), "audit_sequence": record["sequence"],
                         "audit_pending": record})
            self._flush_audit()
            return True
        except (OSError, ValueError, TypeError) as error:
            self.stop_requested = True
            self.audit_failed = True
            print(f"Supervisor audit failed; stopping ({type(error).__name__})",
                  file=sys.stderr, flush=True)
            return False

    def event(self, kind: str, **fields) -> None:
        self.transition(kind, **fields)

    def initialize_provenance(self, *, existing: bool) -> None:
        requested = self.config.run_id
        manifest_path = self.config.run_manifest or self.state.get("run_manifest")
        manifest_digest = self.state.get("run_manifest_sha256")
        if manifest_path is not None:
            path = Path(manifest_path).resolve()
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("Run manifest must be an object")
            manifest_id = identifier(manifest.get("run_id"))
            if requested is not None and requested != manifest_id:
                raise ValueError("Run ID differs from run manifest")
            requested = manifest_id
            current_digest = digest_json(manifest)
            if manifest_digest is not None and current_digest != manifest_digest:
                raise ValueError("Run manifest cannot be changed")
            manifest_path, manifest_digest = str(path), current_digest
        stored = self.state.get("run_id")
        if "run_id" in self.state:
            identifier(stored)
            if requested is not None and requested != stored:
                raise ValueError("Existing run ID cannot be changed")
            self._flush_audit()
            if manifest_path is not None and self.state.get("run_manifest") is None:
                self.transition("run_manifest_attached", {
                    "run_manifest": manifest_path, "run_manifest_sha256": manifest_digest,
                }, manifest_sha256=manifest_digest)
        else:
            self.save(run_id=requested or str(uuid4()), segment=1,
                      segment_id="seg-000001", audit_sequence=0,
                      audit_pending=None, revision_initialized=False, code_revision=None,
                      run_manifest=manifest_path, run_manifest_sha256=manifest_digest)
            self.event("provenance_started", legacy_history=existing,
                       manifest_sha256=manifest_digest)
        if self.state.get("incident") and not self.state["incident"].get("incident_id"):
            self.transition("incident_provenance_attached", {
                "incident": {**self.state["incident"], "incident_id": str(uuid4())},
            }, legacy_history=True)

    def snapshot_revision(self, *, manual: bool = False) -> dict | None:
        if self.stop_requested or (not manual and self.remaining() <= 0):
            return None
        return source_revision(
            self.config.cwd, timeout=10 if manual else min(10, self.remaining()),
            exclude_untracked=(self.config.state_dir, self.config.checkpoint),
            exclude_untracked_prefixes=(self.config.checkpoint.with_name(
                self.config.checkpoint.name + "."),),
        )

    def record_revision(self, revision: dict | None, cause: str, **fields) -> bool:
        before = self.state.get("code_revision")
        initialized = self.state.get("revision_initialized", False)
        if initialized and revision == before:
            return True
        segment = self.state["segment"] + int(initialized)
        if not initialized:
            fields = {**fields, "actor_type": "supervisor", "intervention_type": None}
        kind = ("segment_started" if not initialized else
                "code_revision_changed" if before is not None and revision is not None else
                "source_provenance_changed")
        return self.transition(kind, {
            "segment": segment, "segment_id": f"seg-{segment:06d}",
            "code_revision": revision, "revision_initialized": True,
        }, from_segment_id=self.state["segment_id"] if initialized else None,
            source_before=before, source_after=revision, cause=cause,
            change_known=before is not None and revision is not None, **fields)

    def record_manual_intervention(self, report: dict) -> None:
        if not isinstance(report, dict):
            raise ValueError("Manual intervention report must be an object")
        actor = identifier(report.get("actor"))
        reason = report.get("reason")
        if not isinstance(reason, str) or reason not in {"checkpoint_reconciliation", "game_intervention",
                          "infrastructure_change", "code_change", "other"}:
            raise ValueError("Invalid manual intervention reason")
        evidence = report.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(item, str) and item.strip() for item in evidence
        ):
            raise ValueError("Manual intervention requires nonempty evidence strings")
        before = self.state.get("code_revision")
        after = self.snapshot_revision(manual=True)
        checkpoint = self.checkpoint()
        if (self.has_unresolved_work(checkpoint)
                and (before is None or after is None or before != after)):
            raise ValueError(
                "Code provenance changed while a pending action requires reconciliation "
                "or acknowledged background work requires compatible-source review"
            )
        segment = self.state["segment"] + 1
        previous_segment = self.state["segment_id"]
        if not self.transition("manual_intervention", {
            "segment": segment, "segment_id": f"seg-{segment:06d}",
            "code_revision": after, "revision_initialized": True,
            "manual_interventions": self.state.get("manual_interventions", 0) + 1,
        }, actor_type="human", actor=actor, intervention_type="manual_intervention",
            reason=reason, declaration_only=True, report_sha256=digest_json(report),
            evidence_count=len(evidence), from_segment_id=previous_segment,
            source_before=before, source_after=after):
            return
        if before is not None and after is not None and before != after:
            self.event("code_revision_changed", cause="manual_intervention",
                       actor_type="human", intervention_type="manual_intervention",
                       source_before=before, source_after=after,
                       from_segment_id=previous_segment, change_known=True)

    def save(self, **fields) -> None:
        self.state.update(fields)
        atomic_json(self.state_path, self.state)

    def checkpoint(self) -> dict:
        with self.config.checkpoint.open() as stream:
            checkpoint = json.load(stream)
        if not isinstance(checkpoint, dict):
            raise ValueError("Checkpoint must be a JSON object")
        if checkpoint.get("session_id") != self.config.session_id:
            raise ValueError("Checkpoint session differs from supervised session")
        if checkpoint.get("target") != "rocket_launch":
            raise ValueError("Checkpoint target must be rocket_launch")
        if checkpoint.get("status") not in {"running", "blocked", "uncertain", "completed"}:
            raise ValueError("Checkpoint status is invalid")
        return checkpoint

    @staticmethod
    def has_unresolved_work(checkpoint: dict) -> bool:
        """Return whether the checkpoint still owns executable or paid work.

        Completed attempt outcomes, histories, and other retained evidence are
        deliberately excluded: they are receipts, not live obligations.
        """
        return (any(checkpoint.get(key) is not None for key in (
                    "pending", "attempt", "transfer_recovery",
                    "background_job", "background_attempt"))
                or checkpoint.get("active_plan") is not None
                or bool(checkpoint.get("reservations")))

    def initialize(self, *, record_only: bool = False) -> None:
        self.config.validate()
        cutoff = self.config.started_at + self.config.duration_hours * 3600
        identity = {"session_id": self.config.session_id,
                    "checkpoint": str(self.config.checkpoint.resolve()),
                    "cwd": str(self.config.cwd.resolve()),
                    "started_at": self.config.started_at, "cutoff": cutoff}
        existing = self.state_path.exists()
        if existing:
            self.state = json.loads(self.state_path.read_text())
            if any(self.state.get(key) != value for key, value in identity.items()):
                raise ValueError("Existing supervision identity/cutoff cannot be changed")
        else:
            self.state = {**identity, "attempt": 0, "phase": "ready", "process": None}
            self.save()
        configuration = {
            name: getattr(self.config, name) for name in (
                "factory_scheduling", "background_work",
                "furnace_output_buffers", "furnace_input_belts",
            )
        }
        # Keep old disabled configurations byte-for-byte comparable. Enabling is
        # a distinct treatment, never a silent change to a running supervisor.
        if self.config.mining_outposts:
            configuration['mining_outposts'] = True
        if self.config.ore_side_successors:
            configuration['ore_side_successors'] = True
        if self.config.production_treatment is not None:
            from .treatment import load
            _, configuration['production_treatment_sha256'] = load(self.config.production_treatment)
        for name in ("campaign_diagnostics", "profile_observations", "consolidated_observations",
                     "lead_time_supply", "coverage_margin_lookahead"):
            if getattr(self.config, name):
                configuration[name] = True
        if self.state.get("gameplay_configuration", configuration) != configuration:
            raise ValueError("Existing gameplay configuration cannot be changed")
        self.save(gameplay_configuration=configuration)
        if record_only and self.state.get("process"):
            raise ValueError("Manual intervention requires no saved process; recover supervision separately")
        self.initialize_provenance(existing=existing)
        if record_only:
            return
        self.recover_process()
        self.close_interrupted_attempt()
        if not self.state.get("repair_required"):
            try:
                self.save(last_valid_checkpoint=self.checkpoint())
            except (OSError, ValueError):
                if not self.state.get("last_valid_checkpoint"):
                    raise
                self.begin_repair("checkpoint_invalid_on_restart")

    @staticmethod
    def process_identity(pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return None

    def recover_process(self) -> None:
        saved = self.state.get("process")
        if not saved:
            return
        current = self.process_identity(saved["pid"])
        if current is not None and current != saved["identity"]:
            raise RuntimeError("Saved process PID was reused; refusing unsafe recovery")
        self.kill_group(saved["pid"])
        self.save(process=None, phase="recovered")
        self.event("orphan_process_group_stopped", pid=saved["pid"],
                   intervention_type="operational_recovery", actor_type="supervisor")

    @staticmethod
    def kill_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def launch(self, command: list[str], phase: str, prompt: Path | None = None) -> None:
        if self.remaining() <= 0 or self.stop_requested:
            return
        execution_id = str(uuid4())
        if not self.transition("process_prepared", phase=phase, execution_id=execution_id):
            return
        self.output = (self.config.state_dir / f"{phase}.log").open("ab")
        input_stream = prompt.open("rb") if prompt else subprocess.DEVNULL
        temporary = self.config.cwd / "runs" / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary)
        environment["PYTHONPATH"] = str(self.config.cwd / "src")
        # Do not leak a gameplay context into repair tests or Git verification.
        environment.pop(CONTEXT_ENV, None)
        if phase == "gameplay":
            environment[CONTEXT_ENV] = json.dumps({
                "run_id": self.state["run_id"], "segment_id": self.state["segment_id"],
                "execution_id": execution_id, "code_revision": self.state["code_revision"],
            }, sort_keys=True)
        try:
            self.process = self.popen(
                command, cwd=self.config.cwd, stdin=input_stream,
                stdout=self.output, stderr=subprocess.STDOUT, start_new_session=True,
                env=environment,
            )
        finally:
            if prompt:
                input_stream.close()
        self.save(phase=phase, execution_id=execution_id, process={"pid": self.process.pid,
                                      "identity": self.process_identity(self.process.pid)})
        self.event("process_started", phase=phase, pid=self.process.pid,
                   execution_id=execution_id, code_revision=self.state.get("code_revision"))

    def stop_process(self) -> None:
        if self.process is not None:
            self.kill_group(self.process.pid)
            self.process.wait()
            self.process = None
        if self.output is not None:
            self.output.close()
            self.output = None
        self.save(process=None)

    def pause(self, seconds: float) -> None:
        until = min(self.clock() + seconds, self.state["cutoff"])
        while not self.stop_requested and self.clock() < until:
            self.sleep(min(self.config.poll_seconds, until - self.clock()))

    def gameplay_command(self) -> list[str]:
        self.config.validate()
        command = [
            self.config.python, "-m", "jev_factorio", "--backend", "fle", "--resume",
            "--resume-controller", "--controller", "hierarchical", "--policy", "hybrid",
            "--model", "jev-1.13.0",
            "--target", "rocket_launch", "--checkpoint", str(self.config.checkpoint),
            "--duration-hours", str(self.remaining() / 3600),
            "--tick-seconds", str(self.config.tick_seconds),
            "--log-file", str(self.config.state_dir / "gameplay.jsonl"),
            "--factory-scheduling", self.config.factory_scheduling,
        ]
        for name in ("background_work", "furnace_output_buffers", "furnace_input_belts", "mining_outposts", "ore_side_successors",
                     "campaign_diagnostics", "profile_observations", "consolidated_observations",
                     "lead_time_supply", "coverage_margin_lookahead"):
            if getattr(self.config, name):
                command.append("--" + name.replace("_", "-"))
        if self.config.production_treatment is not None:
            from .treatment import load
            _, digest = load(self.config.production_treatment)
            if self.state.get('gameplay_configuration', {}).get('production_treatment_sha256') != digest:
                raise ValueError('Production treatment changed after supervisor initialization')
            command.extend(['--production-treatment', str(self.config.production_treatment.resolve())])
        if self.config.research_dir is not None:
            command.extend(["--run-dir", str(
                self.config.research_dir.resolve() / f"invocation-{uuid4()}"
            )])
        return command

    def watch_game(self) -> str:
        checkpoint = self.checkpoint()
        revision = self.snapshot_revision()
        source_before = self.state.get("code_revision")
        if (self.has_unresolved_work(checkpoint)
                and (source_before is None or revision is None or revision != source_before)):
            self.begin_repair("checkpoint_reconciliation")
            return "checkpoint_reconciliation"
        if checkpoint["status"] == "completed" and self.has_unresolved_work(checkpoint):
            self.begin_repair("checkpoint_reconciliation")
            return "checkpoint_reconciliation"
        if checkpoint["status"] != "running":
            self.save(last_valid_checkpoint=checkpoint)
            return checkpoint["status"]
        self.save(last_valid_checkpoint=checkpoint)
        if not self.record_revision(revision, "gameplay_start",
                                    actor_type="unknown", intervention_type="unattributed_change"):
            return "stopped"
        self.launch(self.gameplay_command(), "gameplay")
        if self.process is None:
            return "stopped" if self.stop_requested else "cutoff"
        last_change = self.clock()
        signature = self.config.checkpoint.stat().st_mtime_ns
        while self.remaining() > 0 and not self.stop_requested:
            try:
                checkpoint = self.checkpoint()
                changed = self.config.checkpoint.stat().st_mtime_ns
            except (OSError, ValueError) as error:
                return f"checkpoint_invalid: {error}"
            if checkpoint["status"] != "running":
                if (checkpoint["status"] == "completed"
                        and self.has_unresolved_work(checkpoint)):
                    self.begin_repair("checkpoint_reconciliation")
                    return "checkpoint_reconciliation"
                self.save(last_valid_checkpoint=checkpoint)
                return checkpoint["status"]
            if changed != signature:
                signature, last_change = changed, self.clock()
                self.save(last_valid_checkpoint=checkpoint)
            if self.process.poll() is not None:
                return f"process_exit: {self.process.returncode}"
            health = self.runtime_health()
            if health is not None:
                self.save(runtime_health=health)
                # Maintenance/provider/storage states are observable holds, not
                # code-repair requests. Ordinary bounded pending waits are also
                # exempt from a naive unchanged-checkpoint watchdog.
                if health["phase"] in {"provider_blocked", "storage_pressure", "quiescing",
                                      "quiescent", "quiescence_timeout"}:
                    last_change = self.clock()
                elif (checkpoint.get("pending") or checkpoint.get("background_job")):
                    last_change = self.clock()
            if self.clock() - last_change >= self.config.hang_seconds:
                return "checkpoint_heartbeat_timeout"
            self.pause(self.config.poll_seconds)
        return "cutoff" if not self.stop_requested else "stopped"

    def runtime_health(self) -> dict | None:
        try:
            value = read_json(safety_dir(self.config.checkpoint) / "health.json")
        except (OSError, SafetyStateError):
            return None
        if (value is None or not self.state.get("execution_id")
                or value.get("execution_id") != self.state["execution_id"]
                or value.get("session_id") != self.config.session_id
                or value.get("pid") != (self.state.get("process") or {}).get("pid")
                or value.get("phase") not in {"healthy", "provider_blocked", "storage_pressure",
                                             "quiescing", "quiescent", "quiescence_timeout"}
                or type(value.get("at")) not in (int, float)
                or not 0 <= self.clock() - value["at"] <= max(15, self.config.poll_seconds * 3)):
            return None
        return value

    def recovery_class(self, reason: str) -> str:
        try:
            checkpoint = self.checkpoint()
        except (OSError, ValueError):
            checkpoint = self.state.get("last_valid_checkpoint", {})
        try:
            evidence = current_exit(self.config.checkpoint, session_id=self.config.session_id,
                                    execution_id=self.state.get("execution_id"))
        except (OSError, SafetyStateError):
            evidence = None
        return classify(reason, checkpoint, evidence)

    def block_recovery(self, reason: str, failure_class: str) -> int:
        existing = self.state.get("operational_incident") or {}
        self.transition("recovery_blocked", {
            "phase": "blocked", "operational_incident": {
                "incident_id": existing.get("incident_id", str(uuid4())),
                "failure_class": failure_class,
                "detected_at": existing.get("detected_at", self.clock()),
                "blocked_at": self.clock(), "cutoff": self.state["cutoff"],
                "pending_preserved": True,
            },
        }, reason=reason, failure_class=failure_class,
            recovery_path="operator_reconciliation", intervention_type="operational_recovery")
        return 2

    def repair_prompt(self, reason: str, result: Path) -> str:
        return f"""Repair the stopped autonomous Factorio campaign in {self.config.cwd}.
Session: {self.config.session_id}
Research run: {self.state['run_id']}
Research segment: {self.state['segment_id']}
Incident: {(self.state.get('incident') or {}).get('incident_id')}
Repair attempt: {self.state['attempt']}
Controller checkpoint: {self.config.checkpoint}
Production configuration: scheduling={self.config.factory_scheduling}, background_work={self.config.background_work}, furnace_output_buffers={self.config.furnace_output_buffers}, furnace_input_belts={self.config.furnace_input_belts}, mining_outposts={self.config.mining_outposts}, ore_side_successors={self.config.ore_side_successors}
Campaign treatment: diagnostics={self.config.campaign_diagnostics}, profile_observations={self.config.profile_observations}, consolidated_observations={self.config.consolidated_observations}, lead_time_supply={self.config.lead_time_supply}, coverage_margin_lookahead={self.config.coverage_margin_lookahead}
Supervisor audit/log directory: {self.config.state_dir}
Read {self.config.state_dir / 'OPERATIONS.md'} first if present for native session
and repository acceptance details.
Read supervisor.json incident.checkpoint and incident.source as the immutable
pre-repair baseline. Rejected repair attempts never replace that baseline.
Failure: {reason}
Absolute wallclock cutoff (Unix seconds): {self.state['cutoff']}
Do not launch gameplay or reset/recreate/reconnect the game or its FLE client.
FLE runtime handlers are ephemeral: preserve runtime and original entity identity.
Preserve this exact session and
checkpoint. Never clear ambiguous pending actions to enable a retry; reconcile
against observed game evidence and preserve the write-ahead safety contract.
Inspect logs and source, fix the root cause, add focused tests and run them.
Complete future bug fixes inside this repair loop; a diagnosis, proposal, or
unpublished patch is not a completed code repair.
Fairness is a permanent acceptance requirement for every repair: actual walking,
normal mining, standard interaction reach, and 1x game speed.
Never restore teleportation or fast movement/mining bypasses, remote interaction
beyond standard reach, scripted harvest or inventory grants, elapsed-time-only
simulation of walking/mining, or game/player speed changes.
Fix failures in the fair execution path rather than bypassing these requirements.
Add focused regression tests for affected fairness behavior and require the
independent exact-head review to inspect it. Record concrete source, test, and
available native observation evidence, distinguishing mock tests from native proof.
Do not report repaired or permit resume with a fairness regression or an
unresolved fairness concern; report blocked with the missing evidence instead.
You are authorized to make focused commits, push a repair branch, open a PR,
and merge only after required checks pass and independent exact-head source
review approves. Synchronize origin and any configured fork after merge. Never bypass checks,
force-push, expose credentials, or claim success from dispatch acknowledgement.
Complete fix, tests, independent review, publication/merge, and synchronization
before reporting a code repair accepted; the supervisor alone resumes gameplay,
within the original cutoff and with the original session and pending identity.
If any acceptance requirement cannot be completed, report blocked.
Write a JSON object to {result} with these fields:
status ("repaired" or "blocked"), kind ("code" or "operational"),
session_id, checkpoint (absolute path),
run_id, incident_id, attempt (copy the research identity above exactly),
tests_passed (boolean), checks_passed (boolean), exact_head_reviewed (boolean),
merged (boolean), remotes_synced (boolean), commit (full 40-character SHA),
pr_url (GitHub PR URL), evidence (nonempty list of evidence strings).
If GitHub has no independent approval, use a separate Codex source-review agent,
and provide independent_review (path to its JSON artifact) and repair_agent
(your unique agent/session ID). The artifact must contain head (exact PR head),
verdict ("approved"), reviewer (different agent/session ID), and source_evidence
(nonempty list of concrete source findings). Never bypass required branch rules.
For operational reconciliation requiring no code change, omit code-only fields,
set operational_verified=true and provide receipt/inventory/observation evidence.
Operational repairs must leave tracked source and HEAD unchanged. In ALL modes
leave any existing pending action unchanged: the resumed controller must verify
its postcondition from observations. Never erase ambiguity through metadata.
Also preserve its active_plan, step_index, and reservations exactly.
After fixing observation or execution logic, you may set status to running while
retaining pending exactly: controller pending verification runs before dispatch.
Do not silently clear failure budgets or manufacture progress.
Only report repaired when every acceptance requirement is verified.
"""

    def capture(self, command: list[str]) -> tuple[int | None, str]:
        log = self.config.state_dir / "verification.log"
        offset = log.stat().st_size if log.exists() else 0
        self.launch(command, "verification")
        if self.process is None:
            return None, ""
        deadline = min(self.clock() + self.config.repair_seconds, self.state["cutoff"])
        while self.clock() < deadline and not self.stop_requested and self.process.poll() is None:
            self.pause(min(self.config.poll_seconds, 0.25, deadline - self.clock()))
        returncode = self.process.poll()
        self.stop_process()
        with log.open("rb") as stream:
            stream.seek(offset)
            return returncode, stream.read().decode(errors="replace").strip()

    def source_identity(self) -> tuple[str, str] | None:
        head_code, head = self.capture(["git", "rev-parse", "HEAD"])
        diff_code, diff = self.capture(["git", "diff", "HEAD", "--"])
        status_code, status = self.capture(["git", "status", "--porcelain"])
        files_code, files = self.capture(["git", "ls-files", "-z", "--cached", "--others",
                                         "--exclude-standard"])
        if not head_code == diff_code == status_code == files_code == 0:
            return None
        digest = hashlib.sha256()
        for name in sorted(set(files.split("\0")) - {""}):
            path = self.config.cwd / name
            digest.update(name.encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        if self.remaining() <= 0 or self.stop_requested:
                            return None
                        digest.update(chunk)
            else:
                digest.update(b"<missing>")
        return head, diff + "\n" + status + "\n" + digest.hexdigest()

    def independent_review(self, result: dict, head: str) -> bool:
        path = result.get("independent_review")
        if not isinstance(path, str) or not result.get("repair_agent"):
            return False
        review_path = Path(path).resolve()
        if not review_path.is_relative_to(self.config.state_dir.resolve()):
            return False
        review = json.loads(review_path.read_text())
        evidence = review.get("source_evidence")
        return (review.get("head") == head and review.get("verdict") == "approved"
                and isinstance(review.get("reviewer"), str) and bool(review["reviewer"])
                and review["reviewer"] != result["repair_agent"]
                and isinstance(evidence, list) and bool(evidence)
                and all(isinstance(item, str) and item.strip() for item in evidence))

    def verify_code(self, result: dict) -> bool:
        commit = result["commit"]
        code, head = self.capture(["git", "rev-parse", "HEAD"])
        if code != 0 or head != commit:
            return False
        code, worktree = self.capture(["git", "status", "--porcelain"])
        if code != 0 or worktree:
            return False
        code, configured = self.capture(["git", "remote"])
        if code != 0 or "origin" not in configured.splitlines():
            return False
        for remote in ("origin", "fork"):
            if remote == "fork" and remote not in configured.splitlines():
                continue
            code, reference = self.capture(["git", "ls-remote", remote, "refs/heads/main"])
            if code != 0 or reference.split() != [commit, "refs/heads/main"]:
                return False
        url = result.get("pr_url", "")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            return False
        code, raw = self.capture([
            "gh", "pr", "view", url, "--json",
            "state,headRefOid,mergeCommit,reviews,statusCheckRollup",
        ])
        if code != 0:
            return False
        pull = json.loads(raw)
        if pull["state"] != "MERGED" or pull["mergeCommit"]["oid"] != commit:
            return False
        reviews = pull.get("reviews", [])
        latest = {}
        for review in reviews:
            latest[review.get("author", {}).get("login")] = review
        approved = any(
            review.get("state") == "APPROVED"
            and review.get("commit", {}).get("oid") == pull["headRefOid"]
            for review in latest.values()
        )
        if (not approved and not self.independent_review(result, pull["headRefOid"])
                or any(review.get("state") == "CHANGES_REQUESTED" for review in latest.values())):
            return False
        checks = pull.get("statusCheckRollup", [])
        if not checks or not all(
            check.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"}
            or check.get("state") == "SUCCESS" for check in checks
        ):
            return False
        if "prevalidation" in result:
            # Run with the configured interpreter: the verifier must have the
            # same runtime/dependencies as the pre-maintenance full suite.
            reference = result["prevalidation"]
            if (not isinstance(reference, str) or len(reference) != 64
                    or any(character not in "0123456789abcdef" for character in reference)):
                return False
            code, _ = self.capture([self.config.python, "-m", "jev_factorio.prevalidation", "check",
                                    "--state-dir", str(self.config.state_dir), "--artifact-id", reference])
        else:
            code, _ = self.capture([self.config.python, "-m", "pytest", "tests/"])
        if code != 0:
            return False
        status_code, worktree = self.capture(["git", "status", "--porcelain"])
        head_code, head = self.capture(["git", "rev-parse", "HEAD"])
        return status_code == head_code == 0 and not worktree and head == commit

    def validate_repair(self, path: Path, previous: dict,
                        source_before: tuple[str, str] | None = None) -> bool:
        try:
            result = json.loads(path.read_text())
            expected = {"run_id": self.state.get("run_id"),
                        "incident_id": (self.state.get("incident") or {}).get("incident_id"),
                        "attempt": self.state.get("attempt")}
            if any(key in result and result[key] != value for key, value in expected.items()):
                return False
            if result.get("status") != "repaired":
                return False
            if result.get("session_id") != self.config.session_id:
                return False
            if result.get("checkpoint") != str(self.config.checkpoint.resolve()):
                return False
            evidence = result.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence
            ):
                return False
            current = self.checkpoint()
            if current["status"] not in {"running", "completed"}:
                return False
            if current["status"] == "completed" and self.has_unresolved_work(current):
                return False
            if previous.get("pending"):
                if any(current.get(key) != previous.get(key) for key in (
                    "pending", "active_plan", "step_index", "reservations", "attempt"
                )):
                    return False
            if current.get('connection_failure_attribution', {}) != previous.get('connection_failure_attribution', {}):
                return False
            extension_keys = ("background_schema", "background_job", "background_attempt",
                              "output_buffers_schema", "output_commitments",
                              "input_routes_schema", "input_commitments", "outposts_schema", "outpost_commitments",
                              "successor_schema", "successor_projects", "successor_receipts")
            if any(key in previous and current.get(key) != previous[key]
                   for key in extension_keys):
                return False
            if (previous.get("background_job") or previous.get("output_commitments")
                    or previous.get("input_commitments") or previous.get("outpost_commitments")
                    or previous.get("successor_projects")):
                if any(current.get(key) != previous.get(key)
                       for key in ("active_plan", "step_index", "reservations")):
                    return False
                history = previous.get("history", [])
                if current.get("history", [])[:len(history)] != history:
                    return False
            if current.get("attempt_outcomes") != previous.get("attempt_outcomes"):
                return False
            if any(current.get("failures", {}).get(key, 0) < count
                   for key, count in previous.get("failures", {}).items()):
                return False
            if result.get("kind") == "operational":
                return (result.get("operational_verified") is True
                        and source_before is not None
                        and self.source_identity() == tuple(source_before))
            if result.get("kind") != "code" or not all(result.get(key) is True for key in (
                "tests_passed", "checks_passed", "exact_head_reviewed", "merged", "remotes_synced"
            )):
                return False
            commit = result.get("commit", "")
            if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
                return False
            return self.verify_code(result)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return False

    def begin_repair(self, reason: str) -> None:
        if self.state.get("repair_required"):
            return
        try:
            previous = self.checkpoint()
        except (OSError, ValueError):
            previous = self.state.get("last_valid_checkpoint")
            if previous is None:
                raise
        if not self.transition("incident_started", {
            "repair_required": True,
            "incident": {"incident_id": str(uuid4()), "reason": reason,
                         "checkpoint": previous, "source": None,
                         "code_revision": self.state.get("code_revision")},
        }, reason=reason, checkpoint_sha256=digest_json(previous)):
            return
        incident = {**self.state["incident"], "source": self.source_identity()}
        self.save(incident=incident)
        self.event("repair_required", reason=reason)

    def close_interrupted_attempt(self) -> None:
        if not self.state.get("repair_attempt_open"):
            return
        incident_id = self.state.get("attempt_incident_id")
        revision = self.snapshot_revision()
        if not self.record_revision(revision, "interrupted_repair",
                                    incident_id=incident_id, attempt=self.state["attempt"],
                                    accepted=False, actor_type="unknown",
                                    intervention_type="repair_attempt"):
            return
        self.transition("repair_interrupted", {"repair_attempt_open": False},
                        incident_id=incident_id, attempt=self.state["attempt"],
                        accepted=False, outcome="unknown", intervention_type="repair_attempt",
                        source_before=self.state.get("attempt_source_before"),
                        source_after=revision)

    def repair(self, reason: str) -> bool:
        self.close_interrupted_attempt()
        self.begin_repair(reason)
        if self.stop_requested:
            return False
        incident = self.state["incident"]
        if self.state.get("repair_account_blocked"):
            return False
        incident_attempts = (self.state.get("incident_repair_attempts", 0)
                             if self.state.get("repair_budget_incident_id") == incident["incident_id"] else 0)
        if incident_attempts >= self.config.max_repair_attempts:
            self.block_recovery(reason, "source_defect")
            return False
        previous, source_before = incident["checkpoint"], incident["source"]
        attempt = self.state["attempt"] + 1
        attempt_source_before = self.snapshot_revision()
        if not self.transition("repair_started", {
            "attempt": attempt, "repair_attempt_open": True,
            "repair_budget_incident_id": incident["incident_id"],
            "incident_repair_attempts": incident_attempts + 1,
            "attempt_incident_id": incident["incident_id"],
            "attempt_source_before": attempt_source_before,
        }, attempt=attempt, actor_type="repair_agent", source_before=attempt_source_before):
            return False
        result = self.config.state_dir / f"repair-{attempt}.json"
        prompt = self.config.state_dir / f"repair-{attempt}.txt"
        prompt.write_text(self.repair_prompt(reason, result))
        repair_log = self.config.state_dir / "repair.log"
        repair_offset = repair_log.stat().st_size if repair_log.exists() else 0
        repair_started_at = self.clock()
        self.launch(self.config.repair_command, "repair", prompt)
        if self.process is None:
            return False
        deadline = min(self.clock() + self.config.repair_seconds, self.state["cutoff"])
        while (self.clock() < deadline and not self.stop_requested
               and self.process.poll() is None):
            self.pause(min(self.config.poll_seconds, deadline - self.clock()))
        returncode = self.process.poll()
        self.stop_process()
        try:
            raw_result = result.read_bytes()
            report = json.loads(raw_result)
            if not isinstance(report, dict):
                report = {}
        except (OSError, ValueError):
            raw_result, report = b"", {}
        quota_blocked = repair_quota_exhausted(repair_log, repair_offset)
        validation_started_at = self.clock()
        accepted = (not quota_blocked and returncode == 0
                    and self.validate_repair(result, previous, source_before))
        validation_seconds = self.clock() - validation_started_at
        # Evidence fingerprints must refer to the artifact that was validated,
        # not a replacement written during Git/test verification.
        if accepted:
            try:
                accepted = result.read_bytes() == raw_result
            except OSError:
                accepted = False
        revision = self.snapshot_revision()
        declared = report.get("kind")
        if not isinstance(declared, str) or declared not in {"code", "operational"}:
            declared = None
        if (accepted and declared == "operational" and incident.get("code_revision") is not None
                and revision is not None and incident["code_revision"] != revision):
            accepted = False
        if accepted and declared == "code" and (revision is None or revision["commit"] != report.get("commit")):
            accepted = False
        accepted = bool(accepted and not self.stop_requested)
        intervention = ("code_repair" if accepted and declared == "code" else
                        "operational_recovery" if accepted else "repair_attempt")
        if not self.record_revision(revision, "repair_attempt", attempt=attempt,
                                    incident_id=incident["incident_id"], accepted=accepted,
                                    actor_type="repair_agent", intervention_type=intervention):
            return False
        updates = {"repair_attempt_open": False}
        if quota_blocked:
            updates.update(repair_account_blocked=True, phase="blocked")
        if accepted:
            updates.update(repair_required=False, incident=None,
                           last_valid_checkpoint=self.checkpoint(), phase="ready")
        durable = self.transition("repair_finished", updates, attempt=attempt,
            incident_id=incident["incident_id"], returncode=returncode, accepted=accepted,
            declared_kind=declared, intervention_type=intervention, actor_type="repair_agent",
            source_before=attempt_source_before, source_after=revision,
            result_file=result.name, result_sha256=hashlib.sha256(raw_result).hexdigest() if raw_result else None,
            validation_seconds=validation_seconds,
            repair_seconds=self.clock() - repair_started_at,
            repair_account_blocked=quota_blocked,
            correlation_complete=all(key in report for key in ("run_id", "incident_id", "attempt")))
        return accepted and durable

    def run(self, manual_intervention: dict | None = None) -> int:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path().open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another supervisor owns this repository") from error
            if manual_intervention is not None and not self.state_path.exists():
                raise ValueError("Manual intervention requires an existing supervised run")
            self.initialize(record_only=manual_intervention is not None)
            if manual_intervention is not None:
                self.record_manual_intervention(manual_intervention)
                return 1 if self.stop_requested else 0
            if self.state.get("phase") == "blocked" or self.state.get("repair_account_blocked"):
                return 2
            failures = 0
            try:
                while self.remaining() > 0 and not self.stop_requested:
                    if self.state.get("repair_required"):
                        reason = self.state["incident"]["reason"]
                    else:
                        try:
                            reason = self.watch_game()
                        except (OSError, ValueError) as error:
                            reason = f"gameplay_error: {error}"
                        finally:
                            self.stop_process()
                    self.event("gameplay_stopped", reason=reason)
                    if reason == "completed":
                        self.save(phase="completed")
                        return 1 if self.audit_failed else 0
                    if reason in {"cutoff", "stopped"}:
                        break
                    failure_class = self.recovery_class(reason)
                    if failure_class != "source_defect":
                        return self.block_recovery(reason, failure_class)
                    self.begin_repair(reason)
                    accepted = False
                    while self.remaining() > 0 and not self.stop_requested and not accepted:
                        try:
                            accepted = self.repair(reason)
                        except (OSError, ValueError) as error:
                            self.event("repair_error", error=str(error))
                        finally:
                            self.stop_process()
                            self.close_interrupted_attempt()
                        if self.state.get("repair_account_blocked"):
                            return self.block_recovery(reason, "account_quota_blocked")
                        if (not accepted and self.state.get("incident_repair_attempts", 0)
                                >= self.config.max_repair_attempts):
                            return self.block_recovery(reason, "source_defect")
                        failures = 0 if accepted else min(failures + 1, 6)
                        if not accepted:
                            self.pause(min(900, self.config.backoff_seconds * 2 ** failures))
                self.save(phase="stopped" if self.stop_requested else "cutoff")
                self.event(self.state["phase"])
                return 1 if self.audit_failed else 0
            finally:
                self.stop_process()

    @staticmethod
    def lock_path() -> Path:
        return Path.home() / ".jev-factorio-supervisor.lock"


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-id", help="Shared research run ID; immutable once assigned")
    parser.add_argument("--run-manifest", type=Path,
                        help="Read an existing manifest's run_id without modifying it")
    parser.add_argument("--research-dir", type=Path,
                        help="Create a separate exclusive evidence directory for every gameplay invocation")
    parser.add_argument("--record-manual-intervention", type=Path,
                        help="Audit a human intervention JSON report while stopped; do not run gameplay")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--started-at", type=float, required=True)
    parser.add_argument("--repair-command-json", required=True)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--duration-hours", type=float, default=12)
    parser.add_argument("--hang-seconds", type=float, default=600)
    parser.add_argument("--repair-seconds", type=float, default=1800)
    parser.add_argument("--max-repair-attempts", type=int, default=3)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--backoff-seconds", type=float, default=30)
    parser.add_argument("--tick-seconds", type=float, default=1)
    parser.add_argument("--factory-scheduling", choices=("serial", "ready-work"), default="serial")
    parser.add_argument("--background-work", action="store_true")
    parser.add_argument("--furnace-output-buffers", action="store_true")
    parser.add_argument("--furnace-input-belts", action="store_true")
    parser.add_argument("--mining-outposts", action="store_true")
    parser.add_argument("--ore-side-successors", action="store_true")
    parser.add_argument("--production-treatment", type=Path)
    for name in ("campaign-diagnostics", "profile-observations", "consolidated-observations",
                 "lead-time-supply", "coverage-margin-lookahead"):
        parser.add_argument("--" + name, action="store_true")
    arguments = vars(parser.parse_args())
    try:
        manual_path = arguments.pop("record_manual_intervention")
        manual = json.loads(manual_path.read_text(encoding="utf-8")) if manual_path else None
        if manual_path and not isinstance(manual, dict):
            raise ValueError("Manual intervention report must be an object")
        arguments["repair_command"] = json.loads(arguments.pop("repair_command_json"))
        for key in ("state_dir", "checkpoint", "cwd"):
            arguments[key] = arguments[key].resolve()
        supervisor = Supervisor(SupervisorConfig(**arguments))
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))

    def stop(signum, frame) -> None:
        supervisor.stop_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    raise SystemExit(supervisor.run(manual_intervention=manual))


if __name__ == "__main__":
    cli()
