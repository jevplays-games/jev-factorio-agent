"""Strict, observation-only evidence for one native handcraft job.

The engine event receipt, not an inventory delta or a success message, identifies
production. No function here contacts the game or authorizes replay.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass


class InvalidCraftEvidence(ValueError):
    """Missing, inconsistent or regressed native evidence."""


_ACTOR_OBSERVATION_CODES = frozenset({
    "actor_unavailable", "actor_changed", "actor_policy_changed",
})
_ACTOR_OBSERVATION_MARKER = "JEV_CRAFT_OBSERVATION_FAILURE|"


class CraftActorObservationFailure(InvalidCraftEvidence):
    """A native observation could not pass the original-player safety guard."""

    def __init__(self, code: str, receipt: str | None = None) -> None:
        if type(code) is not str or code not in _ACTOR_OBSERVATION_CODES:
            raise ValueError("Unknown craft actor observation failure")
        if receipt is not None:
            identifier(receipt)
        self.code = code
        self.receipt = receipt
        super().__init__("Original craft actor could not be observed")


def craft_actor_observation_failure(value: object) -> CraftActorObservationFailure:
    """Parse the bounded native failure marker without accepting raw Lua errors."""
    if (not isinstance(value, dict) or set(value) != {"schema", "receipt", "code"}
            or type(value.get("schema")) is not int or value["schema"] != 1
            or type(value.get("code")) is not str
            or value["code"] not in _ACTOR_OBSERVATION_CODES):
        raise InvalidCraftEvidence("Invalid craft actor observation marker")
    return CraftActorObservationFailure(value["code"], identifier(value["receipt"]))


def parse_craft_actor_observation_failure(raw: object) -> CraftActorObservationFailure | None:
    """Decode one exact marker line; never infer actor failures from free-form errors."""
    if not isinstance(raw, str):
        return None
    lines = [line for line in raw.splitlines() if line.strip()]
    marker_lines = [line for line in lines if line.startswith(_ACTOR_OBSERVATION_MARKER)]
    if not marker_lines:
        return None
    if len(lines) != 1 or len(marker_lines) != 1 or len(raw) > 4096:
        raise InvalidCraftEvidence("Ambiguous craft actor observation marker")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate craft actor marker field")
            value[key] = item
        return value

    try:
        value = json.loads(
            marker_lines[0][len(_ACTOR_OBSERVATION_MARKER):], object_pairs_hook=unique_object)
    except (RecursionError, TypeError, ValueError) as error:
        raise InvalidCraftEvidence("Invalid craft actor observation marker") from error
    return craft_actor_observation_failure(value)


def natural(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or value < int(positive):
        raise InvalidCraftEvidence("Invalid crafting counter")
    return value


def identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise InvalidCraftEvidence("Invalid crafting identity")
    return value


def counts(value: object, *, positive: bool = False) -> dict[str, int]:
    if not isinstance(value, dict) or not value or len(value) > 32:
        raise InvalidCraftEvidence("Invalid crafting item map")
    return {identifier(key): natural(amount, positive=positive) for key, amount in value.items()}


def receipt_for(parameters: dict, snapshot) -> dict:
    """Validate receipt shape and bind it to the current actor/session/request."""
    factory = snapshot.factory
    actor, receipt = factory.get("craft_job_actor"), factory.get("craft_job")
    if not isinstance(actor, dict) or not isinstance(receipt, dict):
        raise InvalidCraftEvidence("Native crafting evidence is unavailable")
    if (type(factory.get("craft_jobs_protocol")) is not int
            or factory.get("craft_jobs_protocol") != 1
            or receipt.get("paid") is not True
            or factory.get("player_connected") is not True
            or factory.get("player_bound") is not True
            or receipt.get("id") != parameters.get("receipt")
            or receipt.get("recipe") != parameters.get("recipe")
            or receipt.get("requested") != parameters.get("batches")
            or receipt.get("session_id") != snapshot.session_id
            or actor.get("session_id") != snapshot.session_id):
        raise InvalidCraftEvidence("Native crafting identity mismatch")
    for key in ("player_index", "unit_number", "surface_index", "force_index"):
        if natural(receipt.get(key), positive=True) != natural(actor.get(key), positive=True):
            raise InvalidCraftEvidence("Native crafting actor changed")
    identifier(receipt.get("id"))
    identifier(receipt.get("recipe"))
    requested = natural(receipt.get("requested"), positive=True)
    if not requested <= 200 or natural(receipt.get("accepted")) != requested:
        raise InvalidCraftEvidence("Native crafting request was not fully accepted")
    finished = natural(receipt.get("finished"))
    started, last = natural(receipt.get("started_tick")), natural(receipt.get("last_progress_tick"))
    if finished > requested or not started <= last <= snapshot.tick:
        raise InvalidCraftEvidence("Native crafting progress is inconsistent")
    inputs = counts(receipt.get("inputs"), positive=True)
    baseline = counts(receipt.get("baseline"))
    outputs = counts(receipt.get("outputs"), positive=True)
    if (len(outputs) != 1 or set(outputs) != set(baseline) or set(inputs) & set(outputs)
            or any(total % requested for total in outputs.values())):
        raise InvalidCraftEvidence("Unsupported native crafting products")
    if receipt.get("status") not in {"running", "completed"}:
        raise InvalidCraftEvidence("Native crafting was cancelled or contaminated")
    if receipt["status"] == "completed":
        completed = natural(receipt.get("completed_tick"))
        if finished != requested or not last <= completed <= snapshot.tick:
            raise InvalidCraftEvidence("Native crafting completion is inconsistent")
    elif finished >= requested:
        raise InvalidCraftEvidence("Native crafting status is inconsistent")
    if receipt.get("queue_valid") is not True:
        raise InvalidCraftEvidence("Native crafting queue changed")
    return receipt


def craft_complete(parameters: dict, snapshot) -> bool:
    try:
        receipt = receipt_for(parameters, snapshot)
        return receipt["status"] == "completed" and all(
            snapshot.inventory.get(item, 0) >= receipt["baseline"][item] + amount
            for item, amount in receipt["outputs"].items()
        )
    except (InvalidCraftEvidence, TypeError, AttributeError):
        return False


@dataclass
class CraftJob:
    """A positively acknowledged job; its outputs stay locked until verified."""

    parameters: dict
    plan_id: str
    goal: str
    session_id: str
    actor: dict
    inputs: dict[str, int]
    outputs: dict[str, int]
    baseline: dict[str, int]
    started_tick: int
    deadline_tick: int
    finished: int = 0
    last_progress_tick: int = 0
    failed: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> CraftJob:
        try:
            job = cls(**deepcopy(data))
            if (not isinstance(job.parameters, dict)
                    or set(job.parameters) != {"recipe", "batches", "receipt"}):
                raise InvalidCraftEvidence("Invalid background request")
            for key in ("recipe", "receipt"):
                identifier(job.parameters[key])
            requested = natural(job.parameters["batches"], positive=True)
            if requested > 200:
                raise InvalidCraftEvidence("Background batch exceeds budget")
            for value in (job.plan_id, job.goal, job.session_id):
                identifier(value)
            if (not isinstance(job.actor, dict)
                    or set(job.actor) != {"player_index", "unit_number", "surface_index", "force_index"}):
                raise InvalidCraftEvidence("Invalid background actor")
            for value in job.actor.values():
                natural(value, positive=True)
            counts(job.inputs, positive=True)
            counts(job.outputs, positive=True)
            counts(job.baseline)
            if (len(job.outputs) != 1 or set(job.outputs) != set(job.baseline)
                    or set(job.inputs) & set(job.outputs)
                    or any(total % requested for total in job.outputs.values())):
                raise InvalidCraftEvidence("Invalid background products")
            if not (natural(job.started_tick) <= natural(job.last_progress_tick)
                    < natural(job.deadline_tick)):
                raise InvalidCraftEvidence("Invalid background deadline")
            if natural(job.finished) >= requested or not isinstance(job.failed, str) or len(job.failed) > 160:
                raise InvalidCraftEvidence("Invalid background progress")
            return job
        except (TypeError, KeyError, AttributeError) as error:
            raise InvalidCraftEvidence("Invalid background checkpoint") from error

    @classmethod
    def admit(cls, plan, pending: dict, snapshot, catalog) -> CraftJob:
        step = plan.steps[0]
        if (len(plan.steps) != 1 or step.action != "factory_craft_job"
                or pending.get("dispatch") != "returned" or pending.get("action") != step.action):
            raise InvalidCraftEvidence("Only acknowledged single-step crafts may run in background")
        receipt = receipt_for(step.parameters, snapshot)
        recipe = catalog.recipes.get(step.parameters["recipe"], {})
        batches = step.parameters["batches"]
        expected_inputs = {entry["name"]: entry["amount"] * batches
                           for entry in recipe.get("ingredients", []) if entry["type"] == "item"}
        expected_outputs = {entry["name"]: entry["amount"] * batches
                            for entry in recipe.get("products", []) if entry["type"] == "item"}
        if (receipt["inputs"] != expected_inputs or receipt["inputs"] != step.costs
                or receipt["outputs"] != expected_outputs
                or receipt["status"] != "running"
                or receipt["started_tick"] < pending["started_tick"]):
            raise InvalidCraftEvidence("Native craft does not match the committed plan")
        data = dict(
            parameters=deepcopy(step.parameters), plan_id=plan.id, goal=plan.goal,
            session_id=snapshot.session_id,
            actor={key: receipt[key] for key in ("player_index", "unit_number", "surface_index", "force_index")},
            inputs=deepcopy(receipt["inputs"]), outputs=deepcopy(receipt["outputs"]),
            baseline=deepcopy(receipt["baseline"]), started_tick=receipt["started_tick"],
            # Write-ahead persistence can delay dispatch after its observation.
            # Only this validated, acknowledged native start anchors execution;
            # the resulting deadline stays fixed across polls and resumes.
            deadline_tick=receipt["started_tick"] + step.timeout_ticks,
            finished=receipt["finished"], last_progress_tick=receipt["last_progress_tick"],
        )
        return cls.from_dict(data)

    def observe(self, snapshot) -> bool:
        """Update monotonic evidence; return True only for verified completion."""
        if self.failed:
            raise InvalidCraftEvidence(self.failed)
        receipt = receipt_for(self.parameters, snapshot)
        if (snapshot.session_id != self.session_id
                or any(receipt[key] != value for key, value in self.actor.items())
                or any(receipt[key] != getattr(self, key) for key in ("inputs", "outputs", "baseline", "started_tick"))
                or receipt["finished"] < self.finished
                or receipt["last_progress_tick"] < self.last_progress_tick):
            raise InvalidCraftEvidence("Background evidence changed or regressed")
        if receipt["status"] == "completed":
            if receipt["completed_tick"] > self.deadline_tick or not craft_complete(self.parameters, snapshot):
                raise InvalidCraftEvidence("Background output is late or missing")
            return True
        if snapshot.tick >= self.deadline_tick:
            raise InvalidCraftEvidence("Background crafting exceeded its deadline")
        # The event fires before insertion. Routine observations happen later;
        # a shortfall here is external output consumption or missing evidence.
        for item, total in self.outputs.items():
            earned = receipt["finished"] * (total // self.parameters["batches"])
            if snapshot.inventory.get(item, 0) < self.baseline[item] + earned:
                raise InvalidCraftEvidence("Background output was consumed before verification")
        self.finished, self.last_progress_tick = receipt["finished"], receipt["last_progress_tick"]
        return False

    def permits(self, step) -> bool:
        """No second craft, construction, or consumption of any locked output."""
        return not self.failed and permits_locked_outputs(step, set(self.outputs))


def permits_locked_outputs(step, outputs: set[str]) -> bool:
    """Apply the background output lock before or after a paid job is admitted."""
    if (not outputs or step.action not in {"factory_gather", "factory_insert",
                                        "factory_extract", "factory_wait"}):
        return False
    parameters = step.parameters or {}
    touched = set(step.costs or {})
    touched.update(value for value in (parameters.get("item"), parameters.get("resource")) if value)
    return not (touched & outputs)
