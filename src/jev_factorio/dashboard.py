"""Read-only live workflow dashboard. No game, provider, or web dependencies.

Display telemetry is deliberately separate from research evidence: bounded,
redacted, best-effort, and never an authority to dispatch or verify an action.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import dashboard_mission, research_catalog
from .wait_record_codec import (
    Decoder as WaitRecordDecoder, Encoder as WaitRecordEncoder,
    decision_anchor_candidate, encode_line as encode_wait_line,
    parse_json as parse_wait_json, persistent_wait,
)

SCHEMA = "jev.dashboard.v1"
MAX_LINE = 262144
ASSETS = Path(__file__).with_name("dashboard_assets")
ICON_NAME = re.compile(r"/icons/([a-z0-9][a-z0-9-]{0,63})\.png")
SECRET_KEY = re.compile(r"password|secret|authorization|api.?key|access.?token|refresh.?token", re.I)
URL = re.compile(r"(?:https?|wss?)://[^\s\"<>]+", re.I)
CREDENTIAL = re.compile(r"(?:Bearer\s+\S+|\b(?:sk|ts|pk)-[\w-]{12,})", re.I)
RECORD_KEYS = ("controller", "policy", "tick", "session_id", "world_kind", "goal", "target",
               "status", "reason", "action", "outcome", "verified", "completed_goals",
               "decision", "model_call", "requested_model", "resolved_model", "usage", "pending",
               "persistent_recovery", "code_revision", "process_id", "run_id", "segment_id",
               "execution_id")
STATE_KEYS = ("tick", "session_id", "world_kind", "inventory", "player_position", "nearby_resources",
              "placed_entities", "drill_status", "drill_fuel", "drill_output_connected",
              "iron_ore_collected", "production_rates", "researched", "victory", "victory_source")
MISSION_GATE_KEYS = ("contract", "pad", "payload", "rocket", "cargo", "request", "victory")
MISSION_GATE_STATES = frozenset(("unknown", "pending", "observed", "blocked", "submitted"))
MISSION_AUTOMATION_FAMILIES = frozenset(("input_routes", "production_sites", "successors"))
MISSION_AUTOMATION_PER_FAMILY_LIMIT = 6
MISSION_AUTOMATION_TOTAL_LIMIT = (
    MISSION_AUTOMATION_PER_FAMILY_LIMIT * len(MISSION_AUTOMATION_FAMILIES)
)
MISSION_AUTOMATION_SCOPE = "At most six roles per capability; absent rows are unknown."


def secret_values() -> tuple[str, ...]:
    """Use known secrets for redaction, never export environment keys or values."""
    return tuple(value for key, value in os.environ.items() if SECRET_KEY.search(key) and len(value) >= 4)


def sanitize(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Detach JSON data with a total node/depth budget and visible omissions."""
    remaining = [3000]

    def visit(item: Any, depth: int = 0) -> Any:
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 12:
            return "[display limit]"
        if item is None or type(item) in (bool, int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[redacted]")
            item = CREDENTIAL.sub("[redacted]", URL.sub("[URL redacted]", item))
            return item[:2000] + (" [truncated]" if len(item) > 2000 else "")
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 128 or remaining[0] < 0:
                    result["_display_truncated"] = True
                    break
                if not isinstance(key, str):
                    continue
                clean_key = visit(key, depth + 1)
                result[clean_key] = "[redacted]" if SECRET_KEY.search(key) else visit(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = [visit(child, depth + 1) for child in item[:128] if remaining[0] >= 0]
            if len(item) > len(result):
                result.append("[display limit]")
            return result
        return "[unsupported display value]"

    return visit(value)


def project_state(value: dict) -> dict:
    return {"mission": dashboard_mission.project_state(value),
            **{key: value[key] for key in STATE_KEYS if key in value}}


def has_coherent_research_observation(value: Any) -> bool:
    """Whether a snapshot carries a bounded, tick-bound researched list."""
    if not isinstance(value, dict) or not dashboard_mission.integer(value.get("tick")):
        return False
    researched = value.get("researched")
    return (isinstance(researched, list)
            and len(researched) <= research_catalog.MAX_TECHNOLOGIES
            and all(isinstance(name, str) and research_catalog.NAME.fullmatch(name)
                    for name in researched))


def _has_bounded_mission_research(value: Any) -> bool:
    if not isinstance(value, dict) or "name" not in value or "progress" not in value:
        return False
    name = value["name"]
    if name is not None and (not isinstance(name, str) or len(name) > 160):
        return False
    progress = value["progress"]
    if progress is None:
        return True
    if type(progress) is int:
        return 0 <= progress <= 1
    return type(progress) is float and math.isfinite(progress) and 0 <= progress <= 1


def has_coherent_mission_projection(value: Any) -> bool:
    """Whether a complete projected mission is bound to its outer snapshot."""
    if not isinstance(value, dict) or not dashboard_mission.integer(value.get("tick")):
        return False
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 160:
        return False
    mission = value.get("mission")
    if (not isinstance(mission, dict) or type(mission.get("schema")) is not int
            or mission["schema"] != 1):
        return False

    launch = mission.get("launch")
    if (not isinstance(launch, dict) or type(launch.get("schema")) is not int
            or launch["schema"] != 1 or not dashboard_mission.integer(launch.get("tick"))
            or launch["tick"] != value["tick"] or launch.get("session_id") != session_id
            or not isinstance(launch.get("headline"), str) or len(launch["headline"]) > 160
            or launch.get("basis") != "Recorded snapshot only; never a launch command or deployment approval."
            or type(launch.get("evidence_valid")) is not bool
            or launch.get("fault") is not None and type(launch.get("fault")) is not bool):
        return False
    gates = launch.get("gates")
    if not isinstance(gates, list) or len(gates) != len(MISSION_GATE_KEYS):
        return False
    for key, row in zip(MISSION_GATE_KEYS, gates):
        if (not isinstance(row, dict) or row.get("key") != key
                or not isinstance(row.get("title"), str) or len(row["title"]) > 160
                or not isinstance(row.get("state"), str) or row["state"] not in MISSION_GATE_STATES
                or not isinstance(row.get("detail"), str) or len(row["detail"]) > 160):
            return False

    research = mission.get("research")
    if not _has_bounded_mission_research(research):
        return False

    automation = mission.get("automation")
    if (mission.get("automation_scope") != MISSION_AUTOMATION_SCOPE
            or not isinstance(automation, list) or len(automation) > MISSION_AUTOMATION_TOTAL_LIMIT):
        return False
    family_counts: dict[str, int] = {}
    for row in automation:
        if (not isinstance(row, dict) or not isinstance(row.get("family"), str)
                or row["family"] not in MISSION_AUTOMATION_FAMILIES
                or row.get("role") is not None
                and (not isinstance(row["role"], str) or len(row["role"]) > 160)
                or row.get("state") is not None
                and (not isinstance(row["state"], str) or len(row["state"]) > 160)
                or row.get("reason") is not None
                and (not isinstance(row["reason"], str) or len(row["reason"]) > 160)
                or row.get("survey_tick") is not None
                and not dashboard_mission.integer(row["survey_tick"])
                or row.get("cached") is not None and type(row["cached"]) is not bool):
            return False
        family = row["family"]
        family_counts[family] = family_counts.get(family, 0) + 1
        if family_counts[family] > MISSION_AUTOMATION_PER_FAMILY_LIMIT:
            return False
    return True


def without_incoherent_mission(value: Any) -> Any:
    """Do not render stale gates beside current state; keep only tick-bound research."""
    if not isinstance(value, dict) or "mission" not in value or has_coherent_mission_projection(value):
        return value
    mission = value.get("mission")
    launch = mission.get("launch") if isinstance(mission, dict) else None
    session_id = value.get("session_id")
    same_snapshot_identity = (
        type(value.get("tick")) is int
        and dashboard_mission.integer(value.get("tick"))
        and isinstance(launch, dict)
        and type(launch.get("schema")) is int and launch["schema"] == 1
        and dashboard_mission.integer(launch.get("tick"))
        and launch["tick"] == value["tick"]
        and ((session_id is None and launch.get("session_id") is None)
             or isinstance(session_id, str) and 1 <= len(session_id) <= 160
             and launch.get("session_id") == session_id)
    )
    if (has_coherent_research_observation(value) and same_snapshot_identity
            and isinstance(mission, dict) and type(mission.get("schema")) is int
            and mission["schema"] == 1 and _has_bounded_mission_research(mission.get("research"))):
        # Research has its own validated tick-bound evidence. The launch summary
        # and automation rows also need the full outer session binding above.
        safe = dict(value)
        safe["mission"] = {"schema": 1, "research": mission["research"]}
        return safe
    return {key: child for key, child in value.items() if key != "mission"}


def project_record(value: dict) -> dict:
    record = {"mission_record": dashboard_mission.project_record(value)}
    has_after_state = "after_state" in value
    state = value.get("after_state") if has_after_state else value.get("state")
    if has_after_state and not has_coherent_research_observation(state):
        # This internal hint keeps a separately exported catalog from filling
        # in current research after an explicit incomplete observation.
        record["_dashboard_research_observation_incomplete"] = True
    if has_after_state and (not isinstance(state, dict) or not state):
        # An explicit incomplete observation clears the current display. In
        # particular, do not carry forward the pre-action state or research.
        record["state"] = {}
    if isinstance(state, dict):
        if not has_after_state and not isinstance(state.get("factory"), dict):
            # Legacy records keep factory facts (current research) with the decision.
            decision = value.get("decision")
            facts = decision.get("state", {}).get("facts") if isinstance(decision, dict) else None
            factory = facts.get("factory") if isinstance(facts, dict) else None
            if isinstance(factory, dict):
                state = dict(state, factory={key: factory[key] for key in ("research", "research_progress")
                                             if key in factory})
        if not has_after_state or state:
            record["state"] = project_state(state)
    # Keep the bounded observation before potentially large model decision data.
    record.update({key: value[key] for key in RECORD_KEYS
                   if key in value and key != "persistent_recovery"})
    recovery = value.get("persistent_recovery")
    if isinstance(recovery, dict):
        phase = recovery.get("phase")
        record["persistent_recovery"] = {
            "phase": phase if phase in {
                "waiting_for_changed_game_evidence", "evaluating_changed_game_evidence",
                "selected_plan_entered_normal_execution", "provider_blocked",
                "evaluation_outcome_unknown_waiting", "alternatives_exhausted_waiting",
                "idle_wait_exhausted",
            } else "unknown",
            "reason": recovery.get("reason") if isinstance(recovery.get("reason"), str) else None,
            "next_observation_seconds": (
                recovery.get("next_observation_seconds")
                if type(recovery.get("next_observation_seconds")) in (int, float)
                and math.isfinite(recovery["next_observation_seconds"])
                and recovery["next_observation_seconds"] >= 0 else None),
            "model_call": recovery.get("model_call") if type(recovery.get("model_call")) is bool else None,
            "recorded_attempts": (
                recovery.get("recorded_attempts")
                if type(recovery.get("recorded_attempts")) is int
                and recovery["recorded_attempts"] >= 0 else None),
        }
    elif "persistent_recovery" in value:
        record["persistent_recovery"] = None
    return record


class EventWriter:
    """One opt-in writer per controller invocation; append restart boundaries.

    Opening/validating an output fails before backend creation. Later telemetry
    failures disable this optional observer, not the controller. No fsync/audit
    durability or cross-process writer concurrency guarantee is claimed.
    """

    def __init__(self, path: str | Path, forbidden: tuple[str | Path | None, ...] = ()):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("Dashboard output must not be a symlink")
        for other in forbidden:
            if other and (self.path.resolve() == Path(other).resolve() or (
                self.path.exists() and Path(other).exists() and self.path.samefile(other)
            )):
                raise ValueError("Dashboard output must be separate from logs and checkpoints")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if not self.path.is_file():
                raise ValueError("Dashboard output must be a regular file")
            with self.path.open("rb") as existing:
                first = existing.readline(MAX_LINE + 1)
                if first:
                    try:
                        valid = len(first) <= MAX_LINE and json.loads(first).get("schema") == SCHEMA
                    except (ValueError, AttributeError, RecursionError):
                        valid = False
                    if not valid:
                        raise ValueError("Refusing to append dashboard events to an unrelated file")
                    existing.seek(-1, os.SEEK_END)
                    if existing.read(1) != b"\n":
                        raise ValueError("Dashboard output has an incomplete tail; use a new file")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(self.path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(self.fd).st_mode):
            os.close(self.fd)
            raise ValueError("Dashboard output must be a regular file")
        self.run_id = uuid.uuid4().hex
        self.seq = 0
        self.disabled = False
        self.lock = threading.Lock()
        self.secrets = secret_values()
        self._wait_record_encoder = WaitRecordEncoder("dashboard")

    def emit(self, kind: str, stage: int, **data: Any) -> None:
        with self.lock:
            if self.disabled or self.fd is None:
                return
            try:
                self.seq += 1
                now = time.time()
                event = {"schema": SCHEMA, "run_id": self.run_id, "seq": self.seq,
                         "time": now, "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                         "kind": kind, "stage": stage, "data": sanitize(data, self.secrets)}
                prepared = None
                if kind == "decision_recorded":
                    prepared = self._wait_record_encoder.prepare(
                        event, wait=persistent_wait(event, "dashboard"), anchor_candidate=True)
                if prepared is not None and prepared.is_delta:
                    encoded = encode_wait_line(prepared)
                else:
                    encoded = (json.dumps(event, allow_nan=False, ensure_ascii=True) + "\n").encode()
                if len(encoded) > MAX_LINE:
                    event["data"] = {"_display_truncated": "Event exceeds dashboard byte limit"}
                    encoded = (json.dumps(event) + "\n").encode()
                    prepared = None
                if os.write(self.fd, encoded) != len(encoded):
                    raise OSError("short telemetry write")
                if prepared is not None:
                    self._wait_record_encoder.commit(prepared, len(encoded))
                elif kind == "decision_recorded":
                    self._wait_record_encoder.reset()
                else:
                    self._wait_record_encoder.skip(len(encoded))
            except Exception:
                self.disabled = True
                print("Dashboard telemetry disabled after a display-write failure; gameplay is unchanged.",
                      file=sys.stderr, flush=True)

    def __enter__(self) -> EventWriter:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.emit("run_finished", 2, outcome="error" if exc_type else "returned")
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def attach(loop: Any, writer: EventWriter) -> None:
    """Decorate only this loop instance; delegate every existing call exactly once.

    No global patch, extra observation, policy/predicate evaluation, checkpoint
    write, provider call, thread, or action is introduced. The tiny synchronous
    observer has unmeasured wall-clock overhead. It is not research logging.
    """
    if getattr(loop, "_dashboard_attached", False):
        raise ValueError("Dashboard observer is already attached")
    loop._dashboard_attached = True

    def emit(kind: str, stage: int, **data: Any) -> None:
        # Projection failures must also remain outside the gameplay error path.
        try:
            writer.emit(kind, stage, **data)
        except Exception:
            pass

    def measured(kind: str, stage: int, function: Callable, *args, **kwargs):
        emit(kind + "_started", stage)
        started = time.perf_counter_ns()
        try:
            result = function(*args, **kwargs)
        except BaseException:
            elapsed = (time.perf_counter_ns() - started) / 1e6
            emit(kind + "_failed", stage, duration_ms=elapsed)
            raise
        elapsed = (time.perf_counter_ns() - started) / 1e6
        emit(kind + "_returned", stage, duration_ms=elapsed)
        return result

    class BackendObserver:
        def __init__(self, backend):
            self.backend = backend
            # Async controller admission keys known MockBackend instances by
            # their concrete session and transport. Keep that identity visible
            # through this observer only for the explicitly supported mock
            # type; an arbitrary wrapped backend must still fail closed.
            from .backends.mock import MockBackend
            if isinstance(backend, MockBackend):
                self.actor_unit = 0
                self._shared = backend

        def __getattr__(self, key):
            value = getattr(self.backend, key)
            if key == "execute_traced":
                def execute_traced(action, parameters, trace):
                    emit("action", 6, action=action, parameters=parameters)
                    return measured("dispatch", 6, value, action, parameters, trace)
                return execute_traced
            return value

        def act(self, action):
            emit("action", 6, action=action)
            return measured("dispatch", 6, self.backend.act, action)

        def execute(self, action, parameters):
            emit("action", 6, action=action, parameters=parameters)
            return measured("dispatch", 6, self.backend.execute, action, parameters)

    class ModelObserver:
        def __init__(self, model):
            self.model_client = model

        def __getattr__(self, key):
            return getattr(self.model_client, key)

        def evaluate(self, state, questions):
            emit("model_request", 5, candidates=state.get("candidate_plans", {}), questions=questions)
            result = measured("model", 5, self.model_client.evaluate, state, questions)
            emit("model_response", 5, answers=result, usage=getattr(self.model_client, "last_usage", None),
                 model=getattr(self.model_client, "last_model", None))
            return result

    loop.backend = BackendObserver(loop.backend)
    if loop.jev is not None:
        loop.jev = ModelObserver(loop.jev)

    def decorate(name: str, wrapper: Callable) -> None:
        original = getattr(loop, name)
        setattr(loop, name, lambda *args, **kwargs: wrapper(original, *args, **kwargs))

    def observe(original, *args, **kwargs):
        snapshot = measured("observation", 2, lambda: original(*args, **kwargs))
        try:
            emit("observation", 2, state=project_state(snapshot.for_jev()))
        except Exception:
            pass
        return snapshot

    def refresh(original, snapshot):
        result = original(snapshot)
        memory = loop.memory
        emit("goals", 3, goal=memory.active_goal, completed_goals=dict(memory.completed_goals),
             target=loop.target, status=memory.status)
        if memory.active_plan is None and not loop.terminal:
            # Controller is about to compile; not proof a plan has been produced.
            emit("planning", 4 if loop.catalog is not None else 3)
        return result

    def save(original):
        result = original()
        try:
            memory = loop.memory
            emit("controller_state", 2, plan=memory.active_plan, step_index=memory.step_index,
                 pending=memory.pending, status=memory.status, goal=memory.active_goal,
                 decision=asdict(loop._decision) if loop._decision else None,
                 completed_goals=dict(memory.completed_goals))
        except Exception:
            pass
        return result

    def verify(original, snapshot):
        return measured("verification", 7, original, snapshot)

    def record(original, *args, **kwargs):
        result = original(*args, **kwargs)
        try:
            emit("decision_recorded", 7, record=project_record(result))
        except Exception:
            pass
        return result

    def step(original):
        emit("cycle_started", 2)
        return measured("cycle", 2, original)

    decorate("_observe", observe)
    decorate("_refresh_goals", refresh)
    decorate("_save", save)
    decorate("_verify_pending", verify)
    decorate("_record", record)
    decorate("step", step)
    emit("run_started", 2, target=loop.target, policy=loop.policy,
         model=getattr(loop.jev, "model", None), controller="hierarchical")


def open_regular(path: Path):
    """Read only regular files; replacements with FIFOs must not block the viewer."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


class Tail:
    """Bounded incremental JSONL reader, tolerant of a writer's partial last line."""

    def __init__(self, path: Path):
        self.path, self.offset, self.identity = path, 0, None
        self.pending = b""
        self.dropping = False
        self.last_sizes: list[int] = []
        self.status = "waiting"
        self.invalid = 0
        self.reset = False
        self.mtime = None

    def poll(self) -> list[dict]:
        self.reset = False
        self.last_sizes = []
        try:
            with open_regular(self.path) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("not a regular file")
                identity = (info.st_dev, info.st_ino)
                if identity != self.identity or info.st_size < self.offset:
                    self.identity, self.offset, self.pending = identity, 0, b""
                    self.dropping, self.reset = False, True
                    if info.st_size > 2 * 1024 * 1024:
                        self.offset = info.st_size - 2 * 1024 * 1024
                        self.dropping = True
                stream.seek(self.offset)
                chunk = stream.read(MAX_LINE)
                self.offset += len(chunk)
                self.mtime = info.st_mtime
                self.status = "reading" if self.offset < info.st_size else "following"
        except OSError:
            self.status = "unavailable" if self.identity else "waiting"
            return []
        self.pending += chunk
        records = []
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if self.dropping:
                self.dropping = False
                continue
            if not line.strip():
                continue
            try:
                if len(line) > MAX_LINE:
                    raise ValueError("oversized")
                value = parse_wait_json(line)
                if not isinstance(value, dict):
                    raise ValueError("non-object")
                records.append(value)
                self.last_sizes.append(len(line) + 1)
            except (ValueError, UnicodeError, RecursionError):
                self.invalid += 1
        if len(self.pending) > MAX_LINE:
            self.pending = b""
            self.dropping = True
            self.invalid += 1
        return records


class Monitor:
    """Single reader/reducer shared by all SSE clients; never imports gameplay."""

    def __init__(self, path: Path, legacy: bool = False, supervisor: Path | None = None,
                 research: Path | None = None):
        self.tail, self.legacy, self.supervisor = Tail(path), legacy, supervisor
        self.research_path = research
        self.research_identity = None
        self.research_tree: research_catalog.Tree | None = None
        self.research_state: dict | None = None
        self.research_status = "not configured" if research is None else "waiting"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.secrets = secret_values()
        self.version = 0
        self.events: deque[dict] = deque(maxlen=120)
        self.view: dict = {}
        self.supervision: dict = {}
        self.last_run = None
        self.last_seq = 0
        self.rejected = 0
        self._dashboard_decoder = WaitRecordDecoder("dashboard")
        self._legacy_decoder = WaitRecordDecoder("gameplay")
        self.reconstruction_status = "waiting_for_anchor"
        self._tail_invalid_seen = 0
        # None until the first observation; research already done then has no known tick.
        self.research_seen: dict[str, int | None] | None = None
        # Once an explicit gap is seen, a run/log reset alone cannot make an
        # older sidecar current again; a coherent observation clears this latch.
        self._research_observation_incomplete = False

    def accept(self, event: dict) -> None:
        if (event.get("schema") != SCHEMA or not isinstance(event.get("run_id"), str)
                or type(event.get("seq")) is not int or event["seq"] < 1
                or type(event.get("stage")) is not int or not 1 <= event["stage"] <= 8
                or not isinstance(event.get("kind"), str) or not isinstance(event.get("data"), dict)
                or type(event.get("time")) not in (int, float)
                or not -8640000000000 <= event["time"] <= 8640000000000):
            self.rejected += 1
            return
        if event["run_id"] != self.last_run:
            self.view, self.last_seq, self.research_seen = {}, 0, None
            self.events.clear()
            self.last_run = event["run_id"]
        if event["seq"] <= self.last_seq:
            self.rejected += 1
            return
        gap = event["seq"] != self.last_seq + 1
        self.last_seq = event["seq"]
        kind, data = event["kind"], event["data"]
        view = self.view
        view.update(run_id=self.last_run, stage=event["stage"], last_event_time=event["time"], kind=kind)
        view["gap"] = view.get("gap", False) or gap
        if kind == "run_started":
            view.update({key: data[key] for key in ("target", "policy", "model", "controller") if key in data})
            view["lifecycle"] = "running"
        elif kind == "run_finished":
            view["lifecycle"] = data.get("outcome", "returned")
            view["model_busy"] = False
        elif kind == "cycle_started":
            view["seen"] = []
            view["model_busy"] = False
            view["request"], view["response"], view["decision"] = None, None, None
            view["verified"], view["outcome"] = None, None
        elif kind == "observation":
            observed_state = data.get("state")
            coherent_research = has_coherent_research_observation(observed_state)
            coherent_mission = has_coherent_mission_projection(observed_state)
            observed_state = without_incoherent_mission(observed_state)
            view["state"] = observed_state if isinstance(observed_state, dict) else {}
            # A raw observation replaces any earlier completed-record display.
            # Its mission projection can independently keep launch evidence
            # current, while a missing/malformed research list must not revive
            # a catalog's older current topic.
            view.pop("mission_record", None)
            view.pop("recorded_action", None)
            view["state_observed_time"] = (event["time"]
                                           if coherent_research or coherent_mission else None)
            if coherent_research:
                self._research_observation_incomplete = False
            else:
                self._research_observation_incomplete = True
        elif kind in ("goals", "controller_state"):
            keys = ("goal", "target", "status", "completed_goals", "plan", "pending", "step_index", "decision")
            view.update({key: data[key] for key in keys if key in data})
        elif kind == "model_request":
            view["request"] = data
            view["response"] = None
            view["decision"] = None
        elif kind == "model_started":
            view["model_busy"] = True
            view["model_started_at"] = event["time"]
        elif kind in ("model_returned", "model_failed"):
            view["model_busy"] = False
            view["model_ms"] = data.get("duration_ms")
        elif kind == "model_response":
            view["response"] = data
        elif kind == "action":
            view["action"] = data.get("action")
            view["parameters"] = data.get("parameters")
            view["dispatch"] = {
                "observed": True,
                "action": data.get("action"),
                "parameters": data.get("parameters"),
                "event_seq": event["seq"],
                "event_time": event["time"],
            }
        elif kind == "decision_recorded" and isinstance(data.get("record"), dict):
            view.pop("mission_record", None)
            # A truncated/older record cannot rejuvenate a previous observation.
            record = data["record"]
            source_state = record.get("state")
            recorded_state = without_incoherent_mission(source_state)
            record_incomplete = record.get("_dashboard_research_observation_incomplete") is True
            legacy_absent_after_state = (
                self.legacy and data.get("_dashboard_legacy_after_state_absent") is True
            )
            legacy_absent_research_fallback = (
                legacy_absent_after_state and isinstance(source_state, dict)
                and "researched" not in source_state
            )
            if (not record_incomplete and not legacy_absent_research_fallback
                    and isinstance(source_state, dict)
                    and isinstance(source_state.get("mission"), dict)
                    and not has_coherent_research_observation(source_state)):
                # Before the private marker was added, project_record persisted
                # its mission projection beside a missing/null/malformed
                # researched field. That shape is not evidence that the old
                # catalog's current topic is still observed. The legacy reader
                # skips only its specific absent-after_state/no-research-key
                # fallback; explicit malformed research remains incomplete.
                record_incomplete = True
            if record_incomplete:
                self._research_observation_incomplete = True
            elif has_coherent_research_observation(recorded_state):
                self._research_observation_incomplete = False
            view["state"] = recorded_state if isinstance(recorded_state, dict) else {}
            view["state_observed_time"] = event["time"] if view["state"] and not record_incomplete else None
            view["legacy_record_timestamp"] = data.get("record_timestamp") is True
            view["recorded_action"] = data["record"].get("action")
            view.update({key: value for key, value in data["record"].items()
                         if key in RECORD_KEYS or key == "mission_record"})
        elif kind.endswith("_failed"):
            view["last_error"] = kind
        if kind in ("observation", "decision_recorded"):
            self._note_research(view.get("state"))
        seen = view.setdefault("seen", [])
        if event["stage"] not in seen:
            seen.append(event["stage"])
        record = data.get("record") if kind == "decision_recorded" else None
        recorded = record if isinstance(record, dict) else {}
        recorded_state = recorded.get("state")
        recorded_state = recorded_state if isinstance(recorded_state, dict) else {}
        self.events.append({"id": f"{self.last_run}:{event['seq']}", "kind": kind,
                            "stage": event["stage"], "at": event.get("at", ""),
                            "time": event["time"], "duration_ms": data.get("duration_ms"),
                            "action": data.get("action", recorded.get("action")),
                            "tick": recorded.get("tick", recorded_state.get("tick")),
                            "outcome": recorded.get("outcome"),
                            "verified": recorded.get("verified")})

    def _note_research(self, state: Any) -> None:
        """Keep the first observed tick of each milestone technology in this run.

        The tail may start mid-log, so research already present in the first
        observation is recorded without a tick rather than with a misleading one.
        """
        state = state if isinstance(state, dict) else {}
        tick, researched = state.get("tick"), state.get("researched")
        # Treat the researched list as one tick-bound observation. Salvaging
        # valid names from a malformed list would create unverified milestone
        # history even though the current observation is marked incomplete.
        if not has_coherent_research_observation(state):
            return
        first = self.research_seen is None
        seen = self.research_seen = {} if first else self.research_seen
        for tech in researched[:research_catalog.MAX_TECHNOLOGIES]:
            if (isinstance(tech, str) and research_catalog.NAME.match(tech)
                    and len(seen) < research_catalog.MAX_TECHNOLOGIES):
                seen.setdefault(tech, None if first else tick)

    def _mark_reconstruction_gap(self) -> None:
        """Drop current-view claims until a new full record re-establishes them."""
        self._dashboard_decoder.reset()
        self._legacy_decoder.reset()
        self.reconstruction_status = "gap"
        self._research_observation_incomplete = True
        self.view.update(
            gap=True, reconstruction_status="gap", last_event_time=None,
            kind=None, stage=None, status=None, lifecycle=None,
            state={}, state_observed_time=None,
            goal=None, plan=None, step_index=None, pending=None,
            verified=None, outcome=None, decision=None, request=None,
            response=None, action=None, parameters=None,
            model_busy=False, model_started_at=None, model_ms=None,
        )
        self.view.pop("mission_record", None)
        self.view.pop("persistent_recovery", None)
        self.view.pop("dispatch", None)
        self.view.pop("recorded_action", None)

    def poll(self) -> None:
        with self.lock:
            previous_invalid = self.tail.invalid
            rows = self.tail.poll()
            invalid_added = self.tail.invalid > previous_invalid
            self._tail_invalid_seen = self.tail.invalid
            if self.tail.reset:
                self.events.clear()
                self.view, self.last_run, self.last_seq, self.research_seen = {}, None, 0, None
                self._dashboard_decoder.reset()
                self._legacy_decoder.reset()
                self.reconstruction_status = "waiting_for_anchor"
            if invalid_added:
                # A skipped malformed line could have been the required full
                # anchor. Require the next full decision record to re-sync.
                self._mark_reconstruction_gap()
            for row, raw_size in zip(rows, self.tail.last_sizes):
                try:
                    if self.legacy:
                        row = self._legacy_decoder.decode(row, raw_size, anchor_candidate=True)
                        decoded_anchor = self._legacy_decoder.last_was_anchor
                    else:
                        row = self._dashboard_decoder.decode(
                            row, raw_size,
                            anchor_candidate=decision_anchor_candidate(row, "dashboard"))
                        decoded_anchor = self._dashboard_decoder.last_was_anchor
                except (ValueError, TypeError, KeyError, RecursionError):
                    self.rejected += 1
                    self._mark_reconstruction_gap()
                    continue
                if decoded_anchor:
                    self.reconstruction_status = "ok"
                    self.view["reconstruction_status"] = "ok"
                if self.legacy:
                    # Legacy records expose completed decisions, NOT in-flight model phases.
                    if "action" not in row:
                        self.rejected += 1
                        continue
                    has_after_state = "after_state" in row
                    state = row.get("after_state") if has_after_state else row.get("state")
                    if not has_after_state and not isinstance(state, dict):
                        self.rejected += 1
                        continue
                    state_identity = state if isinstance(state, dict) else {}
                    identity = str(row.get("session_id") or state_identity.get("session_id") or "legacy")
                    # A copied legacy file must not rejuvenate an old recorded snapshot.
                    timestamp, recorded_at = self.tail.mtime, ""
                    try:
                        instant = datetime.fromisoformat(row["recorded_at_utc"].replace("Z", "+00:00"))
                        if instant.tzinfo is not None and -8640000000000 <= instant.timestamp() <= 8640000000000:
                            timestamp, recorded_at = instant.timestamp(), instant.isoformat()
                    except (KeyError, AttributeError, ValueError, OverflowError, OSError):
                        pass
                    row = {"schema": SCHEMA, "run_id": identity, "seq": self.last_seq + 1 if identity == self.last_run else 1,
                           "time": timestamp, "at": recorded_at, "kind": "decision_recorded", "stage": 7,
                           "data": {"record": project_record(row), "record_timestamp": bool(recorded_at),
                                    "_dashboard_legacy_after_state_absent": not has_after_state}}
                self.accept(sanitize(row, self.secrets))
            self._read_supervisor()
            self._read_research()
            self.version += 1

    def _read_research(self) -> None:
        """Reload the research sidecar when it changes; drop it when it disappears."""
        if self.research_path is None:
            return
        try:
            info = os.stat(self.research_path)
        except OSError:
            self.research_identity, self.research_tree, self.research_state = None, None, None
            self.research_status = "waiting"
            return
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity == self.research_identity:
            return
        self.research_identity = identity
        try:
            catalog = research_catalog.read(self.research_path)
            self.research_tree, self.research_state = research_catalog.Tree(catalog), catalog["state"]
            self.research_status = "ok"
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            self.research_tree, self.research_state = None, None
            self.research_status = "invalid"

    def _research(self) -> tuple[dict, list[dict] | None]:
        """Research summary from the game's tree plus the freshest recorded research state."""
        tree = self.research_tree
        if tree is None:
            return {"status": self.research_status}, None
        state = self.view.get("state") if isinstance(self.view.get("state"), dict) else {}
        researched, current, progress, source = None, None, None, "telemetry"
        if self._research_observation_incomplete:
            # Preserve the static tree and only the technologies previously
            # observed in telemetry; the catalog's independent state can no
            # longer stand in for a missing current observation.
            researched = list(self.research_seen or {})
            source = "telemetry-history" if self.research_seen is not None else "unknown"
        elif isinstance(state.get("researched"), list):
            researched = [t for t in state["researched"] if isinstance(t, str)]
            now = dashboard_mission.mapping(dashboard_mission.mapping(state.get("mission")).get("research"))
            current, progress = now.get("name"), now.get("progress")
        elif self.research_state is not None:
            researched, source = self.research_state["researched"], "catalog"
            current, progress = self.research_state["current"], self.research_state["progress"]
        if researched is None:
            return {"status": "no research state", "version": tree.version}, None
        unknown = [t for t in researched if t not in tree.catalog["technologies"]]
        if unknown:
            # Telemetry from another game version or mod set: never mix trees.
            return {"status": "version mismatch", "version": tree.version, "unknown": unknown[:5]}, None
        summary = tree.summary(set(researched), current if isinstance(current, str) else None,
                               progress if type(progress) in (int, float) else None, self.research_seen or {})
        milestones = summary.pop("milestones")
        status = "observation incomplete" if self._research_observation_incomplete else "ok"
        return dict(summary, status=status, source=source), milestones

    def _read_supervisor(self) -> None:
        if self.supervisor is None:
            self.supervision = {"available": False}
            return
        try:
            with open_regular(self.supervisor) as stream:
                raw = stream.read(MAX_LINE + 1)
            if len(raw) > MAX_LINE:
                raise ValueError("oversized state")
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("invalid state")
            # Exclude paths, command argv, repair logs and credentials.
            keys = ("session_id", "phase", "cutoff", "started_at", "attempt", "repair_required")
            selected = {key: state[key] for key in keys if key in state}
            observed = self.view.get("state")
            session = (observed.get("session_id") if isinstance(observed, dict) else None) or self.view.get("session_id")
            self.supervision = {"available": True, "session_match": bool(session and session == state.get("session_id")),
                                "state": sanitize(selected, self.secrets)}
        except (OSError, ValueError, TypeError, RecursionError):
            self.supervision = {"available": False, "error": "Supervisor state unavailable"}

    def snapshot(self) -> dict:
        with self.lock:
            research, rows = self._research()
            if rows is not None:
                milestones = dashboard_mission.catalog_milestones(
                    self.view.get("completed_goals"), self.view.get("target"), rows, research.get("space_age") is True)
            else:
                milestones = dashboard_mission.milestones(self.view.get("completed_goals"), self.research_seen)
            view = dict(self.view, milestones=milestones, research=research)
            return copy.deepcopy({"version": self.version, "view": view, "events": list(self.events),
                                  "source": {"mode": "legacy" if self.legacy else "events", "status": self.tail.status,
                                             "invalid": self.tail.invalid + self.rejected,
                                             "partial": bool(self.tail.pending) or self.tail.dropping,
                                             "reconstruction_status": self.reconstruction_status},
                                  "supervisor": self.supervision, "server_time": time.time()})

    def follow(self) -> None:
        while not self.stop.is_set():
            self.poll()
            self.stop.wait(0.25)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def __init__(self, port: int, monitor: Monitor, icon_dir: Path | None = None):
        self.monitor = monitor
        self.icon_dir = icon_dir
        self.clients = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format, *args):
        pass  # Never echo query strings, local paths or attacker-controlled headers.

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), display-capture=(self)")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _allowed(self) -> bool:
        port = self.server.server_port
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        host = hosts[0]
        hostnames = ("127.0.0.1", "localhost")
        allowed = {f"{name}:{port}" for name in hostnames}
        if port == 80:
            allowed.update(hostnames)
        if host not in allowed:
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            return False
        origin = origins[0] if origins else None
        if origin is not None:
            hostname = host.split(":", 1)[0]
            allowed_origins = {f"http://{hostname}:{port}"}
            if port == 80:
                allowed_origins.add(f"http://{hostname}")
            if not origin or origin not in allowed_origins:
                return False
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", [])
        if len(fetch_sites) > 1:
            return False
        fetch_site = fetch_sites[0] if fetch_sites else "same-origin"
        return fetch_site in {"same-origin", "none"}

    def do_GET(self) -> None:
        if not self._allowed():
            self._headers(403, "text/plain", 0)
            return
        path = urlsplit(self.path).path
        if path == "/api/snapshot":
            raw = json.dumps(self.server.monitor.snapshot(), allow_nan=False).encode()
            self._headers(200, "application/json; charset=utf-8", len(raw))
            self.wfile.write(raw)
        elif path == "/api/events":
            self._events()
        elif path.startswith("/icons/"):
            self._icon(path)
        else:
            names = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                     "/styles.css": ("styles.css", "text/css"),
                     "/mission.js": ("mission.js", "text/javascript"),
                     "/mission.css": ("mission.css", "text/css"),
                     "/explain.js": ("explain.js", "text/javascript"),
                     "/factory-steel.png": ("factory-steel.png", "image/png")}
            if path not in names:
                self._headers(404, "text/plain", 0)
                return
            name, content_type = names[path]
            raw = (ASSETS / name).read_bytes()
            self._headers(200, content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""), len(raw))
            self.wfile.write(raw)

    def _icon(self, path: str) -> None:
        """Serve one item icon from the operator's local game install, if configured."""
        match = ICON_NAME.fullmatch(path)
        folder = self.server.icon_dir
        candidate = None
        if match and folder:
            tree = self.server.monitor.research_tree
            # Icons differ between game versions: prefer <icon-dir>/<version>/, then <icon-dir>/.
            for base in ((folder / tree.version,) if tree else ()) + (folder,):
                option = base / f"{match.group(1)}.png"
                if not base.is_symlink() and not option.is_symlink() and option.is_file():
                    candidate = option
                    break
        if candidate is None:
            self._headers(404, "text/plain", 0)
            return
        raw = candidate.read_bytes()
        self._headers(200, "image/png", len(raw))
        self.wfile.write(raw)

    def _events(self) -> None:
        if not self.server.clients.acquire(blocking=False):
            self._headers(503, "text/plain", 0)
            return
        try:
            self.connection.settimeout(3)
            self._headers(200, "text/event-stream; charset=utf-8")
            # Full bounded snapshots make reconnection independent of a dropped history window.
            while not self.server.monitor.stop.is_set():
                snapshot = self.server.monitor.snapshot()
                raw = json.dumps(snapshot, allow_nan=False, separators=(",", ":"))
                self.wfile.write(f"event: snapshot\nid: {snapshot['version']}\ndata: {raw}\n\n".encode())
                self.wfile.flush()
                self.server.monitor.stop.wait(0.5)
        except (OSError, TimeoutError):
            pass
        finally:
            self.server.clients.release()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Local, read-only JEV workflow dashboard")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--events", type=Path, help="Live --dashboard-events JSONL (may not exist yet)")
    source.add_argument("--log-file", type=Path, help="Existing legacy JSONL; completed-decision detail only")
    parser.add_argument("--supervisor-state", type=Path, help="Optional existing supervisor.json (read-only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--research-catalog", type=Path,
                        help=f"Research tree sidecar (default: {research_catalog.FILENAME} next to the telemetry file)")
    parser.add_argument("--icon-dir", type=Path,
                        help="Optional Factorio data/base/graphics/icons directory for item icons (read-only)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    path = args.events or args.log_file
    if path.exists() and not path.is_file():
        parser.error("Telemetry source must be a regular file")
    if args.icon_dir is not None and not args.icon_dir.is_dir():
        parser.error("--icon-dir must be an existing directory")
    research = args.research_catalog or path.with_name(research_catalog.FILENAME)
    monitor = Monitor(path, legacy=args.log_file is not None, supervisor=args.supervisor_state, research=research)
    server = DashboardServer(args.port, monitor, args.icon_dir)
    follower = threading.Thread(target=monitor.follow, daemon=True)
    follower.start()
    print(f"JEV dashboard: http://127.0.0.1:{server.server_port} (read-only; Ctrl+C stops only the viewer)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        server.server_close()
        follower.join(timeout=2)


if __name__ == "__main__":
    cli()
