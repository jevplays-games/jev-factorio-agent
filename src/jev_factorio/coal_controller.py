"""Explicit coal/solid controller composition, with durable whole-bundle ownership.

Production opt-in is carried by an immutable treatment file. Pinned native
qualification and an authorized handoff remain separate gates. This module
never resets a campaign or changes its cutoff.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path

from . import coal_supply as coal, solid_routes as solid
from .backends.coal_supply import CoalSupplyFactory
from .planning.coal_supply import MARKER, candidates
from .planning import coal_admission, coal_funding, solid_funding, solid_investment
from .planning.demand import SupplyLedger
from .research_log import RunConfiguration, ResearchLogError
from .skills import Plan
from .solid_controller import UNBOUND_FAULT as SOLID_UNBOUND
from .telemetry import fingerprint, phase

CHECKPOINT_FIELDS = {"coal_supply_schema", "coal_targets", "coal_epoch", "coal_commitments"}
UNBOUND_FAULT = "Coal-source epoch unbound; native reconciliation required"


def _ready_required_science(snapshot, catalog, plans) -> bool:
    """Current executable science is a bounded reason to defer a new kit."""
    from .planning.scheduling import research_schedule
    try:
        due = {row['item'] for row in research_schedule(snapshot, catalog) if row['due']}
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    if not due:
        return False
    entities = snapshot.factory.get('entities', {})
    for plan in plans:
        if len(plan.steps) != 1:
            continue
        step = plan.steps[0]
        p = step.parameters or {}
        item = p.get('item')
        if (step.action == 'factory_extract' and item in due
                and entities.get(p.get('role'), {}).get('output', {}).get(item, 0) > 0):
            return True
        if step.action == 'factory_insert' and p.get('role') == 'utility:lab' and item in due:
            return True
        if step.action == 'factory_craft':
            recipe = catalog.recipes.get(p.get('recipe'), {})
            if any(product.get('type') == 'item' and product.get('name') in due
                   for product in recipe.get('products', [])):
                return True
    return False


class CoalSupplyMixin:
    def __init__(self, backend, jev=None, *, coal_targets, coal_kit_policy=False,
                 coal_economic_admission=False, **options) -> None:
        if type(coal_kit_policy) is not bool or type(coal_economic_admission) is not bool:
            raise ValueError("Coal kit policy must be an explicit boolean")
        self._coal_kit_policy = coal_kit_policy
        self._coal_economic_admission = coal_economic_admission
        self._coal_admission_evidence = {}
        self._coal_admission_cache_key = None
        self._coal_kit_evidence = {}
        self._coal_targets = coal.validate_targets(coal_targets)
        self._coal_fault = False
        self._coal_protocol_fault = False
        self._coal_protocol_rejected_observation = False
        self._coal_protocol_defer_save = False
        self._coal_protocol_error = "Coal-source protocol is invalid or differs from configured treatment"
        self._coal_evidence = {}
        coal.validate_transport_intents(self._coal_targets, options.get("solid_intents"))
        sink = options.get("research_log")
        if sink is not None and (not isinstance(getattr(sink, "configuration", None), RunConfiguration)
                                 or sink.configuration.coal_supply is not True
                                 or sink.configuration.coal_kit_policy is not coal_kit_policy
                                 or sink.configuration.coal_economic_admission is not coal_economic_admission):
            raise ValueError("Research manifest must explicitly bind the coal treatment")
        if coal_economic_admission and not coal_kit_policy:
            raise ValueError("Coal economic admission requires coal kit policy")
        if options.get("resume_controller"):
            raw = Path(options["checkpoint"]).read_bytes()
            data = json.loads(raw)
            if (not isinstance(data, dict) or not CHECKPOINT_FIELDS <= data.keys()
                    or data["coal_targets"] != self._coal_targets or not data["coal_epoch"]
                    or data.get("coal_kit_policy", False) is not coal_kit_policy
                    or data.get("coal_economic_admission", False) is not coal_economic_admission
                    or data["coal_supply_schema"] != (2 if coal_economic_admission else 1)
                    or coal_kit_policy and "coal_funding" not in data):
                raise ValueError("Coal treatment cannot adopt or migrate an unbound checkpoint")
            # The inner solid initializer does the full composed memory validation
            # plus sticky pre/post-observation byte checks before any mutation.
        super().__init__(backend, jev, **options)
        native = getattr(backend, "_factory", None)
        if native is not None:
            current = native
            while current is not None and not isinstance(current, CoalSupplyFactory):
                current = getattr(current, "native", None)
            if current is None:
                backend._factory = CoalSupplyFactory(native, self._coal_targets,
                    coal_economic_admission=coal_economic_admission)
            elif (current.targets != self._coal_targets
                  or current.coal_economic_admission is not coal_economic_admission):
                raise ValueError("Existing native coal treatment differs")
        elif getattr(backend, "coal_supply_supported", False) is not True:
            raise ValueError("Backend does not support owned coal source observations")

    def _save(self):
        if self._coal_protocol_defer_save:
            return
        return super()._save()

    def _coal_protocol_matches_treatment(self, snapshot):
        """Validate the versioned envelope before binding protocol treatment.

        Detailed source-row ownership remains with ``coal.sources`` during the
        existing reconciliation phase; this boundary rejects only a malformed
        or cross-wired protocol/admission envelope before it can be checkpointed.
        """
        data = snapshot.factory.get("coal_supply")
        base_fields = {"protocol", "session_id", "tick", "actor_index", "surface_index",
                       "force_index", "targets", "committed", "sources", "reason"}
        if (not isinstance(data, dict) or type(data.get("protocol")) is not int
                or data["protocol"] not in (1, 2)
                or data["protocol"] != (2 if self._coal_economic_admission else 1)):
            return False
        protocol = data["protocol"]
        if set(data) != (base_fields if protocol == 1 else base_fields | {"admission"}):
            return False
        if (data["session_id"] != snapshot.session_id
                or type(data["tick"]) is not int or data["tick"] != snapshot.tick
                or any(type(data[key]) is not int or data[key] < 1
                       for key in ("actor_index", "surface_index", "force_index"))):
            return False
        if protocol == 2:
            admission = data["admission"]
            bound = {"session_id", "tick", "actor_index", "surface_index", "force_index"}
            if (not isinstance(admission, dict)
                    or set(admission) != bound | {"protocol", "qualified", "reason"}
                    or type(admission["protocol"]) is not int or admission["protocol"] != 1
                    or admission["session_id"] != data["session_id"]
                    or any(type(admission[key]) is not int or admission[key] != data[key]
                           for key in ("tick", "actor_index", "surface_index", "force_index"))
                    or admission["qualified"] is not False
                    or admission["reason"] != "electric_conversion_and_construction_cost_unknown"):
                return False
        return True

    def _observe_snapshot(self):
        snapshot = super()._observe_snapshot()
        if not self._coal_protocol_matches_treatment(snapshot):
            # Do not raise inside the observation phase: its diagnostic callback
            # would attach a new error to a retained pending attempt. Defer all
            # composed saves until _observe_solid rejects this same snapshot.
            self._coal_fault = True
            self._coal_protocol_fault = True
            self._coal_protocol_rejected_observation = True
            self._coal_protocol_defer_save = True
            self.memory.status = "uncertain"
            self.memory.reason = self._coal_protocol_error
            return snapshot
        if not self.memory.coal_targets:
            self.memory.coal_targets = list(self._coal_targets)
            self.memory.coal_kit_policy = self._coal_kit_policy
            self.memory.coal_economic_admission = self._coal_economic_admission
            self.memory.coal_supply_schema = 2 if self._coal_economic_admission else 1
        if not self.memory.coal_epoch:
            try:
                coal.sources(snapshot)
                self.memory.coal_epoch = {k: snapshot.factory["coal_supply"][k]
                                         for k in ("actor_index", "surface_index", "force_index")}
            except (ValueError, KeyError, TypeError, AttributeError):
                self._coal_fault = True
                self.memory.status, self.memory.reason = "uncertain", UNBOUND_FAULT
        return snapshot

    def _observe_solid(self, stage="observe"):
        if self._coal_protocol_fault:
            raise ValueError("Coal protocol fault is latched; reconstruct before continuing")
        self._coal_protocol_rejected_observation = False
        # The solid layer owns the outer first-resume transaction. Coal-specific
        # validation and saves must finish inside that same publication barrier.
        snapshot = super()._observe_solid(stage)
        if self._coal_protocol_rejected_observation:
            if not self._solid_resume_observing:
                self._coal_protocol_defer_save = False
            raise ValueError(self._coal_protocol_error)
        try:
            rows = coal.sources(snapshot)
            data = snapshot.factory["coal_supply"]
            epoch = {k: data[k] for k in ("actor_index", "surface_index", "force_index")}
            if (self.memory.coal_kit_policy is not self._coal_kit_policy
                    or self.memory.coal_economic_admission is not self._coal_economic_admission
                    or self.memory.coal_targets != self._coal_targets or data["targets"] != self._coal_targets
                    or self.memory.coal_epoch != epoch or any(not coal.current(row, snapshot) for row in rows.values())):
                raise ValueError("Coal source binding or ownership changed")
            from .construction_journal import require_owner
            for target, row in rows.items():
                require_owner(self.memory, action=coal.COMMAND, binding={"target": target},
                              layout=row["layout"], journal=row["pending"])
            plan = Plan.from_dict(self.memory.active_plan) if self.memory.active_plan else None
            step = plan.steps[self.memory.step_index] if plan else None
            tracked = self.memory.coal_commitments
            if tracked and (not data["committed"] or set(rows) != set(tracked)):
                raise ValueError("Coal commitment disappeared")
            if data["committed"] and not tracked:
                if not self.memory.pending or step is None or step.action != coal.COMMAND:
                    raise ValueError("Untracked native coal bundle")
                evidence = (plan.materials or {}).get(MARKER, {}).get("bundle")
                if not isinstance(evidence, dict) or set(evidence) != set(rows):
                    raise ValueError("Missing prepared whole-bundle commitment")
                for target, saved in evidence.items():
                    coal.validate_commitment(saved, target)
                    if not coal.reconciles(saved, rows[target]):
                        raise ValueError("Native coal bundle differs from prepared geometry")
                tracked = deepcopy(evidence)
            for target, saved in tracked.items():
                row = rows[target]
                if not coal.reconciles(saved, row):
                    raise ValueError("Coal source identity or payment regressed")
                new_parts = set(row["parts"]) - set(saved["parts"])
                if new_parts:
                    p = step.parameters if step else {}
                    if (not self.memory.pending or step is None or step.action != coal.COMMAND
                            or p["target"] != target or p["layout"] != row["layout"]
                            or new_parts != {p["part"]} or row["parts"][p["part"]]["receipt"] != p["receipt"]):
                        raise ValueError("Untracked paid coal part")
            if data["committed"]:
                self.memory.coal_commitments = {target: coal.commitment(row) for target, row in rows.items()}
            self._reconcile_coal_funding(snapshot)
            if not self._coal_fault:
                super()._finalize_solid_funding(snapshot)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            self._coal_fault = True
            self.memory.status = "uncertain"
            self.memory.reason = ("Coal-source evidence invalid; preserve pending work and ownership"
                                  if self.memory.coal_epoch else UNBOUND_FAULT)
        self._coal_evidence = deepcopy(snapshot.factory.get("coal_supply", {}))
        self._save()
        return snapshot

    def _execution_barrier(self, snapshot):
        return self._coal_fault or super()._execution_barrier(snapshot)

    def _coal_admission_allows_start(self, snapshot):
        # Admission covers the whole optional project, including a fully
        # carried kit. Existing ownership keeps its reconciliation/continuation
        # path; inventory availability alone is not economic permission.
        if (not self._coal_economic_admission or self.memory.coal_commitments
                or self.memory.coal_funding is not None):
            return True
        cache_key = (id(snapshot), snapshot.tick, self.memory.last_tick,
                     self.memory.active_goal, self.memory.status)
        if self._coal_admission_cache_key == cache_key:
            return self._coal_admission_evidence.get("eligible") is True
        factory = getattr(getattr(self, "backend", None), "_factory", None)
        while factory is not None and not isinstance(factory, CoalSupplyFactory):
            factory = getattr(factory, "native", None)
        try:
            if factory is None:
                raise ValueError("Native coal economics adapter is unavailable")
            projection = factory.economic_projection_v7(snapshot, self.memory)
            # Compute the complete source+corridor acquisition offer first so
            # eligibility can distinguish a fundable project from a bill that
            # happens to be fully carried already. This is a forecast only;
            # first payment still rechecks the whole carried bill natively.
            _, acquisition = coal_funding.candidate(
                snapshot, self.catalog, **self._coal_funding_options())
            projection["project_setup_cost_estimate"] = (
                coal_funding.project_setup_cost_estimate(snapshot, acquisition))
            evidence = coal_admission.evaluate(snapshot, self.memory,
                                               self.catalog, projection)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            evidence = {"eligible": False,
                        "reason": "native_current_goal_projection_unavailable",
                        "observed_tick": snapshot.tick,
                        "session_id": snapshot.session_id}
        self._coal_admission_evidence = evidence
        self._coal_admission_cache_key = cache_key
        return self._coal_admission_evidence.get("eligible") is True

    def _coal_prepared_first_payment_retry(self, step, snapshot):
        """Allow only the exact prepared first-payment retry to reach Lua.

        A fresh read-only v7 projection intentionally describes unpaid proposals
        and cannot authorize an already committed/prepared native transaction.
        For that one exact write-ahead attempt, the fixed same-RPC Lua guard is
        still the payment authority: it checks the current economics, owned
        builder, pending source identity, and one-use admission journal before
        performing the debit. Unknown/paid journal rows or any paid source
        prefix are rejected there and are never replayed here.
        """
        if not self._coal_economic_admission or step.action != coal.COMMAND:
            return False
        pending = self.memory.pending
        attempt = self.memory.attempt
        if (not isinstance(pending, dict)
                or pending.get("dispatch") not in {"prepared", "ambiguous"}
                or type(pending.get("polls")) is not int or pending["polls"] != 0
                or pending.get("action") != coal.COMMAND
                or type(pending.get("started_tick")) is not int
                or pending["started_tick"] > snapshot.tick
                or self.memory.session_id != snapshot.session_id
                or self.memory.last_tick != snapshot.tick
                or self.memory.coal_economic_admission is not True
                or self.memory.coal_kit_policy is not True
                or not isinstance(attempt, dict)
                or not isinstance(self.memory.active_plan, dict)):
            return False
        try:
            plan = Plan.from_dict(self.memory.active_plan)
            index = self.memory.step_index
            if not 0 <= index < len(plan.steps):
                return False
            bound_step = plan.steps[index]
            if bound_step != step:
                return False
            parameters = bound_step.parameters or {}
            coal.validate(parameters)
            if parameters["part"] != "chest":
                return False
            if (attempt.get("action") != coal.COMMAND
                    or attempt.get("plan_id") != plan.id
                    or attempt.get("step_index") != index
                    or attempt.get("step_sha256") != fingerprint(
                        self.memory.active_plan["steps"][index])
                    or attempt.get("started_tick") != pending["started_tick"]
                    or attempt.get("receipt") != parameters["receipt"]):
                return False
            rows = coal.sources(snapshot)
            row = rows.get(parameters["target"])
            expected = {"part": "chest", "receipt": parameters["receipt"],
                        "phase": "prepared"}
            if (not row or row["layout"] != parameters["layout"]
                    or row["pending"] != expected
                    or any(other["parts"] or other["manual_pending"]
                           or other["state"] == "fault"
                           or (other["pending"] and other is not row)
                           for other in rows.values())
                    or not snapshot.factory["coal_supply"]["committed"]):
                return False
            return True
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            return False

    def _coal_step_admitted(self, step, snapshot):
        """Check new-start policy or the exact Lua-revalidated prepared retry."""
        return (self._coal_prepared_first_payment_retry(step, snapshot)
                or self._coal_admission_allows_start(snapshot))

    def _step_allowed(self, step, snapshot):
        if (self._execution_barrier(snapshot) or self._coal_job_conflict(step)
                or not coal.permits(step.action, step.parameters or {}, snapshot)):
            return False
        try:
            rows = coal.sources(snapshot)
            network = step.action == coal.COMMAND or (step.action == solid.COMMAND and coal.is_network_route(step.parameters, snapshot))
            if network and not self._coal_step_admitted(step, snapshot):
                return False
            bill = Counter(coal.remaining_kit(rows, snapshot)) if network or self.memory.coal_commitments else Counter()
            own = (step.parameters or {}).get("route") if step.action == solid.COMMAND else None
            if not network:
                bill.update(solid.remaining(solid.routes(snapshot)[own]) if own else step.costs or {})
            reserved = Counter()
            state = self.memory.coal_funding
            if state and not network:
                reserved.update(state["held"])
            if (network and self._coal_kit_policy and not self.memory.coal_commitments
                    and (self.memory.failures.get(coal_funding.project_key(self._coal_targets), 0) >= 2
                         or coal_funding.build_budget_exhausted(rows, self.memory.failures))):
                return False
            # Coal corridors are already in the whole-network bill; do not count
            # them twice. Other route, background and active-plan locks survive.
            for key, saved in self.memory.solid_commitments.items():
                if key != own and saved["source"]["role"] not in {coal.role(t, "chest") for t in rows}:
                    reserved.update(solid.remaining(saved))
            active = (self.memory.active_plan or {}).get("id")
            for owner, held in self.memory.reservations.items():
                if owner != active:
                    reserved.update(held)
            ledger = SupplyLedger.capture(snapshot, self.catalog, reserved=dict(reserved),
                                           job=getattr(self, "_job", lambda: None)())
            if any(ledger.carried.get(item, 0) < n for item, n in bill.items()):
                return False
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        return super()._step_allowed(step, snapshot)

    def _solid_reservations(self):
        reserved = Counter(super()._solid_reservations())
        if self.memory.coal_funding:
            reserved.update(self.memory.coal_funding["held"])
        # Solid already accounts for each committed corridor. Source components
        # and not-yet-committed receiving corridors are part of the same durable
        # coal bundle, even before a chest has produced an observable route.
        reserved.update(coal.reserved_components(self.memory.coal_commitments, self.memory.solid_commitments))
        return dict(reserved)

    def _coal_job_conflict(self, step):
        state = self.memory.coal_funding
        if state is None or step.action != "factory_craft_job":
            return False
        recipe = self.catalog.recipes.get((step.parameters or {}).get("recipe"))
        # A tracked job locks its entire output inventory, including baseline
        # stock. Include newly acquired kit components: post-action observation
        # may hold partial output before the background job is admitted.
        return recipe is None or any(product["name"] in state["kit"]
                                     for product in recipe.get("products", []))

    def _compile_candidates(self, snapshot):
        plans, blocker = super()._compile_candidates(snapshot)
        if self.memory.active_goal == "bootstrap_mining" or self._execution_barrier(snapshot):
            return plans, blocker
        # Replace only this network's ordinary solid proposals with the balanced
        # bundle proposals. The original useful production frontier is not rerun.
        plans = [p for p in plans if not (p.steps[0].action == solid.COMMAND
                                         and coal.is_network_route(p.steps[0].parameters, snapshot))]
        state = self.memory.coal_funding
        if state:
            plans = [p for p in plans if "capital_investment" not in (p.materials or {})
                     and (p.materials or {}).get(solid_investment.MARKER, {}).get("stage") != "kit"
                     and not self._coal_job_conflict(p.steps[0])]
        extra = candidates(snapshot, self.memory.active_goal, failures=self.memory.failures)
        if self._coal_kit_policy and not self.memory.coal_commitments:
            try:
                if not self._coal_admission_allows_start(snapshot):
                    self._coal_kit_evidence = self._coal_admission_evidence
                    return plans, blocker if not plans else ""
                offer, self._coal_kit_evidence = coal_funding.candidate(
                    snapshot, self.catalog, **self._coal_funding_options())
                if offer is not None:
                    if state is None and _ready_required_science(snapshot, self.catalog, plans):
                        # The model may choose any offered ID regardless of ranking.
                        # Defer only a new optional kit; paid/pending work keeps
                        # its durable continuation and ordinary fuel work remains.
                        self._coal_kit_evidence = {**self._coal_kit_evidence,
                            'selection_deferred_reason': 'ready_required_science'}
                    else:
                        # Ranking may trust only this decision's exact offer.
                        snapshot._coal_kit_annotations = {
                            offer.id: deepcopy(offer.to_dict())}
                        extra.append(offer)
            except (ValueError, KeyError, TypeError, AttributeError):
                self._coal_kit_evidence = {"reason": "unfunded_or_locked_bundle", "native_flow_proven": False}
        plans.extend(p for p in extra if p.id not in {p.id for p in plans} and self._step_allowed(p.steps[0], snapshot))
        return plans, blocker if not plans else ""

    def _coal_funding_options(self, plan=None):
        reserved = Counter(self._solid_reservations())
        if plan is not None:
            reserved.subtract(self.memory.reservations.get(plan.id, {}))
        return {"reserved": {k: v for k, v in reserved.items() if v > 0},
                "job": getattr(self, "_job", lambda: None)(),
                "failures": self.memory.failures, "state": self.memory.coal_funding,
                "capital": self.memory.capital_investment,
                "other_funding": self.memory.solid_funding, "goal": self.memory.active_goal,
                "successor_projects": getattr(self.memory, "successor_projects", {})}

    def _commit_solid(self, plan, snapshot):
        step = plan.steps[0]
        if (coal_funding.MARKER in (plan.materials or {}) or step.action == coal.COMMAND
                or step.action == solid.COMMAND and coal.is_network_route(step.parameters, snapshot)):
            if not self._coal_admission_allows_start(snapshot):
                raise ValueError("Cannot start coal investment without economic admission")
        if coal_funding.MARKER not in (plan.materials or {}):
            if self.memory.coal_funding and ((plan.materials or {}).get("capital_investment")
                    or (plan.materials or {}).get(solid_investment.MARKER, {}).get("stage") == "kit"):
                raise ValueError("An existing coal funding project owns the optional investment lane")
            return super()._commit_solid(plan, snapshot)
        if (not self._coal_kit_policy or self.memory.pending
                or not coal_funding.fresh_permission(plan, plan.steps[0], snapshot, self.catalog,
                                                      **self._coal_funding_options(plan))):
            raise ValueError("Cannot commit an unqualified coal kit")
        state = self.memory.coal_funding
        if state is None:
            options = self._coal_funding_options(plan)
            self.memory.coal_funding = coal_funding.start(
                plan, snapshot, self.catalog, options["reserved"], options["job"])
            self.memory.event("coal_kit_committed", key=plan.id, tick=snapshot.tick)
        else:
            if state["actions"] >= coal_funding.MAX_ACTIONS:
                raise ValueError("Coal funding action budget exhausted")
            state["actions"] += 1
        # Base plan-commit and prepared-action saves are the authoritative barriers.

    def _finalize_solid_funding(self, snapshot):
        # The inner observer cannot release funding before coal ownership checks.
        # _observe_solid invokes the parent hook once after those checks succeed.
        return

    def _reconcile_coal_funding(self, snapshot):
        if self._coal_fault or self._solid_fault or self.memory.status == "uncertain":
            return
        state = self.memory.coal_funding
        if state is None:
            return
        coal_funding.validate_state(state, snapshot.tick, self._coal_targets)
        rows = coal.sources(snapshot)
        if self.memory.coal_commitments:
            if not all(coal.reconciles(saved, rows[target]) for target, saved in state["bundle"].items()):
                raise ValueError("Coal funding cannot hand off to a different paid bundle")
            self.memory.event("coal_kit_paid_handoff", key=state["key"], tick=snapshot.tick)
            self.memory.coal_funding = None
            return  # The exact whole-network commitment is saved by this observer.
        reserved = Counter(self._solid_reservations())
        if self.memory.pending and self.memory.active_plan:
            # The pending action may already have consumed its own cost hold.
            # It is not another project's still-carried reservation. The native
            # receipt remains unresolved; only actual final-kit stock is held.
            reserved.subtract(self.memory.reservations.get(self.memory.active_plan["id"], {}))
        external = coal_funding.spendable_reservations(dict(+reserved), state)
        held = coal_funding.held_stock(snapshot, self.catalog, state["kit"], external,
                                      getattr(self, "_job", lambda: None)())
        if any(held.get(item, 0) < n for item, n in state["held"].items()):
            raise ValueError("An unaccounted mutation consumed a held coal-kit component")
        state["held"] = held
        if self.memory.pending:
            return  # Never release or abandon an unresolved action.
        reason = None
        if not coal_funding.bound(state, rows):
            reason = "kit_bundle_changed"
        elif snapshot.tick >= state["deadline_tick"]:
            reason = "kit_deadline"
        elif self.memory.failures.get(state["key"], 0) >= 2:
            reason = "kit_failure_budget"
        elif coal_funding.build_budget_exhausted(rows, self.memory.failures):
            reason = "kit_build_failure_budget"
        elif state["actions"] >= coal_funding.MAX_ACTIONS and self.memory.active_plan is None:
            if any(held.get(k, 0) < v for k, v in state["kit"].items()):
                reason = "kit_action_budget"
        if reason is None:
            try:
                if solid_funding.bill_catalog_digest(state["kit"], snapshot, self.catalog) != state["catalog_sha256"]:
                    reason = "kit_catalog_changed"
            except (ValueError, KeyError, TypeError, AttributeError):
                reason = "kit_catalog_unavailable"
        if reason is None:
            try:
                solid_funding._acquire_bill(
                    state["kit"], state["key"], snapshot, self.catalog,
                    reserved=external, job=getattr(self, "_job", lambda: None)(),
                    failures=self.memory.failures, budget_check=coal_funding.failure_count,
                    protect_final_stock=True)
            except solid_funding.KitBudgetExhausted:
                reason = "kit_acquisition_failure_budget"
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # Temporary stock/queue visibility is not budget exhaustion.
        if reason:
            old = self.memory.failures.get(state["key"], 0)
            self.memory.failures[state["key"]] = max(2, old)
            if old < 2:
                self.memory.event("coal_kit_abandoned", key=state["key"], reason=reason, tick=snapshot.tick)
            if (self.memory.active_plan or {}).get("id") != state["key"]:
                self.memory.coal_funding = None

    def _clear_plan(self):
        super()._clear_plan()
        state = self.memory.coal_funding
        if (state and not self._coal_fault and not self._solid_fault
                and self.memory.status != "uncertain" and not self.memory.pending
                and self.memory.failures.get(state["key"], 0) >= 2):
            self.memory.coal_funding = None

    def _plan_failure_count(self, plan):
        count = super()._plan_failure_count(plan)
        if coal_funding.MARKER in (plan.materials or {}) and len(plan.steps) == 1:
            count = max(count, coal_funding.failure_count(plan, self.memory.failures))
        return count

    def _investment_step_allowed(self, plan, step, snapshot):
        if (step.action == coal.COMMAND
                or step.action == solid.COMMAND and coal.is_network_route(step.parameters, snapshot)):
            try:
                if not self._coal_step_admitted(step, snapshot):
                    return False
            except (ValueError, KeyError, TypeError, AttributeError):
                return False
        if coal_funding.MARKER in (plan.materials or {}) or plan.id.startswith("coal-kit:"):
            if (not self._coal_kit_policy or self.memory.coal_funding is None
                    or self._plan_failure_count(plan) >= 2
                    or not coal_funding.fresh_permission(plan, step, snapshot, self.catalog,
                                                          **self._coal_funding_options(plan))):
                return False
        return super()._investment_step_allowed(plan, step, snapshot)

    def _verify_pending(self, snapshot):
        pending = self.memory.pending
        if pending and not self._execution_barrier(snapshot):
            plan = Plan.from_dict(self.memory.active_plan)
            step = plan.steps[self.memory.step_index]
            if step.action == coal.COMMAND:
                row = coal.sources(snapshot).get(step.parameters["target"])
                proof = {"part": step.parameters["part"], "receipt": step.parameters["receipt"], "phase": "prepared"}
                if (pending.get("dispatch") in {"prepared", "ambiguous"} and pending.get("polls") == 0
                        and row and row["pending"] == proof and self._step_allowed(step, snapshot)
                        and self._investment_step_allowed(plan, step, snapshot)):
                    pending["polls"] = 1
                    self.memory.event("coal_prepared_recovery", plan=plan.id, tick=snapshot.tick)
                    self._save()
                    try:
                        with phase("dispatch", self._diagnostic_trace):
                            self._trace.dispatch(lambda: (self.backend.execute_traced(step.action, step.parameters, self._diagnostic_trace)
                                if getattr(self.backend, "execute_traced", None) else self.backend.execute(step.action, step.parameters)),
                                step.action, parameters=step.parameters, plan_id=plan.id,
                                step_index=self.memory.step_index, pending=pending,
                                checkpointed=True, attempt_id=self.memory.attempt["id"])
                    except ResearchLogError:
                        raise
                    except Exception:
                        if self._persistence_failed:
                            raise
                        pending["dispatch"] = "ambiguous"
                        self._save()
                        return self._record(snapshot, "observe", "Exact coal replay remains ambiguous; preserve pending receipt")
                    pending["dispatch"] = "returned"
                    self._save()
                    self._trace.observation_phase = "post_recovery_dispatch"
                    snapshot = self._observe("post_dispatch_observe")
        return super()._verify_pending(snapshot)

    def _record_extras(self):
        return {**super()._record_extras(), "coal_supply": True,
                "coal_supply_evidence": deepcopy(self._coal_evidence), "coal_supply_fault": self._coal_fault,
                "coal_kit_policy": self._coal_kit_policy, "coal_kit_evidence": deepcopy(self._coal_kit_evidence),
                "coal_economic_admission": self._coal_economic_admission,
                "coal_admission_evidence": deepcopy(self._coal_admission_evidence)}

    def _model_facts(self, snapshot):
        facts = super()._model_facts(snapshot)
        summary = {}
        for target, row in coal.sources(snapshot).items():
            for paid in row["parts"].values():
                facts["factory"].get("entities", {}).pop(paid["role"], None)
            summary[target] = {"state": row["state"], "paid_source_parts": len(row["parts"]),
                               "remaining_coal": row["remaining"], "reason": row["reason"],
                               "delivered_lower": row["flow"].get("delivered_lower", 0),
                               "manual_inserted": row["flow"].get("manual_inserted", 0)}
        facts["factory"]["coal_supply"] = {"sources": summary, "network_flow_observed": coal.flow_complete(snapshot)}
        return facts


def coal_loop_type(base):
    if not getattr(base, "_solid_routes_enabled", False):
        raise ValueError("Coal composition requires the existing solid-route controller")

    @dataclass
    class CoalMemory(base.memory_type):
        coal_supply_schema: int = 1
        coal_kit_policy: bool = False
        coal_economic_admission: bool = False
        coal_funding: dict | None = None
        coal_targets: list = field(default_factory=list)
        coal_epoch: dict = field(default_factory=dict)
        coal_commitments: dict = field(default_factory=dict)

        @classmethod
        def _from_data(cls, data, session_id, target):
            if not isinstance(data, dict) or not CHECKPOINT_FIELDS <= data.keys():
                raise ValueError("Incomplete coal checkpoint extension")
            memory = super()._from_data(data, session_id, target)
            if (not solid.integer(memory.coal_supply_schema, 1, 2)
                    or type(memory.coal_economic_admission) is not bool
                    or (memory.coal_supply_schema == 2) is not memory.coal_economic_admission
                    or memory.coal_economic_admission and 'coal_economic_admission' not in data):
                raise ValueError("Unsupported coal checkpoint schema")
            coal.validate_targets(memory.coal_targets)
            if type(memory.coal_kit_policy) is not bool:
                raise ValueError("Invalid immutable coal kit policy")
            if memory.coal_economic_admission and not memory.coal_kit_policy:
                raise ValueError("Coal economic checkpoint requires coal kit policy")
            if memory.coal_funding is not None:
                if (not memory.coal_kit_policy or memory.capital_investment is not None
                        or memory.solid_funding is not None
                        or any(p.get("status") != "qualified" for p in
                               getattr(memory, "successor_projects", {}).values())):
                    raise ValueError("Coal funding conflicts with immutable policy or another investment")
                coal_funding.validate_state(memory.coal_funding, memory.last_tick, memory.coal_targets)
            active = Plan.from_dict(memory.active_plan) if memory.active_plan else None
            if active and (active.id.startswith("coal-kit:") or coal_funding.MARKER in (active.materials or {})):
                coal_funding.validate_active(memory.coal_funding, active, memory.last_tick, memory.active_goal)
            coal.validate_transport_intents(memory.coal_targets, memory.solid_intents)
            if not isinstance(memory.coal_epoch, dict) or not isinstance(memory.coal_commitments, dict):
                raise ValueError("Invalid coal checkpoint binding")
            if not memory.coal_epoch:
                if (memory.coal_commitments or memory.status != "uncertain"
                        or memory.reason not in {UNBOUND_FAULT, SOLID_UNBOUND, "Solid-route evidence invalid; preserve pending state and ownership"}):
                    raise ValueError("Invalid unbound coal checkpoint")
            elif memory.coal_epoch != memory.solid_epoch:
                raise ValueError("Coal checkpoint epoch differs from its transport")
            commitments = memory.coal_commitments
            if commitments and set(commitments) != set(memory.coal_targets):
                raise ValueError("Coal checkpoint lost a consumer")
            units, receipts, areas, layouts = set(), set(), [], set()
            for consumer, saved in commitments.items():
                coal.validate_commitment(saved, consumer)
                ids = {saved["target"]["unit_number"], *[v["unit_number"] for v in saved["parts"].values()]}
                paid = {v["receipt"] for v in saved["parts"].values()}
                area = coal._bounds(saved["mining_area"])
                if units & ids or receipts & paid or any(coal._overlap(area, old) for old in areas):
                    raise ValueError("Coal checkpoint shares ownership or mining areas")
                units.update(ids); receipts.update(paid); areas.append(area); layouts.add(saved["layout"])
            if len(layouts) > 1:
                raise ValueError("Coal checkpoint mixes bundle generations")
            return memory

    return type("CoalSupplyLoop", (CoalSupplyMixin, base), {"memory_type": CoalMemory, "__module__": __name__})
