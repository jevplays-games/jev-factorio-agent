"""Opt-in, bounded ready-work choices over the existing native action contract.

This is not a concurrent dispatcher or a belt planner. Forecasts influence only
batching/priorities; the unchanged native preconditions and postconditions own
execution. The serial planner remains the fallback for unsupported production.
"""
from __future__ import annotations

import math
from dataclasses import replace

from ..skills import Plan
from ..state import GameSnapshot
from .catalog import Catalog
from .demand import SupplyLedger, horizon_demands
from .service_visits import service_visit
from .scheduling import scheduled_research_wait, ready_research_work, current_research_supply
from .factory import FactoryPlanner, RAW_ITEMS, compile_factory
from .economics import EconomicProduction
from .productive_work import productive_work


class ReadyWorkPlanner(EconomicProduction, FactoryPlanner):
    def __init__(self, catalog: Catalog, snapshot: GameSnapshot, goal: str,
                 collection_batch: int = 10, max_candidates: int = 8) -> None:
        super().__init__(catalog, snapshot, goal)
        if type(collection_batch) is not int or not 1 <= collection_batch <= 50:
            raise ValueError("Collection batch must be an integer in [1, 50]")
        if type(max_candidates) is not int or not 1 <= max_candidates <= 16:
            raise ValueError("Candidate budget must be an integer in [1, 16]")
        self.collection_batch = collection_batch
        self.max_candidates = max_candidates
        self.focus: tuple[str, int] | None = None
        self.targets: dict[str, int] = {}
        self.raw_targets: dict[str, int] = {}
        self.demands: dict[str, int] = {}
        self.ledger = SupplyLedger.capture(snapshot, catalog)
        self.speculative = False
        self.allow_service_visits = True

    def _fuel(self, role, path):
        """Do not turn a stocked owned furnace's service threshold into a blocker.

        A positive fuel inventory is not a guarantee for the whole forecast
        batch. The next ore/input action and its receipt get a fresh observation;
        actual empty fuel is serviced by the existing due-consumer policy.
        Mock/legacy snapshots without production-site ownership keep the base
        planner's established behavior.
        """
        machine = self.entities.get(role, {})
        if role == 'utility:boiler' and self.snapshot.world_kind == 'fle':
            fuel = machine.get('fuel')
            # Native inventory maps omit item names whose count is zero.
            # An absent map still means unavailable telemetry.
            coal = fuel.get('coal', 0) if isinstance(fuel, dict) else None
            unit = machine.get('unit_number')
            if (machine.get('name') != 'boiler' or type(unit) is not int or unit <= 0
                    or type(coal) is not int or coal < 0):
                raise ValueError('Current native boiler identity and coal stock are required')
            if coal >= 5:
                return None
            from .fuel_service import service_plan
            return service_plan(self, role, role, path, self._need)
        if (not role.startswith('recipe:') or machine.get('name') not in
                {'stone-furnace', 'steel-furnace'}):
            return super()._fuel(role, path)
        sites = self.factory.get('production_sites')
        if not isinstance(sites, dict):
            return super()._fuel(role, path)
        # Older synthetic/legacy snapshots contain a partial site map without
        # a current record for this role. They cannot qualify the native due
        # policy; retain their established planner behavior. A native partial
        # observation still fails closed below.
        mock_sources = sites.get('sources')
        if self.snapshot.world_kind == 'mock' and (sites.get('protocol') != 1
                or isinstance(mock_sources, dict) and role not in mock_sources):
            return super()._fuel(role, path)
        if (sites.get('protocol') != 1 or sites.get('session_id') != self.snapshot.session_id
                or sites.get('tick') != self.snapshot.tick):
            raise ValueError('Furnace ownership observation is stale')
        sources = sites.get('sources')
        owned = sources.get(role) if isinstance(sources, dict) else None
        unit = machine.get('unit_number')
        if (not isinstance(owned, dict) or owned.get('state') != 'owned'
                or type(unit) is not int or unit <= 0
                or owned.get('source_unit') != unit):
            raise ValueError('Furnace fuel service requires current owned source identity')
        fuel = machine.get('fuel')
        if (not isinstance(fuel, dict) or type(fuel.get('coal', 0)) is not int
                or fuel.get('coal', 0) < 0):
            raise ValueError('Furnace fuel telemetry unavailable')
        if fuel.get('coal', 0) > 0:
            return None
        from .fuel_service import service_plan
        return service_plan(self, role, role, path, self._need)

    def _plan(self, *args, **kwargs) -> Plan:
        plan = super()._plan(*args, **kwargs)
        if self.focus is None:
            return plan
        return replace(plan, materials={**(plan.materials or {}), "local_objective": {
            "item": self.focus[0], "inventory_target": self.focus[1],
            "ultimate_goal": self.goal,
        }, "work_intent": {
            "scope": "lookahead" if self.speculative else "immediate",
            "observed_tick": self.snapshot.tick,
        }})

    def plan(self):
        supply = current_research_supply(self)
        if supply is not None:
            return supply
        primary = ready_research_work(self, super().plan())
        if primary and (primary.materials or {}).get("collection_only_lookahead"):
            return primary
        from .launch import opportunistic
        return opportunistic(self, self._capacity_work(productive_work(self, primary)))

    def _wait(self, effect, item="", threshold=0, role="", timeout=36000, identity=None):
        plan = super()._wait(effect, item, threshold, role, timeout, identity)
        return scheduled_research_wait(self, plan)

    def _set_focus(self, item: str, amount: int) -> None:
        self.focus = (item, math.ceil(amount))
        self.demands = horizon_demands(self.snapshot, self.catalog, self.goal, item, amount)
        try:
            bill = self.catalog.material_demands(
                self.demands, self.ledger.forecast_stock(), self.researched)
        except (KeyError, ValueError):
            return  # Unsupported lookahead never authorizes a speculative action.
        for name, batches in bill.batches.items():
            for ingredient in self.catalog.recipes[name]["ingredients"]:
                if ingredient["type"] != "item":
                    continue
                material = ingredient["name"]
                self.targets[material] = self.targets.get(material, 0) + math.ceil(
                    ingredient["amount"] * batches
                )
        for material, quantity in bill.shortages.items():
            if material in RAW_ITEMS and material != "wood":
                target = self.snapshot.inventory.get(material, 0) + math.ceil(quantity)
                self.raw_targets[material] = target
        # Do not gather raw inputs already paid into machines just because an
        # intermediate recipe appears in the material bill.
        for material in RAW_ITEMS:
            self.targets.pop(material, None)
        self.targets.update(self.raw_targets)

    def _batch_collection(self, plan: Plan, amount: int) -> Plan:
        step = plan.steps[0]
        if step.action != "factory_extract":
            return plan
        parameters = step.parameters
        role, item = parameters["role"], parameters["item"]
        machine = self.entities[role]
        # Batch dedicated, actively producing deterministic solid-item machines.
        # A chest, stopped machine, mixed fluid recipe, or missing telemetry is
        # not evidence that a larger output will arrive.
        recipe_name = machine.get("recipe") or role.removeprefix("recipe:")
        recipe = self.catalog.recipes.get(recipe_name, {})
        if (not role.startswith(("recipe:", "capacity:")) or not recipe
                or machine.get("crafting") is not True
                or any(entry["type"] != "item" for entry in recipe.get("ingredients", []))):
            return plan
        prototype = self.catalog.machines.get(machine["name"], {})
        if prototype.get("burner") and machine.get("fuel", {}).get("coal", 0) < 5:
            return plan
        if prototype.get("electric") and machine.get("energy", 0) <= 0:
            return plan
        products = [entry for entry in recipe.get("products", [])
                    if entry["name"] == item and entry.get("probability", 1) == 1]
        if len(products) != 1 or not recipe.get("ingredients"):
            return plan
        output = products[0].get("amount", 0)
        if output <= 0:
            return plan
        available = machine.get("output", {}).get(item, 0)
        buffered_batches = min(
            math.floor(machine.get("input", {}).get(entry["name"], 0) / entry["amount"])
            for entry in recipe["ingredients"] if entry["amount"] > 0
        )
        potential = available + output * (buffered_batches + 1)
        target = min(self.collection_batch, amount - self.snapshot.inventory.get(item, 0),
                     potential)
        if target <= available:
            return plan
        return self._wait(
            "machine_output", item, target, role,
            timeout=max(3600, math.ceil(recipe["energy"] * self.collection_batch * 120)),
            identity=f"batch:{role}:{item}:target:{target}",
        )

    def _need(self, item, amount, path=()):
        if self.focus is None and self.snapshot.inventory.get(item, 0) < amount:
            self._set_focus(item, amount)
        # Horizon quantities are optional candidates, never a prerequisite for
        # supplying a producer whose immediate input requirement is smaller.
        if self.speculative and item in self.raw_targets and self.snapshot.inventory.get(item, 0) < amount:
            amount = max(amount, self.raw_targets[item])
        # Do not create a dedicated trip for the one-unit edge of a speculative
        # horizon. The immediate prerequisite path is never suppressed.
        if (self.speculative and item in RAW_ITEMS - {"wood"}
                and 0 < amount - self.snapshot.inventory.get(item, 0) < self.collection_batch):
            return None
        plan = super()._need(item, amount, path)
        # Only the item whose need produced this extraction may set its batch
        # target. An ancestor recipe can require many outputs but few plates.
        if plan and (plan.steps[0].parameters or {}).get("item") == item:
            batched = self._batch_collection(plan, amount)
            if batched.steps[0].action == "factory_wait":
                missing = math.ceil(amount - self.snapshot.inventory.get(item, 0))
                from ..coal_supply import private_source_roles
                network_sources = private_source_roles(self.snapshot)
                for role, machine in sorted(self.entities.items()):
                    if role in network_sources:
                        continue  # The batch-wait fallback obeys the same source lock.
                    available = machine.get("output", {}).get(item, 0)
                    if available and role != plan.steps[0].parameters["role"]:
                        alternative = self._batch_collection(
                            self._transfer(role, item, min(missing, available), extracting=True),
                            amount,
                        )
                        if alternative.steps[0].action == "factory_extract":
                            return alternative
            return batched
        return plan

    def _candidate_worker(self):
        """Fork lookahead within this observation; never cache across decisions.

        The ledger and native facts are read-only. Copy the bounded economic
        workload so speculative probes cannot mutate their parent's demand.
        Execution still checks the original live snapshot and its reservations.
        """
        worker = type(self)(self.catalog, self.snapshot, self.goal,
                            self.collection_batch, self.max_candidates)
        worker.focus, worker.raw_targets = self.focus, dict(self.raw_targets)
        worker.materials = self.materials or {}
        worker.ledger, worker.demands = self.ledger, dict(self.demands)
        worker.speculative = True
        worker.allow_service_visits = self.allow_service_visits
        worker._economic_products = dict(self._remaining_products())
        return worker

    def _shared_bill_candidate(self, plan: Plan | None, item: str, amount: int) -> Plan | None:
        """Bind a speculative handcraft to this observation's current material bill."""
        if (plan is None or self.focus is None or self.targets.get(item) != amount
                or len(plan.steps) != 1):
            return plan
        step = plan.steps[0]
        if step.action != 'factory_craft' or step.item != item:
            return plan
        recipe = self.catalog.recipes.get(step.parameters.get('recipe'), {})
        products = recipe.get('products', [])
        batches = step.parameters.get('batches')
        carried = self.snapshot.inventory.get(item, 0)
        if (type(amount) is not int or amount <= 0
                or type(carried) is not int or not 0 <= carried < amount
                or type(batches) is not int or batches <= 0
                or len(products) != 1 or products[0].get('type') != 'item'
                or products[0].get('name') != item
                or products[0].get('probability', 1) != 1
                or type(products[0].get('amount')) is not int
                or products[0]['amount'] <= 0
                or products[0]['amount'] * batches < amount - carried):
            return plan
        return replace(plan, materials={**(plan.materials or {}),
            'shared_bill_craft': {
                'observed_tick': self.snapshot.tick,
                'local_target_item': self.focus[0],
                'local_target_amount': self.focus[1],
                'craft_item': item,
                'bill_inventory_target': amount,
                'inventory_now': carried,
                'planned_product_units': products[0]['amount'] * batches,
                'basis': 'current_catalog_shared_material_bill',
            }})

    def _partial_current_target_craft(self) -> Plan | None:
        """Offer paid direct handcraft progress without requiring the whole bill."""
        if (self.focus is None or self.speculative or getattr(self, '_buffer_service', False)
                or self.factory.get('crafting_queue') != 0
                or self.factory.get('player_connected') is not True
                or self.factory.get('player_bound') is not True):
            return None
        if self.snapshot.world_kind == 'fle' and (
                getattr(self.snapshot, '_atomic_inventory_verified', None) !=
                (self.snapshot.session_id, self.snapshot.tick)
                or getattr(self.snapshot, '_coherent_observation_verified', None) !=
                (self.snapshot.session_id, self.snapshot.tick)):
            return None
        item, target = self.focus
        have = self.snapshot.inventory.get(item, 0)
        if type(target) is not int or type(have) is not int or have < 0 or have >= target:
            return None
        try:
            recipe = self.catalog.recipe_for(item)
        except (KeyError, ValueError):
            return None
        ingredients, products = recipe.get('ingredients', []), recipe.get('products', [])
        if (recipe.get('hidden') or not self.catalog.enabled(recipe, self.researched)
                or not self.catalog.hand_categories.get(recipe.get('category'))
                or not ingredients or len(products) != 1
                or products[0].get('type') != 'item' or products[0].get('name') != item
                or products[0].get('probability', 1) != 1
                or type(products[0].get('amount')) is not int or products[0]['amount'] <= 0
                or any(x.get('type') != 'item' or type(x.get('amount')) is not int
                       or x['amount'] <= 0 for x in ingredients)):
            return None
        per_batch = {}
        for ingredient in ingredients:
            name = ingredient['name']
            per_batch[name] = per_batch.get(name, 0) + ingredient['amount']
        if any(type(self.snapshot.inventory.get(name, 0)) is not int
               or self.snapshot.inventory.get(name, 0) < 0 for name in per_batch):
            return None
        output = products[0]['amount']
        full_batches = math.ceil((target-have)/output)
        affordable = min(self.snapshot.inventory.get(name, 0)//count
                         for name, count in per_batch.items())
        batches = min(20, affordable, (target-have)//output)
        if batches <= 0 or batches >= full_batches:
            return None  # Existing full-target planner owns complete batches.
        costs = {name: count*batches for name,count in per_batch.items()}
        plan = self._plan('factory_craft','inventory',item,have+output*batches,
            parameters={'recipe':recipe['name'],'batches':batches},costs=costs,
            timeout=max(1800,math.ceil(recipe['energy']*batches*120)),
            identity=f"partial:{recipe['name']}:target:{target}:batches:{batches}",
            description=f"Hand-craft {output*batches} paid {item}; partially advance target {target}")
        return replace(plan,materials={**(plan.materials or {}),'craft_dependency':{
            'observed_tick':self.snapshot.tick,'recipe':recipe['name'],
            'product':item,'planner_item_path':[item]},'partial_current_target_craft':{
            'observed_tick':self.snapshot.tick,'inventory_now':have,
            'inventory_target':target,'planned_product_units':output*batches,
            'target_not_completed':True}})

    def candidates(self) -> list[Plan]:
        primary = self.plan()
        if primary is None:
            return []
        if (primary.materials or {}).get("collection_only_lookahead"):
            return [primary]  # No speculative forks or bundled ingredient spending.
        # Binding, in-flight handcrafting, and infrastructure prerequisites stay
        # serial. Nothing here releases a pending mutation or spends its inputs.
        if (primary.steps[0].action not in {
                "factory_gather", "factory_insert", "factory_extract", "factory_wait"
            } or not self.focus or self.factory.get("crafting_queue", 0)):
            return self._current_raw_craft_alternatives(primary)
        candidates = [primary]
        partial = self._partial_current_target_craft()
        if partial is not None:
            candidates.append(partial)
        # Evaluate a bounded frontier, not every item in a rocket-sized tree.
        for item, amount in list(sorted(self.targets.items()))[:32]:
            if self.snapshot.inventory.get(item, 0) >= amount:
                continue
            worker = self._candidate_worker()
            try:
                plan = worker._need(item, amount)
            except (KeyError, ValueError):
                continue
            plan = self._shared_bill_candidate(plan, item, amount)
            if plan and plan.steps[0].action in {
                "factory_gather", "factory_insert", "factory_extract", "factory_craft"
            }:
                candidates.append(plan)
        unique = {}
        for plan in candidates:
            step = plan.steps[0]
            if step.allowed(self.snapshot) and not step.satisfied(self.snapshot):
                unique.setdefault(plan.id, plan)
        ready = [plan for plan in unique.values() if plan.steps[0].action != "factory_wait"]
        # A useful primary retains deterministic priority. A passive wait never
        # outranks executable independent work; JEV sees the remaining options.
        selected = ready or list(unique.values()) or [primary]
        item, amount = self.focus
        prefix = f"Next production batch: {amount} {item}. "
        return [service_visit(self, replace(plan, description=prefix + plan.description))
                for plan in selected[:self.max_candidates]]

    def _current_raw_craft_alternatives(self, primary: Plan) -> list[Plan]:
        """Offer non-spending current-target inputs beside an unstarted craft.

        The speculative collection minimum is a trip heuristic, not a reason
        to hide a smaller deficit in the immediate target's own material bill.
        Keep placement, binding, service and in-flight work serial. JEV still
        chooses and independently judges every offered action.
        """
        plans = [service_visit(self, primary)]
        if (len(primary.steps) != 1 or primary.steps[0].action != 'factory_craft'
                or self.focus is None or self.speculative or self.max_candidates <= 1
                or getattr(self, '_buffer_service', False)
                or (primary.materials or {}).get('collection_only_lookahead')
                or (primary.materials or {}).get('work_intent') != {
                    'scope': 'immediate', 'observed_tick': self.snapshot.tick}
                or (primary.materials or {}).get('local_objective') != {
                    'item': self.focus[0], 'inventory_target': self.focus[1],
                    'ultimate_goal': self.goal}
                or self.factory.get('crafting_queue') != 0
                or self.factory.get('player_connected') is not True
                or self.factory.get('player_bound') is not True
                or not primary.steps[0].allowed(self.snapshot)
                or primary.steps[0].satisfied(self.snapshot)):
            return plans
        if (self.snapshot.world_kind == 'fle' and
                getattr(self.snapshot, '_coherent_observation_verified', None) !=
                (self.snapshot.session_id, self.snapshot.tick)):
            return plans
        item, amount = self.focus
        try:
            # Deliberately exclude optional horizon_demands. Forecast stock
            # credits paid machine inputs/output; none becomes spendable here.
            bill = self.catalog.material_demands(
                {item: amount}, self.ledger.forecast_stock(), self.researched)
            for raw, shortage in sorted(bill.shortages.items()):
                if raw not in RAW_ITEMS - {'wood'} or not 0 < shortage < math.inf:
                    continue
                path = self._current_bill_raw_path(item, raw, bill.batches)
                if path is None:
                    continue
                worker = self._candidate_worker()
                worker.speculative = False
                target = self.snapshot.inventory.get(raw, 0) + math.ceil(shortage)
                # The existing direct raw branch handles fair targets and
                # existing output pickup. Optional outpost construction is not
                # an alternative that preserves this craft's carried inputs.
                candidate = FactoryPlanner._need(
                    worker, raw, target, tuple('item:' + p for p in path[:-1]))
                if (candidate is None or len(candidate.steps) != 1
                        or candidate.id == primary.id):
                    continue
                step = candidate.steps[0]
                if (step.action != 'factory_gather' or step.item != raw
                        or step.costs not in (None, {})
                        or not step.allowed(self.snapshot) or step.satisfied(self.snapshot)):
                    continue
                candidate = replace(candidate, materials={**(candidate.materials or {}),
                    'current_target_raw_alternative': {
                        'observed_tick': self.snapshot.tick,
                        'local_target_item': item, 'local_target_amount': amount,
                        'raw_item': raw, 'current_bill_shortage': shortage,
                        'primary_plan_id': primary.id,
                        'preserved_primary_costs': dict(primary.steps[0].costs or {}),
                        'basis': 'immediate_target_bill_after_forecast_stock',
                    }})
                plans.append(candidate)
                if len(plans) >= self.max_candidates:
                    break
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return plans[:1]  # Partial/invalid bill evidence cannot add work.
        return plans

    def _current_bill_raw_path(self, root, raw, batches):
        """Bounded selected-recipe path using only recipes active in this bill."""
        stack = [(root, ())]
        visits = 0
        while stack and visits < 128:
            product, ancestors = stack.pop()
            visits += 1
            if product in ancestors or len(ancestors) >= 31:
                continue
            path = (*ancestors, product)
            if product == raw:
                return path
            if product in RAW_ITEMS:
                continue
            recipe = self.catalog.recipe_for(product)
            if (recipe.get('hidden') or not self.catalog.enabled(recipe, self.researched)
                    or batches.get(recipe['name'], 0) <= 0):
                continue
            inputs = sorted({row['name'] for row in recipe['ingredients']
                             if row.get('type') == 'item' and row.get('amount', 0) > 0})
            stack.extend((name, path) for name in reversed(inputs))
        return None


def compile_ready_factory(goal: str, snapshot: GameSnapshot,
                          catalog: Catalog, *, planner_type=None) -> tuple[list[Plan], str]:
    """Compile once with the negotiated capabilities, scoped to this decision."""
    if goal == "bootstrap_mining":
        return compile_factory(goal, snapshot, catalog)
    try:
        kind = planner_type or ReadyWorkPlanner
        plans = kind(catalog, snapshot, goal).candidates()
        return plans, "" if plans else "No remaining native production action"
    except (ValueError, KeyError) as error:
        return [], str(error)
