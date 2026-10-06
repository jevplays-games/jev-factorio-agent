"""Opt-in, local research evidence. No network calls or game observations.

V1 is a single-process run: create exclusively, append durably, then seal.
A hash chain detects edits relative to its retained anchor; it is not a signature.
See docs/RESEARCH_LOGGING.md for serialization, durability, and threat boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol
from .blocked_persistence import MAX_IDLE_OBSERVATIONS
from .iteration_timing import measured, span
from .timing_attribution import elapsed_clocks, sample_clocks

EVENT_SCHEMA = "jev-factorio.event.v1"
MANIFEST_SCHEMA = "jev-factorio.manifest.v1"
INTEGRITY_SCHEMA = "jev-factorio.integrity.v1"
MAX_RECORD_BYTES = 1_048_576
REDACTED = "[REDACTED]"
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVENT_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_SENSITIVE = re.compile(
    r"password|secret|api[_-]?key|authorization|cookie|credential|private[_-]?key"
    r"|access[_-]?token|refresh[_-]?token|(?:^|[_-])token(?:$|[_-])", re.I
)
_PACKAGES = ("jev-factorio", "requests", "python-dotenv",
             "factorio-learning-environment", "a2a-sdk")
_CREDENTIALS = ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN", "FACTORIO_RCON_PASSWORD")
_CORRELATION_KEYS = {"decision_id", "model_call_id", "plan_id", "action_id"}
_TREATMENT_FIELDS = {"factory_scheduling", "background_work",
                     "furnace_output_buffers", "furnace_input_belts", "mining_outposts",
                     "campaign_diagnostics", "profile_observations", "consolidated_observations",
                     "lead_time_supply", "coverage_margin_lookahead", "solid_routes", "solid_science_policy", "coal_supply", "coal_kit_policy", "coal_economic_admission"}
_STRUCTURAL_CONFIGURATION_LABELS = {
    "backend": ("mock", "play_api", "fle"),
    "controller": ("flat", "hierarchical"),
    "policy": ("jev", "deterministic", "hybrid"),
    "target": ("bootstrap_mining", "iron_smelting", "steam_power",
               "automation_science", "rocket_launch"),
    "factory_scheduling": ("serial", "ready-work"),
}
_OPTIONAL_CONFIGURATION_FIELDS = _TREATMENT_FIELDS | {
    "treatment_sha256", "until_complete", "reconcile_only",
    "reevaluate_blocked_once", "exact_checkpoint_sha256", "blocked_source_revision",
    "profile_latency", "persist_recoverable_blocks", "persistent_idle_observations",
    "initialize_persistent_campaign", "two_stage_decisions",
}


class ResearchLogError(RuntimeError):
    """A causal trace cannot be persisted."""


class EventSink(Protocol):
    def emit(self, event_type: str, payload: dict) -> object:
        """Persist detached evidence before returning, or raise."""


def validate_output_paths(sink: object, *paths: str | Path | None) -> None:
    """Reject portable aliases between mutable output and research artifacts."""
    run_dir = sink if isinstance(sink, (str, Path)) else getattr(sink, "run_dir", None)
    if run_dir is None:
        return
    directory = Path(run_dir).resolve()
    root = tuple(part.casefold() for part in directory.parts)
    artifacts = [directory / name for name in ("manifest.json", "events.jsonl", "integrity.json")]
    reserved = {root + (artifact.name,) for artifact in artifacts}
    for path in paths:
        if path is None:
            continue
        destination = tuple(part.casefold() for part in Path(path).resolve().parts)
        if (root[:len(destination)] == destination
                or any(destination[:len(artifact)] == artifact for artifact in reserved)
                or (Path(path).exists() and any(
                    artifact.exists() and Path(path).samefile(artifact) for artifact in artifacts))):
            raise ValueError("Output paths must not overwrite research artifacts or their directories")


@dataclass(frozen=True)
class RunConfiguration:
    """Allowlisted CLI settings, deliberately excluding paths, argv and URLs."""

    backend: str
    controller: str
    policy: str
    target: str | None = None
    requested_model: str | None = None
    steps: int | None = None
    duration_seconds: float | None = None
    tick_seconds: float = 0.0
    confidence_floor: float = 0.45
    resume: bool = False
    resume_controller: bool = False
    adopt_session: bool = False
    mock_model: bool = False
    legacy_log_enabled: bool = False
    checkpoint_enabled: bool = False
    factory_scheduling: str = "serial"
    background_work: bool = False
    furnace_output_buffers: bool = False
    furnace_input_belts: bool = False
    mining_outposts: bool = False
    ore_side_successors: bool = False
    campaign_diagnostics: bool = False
    profile_observations: bool = False
    profile_latency: bool = False
    consolidated_observations: bool = False
    lead_time_supply: bool = False
    coverage_margin_lookahead: bool = False
    solid_routes: bool = False
    solid_science_policy: bool = False
    coal_supply: bool = False
    coal_kit_policy: bool = False
    coal_economic_admission: bool = False
    treatment_sha256: str | None = None
    until_complete: bool = False
    reconcile_only: bool = False
    reevaluate_blocked_once: bool = False
    exact_checkpoint_sha256: str | None = None
    blocked_source_revision: str | None = None
    persist_recoverable_blocks: bool = False
    initialize_persistent_campaign: bool = False
    # None means this setting was inapplicable or absent in a historical run.
    persistent_idle_observations: int | None = None
    two_stage_decisions: bool = False


def canonical_bytes(value: object) -> bytes:
    """V1 encoding, NOT RFC 8785: sorted compact ASCII JSON, finite numbers."""
    _json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("Evidence must contain only finite JSON values and string keys")


class Redactor:
    """Defense in depth for payloads; provenance itself uses an allowlist.

    Environment secret values stay in memory. Arbitrary unlabeled/encoded secrets
    cannot be guaranteed detectable, so never send raw exception/HTTP/argv dumps.
    """

    def __init__(self, environ: Mapping[str, str]):
        self._secrets = sorted({value for key, value in environ.items()
                                if _SENSITIVE.search(key) and value}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self._secrets:
            if len(secret) >= 4:
                value = value.replace(secret, REDACTED)
            elif value == secret:
                value = REDACTED
        value = re.sub(r"https?://[^\s<>\"']+", REDACTED, value, flags=re.I)
        value = re.sub(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.\-]+",
                       REDACTED, value, flags=re.I)
        return value

    @measured("research_redact_validate")
    def clean(self, value: object) -> object:
        _json_value(value)
        return self._clean_validated(value)

    def _clean_validated(self, value: object) -> object:
        """Detach and redact an already validated tree without rescanning subtrees."""
        if type(value) is str:
            return self.text(value)
        if type(value) is list:
            return [self._clean_validated(item) for item in value]
        if type(value) is dict:
            result = {}
            for key, item in value.items():
                safe_key = self.text(key)
                if safe_key in result:
                    raise ValueError("Redaction produced duplicate evidence keys")
                result[safe_key] = REDACTED if _SENSITIVE.search(key) else self._clean_validated(item)
            return result
        return value


@measured("trace_normalize_redact")
def safe_payload(value: object, secrets: Iterable[str] = ()) -> object:
    """Normalize, redact, and detach an event without expanding dataclasses."""
    return _normalize_payload(value, secrets)


@measured("trace_normalize_redact")
def _safe_observation_payload(value: object, secrets: Iterable[str] = ()) -> object:
    """Expand dataclass fields only below the observation event's snapshot key."""
    return _normalize_payload(value, secrets, expand_dataclasses_at=("snapshot",))


def _normalize_payload(value: object, secrets: Iterable[str], *,
                       expand_dataclasses_at: tuple[str, ...] | None = None) -> object:
    redactor = Redactor({f"SECRET_{index}": secret for index, secret in enumerate(secrets)
                         if isinstance(secret, str) and secret})

    def expands_dataclasses(path: tuple[str, ...]) -> bool:
        return (expand_dataclasses_at is not None
                and path[:len(expand_dataclasses_at)] == expand_dataclasses_at)

    def validate_discarded(item: object, path: tuple[str, ...]) -> None:
        """Retain key validation under sensitive fields without copying values."""
        if is_dataclass(item) and not isinstance(item, type):
            if expands_dataclasses(path):
                for field in fields(item):
                    validate_discarded(getattr(item, field.name), path + (field.name,))
        elif type(item) in (list, tuple):
            for child in item:
                validate_discarded(child, path)
        elif type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("Causal evidence keys must be strings")
            for key, child in item.items():
                validate_discarded(child, path + (key,))

    def clean_mapping(items, path):
        result = {}
        for key, child in items:
            if type(key) is not str:
                raise ValueError("Causal evidence keys must be strings")
            child_path = path + (key,)
            if _SENSITIVE.search(key):
                validate_discarded(child, child_path)
                cleaned = REDACTED
            else:
                cleaned = normalize(child, child_path)
            safe_key = redactor.text(key)
            if safe_key in result:
                raise ValueError("Redaction produced duplicate evidence keys")
            result[safe_key] = cleaned
        return result

    def normalize(item: object, path: tuple[str, ...] = ()) -> object:
        if item is None or type(item) in (str, bool, int):
            return redactor.text(item) if type(item) is str else item
        if type(item) is float:
            return item if math.isfinite(item) else clean_mapping((("invalid_numeric", repr(item)),), path)
        if is_dataclass(item) and not isinstance(item, type):
            if not expands_dataclasses(path):
                return redactor.text("[unsupported value]")
            return clean_mapping(((field.name, getattr(item, field.name)) for field in fields(item)), path)
        if type(item) in (list, tuple):
            return [normalize(child, path) for child in item]
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("Causal evidence keys must be strings")
            return clean_mapping(item.items(), path)
        return redactor.text("[unsupported value]")

    return normalize(value)


def collect_provenance(repo_dir: Path, environ: Mapping[str, str]) -> dict:
    """Read bounded local metadata only; never collect remote URLs or source diffs."""
    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                 "-C", str(repo_dir), *args], capture_output=True, text=True,
                timeout=2, check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            return None

    root = git("rev-parse", "--show-toplevel")
    tracked = False
    if root:
        try:
            relative = Path(__file__).resolve().relative_to(Path(root).resolve())
            tracked = git("ls-files", "--error-unmatch", "--",
                          ":(top,literal)" + relative.as_posix()) is not None
        except ValueError:
            pass
    commit = git("rev-parse", "HEAD") if tracked else None
    if commit is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        commit = None
    status = git("status", "--porcelain", "--untracked-files=normal") if commit else None
    packages = {}
    for package in _PACKAGES:
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = None
    return {
        "git": {"commit": commit, "dirty": bool(status) if status is not None else None},
        "runtime": {"python": platform.python_version(), "system": platform.system(),
                    "machine": platform.machine(), "packages": packages},
        # Values, variable names beyond this allowlist, hosts and account IDs are absent.
        "provider_configuration": {"typesafe": bool(environ.get(_CREDENTIALS[0])),
                                   "cloudflare": bool(environ.get(_CREDENTIALS[1])),
                                   "factorio_rcon": bool(environ.get(_CREDENTIALS[2]))},
    }


def _keys(value: object, expected: set[str]) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("Unexpected evidence schema fields")


def _integer(value: object, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid evidence integer")


def _hash(value: object) -> None:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ValueError("Invalid SHA-256 reference")


def _identity(value: object) -> None:
    if type(value) is not str:
        raise ValueError("Invalid run identity")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("Invalid run identity")
    except (ValueError, AttributeError) as error:
        raise ValueError("Invalid run identity") from error


def _utc(value: object) -> None:
    if type(value) is not str or not _UTC.fullmatch(value):
        raise ValueError("Invalid UTC timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid UTC timestamp") from error


def _optional_text(value: object) -> None:
    if value is not None and (type(value) is not str or not value):
        raise ValueError("Invalid optional evidence text")


def _configuration(configuration: dict) -> None:
    expected = set(RunConfiguration.__dataclass_fields__)
    if (type(configuration) is not dict or not expected - _OPTIONAL_CONFIGURATION_FIELDS <= set(configuration)
            or not set(configuration) <= expected):
        raise ValueError("Unexpected evidence schema fields")
    if configuration.get("factory_scheduling", "serial") not in ("serial", "ready-work"):
        raise ValueError("Invalid factory scheduling policy")
    for key in _TREATMENT_FIELDS - {"factory_scheduling"}:
        if type(configuration.get(key, False)) is not bool:
            raise ValueError("Invalid run treatment flag")
    if type(configuration.get('profile_latency', False)) is not bool:
        raise ValueError('Invalid latency profiling flag')
    if configuration.get('profile_latency', False) and configuration['controller'] != 'hierarchical':
        raise ValueError('Latency profiling requires hierarchical control')
    if configuration.get("coal_kit_policy", False) and not configuration.get("coal_supply", False):
        raise ResearchLogError("Coal kit policy requires its coal supply treatment")
    if configuration.get("coal_economic_admission", False) and not (
            configuration.get("coal_kit_policy", False) and configuration.get("treatment_sha256")):
        raise ResearchLogError("Coal economic admission requires an immutable coal kit treatment")
    if configuration.get("coal_supply", False) and not configuration.get("solid_routes", False):
        raise ResearchLogError("Coal supply requires its solid route treatment")
    if configuration.get("solid_science_policy", False) and not configuration.get("solid_routes", False):
        raise ValueError("Solid science policy requires solid routes")
    treatment_hash = configuration.get('treatment_sha256')
    if treatment_hash is not None and (not isinstance(treatment_hash, str)
            or re.fullmatch(r'[0-9a-f]{64}', treatment_hash) is None
            or not configuration.get('solid_routes', False)):
        raise ValueError('Invalid immutable production treatment digest')
    for key in ("backend", "controller", "policy"):
        if type(configuration[key]) is not str or not configuration[key]:
            raise ValueError("Invalid run configuration label")
    for key in ("target", "requested_model"):
        _optional_text(configuration[key])
    for key in ("exact_checkpoint_sha256", "blocked_source_revision"):
        _optional_text(configuration.get(key))
    if configuration.get("exact_checkpoint_sha256") is not None and re.fullmatch(
            r"[0-9a-f]{64}", configuration["exact_checkpoint_sha256"]) is None:
        raise ValueError("Invalid exact checkpoint SHA-256")
    if configuration.get("blocked_source_revision") is not None and re.fullmatch(
            r"[0-9a-f]{40}", configuration["blocked_source_revision"]) is None:
        raise ValueError("Invalid blocked source revision")
    if configuration["steps"] is not None:
        _integer(configuration["steps"])
    for key in ("duration_seconds", "tick_seconds", "confidence_floor"):
        value = configuration[key]
        if key == "duration_seconds" and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid run configuration number")
        if (key == "duration_seconds" and value == 0) or (key == "confidence_floor" and value > 1):
            raise ValueError("Run configuration number outside bounds")
    if configuration["steps"] is not None and configuration["duration_seconds"] is not None:
        raise ValueError("Run configuration has conflicting limits")
    for key in ("resume", "resume_controller", "adopt_session", "mock_model",
                "legacy_log_enabled", "checkpoint_enabled", "until_complete", "reconcile_only",
                "reevaluate_blocked_once", "persist_recoverable_blocks", "initialize_persistent_campaign",
                "two_stage_decisions"):
        value = (configuration.get(key, False)
                 if key in {"until_complete", "reconcile_only", "reevaluate_blocked_once",
                            "persist_recoverable_blocks", "initialize_persistent_campaign", "two_stage_decisions"}
                 else configuration[key])
        if type(value) is not bool:
            raise ValueError("Invalid run configuration flag")
    until_complete = configuration.get("until_complete", False)
    reconcile_only = configuration.get("reconcile_only", False)
    reevaluate_blocked_once = configuration.get("reevaluate_blocked_once", False)
    persist_recoverable_blocks = configuration.get("persist_recoverable_blocks", False)
    initialize_persistent_campaign = configuration.get("initialize_persistent_campaign", False)
    if configuration.get("two_stage_decisions", False) and not persist_recoverable_blocks:
        raise ValueError("Two-stage decisions require persistent strict JEV control")
    if initialize_persistent_campaign and (
            not persist_recoverable_blocks or configuration["resume"]
            or configuration["resume_controller"] or configuration["adopt_session"]
            or reevaluate_blocked_once):
        raise ValueError("New persistent campaign cannot resume or migrate existing state")
    if "persistent_idle_observations" in configuration:
        idle_observations = configuration["persistent_idle_observations"]
        if persist_recoverable_blocks:
            if (type(idle_observations) is not int
                    or not 0 <= idle_observations <= MAX_IDLE_OBSERVATIONS):
                raise ValueError("Invalid persistent idle observation bound")
        elif idle_observations is not None:
            raise ValueError("Idle observation bound requires persistent blocked recovery")
    if until_complete and reconcile_only:
        raise ValueError("Run configuration has conflicting execution modes")
    if (until_complete or reconcile_only) and (
        configuration["steps"] is not None or configuration["duration_seconds"] is not None
    ):
        raise ValueError("Run configuration mode conflicts with bounded limits")
    if until_complete and configuration["controller"] != "hierarchical":
        raise ValueError("Until-complete requires hierarchical terminal status")
    if persist_recoverable_blocks and (
        not until_complete or configuration["backend"] != "fle"
        or configuration["controller"] != "hierarchical" or configuration["policy"] != "jev"
        or configuration["mock_model"]
        or (not initialize_persistent_campaign and (
            not configuration["resume"] or not configuration["resume_controller"]))
        or not configuration["checkpoint_enabled"]
        or reconcile_only
    ):
        raise ValueError("Persistent blocked recovery requires until-complete resumed native Jev control")
    if reconcile_only and (
        configuration["backend"] != "fle" or configuration["controller"] != "hierarchical"
        or configuration["factory_scheduling"] != "ready-work"
        or not configuration["background_work"] or not configuration["resume"]
        or not configuration["resume_controller"] or not configuration["checkpoint_enabled"]
        or configuration["target"] == "bootstrap_mining"
    ):
        raise ValueError("Reconcile-only requires a resumed native background-work campaign")
    if reevaluate_blocked_once:
        if (configuration["backend"] != "fle" or configuration["controller"] != "hierarchical"
                or configuration["policy"] != "jev" or configuration["mock_model"]
                or not configuration["resume"] or not configuration["resume_controller"]
                or not configuration["checkpoint_enabled"] or reconcile_only
                or not configuration.get("exact_checkpoint_sha256")
                or not configuration.get("blocked_source_revision")):
            raise ValueError("Blocked decision re-evaluation requires pinned resumed native Jev control")
    elif (configuration.get("exact_checkpoint_sha256") is not None
          or configuration.get("blocked_source_revision") is not None):
        raise ValueError("Blocked decision pins require blocked re-evaluation mode")


def _provenance(provenance: dict) -> None:
    _keys(provenance, {"git", "runtime", "provider_configuration"})
    _keys(provenance["git"], {"commit", "dirty"})
    commit = provenance["git"]["commit"]
    if commit is not None and (type(commit) is not str or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", commit
    )):
        raise ValueError("Invalid provenance commit")
    dirty = provenance["git"]["dirty"]
    if dirty is not None and type(dirty) is not bool:
        raise ValueError("Invalid provenance dirty flag")
    runtime = provenance["runtime"]
    _keys(runtime, {"python", "system", "machine", "packages"})
    if any(type(runtime[key]) is not str for key in ("python", "system", "machine")):
        raise ValueError("Invalid runtime provenance")
    _keys(runtime["packages"], set(_PACKAGES))
    for version in runtime["packages"].values():
        _optional_text(version)
    _keys(provenance["provider_configuration"], {"typesafe", "cloudflare", "factorio_rcon"})
    if any(type(value) is not bool for value in provenance["provider_configuration"].values()):
        raise ValueError("Invalid provider presence flag")


def validate_manifest(manifest: dict) -> None:
    _keys(manifest, {"schema", "schema_version", "run_id", "created_utc",
                     "configuration", "provenance", "durability"})
    if manifest["schema"] != MANIFEST_SCHEMA or type(manifest["schema_version"]) is not int \
            or manifest["schema_version"] != 1:
        raise ValueError("Unsupported manifest schema")
    _identity(manifest["run_id"])
    _utc(manifest["created_utc"])
    _configuration(manifest["configuration"])
    _provenance(manifest["provenance"])
    if manifest["durability"] not in (
        "file-and-directory-fsync", "file-fsync-only"
    ):
        raise ValueError("Invalid manifest provenance or durability")
    _json_value(manifest)


def validate_event(event: dict) -> None:
    """Strict V1 envelope validation independent of the hash-chain verifier."""
    _keys(event, {"schema", "schema_version", "run_id", "sequence", "event_type",
                  "time", "session_id", "correlation", "payload", "prev_hash", "event_hash"})
    if event["schema"] != EVENT_SCHEMA or type(event["schema_version"]) is not int \
            or event["schema_version"] != 1:
        raise ValueError("Unsupported event schema")
    _identity(event["run_id"])
    _integer(event["sequence"], 1)
    if type(event["event_type"]) is not str or not _EVENT_TYPE.fullmatch(event["event_type"]):
        raise ValueError("Invalid event type")
    _keys(event["time"], {"utc", "monotonic_ns", "factorio_tick"})
    _utc(event["time"]["utc"])
    _integer(event["time"]["monotonic_ns"])
    if event["time"]["factorio_tick"] is not None:
        _integer(event["time"]["factorio_tick"])
    if event["session_id"] is not None and (
        type(event["session_id"]) is not str or not event["session_id"]
    ):
        raise ValueError("Invalid session identity")
    if type(event["correlation"]) is not dict or not set(event["correlation"]) <= _CORRELATION_KEYS:
        raise ValueError("Invalid correlation fields")
    if any(type(value) is not str or not value for value in event["correlation"].values()):
        raise ValueError("Invalid correlation identity")
    if type(event["payload"]) is not dict:
        raise ValueError("Event payload must be an object")
    _hash(event["prev_hash"])
    _hash(event["event_hash"])
    _json_value(event)


def _encode_event(event: dict) -> bytes:
    """Encode one detached unsigned event without serializing its payload twice.

    V1 validation is still performed on the complete envelope. Each validated
    top-level value is encoded once with the same canonical JSON settings; the
    unsigned hash input and final record reuse those exact immutable bytes.
    The independent reader/verifier deliberately retains canonical_bytes().
    This private function is used only while the writer owns its lock and local
    detached event. No callback or asynchronous writer observes an interim hash.
    """
    if type(event) is not dict or 'event_hash' in event:
        raise ValueError('Expected an unsigned research event')
    event['event_hash'] = 'sha256:' + '0' * 64
    with span("research_validate"):
        validate_event(event)
    chunks = {}
    for key, value in event.items():
        if key == 'event_hash':
            continue
        # validate_event checks all keys/types/finite nested values before this
        # internal encoding. Envelope field names are fixed ASCII identifiers.
        with span("research_serialize"):
            encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                                 ensure_ascii=True, allow_nan=False).encode('ascii')
        chunks[key] = b'"' + key.encode('ascii') + b'":' + encoded
    with span("research_assemble"):
        unsigned = b'{' + b','.join(chunks[key] for key in sorted(chunks)) + b'}'
    with span("research_hash"):
        event['event_hash'] = 'sha256:' + hashlib.sha256(unsigned).hexdigest()
    with span("research_assemble"):
        chunks['event_hash'] = b'"event_hash":"' + event['event_hash'].encode('ascii') + b'"'
        data = b'{' + b','.join(chunks[key] for key in sorted(chunks)) + b'}\n'
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError('Evidence event exceeds V1 size limit')
    return data


def _sync_directory(path: Path) -> None:
    # Windows does not expose POSIX directory fsync through this interface.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_parents(path: Path) -> None:
    if path.is_dir():
        return
    _make_parents(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
    _sync_directory(path.parent)


def _exclusive_file(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def _write_durable(stream, data: bytes) -> None:
    with span("research_write"):
        if stream.write(data) != len(data):
            raise OSError("Incomplete research-log write")
        stream.flush()
    with span("research_fsync"):
        os.fsync(stream.fileno())


def _write_document(path: Path, value: dict) -> None:
    data = canonical_bytes(value) + b"\n"
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("Evidence document exceeds V1 size limit")
    with _exclusive_file(path) as stream:
        _write_durable(stream, data)
    _sync_directory(path.parent)


class ResearchLog:
    """One exclusive run directory; thread-serialized, never reopened or resumed.

    Failed writes poison the writer. A later call cannot silently continue after
    an uncertain append. close() alone intentionally leaves an unsealed run.
    """

    def __init__(self, run_dir: Path, configuration: RunConfiguration, *,
                 repo_dir: Path | None = None, environ: Mapping[str, str] | None = None,
                 monotonic_ns: Callable[[], int] | None = None,
                 utc_now: Callable[[], datetime] | None = None,
                 timing_wall_clock: Callable[[], int] | None = None,
                 timing_process_clock: Callable[[], int] | None = None,
                 timing_thread_clock: Callable[[], int] | None = None):
        self.run_dir = Path(run_dir)
        environment = dict(os.environ if environ is None else environ)
        self._redactor = Redactor(environment)
        self._monotonic_ns = time.monotonic_ns if monotonic_ns is None else monotonic_ns
        self._utc_now = (lambda: datetime.now(timezone.utc)) if utc_now is None else utc_now
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._sequence = 0
        self._last_monotonic_ns = -1
        self._failed = False
        self._closed = False
        self._finished = False
        self._stream = None
        self.run_id = str(uuid.uuid4())
        # Capture before creating artifacts, so our own files do not dirty provenance.
        manifest = {
            "schema": MANIFEST_SCHEMA, "schema_version": 1, "run_id": self.run_id,
            "created_utc": self._timestamp(),
            "configuration": {key: (value if type(value) is str and value in
                                     _STRUCTURAL_CONFIGURATION_LABELS.get(key, ())
                                     else self._redactor.clean(value))
                              for key, value in asdict(configuration).items()},
            "provenance": collect_provenance(
                repo_dir or Path(__file__).resolve().parents[2], environment),
            "durability": "file-fsync-only" if os.name == "nt" else "file-and-directory-fsync",
        }
        validate_manifest(manifest)
        # Retain precisely the validated, redacted manifest values. Neither the
        # caller's object nor a later public snapshot can change this treatment.
        self._configuration = RunConfiguration(**manifest["configuration"])
        self._profile_latency = self._configuration.profile_latency
        self._timing_clocks = (timing_wall_clock, timing_process_clock, timing_thread_clock)
        self._startup_timing_start = None
        self._manifest_hash = digest(manifest)
        self._previous_hash = self._manifest_hash
        _make_parents(self.run_dir.parent)
        # mkdir is the single-writer claim; even an existing empty directory is refused.
        self.run_dir.mkdir(mode=0o700)
        try:
            _sync_directory(self.run_dir.parent)
            _write_document(self.run_dir / "manifest.json", manifest)
            self._stream = _exclusive_file(self.run_dir / "events.jsonl")
            _sync_directory(self.run_dir)
            self._append("run_started", {"manifest_hash": self._manifest_hash})
        except BaseException:
            self.close()
            raise

    @property
    def configuration(self) -> RunConfiguration:
        """The frozen, validated configuration written to this run's manifest."""
        return replace(self._configuration)

    def _timestamp(self) -> str:
        value = self._utc_now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Research clock must return timezone-aware UTC-convertible time")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def emit(self, event_type: str, payload: dict, *, factorio_tick: int | None = None,
             session_id: str | None = None, correlation: dict[str, str] | None = None) -> dict:
        """Persist one event; use only already-observed game facts as optional IDs."""
        if event_type in {"run_started", "run_finished"}:
            raise ValueError("Lifecycle events are owned by ResearchLog")
        if type(payload) is dict:
            if factorio_tick is None:
                factorio_tick = payload.get("factorio_tick")
            if session_id is None:
                session_id = payload.get("session_id")
            if correlation is None:
                correlation = {key: payload[key] for key in _CORRELATION_KEYS
                               if payload.get(key) is not None}
        return self._append(event_type, payload, factorio_tick=factorio_tick,
                            session_id=session_id, correlation=correlation)

    @measured("research_append")
    def _append(self, event_type: str, payload: dict, *, factorio_tick: int | None = None,
                session_id: str | None = None, correlation: dict[str, str] | None = None) -> dict:
        with self._lock:
            if correlation is not None and type(correlation) is not dict:
                raise ValueError("Invalid correlation fields")
            if os.getpid() != self._owner_pid:
                raise RuntimeError("Research writer cannot be shared across processes")
            if self._closed or self._failed or self._finished:
                raise RuntimeError("Research writer is closed, failed, or sealed")
            monotonic = self._monotonic_ns()
            _integer(monotonic)
            if monotonic < self._last_monotonic_ns:
                raise ValueError("Monotonic research clock regressed")
            timing_payload = payload
            timing_sample = None
            if self._profile_latency and event_type in {'run_started', 'controller_initialized'}:
                timing_sample = sample_clocks(
                    wall_clock=self._timing_clocks[0],
                    process_clock=self._timing_clocks[1],
                    thread_clock=self._timing_clocks[2])
                if event_type == 'run_started':
                    self._startup_timing_start = timing_sample
                elif type(payload) is dict:
                    timing_payload = dict(payload)
                    timing_payload['startup_window_timing'] = elapsed_clocks(
                        self._startup_timing_start, timing_sample,
                        'before_run_started_event_construction_to_before_controller_initialized_event_construction')
            event = {
                "schema": EVENT_SCHEMA, "schema_version": 1, "run_id": self.run_id,
                "sequence": self._sequence + 1, "event_type": event_type,
                "time": {"utc": self._timestamp(), "monotonic_ns": monotonic,
                         "factorio_tick": factorio_tick},
                "session_id": self._redactor.clean(session_id),
                "correlation": {key: self._redactor.clean(value)
                                for key, value in (correlation or {}).items()},
                "payload": (timing_payload if event_type == "run_started" else
                            {"outcome": timing_payload["outcome"],
                             "error_type": self._redactor.clean(timing_payload["error_type"])}
                            if event_type == "run_finished" else self._redactor.clean(timing_payload)),
                "prev_hash": self._previous_hash,
            }
            data = _encode_event(event)
            try:
                _write_durable(self._stream, data)
            except BaseException:
                self._failed = True
                raise
            self._sequence += 1
            self._last_monotonic_ns = monotonic
            self._previous_hash = event["event_hash"]
            return event

    def finish(self, outcome: str = "returned", *, error_type: str | None = None) -> None:
        """Seal a complete lifecycle, NOT a successful gameplay outcome."""
        if type(outcome) is not str or outcome not in {"returned", "error", "interrupted"}:
            raise ValueError("Invalid run outcome")
        _optional_text(error_type)
        with self._lock:
            self._append("run_finished", {"outcome": outcome, "error_type": error_type})
            self._finished = True
            try:
                _write_document(self.run_dir / "integrity.json", {
                    "schema": INTEGRITY_SCHEMA, "schema_version": 1, "run_id": self.run_id,
                    "manifest_hash": self._manifest_hash, "event_count": self._sequence,
                    "final_event_hash": self._previous_hash,
                })
            except BaseException:
                self._failed = True
                raise
            finally:
                self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def __enter__(self) -> ResearchLog:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if not self._failed and not self._finished and not self._closed:
                outcome = "returned" if exc_type is None else (
                    "interrupted" if issubclass(exc_type, (KeyboardInterrupt, SystemExit)) else "error"
                )
                self.finish(outcome, error_type=exc_type.__name__ if exc_type else None)
        finally:
            self.close()
        return False


def _decode(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON evidence key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Nonfinite JSON evidence number")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if type(value) is not dict or canonical_bytes(value) + b"\n" != data:
            raise ValueError("Noncanonical or incomplete evidence record")
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError("Invalid JSON evidence record") from error


def _read_document(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("Evidence document exceeds V1 size limit")
    return _decode(data)


def verify_run(run_dir: Path, *, allow_incomplete: bool = False,
               expected_final_hash: str | None = None,
               max_events: int | None = None) -> dict:
    """Offline, read-only audit. Never repairs or silently ignores a torn tail."""
    if max_events is not None and (type(max_events) is not int or max_events < 1):
        raise ValueError("Invalid evidence event budget")
    run_dir = Path(run_dir)
    manifest = _read_document(run_dir / "manifest.json")
    validate_manifest(manifest)
    manifest_hash = digest(manifest)
    previous = manifest_hash
    last_monotonic = -1
    count = 0
    finished = False
    outcome = None
    with (run_dir / "events.jsonl").open("rb") as stream:
        while True:
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if max_events is not None and count >= max_events:
                raise ValueError("Evidence event count exceeds verification budget")
            if len(line) > MAX_RECORD_BYTES:
                raise ValueError("Evidence event exceeds V1 size limit")
            event = _decode(line)
            validate_event(event)
            if finished or event["run_id"] != manifest["run_id"] or event["sequence"] != count + 1:
                raise ValueError("Mixed, reordered, duplicate, or post-terminal evidence")
            if event["prev_hash"] != previous:
                raise ValueError("Broken evidence hash chain")
            computed = digest({key: value for key, value in event.items() if key != "event_hash"})
            if computed != event["event_hash"]:
                raise ValueError("Evidence hash mismatch")
            monotonic = event["time"]["monotonic_ns"]
            if monotonic < last_monotonic:
                raise ValueError("Monotonic evidence time regressed")
            if count == 0:
                if event["event_type"] != "run_started" or event["payload"] != {"manifest_hash": manifest_hash}:
                    raise ValueError("Missing manifest-bound run start")
            elif event["event_type"] == "run_started":
                raise ValueError("Duplicate run start")
            if event["event_type"] == "run_finished":
                _keys(event["payload"], {"outcome", "error_type"})
                outcome = event["payload"]["outcome"]
                _optional_text(event["payload"]["error_type"])
                if type(outcome) is not str or outcome not in {"returned", "error", "interrupted"}:
                    raise ValueError("Invalid run outcome")
                finished = True
            count += 1
            previous = computed
            last_monotonic = monotonic
    sealed = (run_dir / "integrity.json").exists()
    if sealed:
        seal = _read_document(run_dir / "integrity.json")
        _keys(seal, {"schema", "schema_version", "run_id", "manifest_hash", "event_count", "final_event_hash"})
        _integer(seal["schema_version"], 1)
        _integer(seal["event_count"], 1)
        _hash(seal["manifest_hash"])
        _hash(seal["final_event_hash"])
        if seal != {"schema": INTEGRITY_SCHEMA, "schema_version": 1,
                    "run_id": manifest["run_id"], "manifest_hash": manifest_hash,
                    "event_count": count, "final_event_hash": previous} or not finished:
            raise ValueError("Run seal does not match evidence")
    complete = finished and sealed
    if not complete and not allow_incomplete:
        raise ValueError("Run is incomplete or unsealed")
    if expected_final_hash is not None:
        _hash(expected_final_hash)
        if expected_final_hash != previous:
            raise ValueError("Trusted final hash does not match evidence")
    return {"run_id": manifest["run_id"], "event_count": count, "complete": complete,
            "outcome": outcome, "manifest_hash": manifest_hash, "final_event_hash": previous}


def cli() -> None:
    parser = argparse.ArgumentParser(description="Verify local research evidence without game/API access")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--expected-final-hash")
    args = parser.parse_args()
    try:
        result = verify_run(args.run_dir, allow_incomplete=args.allow_incomplete,
                            expected_final_hash=args.expected_final_hash)
    except (OSError, ValueError) as error:
        # No path, record contents, or provider details in diagnostic output.
        parser.exit(1, f"Research evidence verification failed ({type(error).__name__}).\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    cli()
