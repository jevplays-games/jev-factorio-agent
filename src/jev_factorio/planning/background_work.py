"""Bounded research resupply and independent work during a tracked handcraft."""
from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import replace

from ..craft_jobs import CraftJob
from ..skills import Plan, Step
from ..factory_contract import connected
from .ready_work import ReadyWorkPlanner
from .demand import SupplyLedger
from .scheduling import research_schedule, future_research_demands, future_research_plan

RESEARCH_PREFETCH_BINDING = "background_research_prefetch"


def research_demands(snapshot, catalog, *, early: bool = False) -> list[tuple[str, int]]:
    """Refill before forecast starvation, with the existing bounded quantities."""
    return [(row["item"], row["amount"]) for row in
            research_schedule(snapshot, catalog, early=early) if row["due"]]


def independent_candidates(goal, snapshot, catalog, job: CraftJob | None = None,
                           planner_type: type[ReadyWorkPlanner] = ReadyWorkPlanner) -> list[Plan]:
    """Use the active production capabilities even during independent work.

    Forecast outputs allow dependency lookahead only. Admission still uses the
    original snapshot and the acknowledged job's output/dispatch locks. Never
    fall back to a less capable planner if a route/buffer plan is unavailable.
    """
    view = deepcopy(snapshot)
    if job:
        view.factory["crafting_queue"] = 0  # Permit planning, never dispatch permission.
        for item, amount in job.outputs.items():
            view.inventory[item] = max(view.inventory.get(item, 0), job.baseline[item] + amount)
    ledger = SupplyLedger.capture(snapshot, catalog, job=job)
    def new_planner():
        worker = planner_type(catalog, view, goal)
        worker.ledger = ledger
        worker.allow_service_visits = False
        return worker
    candidates = []
    worker = new_planner()
    # Keep the existing boiler alive while handcrafting; do not build new power.
    if (job and "utility:boiler" in worker.entities
            and all(connected(snapshot.factory, source, target, kind, fluid)
                    for source, target, kind, fluid in (
                        ("utility:water", "utility:boiler", "pipe", "water"),
                        ("utility:boiler", "utility:engine", "pipe", "steam"),
                        ("utility:engine", "utility:lab", "small-electric-pole", "electricity"),
                    ))):
        try:
            research = snapshot.factory.get("research")
            path = ("technology:" + research,) if research else ()
            maintenance = worker._powered("utility:lab", path)
            if maintenance:
                candidates.append(maintenance)
        except (KeyError, ValueError):
            pass
    probes = 0
    if goal == "rocket_launch":
        due_rows = {row["item"]: row for row in
                    research_schedule(snapshot, catalog, early=job is not None)
                    if row["due"]}
        for item, amount in research_demands(snapshot, catalog, early=job is not None):
            if job and item in job.outputs:
                continue
            try:
                worker = new_planner()
                if snapshot.inventory.get(item, 0) >= amount:
                    plan = worker._transfer("utility:lab", item, amount)
                else:
                    plan = worker._need(item, amount)
                if plan:
                    step = plan.steps[0]
                    parameters = step.parameters or {}
                    row = due_rows.get(item)
                    materials = dict(plan.materials or {})
                    if (row is not None and step.action == "factory_insert"
                            and step.effect == "transfer"
                            and parameters.get("role") == "utility:lab"
                            and parameters.get("item") == item
                            and parameters.get("quantity") == amount
                            and isinstance(parameters.get("receipt"), str)):
                        materials[RESEARCH_PREFETCH_BINDING] = {
                            "schema": 1, "observed_tick": snapshot.tick,
                            "research": snapshot.factory.get("research"),
                            "item": item, "quantity": amount,
                            "demand": row["amount"], "remaining": row["remaining"],
                            "receipt": parameters["receipt"],
                        }
                    candidates.append(replace(
                        plan, materials=materials or None,
                        description=f"Prefetch research supply: {amount} {item}. "
                                    + plan.description))
                # A locked intermediate or busy handcraft must not hide an
                # independent raw ingredient of this same science batch.
                for material, target in sorted(worker.targets.items()):
                    if probes >= 32:
                        break
                    probes += 1
                    probe = new_planner()
                    probe.focus = worker.focus
                    probe.raw_targets = dict(worker.raw_targets)
                    probe.demands = dict(worker.demands)
                    probe.speculative = True
                    try:
                        alternative = probe._need(material, target)
                    except (KeyError, ValueError):
                        continue
                    if alternative:
                        candidates.append(alternative)
            except (KeyError, ValueError):
                continue
    # Fully supplied research frees the actor to prepare one next batch.
    # This preview never starts/cancels research or spends future job output.
    if goal == "rocket_launch":
        for item, amount in future_research_demands(snapshot, catalog):
            try:
                worker = new_planner()
                candidate = future_research_plan(worker, item, amount)
                if candidate and candidate.steps[0].action not in {"factory_research", "factory_wait"}:
                    candidates.append(replace(candidate, description=f"Prepare next research batch: {amount} {item}. "
                                              + candidate.description))
            except (ValueError, KeyError):
                continue
    if job:
        try:
            candidates.extend(new_planner().candidates())
        except (KeyError, ValueError):
            pass  # Unsupported lookahead cannot bypass active production rules.
    unique = {}
    rejected = []
    for plan in candidates:
        step = plan.steps[0]
        if (len(plan.steps) != 1 or step.action == "factory_wait"
                or (job and not job.permits(step))
                or not step.allowed(snapshot) or step.satisfied(snapshot)):
            reason = ('single_step_or_wait' if len(plan.steps) != 1 or step.action == 'factory_wait'
                      else 'acknowledged_job_reservation' if job and not job.permits(step)
                      else 'native_precondition' if not step.allowed(snapshot) else 'already_satisfied')
            if len(rejected) < 32:
                rejected.append({'plan_id': plan.id, 'action': step.action, 'reason': reason})
            continue
        unique.setdefault(plan.id, plan)
        if len(unique) >= 8:
            break
    if getattr(snapshot, '_campaign_diagnostics', False):
        snapshot._background_eligibility = {'candidate_count': len(candidates),
            'eligible_count': len(unique), 'rejected': rejected,
            'job_active': job is not None, 'probe_count': probes, 'probe_budget': 32,
            'reason': 'eligible_work' if unique else 'no_independent_safe_candidate'}
    return list(unique.values())


def background_wait(goal: str, job: CraftJob, tick: int) -> Plan:
    return Plan(
        "background-wait:" + job.parameters["receipt"], goal,
        "No independent ready work; observe the tracked native crafting queue",
        (Step("factory_wait", "crafting_idle", timeout_ticks=max(1, job.deadline_tick - tick)),),
    )
