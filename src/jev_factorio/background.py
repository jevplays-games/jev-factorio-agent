"""Opt-in productive background work, using the existing single dispatcher.

Only a fully accepted, receipt-bound craft may leave pending verification. All
uncertain dispatches retain the original write-ahead and no-replay barrier.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from .controller import HierarchicalLoop
from .craft_jobs import CraftJob, InvalidCraftEvidence
from .memory import CampaignMemory, retain_latest_craft
from .planning.background_work import (
    RESEARCH_PREFETCH_BINDING, background_wait, independent_candidates,
)
from .planning.ready_work import ReadyWorkPlanner
from .planning.scheduling import research_schedule
from .skills import Plan, Step
from .telemetry import fingerprint, phase, utc_now, validate_attempt

_BACKGROUND_WAIT_ROLLOVER = "background_wait_rollover"


def _craft_job_step_fingerprint(job: CraftJob) -> str:
    """Rebuild the canonical step for a legacy schema-2 job.

    Schema 2 did not persist the full step. Accept it only when the hash proves
    this canonical form; otherwise its exact binding cannot be recovered.
    """
    if len(job.outputs) != 1:
        raise ValueError("Background craft does not have one bound output")
    item, output = next(iter(job.outputs.items()))
    step = Step(
        action="factory_craft_job", effect="craft_job_complete", item=item,
        threshold=job.baseline[item] + output, costs=deepcopy(job.inputs),
        timeout_ticks=job.deadline_tick - job.started_tick,
        parameters=deepcopy(job.parameters),
    )
    return fingerprint(asdict(step))


def _validate_background_step(job: CraftJob, attempt: dict, data: dict) -> Step:
    """Validate a schema-3 saved step against both its attempt and craft job."""
    if not isinstance(data, dict):
        raise ValueError("Background attempt step is missing")
    try:
        step = Step(**deepcopy(data))
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid background attempt step") from error
    if (asdict(step) != data
            or attempt["step_sha256"] != fingerprint(asdict(step))
            or step.action != "factory_craft_job"
            or step.effect != "craft_job_complete"
            or step.parameters != job.parameters
            or step.costs != job.inputs
            or set(job.outputs) != {step.item}
            or step.timeout_ticks != job.deadline_tick - job.started_tick):
        raise ValueError("Background attempt step fingerprint or job binding mismatch")
    return step


def _validate_background_wait_rollover(memory: BackgroundMemory) -> None:
    """Validate the optional progress witness inside an active wait plan."""
    if memory.active_plan is None:
        return
    plan = Plan.from_dict(memory.active_plan)
    materials = plan.materials or {}
    if not isinstance(materials, dict) or _BACKGROUND_WAIT_ROLLOVER not in materials:
        return
    marker = materials[_BACKGROUND_WAIT_ROLLOVER]
    pending, attempt = memory.pending, memory.attempt
    try:
        job = CraftJob.from_dict(memory.background_job)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Background wait rollover has no valid craft job") from error
    if not isinstance(pending, dict) or not isinstance(attempt, dict):
        raise ValueError("Background wait rollover has no active wait attempt")
    step = plan.steps[memory.step_index]
    phases = attempt.get("dispatch_phases")
    dispatch = phases.get("dispatch") if isinstance(phases, dict) else None
    if (not isinstance(marker, dict)
            or set(marker) != {"schema", "receipt", "plan_id", "attempt_id", "tick"}
            or type(marker.get("schema")) is not int or marker["schema"] != 1
            or marker.get("receipt") != job.parameters["receipt"]
            or marker.get("plan_id") != plan.id
            or marker.get("attempt_id") != attempt.get("id")
            or type(marker.get("tick")) is not int
            or type(pending.get("started_tick")) is not int
            or marker["tick"] <= pending["started_tick"]
            or marker["tick"] > memory.last_tick
            or marker["tick"] >= job.deadline_tick
            or type(step.timeout_ticks) is not int
            or marker["tick"] >= pending["started_tick"] + step.timeout_ticks
            or plan.id != "background-wait:" + job.parameters["receipt"]
            or plan.goal != job.goal or len(plan.steps) != 1 or memory.step_index != 0
            or step.action != "factory_wait" or step.effect != "crafting_idle"
            or step.parameters is not None
            or pending.get("action") != "factory_wait"
            or pending.get("dispatch") != "returned"
            or attempt.get("action") != "factory_wait"
            or attempt.get("plan_id") != plan.id or attempt.get("step_index") != 0
            or attempt.get("started_tick") != pending["started_tick"]
            or attempt.get("step_sha256") != fingerprint(asdict(step))
            or not isinstance(dispatch, dict) or dispatch.get("status") != "returned"):
        raise ValueError("Background wait rollover witness is not bound to the active job and attempt")


@dataclass
class BackgroundMemory(CampaignMemory):
    # Schema 3 is required only while the active job carries its exact Step.
    # Preserve the schema-2 empty/completed extension for existing consumers.
    background_schema: int = 2
    background_job: dict | None = None
    background_attempt: dict | None = None
    background_step: dict | None = None

    @classmethod
    def _from_data(cls, data: dict, session_id: str, target: str) -> BackgroundMemory:
        memory = super()._from_data(data, session_id, target)
        if type(memory.background_schema) is not int or memory.background_schema not in {1, 2, 3}:
            raise ValueError("Unsupported background checkpoint extension")
        if memory.background_schema == 1:
            if memory.background_attempt is not None or memory.background_step is not None:
                raise ValueError("Legacy background checkpoint has unexpected attempt")
            if memory.background_job is None:
                memory.background_schema = 2
        elif memory.background_schema == 2:
            if memory.background_step is not None:
                raise ValueError("Legacy background checkpoint has unexpected step")
            if (memory.background_job is None) != (memory.background_attempt is None):
                raise ValueError("Background job and attempt identity must coexist")
        elif ((memory.background_job is None) != (memory.background_attempt is None)
              or (memory.background_job is None) != (memory.background_step is None)):
            raise ValueError("Background job and attempt identity must coexist")
        if memory.background_job is not None:
            job = CraftJob.from_dict(memory.background_job)
            attempt = memory.background_attempt
            if attempt is not None:
                validate_attempt(attempt)
                if memory.background_schema == 2:
                    try:
                        expected_step_sha256 = _craft_job_step_fingerprint(job)
                    except (TypeError, ValueError, KeyError) as error:
                        raise ValueError("Legacy background step binding is unprovable") from error
                else:
                    _validate_background_step(job, attempt, memory.background_step)
                    expected_step_sha256 = attempt["step_sha256"]
                if (attempt["action"] != "factory_craft_job"
                        or attempt["plan_id"] != job.plan_id
                        or attempt["step_index"] != 0
                        or attempt["receipt"] != job.parameters["receipt"]
                        or attempt["step_sha256"] != expected_step_sha256
                        or attempt["started_tick"] > job.started_tick
                        or (memory.attempt and attempt["id"] == memory.attempt["id"])
                        or any(item["id"] == attempt["id"] for item in memory.attempt_outcomes)):
                    raise ValueError("Background attempt identity or step fingerprint mismatch")
            if (job.session_id != session_id or job.goal != memory.active_goal
                    or job.started_tick > memory.last_tick
                    or job.last_progress_tick > memory.last_tick
                    or memory.status == "completed"):
                raise ValueError("Background checkpoint identity or tick mismatch")
            if memory.active_plan and any(
                (step.get("parameters") or {}).get("receipt") == job.parameters["receipt"]
                for step in memory.active_plan["steps"]
            ):
                raise ValueError("Craft cannot be both foreground and background")
        _validate_background_wait_rollover(memory)
        return memory



class BackgroundWorkLoop(HierarchicalLoop):
    memory_type = BackgroundMemory
    planner_type = ReadyWorkPlanner

    def __init__(self, backend, jev=None, **options) -> None:
        if options.get("factory_scheduling") != "ready-work":
            raise ValueError("Background work requires ready-work scheduling")
        self._save_poisoned = False
        super().__init__(backend, jev, **options)
        if self.catalog is None:
            raise ValueError("Background work requires a native production catalog")
        # Backend decorators install telemetry only for this opt-in controller.
        # Test backends implement the same receipt protocol without game access.
        native = getattr(backend, "_factory", None)
        if native is not None:
            from .backends import has_adapter
            from .backends.craft_jobs import CraftJobFactory

            if not has_adapter(native, CraftJobFactory):
                backend._factory = CraftJobFactory(native)
        elif getattr(backend, "craft_jobs_supported", False) is not True:
            raise ValueError("Backend does not support native craft receipts")

    def _save(self) -> None:
        if self._save_poisoned:
            raise RuntimeError("Checkpoint persistence failed; reconstruct before continuing")
        try:
            super()._save()
        except BaseException:
            self._save_poisoned = True
            raise

    def _job(self) -> CraftJob | None:
        data = self.memory.background_job if self.memory else None
        return CraftJob.from_dict(data) if data is not None else None

    def _observe(self, stage="observe"):
        if self._save_poisoned:
            raise RuntimeError("Checkpoint persistence failed; reconstruct before continuing")
        snapshot = super()._observe(stage)
        job = self._job()
        if job:
            attempt = self.memory.background_attempt
            prior_verified_attempts = {
                outcome["id"] for outcome in self.memory.attempt_outcomes
                if outcome.get("outcome") == "verified"
            }
            evidence = {**self._trace.attempt_ref(attempt["id"] if attempt else None),
                        "plan_id": job.plan_id, "receipt": job.parameters["receipt"]}
            try:
                complete = self._trace.call(
                    "background_job_observed", lambda: job.observe(snapshot),
                    details=evidence, result=lambda verified: {"verified": verified})
            except InvalidCraftEvidence as error:
                job.failed = str(error)
                self.memory.background_job = job.to_dict()
                self.memory.status, self.memory.reason = "uncertain", job.failed
                self.memory.event("background_job_uncertain", job=job.parameters["receipt"],
                                  reason=job.failed, tick=snapshot.tick)
                self._last_background_observation = {
                    "background_state": "uncertain", "verified_attempt_added": False,
                }
            else:
                self.memory.background_job = None if complete else job.to_dict()
                if complete:
                    attempt = self.memory.background_attempt
                    verified_attempt_added = bool(
                        attempt is not None and attempt["id"] not in prior_verified_attempts
                    )
                    if attempt is not None:
                        self.memory.attempt_outcomes.append({
                            **deepcopy(attempt), "outcome": "verified", "finished_tick": snapshot.tick,
                            "finished_at_utc": utc_now(), "latency_seconds": None,
                        })
                        self.memory.attempt_outcomes = retain_latest_craft(self.memory.attempt_outcomes)
                    self.memory.background_attempt = None
                    self.memory.background_step = None
                    self.memory.background_schema = 2
                    # Verified native progress breaks a consecutive no-choice streak.
                    self.memory.stalled_decisions = 0
                    self.memory.event("background_job_completed", job=job.parameters["receipt"],
                                      plan=job.plan_id, outputs=job.outputs, tick=snapshot.tick)
                    self._last_background_observation = {
                        "background_state": "verified_completed",
                        "verified_attempt_added": verified_attempt_added,
                    }
                else:
                    self._last_background_observation = {
                        "background_state": "pending", "verified_attempt_added": False,
                    }
            # Persist updates before another action; this also protects the
            # release of output locks when completion is observed after restart.
            self._save()
            if self.memory.background_job is None:
                self._trace.emit("background_job_completed", {**evidence, "verified": True,
                                                              "outputs": job.outputs})
                self._trace.release_attempt(evidence["attempt_id"])
        else:
            self._last_background_observation = {
                "background_state": "none", "verified_attempt_added": False,
            }
        return snapshot

    def reconcile_only(self) -> dict:
        """Observe and durably reconcile one resumed background job, without acting.

        The normal hierarchical observation path validates session identity,
        connector/capital state, and native receipts. The background override
        above then verifies the persisted craft receipt and saves any outcome.
        This method deliberately does not plan, call Jev, or dispatch an action.
        """
        if not self.resume_controller or self.checkpoint is None:
            raise ValueError("Reconcile-only requires a resumed controller checkpoint")
        if self.factory_scheduling != "ready-work":
            raise ValueError("Reconcile-only requires ready-work scheduling")
        self._last_background_observation = None
        snapshot = self._observe(stage="reconcile")
        self._save()
        reconciliation = self._last_background_observation or {
            "background_state": "none", "verified_attempt_added": False,
        }
        return {
            "status": self.memory.status,
            "tick": snapshot.tick,
            **reconciliation,
        }

    def _execution_barrier(self, snapshot) -> bool:
        job = self._job()
        return (self._save_poisoned or bool(job and job.failed)
                or super()._execution_barrier(snapshot))

    def _step_allowed(self, step, snapshot) -> bool:
        job = self._job()
        return (not self._execution_barrier(snapshot)
                and (job is None or job.permits(step)) and super()._step_allowed(step, snapshot))

    def _investment_step_allowed(self, plan, step, snapshot) -> bool:
        if not super()._investment_step_allowed(plan, step, snapshot):
            return False
        materials = plan.materials or {}
        has_binding = RESEARCH_PREFETCH_BINDING in materials
        parameters = step.parameters or {}
        direct_lab_transfer = (
            step.action == "factory_insert" and step.effect == "transfer"
            and parameters.get("role") == "utility:lab"
        )
        named_prefetch = plan.description.startswith("Prefetch research supply:")
        if not has_binding:
            # Old checkpoints may contain the original descriptive prefetch
            # plan without this binding. Replan it before dispatch; generic
            # transfers and non-transfer preparation keep their old contract.
            return not (named_prefetch and direct_lab_transfer)

        binding = materials.get(RESEARCH_PREFETCH_BINDING)
        required = {"schema", "observed_tick", "research", "item", "quantity",
                    "demand", "remaining", "receipt"}
        if (not isinstance(binding, dict) or set(binding) != required
                or type(binding.get("schema")) is not int or binding["schema"] != 1
                or type(binding.get("observed_tick")) is not int or binding["observed_tick"] < 0
                or not isinstance(binding.get("research"), str) or not binding["research"]
                or not isinstance(binding.get("item"), str) or not binding["item"]
                or any(type(binding.get(key)) is not int or binding[key] <= 0
                       for key in ("quantity", "demand", "remaining"))
                or not isinstance(binding.get("receipt"), str) or not binding["receipt"]
                or not named_prefetch or not direct_lab_transfer
                or plan.goal != "rocket_launch"):
            return False
        item, quantity = binding["item"], binding["quantity"]
        if (binding["demand"] != quantity or binding["remaining"] < quantity
                or parameters.get("item") != item
                or parameters.get("quantity") != quantity
                or parameters.get("receipt") != binding["receipt"]
                or step.costs != {item: quantity}):
            return False

        current_research = snapshot.factory.get("research")
        if not isinstance(current_research, str) or not current_research:
            return False
        try:
            fresh_rows = research_schedule(
                snapshot, self.catalog, early=self._job() is not None)
        except (KeyError, TypeError, ValueError):
            return False
        row = next((candidate for candidate in fresh_rows
                    if candidate.get("item") == item and candidate.get("due") is True), None)
        if (row is None or type(row.get("amount")) is not int
                or type(row.get("remaining")) is not int
                or row["amount"] < quantity or row["remaining"] < quantity):
            return False

        # A different technology may still legitimately use the same pack.
        # The fresh current technology and its exact remaining due quantity
        # must independently support this whole prepared transfer.
        held_elsewhere = sum(
            costs.get(item, 0) for owner, costs in self.memory.reservations.items()
            if owner != plan.id
        )
        available = snapshot.inventory.get(item, 0) - held_elsewhere
        return type(available) in {int, float} and available >= quantity

    def _refresh_goals(self, snapshot) -> None:
        if self._job() is None:
            super()._refresh_goals(snapshot)

    def _record_extras(self) -> dict:
        return {**super()._record_extras(),
                "background_work": True, "background_schema": self.memory.background_schema,
                "background_job": deepcopy(self.memory.background_job),
                "background_attempt": deepcopy(self.memory.background_attempt)}

    def _background_admission_candidate(self, snapshot) -> CraftJob | None:
        if (self.memory.status != "running" or self.memory.background_job is not None
                or not self.memory.active_plan or not self.memory.pending):
            return None
        plan = Plan.from_dict(self.memory.active_plan)
        try:
            job = CraftJob.admit(plan, self.memory.pending, snapshot, self.catalog)
            # Also require all already produced output to be present before
            # freeing the actor. Admission is not completion verification.
            job.observe(snapshot)
        except InvalidCraftEvidence:
            return None  # Keep pending and its original deadline; never retry.
        step = plan.steps[self.memory.step_index]
        attempt = self.memory.attempt
        if (attempt is None
                or attempt["step_sha256"] != fingerprint(asdict(step))
                or step.effect != "craft_job_complete"
                or step.parameters != job.parameters
                or step.costs != job.inputs
                or set(job.outputs) != {step.item}
                or step.timeout_ticks != job.deadline_tick - job.started_tick):
            return None  # A paid receipt cannot replace an unbound foreground step.
        return job

    def _admit_background(self, snapshot, *, candidate: CraftJob | None = None) -> bool:
        if (self.memory.status != "running" or self.memory.background_job is not None
                or not self.memory.active_plan or not self.memory.pending):
            return False
        job = candidate or self._background_admission_candidate(snapshot)
        if job is None:
            return False
        plan = Plan.from_dict(self.memory.active_plan)
        self.memory.background_job = job.to_dict()
        self.memory.background_attempt = deepcopy(self.memory.attempt)
        self.memory.background_step = asdict(plan.steps[self.memory.step_index])
        self.memory.background_schema = 3
        evidence = {**self._trace.attempt_ref(
            self.memory.attempt["id"] if self.memory.attempt else None),
                    "plan_id": plan.id, "receipt": job.parameters["receipt"],
                    "inputs_paid": job.inputs, "outputs_locked": job.outputs}
        self.memory.event("background_job_admitted", job=job.parameters["receipt"],
                          plan=plan.id, inputs_paid=job.inputs, outputs_locked=job.outputs,
                          tick=snapshot.tick)
        self._clear_plan()  # Releases inputs proven already paid, not future outputs.
        self._save()
        self._trace.emit("background_job_admitted", evidence)
        return True

    def _record(self, before, action, outcome, after=None, verified=False):
        # The base dispatcher has already persisted dispatch=returned, then
        # taken its normal post-action observation. No extra game poll here.
        if not verified and after is not None and self._admit_background(after):
            outcome += "; tracked in background, output not yet verified"
        return super()._record(before, action, outcome, after, verified)

    def _verify_pending(self, snapshot):
        if self._execution_barrier(snapshot):
            return self._record(snapshot, "observe", self.memory.reason)
        pending = self.memory.pending
        plan = Plan.from_dict(self.memory.active_plan)
        step = plan.steps[self.memory.step_index]
        if step.action == "factory_craft_job":
            candidate = self._background_admission_candidate(snapshot)
            if candidate is not None:
                # Candidate creation proves a running receipt; the completion
                # predicate is therefore expected to be false. Record that
                # exact pending-poll observation before admission clears it.
                with phase("verification", self._diagnostic_trace):
                    verified = self._trace.verify(
                        step, snapshot, plan_id=plan.id, index=self.memory.step_index,
                        pending=pending, phase="pending_poll",
                        attempt_id=self.memory.attempt["id"],
                    )
                if not verified and self._admit_background(snapshot, candidate=candidate):
                    return self._record(snapshot, "observe", "Acknowledged craft continues in background")
        if (self.memory.status == "running" and pending.get("dispatch") == "returned"
                and step.action == "factory_wait"
                and step.effect in {"crafting_idle", "research_progress"}
                and not step.satisfied(snapshot)):
            candidates, _ = self._work_candidates(snapshot)
            if any(candidate.steps[0].action != "factory_wait"
                   and self.memory.failures.get(candidate.id, 0) < 2
                   and self._step_allowed(candidate.steps[0], snapshot)
                   for candidate in candidates):
                self.memory.event("background_wait_yielded", plan=plan.id, tick=snapshot.tick)
                self._finish_attempt(snapshot, "wait_replanned")
                self._clear_plan()
                return self._record(snapshot, "observe", "Yield passive wait to independent work")
        if self._roll_background_wait_poll_window(snapshot, plan, step, pending):
            receipt = self._job().parameters["receipt"]
            materials = deepcopy(plan.materials or {})
            materials[_BACKGROUND_WAIT_ROLLOVER] = {
                "schema": 1, "receipt": receipt, "plan_id": plan.id,
                "attempt_id": self.memory.attempt["id"], "tick": snapshot.tick,
            }
            self.memory.active_plan["materials"] = materials
            self.memory.event(
                "background_wait_poll_window_rolled", plan=plan.id,
                receipt=receipt,
                previous_polls=pending["polls"], tick=snapshot.tick,
            )
            pending["polls"] = 0
            # Persist the same owned job/attempt/deadline before the common
            # verifier starts the next bounded local observation window.
            self._save()
        return super()._verify_pending(snapshot)

    def _roll_background_wait_poll_window(self, snapshot, plan, step, pending) -> bool:
        job = self._job()
        attempt = self.memory.attempt
        if (job is None or job.failed or self.memory.status != "running"
                or pending.get("dispatch") != "returned"
                or pending.get("action") != "factory_wait"
                or plan.id != "background-wait:" + job.parameters["receipt"]
                or plan.goal != job.goal or step.action != "factory_wait"
                or step.effect != "crafting_idle" or step.parameters is not None
                or type(pending.get("started_tick")) is not int
                or type(pending.get("polls")) is not int
                or pending["polls"] + 1 < self.max_pending_polls
                or snapshot.tick >= job.deadline_tick
                or snapshot.tick - pending["started_tick"] >= step.timeout_ticks
                or step.satisfied(snapshot)
                or not isinstance(attempt, dict)
                or attempt.get("action") != "factory_wait"
                or attempt.get("plan_id") != plan.id
                or attempt.get("step_index") != self.memory.step_index
                or attempt.get("started_tick") != pending["started_tick"]
                or attempt.get("step_sha256") != fingerprint(asdict(step))):
            return False
        materials = plan.materials or {}
        if not isinstance(materials, dict):
            return False
        if _BACKGROUND_WAIT_ROLLOVER not in materials:
            last_rollover_tick = pending["started_tick"]
        else:
            rollover = materials[_BACKGROUND_WAIT_ROLLOVER]
            if (not isinstance(rollover, dict)
                    or set(rollover) != {"schema", "receipt", "plan_id", "attempt_id", "tick"}
                    or type(rollover.get("schema")) is not int or rollover["schema"] != 1
                    or rollover.get("receipt") != job.parameters["receipt"]
                    or rollover.get("plan_id") != plan.id
                    or rollover.get("attempt_id") != attempt.get("id")
                    or type(rollover.get("tick")) is not int
                    or rollover["tick"] < pending["started_tick"]
                    or rollover["tick"] > snapshot.tick):
                return False
            last_rollover_tick = rollover["tick"]
        if type(last_rollover_tick) is not int or snapshot.tick <= last_rollover_tick:
            return False
        dispatch = attempt.get("dispatch_phases", {}).get("dispatch")
        if not isinstance(dispatch, dict) or dispatch.get("status") != "returned":
            return False
        craft_attempt = self.memory.background_attempt
        if craft_attempt is not None and (
                craft_attempt.get("action") != "factory_craft_job"
                or craft_attempt.get("receipt") != job.parameters["receipt"]):
            return False
        return True

    def _tracked_plan(self, plan: Plan, snapshot) -> Plan:
        if len(plan.steps) != 1 or plan.steps[0].action != "factory_craft":
            return plan
        step = plan.steps[0]
        recipe = self.catalog.recipes[step.parameters["recipe"]]
        products = recipe.get("products", [])
        ingredients = recipe.get("ingredients", [])
        if (len(products) != 1 or products[0]["type"] != "item"
                or products[0].get("probability", 1) != 1
                or not ingredients or any(entry["type"] != "item" for entry in ingredients)
                or any(entry["name"] == products[0]["name"] for entry in ingredients)):
            return plan  # Unsupported recipes retain foreground verification.
        parameters = {**step.parameters, "receipt": uuid4().hex}
        return replace(plan, description=plan.description + "; native receipt-tracked output",
                       steps=(replace(step, action="factory_craft_job", effect="craft_job_complete",
                                      parameters=parameters),))

    def _compile_candidates(self, snapshot):
        job = self._job()
        if job:
            plans = independent_candidates(
                self.memory.active_goal, snapshot, self.catalog, job, self.planner_type)
            plans = [plan for plan in plans if self.memory.failures.get(plan.id, 0) < 2]
            return plans or [background_wait(self.memory.active_goal, job, snapshot.tick)], ""
        plans, blocker = super()._compile_candidates(snapshot)
        if (plans and plans[0].steps[0].action == "factory_wait"
                and plans[0].steps[0].effect == "research_progress"):
            independent = independent_candidates(
                self.memory.active_goal, snapshot, self.catalog, None, self.planner_type)
            plans = [plan for plan in independent
                     if self.memory.failures.get(plan.id, 0) < 2] or plans
        return [self._tracked_plan(plan, snapshot) for plan in plans], blocker
