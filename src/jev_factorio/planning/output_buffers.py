"""Small furnace-output cells, not a general belt/layout planner."""
from __future__ import annotations

import math
from dataclasses import replace

from ..output_buffers import COMMAND, PARTS, current, flow_complete, potential, sources, validate_commitments
from ..skills import Plan
from .ready_work import ReadyWorkPlanner
from .service_visits import service_visit


def construction_pickup_bill(snapshot, catalog, row, part, item):
    """Current paid construction demand, stopping expansion at the pickup item.

    This is one missing component's bill, not forecast demand or proof of a
    completed kit. Carried intermediate stock is consumed by the same native
    material calculator used elsewhere in the planner.
    """
    if (not isinstance(row, dict) or not isinstance(item, str) or not item
            or not current(row, snapshot) or row.get('state') != 'building'
            or part not in PARTS or part in row.get('parts', {})
            or next((p for p in PARTS if p not in row.get('parts', {})), None) != part
            or not row.get('parts')):
        return None
    try:
        if (sources(snapshot).get(row['source']) != row
                or row.get('item') != row['source'].removeprefix('recipe:')
                or snapshot.game_version != catalog.version):
            return None
        validate_commitments({role: {
            'source_unit': entry['source_unit'], 'layout': entry['layout'],
            'parts': entry['parts'],
        } for role, entry in sources(snapshot).items()},
            successors='successors' in snapshot.factory)
        for built_part, paid in row['parts'].items():
            entity = snapshot.factory['entities'][paid['role']]
            if (entity['unit_number'] != paid['unit_number']
                    or entity.get('name') != PARTS[built_part]):
                return None
        carried = snapshot.inventory.get(item, 0)
        if type(carried) is not int or not 0 <= carried < 1000000:
            return None
        stock = dict(snapshot.inventory)
        stock[item] = 1000000
        bill = catalog.material_plan(PARTS[part], 1, stock, snapshot.researched or [])
        required = 1000000 - bill.remaining.get(item, 0)
        if not math.isfinite(required) or required != math.ceil(required) or not carried < required <= 200:
            return None
        recipes = {name: catalog.recipes[name] for name in sorted(bill.batches)}
        return {
            'observed_tick': snapshot.tick, 'native_catalog_version': catalog.version,
            'source_role': row['source'],
            'source_unit': row['source_unit'], 'layout': row['layout'],
            'paid_parts': row['parts'], 'next_part': part,
            'component_item': PARTS[part], 'component_quantity': 1,
            'pickup_item': item, 'actor_item_now': carried,
            'component_input_required': int(required),
            'component_input_deficit': int(required) - carried,
            'native_recipe_batches': bill.batches, 'native_recipes': recipes,
            'actor_inventory_now': dict(snapshot.inventory),
            'basis': 'current_paid_output_buffer_missing_component_bill',
            'component_craft_build_and_flow_require_native_verification': True,
        }
    except (KeyError, TypeError, ValueError):
        return None


def buffer_build_start(snapshot, catalog, parameters):
    """Prove the current paid next component can enter native preparation.

    Observed ownership and inventory are start conditions. Geometry, approach,
    placement and transport remain the native dispatch/receipt responsibilities.
    """
    from ..output_buffers import allowed, validate
    try:
        validate(parameters)
        role, part = parameters['source'], parameters['part']
        rows = sources(snapshot)
        row = rows.get(role)
        identity = (snapshot.session_id, snapshot.tick)
        runtime = snapshot.factory.get('acceptance_runtime', {})
        if (not isinstance(row, dict) or not current(row, snapshot)
                or row.get('source') != role or row.get('item') != role.removeprefix('recipe:')
                or row.get('state') != 'building' or not row.get('parts')
                or row.get('layout') != parameters['layout']
                or next((p for p in PARTS if p not in row['parts']), None) != part
                or snapshot.game_version != catalog.version
                or not isinstance(catalog.version, str) or not catalog.version.startswith('2.0.')
                or getattr(snapshot, '_atomic_inventory_verified', None) != identity
                or getattr(snapshot, '_coherent_observation_verified', None) != identity
                or snapshot.factory.get('observation_snapshot_schema') != 2
                or snapshot.factory.get('tick') != snapshot.tick
                or type(snapshot.factory.get('crafting_queue')) is not int
                or runtime.get('schema') != 1 or runtime.get('session_id') != snapshot.session_id
                or runtime.get('mods', {}).get('base') != catalog.version
                or runtime.get('speed') != 1 or runtime.get('tick_paused') is not False
                or parameters['receipt'] != f"buffer:{snapshot.tick}:{row['source_unit']}:{part}"
                or not isinstance(snapshot.factory.get('receipts'), dict)
                or parameters['receipt'] in snapshot.factory.get('receipts', {})
                or not allowed(parameters, snapshot)):
            return None
        validate_commitments({source: {'source_unit': entry['source_unit'],
            'layout': entry['layout'], 'parts': entry['parts']} for source, entry in rows.items()},
            successors='successors' in snapshot.factory)
        if any(paid['receipt'] == parameters['receipt']
                for entry in rows.values() for paid in entry['parts'].values()):
            return None
        entity = snapshot.factory['entities'][role]
        if entity.get('name') != 'stone-furnace':
            return None
        for paid_part, paid in row['parts'].items():
            owned = snapshot.factory['entities'][paid['role']]
            if owned.get('unit_number') != paid['unit_number'] or owned.get('name') != PARTS[paid_part]:
                return None
        quantity = snapshot.inventory.get(PARTS[part])
        if type(quantity) is not int or quantity < 1:
            return None
        return {'observed_tick': snapshot.tick, 'session_id': snapshot.session_id,
            'native_catalog_version': catalog.version, 'source_role': role,
            'source_unit': row['source_unit'], 'source_item': row['item'],
            'layout': row['layout'], 'paid_parts': row['parts'], 'part': part,
            'component_item': PARTS[part], 'component_quantity': 1,
            'receipt': parameters['receipt'], 'actor_inventory_now': dict(snapshot.inventory),
            'native_receipt_query': {'schema': 1, 'session_id': snapshot.session_id,
                'tick': snapshot.tick, 'receipt': parameters['receipt'],
                'present': False, 'map_verified': True,
                'receipt_count': len(snapshot.factory['receipts'])},
            'paid_component_in_inventory_now': True, 'planned_receipt_absent_now': True,
            'player_connected_and_bound_now': True, 'crafting_queue_empty_now': True,
            'basis': 'current_paid_partial_output_buffer_next_component',
            'native_prepare_rechecks_geometry_and_clearance': True,
            'approach_and_placement_require_native_verification': True,
            'flow_not_established': True}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def buffer_fuel_start(snapshot, catalog, parameters):
    """Current paid output arm can accept bounded coal to commission ready stock."""
    try:
        if (set(parameters) != {'role', 'item', 'quantity', 'receipt'}
                or parameters['item'] != 'coal'):
            return None
        identity = (snapshot.session_id, snapshot.tick)
        runtime = snapshot.factory.get('acceptance_runtime', {})
        receipts = snapshot.factory.get('receipts')
        if (snapshot.game_version != catalog.version or not isinstance(catalog.version, str)
                or not catalog.version.startswith('2.0.')
                or getattr(snapshot, '_atomic_inventory_verified', None) != identity
                or getattr(snapshot, '_coherent_observation_verified', None) != identity
                or snapshot.factory.get('observation_snapshot_schema') != 2
                or snapshot.factory.get('tick') != snapshot.tick
                or runtime.get('schema') != 1 or runtime.get('session_id') != snapshot.session_id
                or runtime.get('mods', {}).get('base') != catalog.version
                or runtime.get('speed') != 1 or runtime.get('tick_paused') is not False
                or snapshot.factory.get('player_connected') is not True
                or snapshot.factory.get('player_bound') is not True
                or type(snapshot.factory.get('crafting_queue')) is not int
                or snapshot.factory['crafting_queue'] != 0
                or not isinstance(receipts, dict)):
            return None
        rows = sources(snapshot)
        validate_commitments({source: {'source_unit': row['source_unit'],
            'layout': row['layout'], 'parts': row['parts']} for source, row in rows.items()},
            successors='successors' in snapshot.factory)
        if any(paid['receipt'] == parameters['receipt']
                for row in rows.values() for paid in row['parts'].values()):
            return None
        for source, row in rows.items():
            paid = row.get('parts', {}).get('inserter', {})
            if paid.get('role') != parameters['role']:
                continue
            if (not current(row, snapshot) or row.get('source') != source
                    or row.get('item') != source.removeprefix('recipe:')
                    or row.get('state') != 'ready' or row.get('topology') is not True
                    or set(row['parts']) != set(PARTS)):
                return None
            machine = snapshot.factory['entities'][source]
            if machine.get('name') != 'stone-furnace':
                return None
            for name, owner in row['parts'].items():
                entity = snapshot.factory['entities'][owner['role']]
                if entity.get('unit_number') != owner['unit_number'] or entity.get('name') != PARTS[name]:
                    return None
            arm = snapshot.factory['entities'][parameters['role']]
            fuel = arm.get('fuel', {}).get('coal', 0)
            capacity = arm.get('fuel_insertable', {}).get('coal')
            carried, quantity = snapshot.inventory.get('coal'), parameters['quantity']
            ready = machine.get('output', {}).get(row['item'])
            receipt = parameters['receipt']
            if (type(fuel) is not int or not 0 <= fuel < 2
                    or type(capacity) is not int or capacity <= 0
                    or type(carried) is not int or carried < 1
                    or type(quantity) is not int or not 0 < quantity <= min(carried, 5-fuel, capacity)
                    or type(ready) is not int or ready < 1
                    or receipt != f'{snapshot.tick}:factory_insert:{parameters["role"]}:coal'
                    or receipt in receipts):
                return None
            return {'observed_tick': snapshot.tick, 'session_id': snapshot.session_id,
                'native_catalog_version': catalog.version, 'source_role': source,
                'source_unit': row['source_unit'], 'source_item': row['item'],
                'layout': row['layout'], 'paid_parts': row['parts'],
                'burner_role': parameters['role'], 'burner_unit': paid['unit_number'],
                'fuel_now': fuel, 'fuel_insertable_now': capacity,
                'coal_in_inventory_now': carried, 'coal_to_transfer': quantity,
                'current_coal_deficit': min(5-fuel, capacity), 'ready_source_output_now': ready,
                'native_receipt': receipt, 'actor_inventory_now': dict(snapshot.inventory),
                'native_receipt_query': {'schema': 1, 'session_id': snapshot.session_id,
                    'tick': snapshot.tick, 'receipt': receipt, 'present': False,
                    'map_verified': True, 'receipt_count': len(receipts)},
                'basis': 'current_paid_output_buffer_arm_commissioning',
                'planned_receipt_absent_now': True, 'player_connected_and_bound_now': True,
                'crafting_queue_empty_now': True,
                'native_transfer_and_later_flow_require_verification': True,
                'flow_not_established': True}
        return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class OutputBufferPlanner(ReadyWorkPlanner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._acquiring_buffer = False
        self._buffer_service = False

    def _prerequisite(self, item: str, count: int, path) -> Plan | None:
        acquiring = self._acquiring_buffer
        prefix = self._recipe_cycle_prefix
        self._acquiring_buffer = True
        self._recipe_cycle_prefix = tuple(path)
        try:
            # Commissioning is a separate bounded demand. Its kit can require
            # the same intermediate as the suspended production goal (gears
            # for an inserter while planning science gears). Retaining that
            # outer path mistakes this agenda change for a recipe cycle.
            # Disable nested acquisition and scope cycle detection to this
            # component. Keep the complete path for power/demand evidence;
            # genuine cycles inside the component still reach _visit normally.
            return super()._need(item, count, path)
        finally:
            self._acquiring_buffer = acquiring
            self._recipe_cycle_prefix = prefix

    def _buffer(self, row: dict, amount: int, path) -> Plan | None:
        parts = row.get("parts", {})
        # Reserve by committing one bounded prerequisite at a time. Do not
        # offer sibling alternatives that might consume the construction kit.
        for part, name in PARTS.items():
            if part not in parts:
                self._buffer_service = True
                # Placement pays only this component. Acquire commissioning fuel
                # after the paid inserter exists, through its normal service path.
                prerequisite = self._prerequisite(name, 1, path)
                if prerequisite:
                    step = prerequisite.steps[0]
                    if step.action in {'factory_extract', 'factory_craft'}:
                        item = step.parameters.get('item') if step.action == 'factory_extract' else step.item
                        bill = construction_pickup_bill(self.snapshot, self.catalog, row, part, item)
                        if bill is not None:
                            prerequisite = replace(prerequisite, materials={
                                **(prerequisite.materials or {}),
                                'buffer_component_prerequisite': bill})
                        if bill is not None and step.action == 'factory_extract':
                            role = step.parameters['role']
                            available = self.entities[role].get('output', {}).get(item, 0)
                            if type(available) is int and available > 0:
                                quantity = min(self.collection_batch, available,
                                               bill['component_input_deficit'])
                                provenance = (prerequisite.materials or {}).get('output_pickup')
                                if isinstance(provenance, dict):
                                    prerequisite = replace(prerequisite, steps=(replace(
                                        step, parameters={**step.parameters, 'quantity': quantity}),),
                                        description=f"Collect {quantity} {item} for current {name} output-buffer component",
                                        materials=prerequisite.materials)
                    from .buffer_demand import scope_prerequisite
                    return scope_prerequisite(self.snapshot, self.catalog, prerequisite, row, part, path)
                receipt = f"buffer:{self.snapshot.tick}:{row['source_unit']}:{part}"
                plan = self._plan(
                    COMMAND, "buffer_component", parameters={
                        "source": row["source"], "layout": row["layout"],
                        "part": part, "receipt": receipt,
                    }, costs={name: 1}, identity=f"{row['layout']}:{part}",
                    description=f"Build paid {name} for {row['source']} output buffer",
                    timeout=18000,
                )
                from .buffer_demand import scope_commissioning
                return scope_commissioning(self.snapshot, self.catalog, plan, row, path)
        inserter_role = parts["inserter"]["role"]
        inserter = self.entities[inserter_role]
        fuel = inserter.get("fuel", {}).get("coal", 0)
        if fuel < 2:
            self._buffer_service = True
            from .fuel_service import service_plan
            plan = service_plan(self, inserter_role, row["source"], path, self._prerequisite)
            from .buffer_demand import scope_commissioning
            return scope_commissioning(self.snapshot, self.catalog, plan, row, path)
        if not flow_complete(row["source"], row["layout"], self.snapshot):
            self._buffer_service = True
            recipe = self.catalog.recipe_for(row['item'])
            chest = self.entities.get(row['chest_role'], {})
            machine = self.entities[row['source']]
            # Stored stock cannot produce another positive transport sample.
            # An empty producer must receive its ordinary current recipe inputs
            # before waiting for the unchanged native three-sample flow gate.
            transportable = potential(row, self.snapshot, recipe) - chest.get('output', {}).get(row['item'], 0)
            ready = machine.get('output', {}).get(row['item'], 0) + row.get('held', 0)
            if transportable <= 0 or (ready <= 0 and machine.get('fuel', {}).get('coal', 0) < 1):
                output = next(entry['amount'] for entry in recipe['products'] if entry['name'] == row['item'])
                missing = amount - self.snapshot.inventory.get(row['item'], 0)
                if missing > 0:
                    production_path = self._visit('item:' + row['item'], path)
                    prerequisite = self._production(recipe, row['source'],
                        min(20, math.ceil(missing / output)), production_path)
                    if prerequisite:
                        return prerequisite
            # A short, explicit commissioning interval. Never call placement
            # success proof of transport; all three native growth samples count.
            return self._wait("buffer_flow", row["layout"], 3, row["source"],
                              timeout=1800, identity=f"commission:{row['layout']}")
        return None

    def _ready_buffer_output(self, item, amount, path=()) -> Plan | None:
        """Collect only paid, commissioned, current stock before refueling it.

        This does not certify new flow or authorize a transfer by forecast.
        The unchanged controller/dispatch guards still own locks and receipts.
        Retain bounded collection batching: do not turn each newly arrived
        plate into its own trip. Remaining demand is replanned after receipts.
        """
        missing = math.ceil(amount - self.snapshot.inventory.get(item, 0))
        if missing <= 0:
            return None
        for row in sources(self.snapshot).values():
            if (row.get("source", "").startswith("growth:")
                    or row.get("item") != item
                    or not flow_complete(row.get("source", ""), row.get("layout", ""), self.snapshot)):
                continue
            role = row.get("chest_role", "")
            available = self.entities.get(role, {}).get("output", {}).get(item, 0)
            if type(available) not in {int, float} or not math.isfinite(available) or available < 1:
                continue
            recipe = self.catalog.recipes.get(item, {})
            incoming = potential(row, self.snapshot, recipe) if recipe else available
            target = min(self.collection_batch, missing, incoming)
            if available < max(1, target):
                continue
            plan = self._transfer(role, item, min(missing, math.floor(available)), extracting=True)
            if plan.steps[0].allowed(self.snapshot) and not plan.steps[0].satisfied(self.snapshot):
                item_path = [entry.removeprefix('item:') for entry in
                             self._visit('item:' + item, path) if entry.startswith('item:')]
                return replace(plan, materials={**(plan.materials or {}), 'output_pickup': {
                    'observed_tick': self.snapshot.tick, 'planner_item_path': item_path,
                    'source_role': role, 'source_unit': self.entities[role]['unit_number'],
                    'item': item, 'observed_output': available,
                }, "maintenance_policy": {
                    "schema": 1, "reason": "collect_ready_owned_output_before_upstream_refill",
                    "observed_tick": self.snapshot.tick, "required": missing,
                    "ready": math.floor(available), "source": row["source"],
                }})
        return None

    def _need(self, item, amount, path=()):
        if self._acquiring_buffer or self.snapshot.inventory.get(item, 0) >= amount:
            return super()._need(item, amount, path)
        if self.focus is None:
            self._set_focus(item, amount)
        ready = self._ready_buffer_output(item, amount, path)
        if ready is not None:
            return ready
        for row in sources(self.snapshot).values():
            if row.get("source", "").startswith("growth:"):
                continue  # Explicit trial/preference policy owns successor collection.
            if row.get("item") != item or row.get("state") == "fault":
                continue
            machine = self.entities[row["source"]]
            recipe = self.catalog.recipes.get(item, {})
            if not recipe:
                continue
            # An unbuilt proposal cannot make already-paid furnace output
            # depend on buying a chest, an arm, or its future fuel. Keep the
            # ordinary pickup compiler and its exact parent-demand evidence.
            ready = machine.get("output", {}).get(item, 0)
            missing = math.ceil(amount - self.snapshot.inventory.get(item, 0))
            if (row["state"] == "proposed" and not row.get("parts")
                    and type(ready) in {int, float} and math.isfinite(ready)
                    and ready >= min(self.collection_batch, missing)):
                return super()._need(item, amount, path)
            if row["state"] == "proposed" and (
                self.goal != "rocket_launch" or amount - self.snapshot.inventory.get(item, 0) < 10
                or machine.get("products_finished", 0) < 20
                or machine.get("fuel", {}).get("coal", 0) < 5
                or potential(row, self.snapshot, recipe) < 10
            ):
                continue  # Do not build infrastructure for a one-off bootstrap item.
            service = self._buffer(row, amount, path)
            if service:
                return service
            missing = math.ceil(amount - self.snapshot.inventory.get(item, 0))
            available = self.entities[row["chest_role"]].get("output", {}).get(item, 0)
            incoming = potential(row, self.snapshot, recipe)
            target = min(self.collection_batch, missing, incoming)
            if available and available >= target:
                return self._transfer(row["chest_role"], item, min(missing, available), extracting=True)
            if incoming:
                # Refuel the producer when its in-flight inventory depends on it.
                if machine.get("fuel", {}).get("coal", 0) < 5:
                    prerequisite = self._fuel(row["source"], path)
                    if prerequisite:
                        return prerequisite
                return self._wait("machine_output", item, max(1, target), row["chest_role"],
                                  timeout=7200, identity=f"collect:{row['layout']}:{target}")
            output = next(entry["amount"] for entry in recipe["products"] if entry["name"] == item)
            # The owned-buffer branch bypasses FactoryPlanner._need's normal
            # product visit. Retain that recipe edge for its immediate input
            # gather/transfer and apply the same recursive-cycle guard.
            production_path = self._visit("item:" + item, path)
            prerequisite = self._production(recipe, row["source"], min(20, math.ceil(missing / output)), production_path)
            return prerequisite or self._wait("machine_output", item, min(10, missing), row["chest_role"],
                                              timeout=7200, identity=f"collect:{row['layout']}:{missing}")
        return super()._need(item, amount, path)

    def candidates(self) -> list[Plan]:
        primary = self.plan()
        if primary is None:
            return []
        if (self._buffer_service or not self.focus or self.factory.get("crafting_queue", 0)
                or primary.steps[0].action not in {
                    "factory_gather", "factory_insert", "factory_extract", "factory_wait"
                }):
            return [primary] if self._buffer_service else self._current_raw_craft_alternatives(primary)
        candidates = [primary]
        partial = self._partial_current_target_craft()
        if partial is not None:
            candidates.append(partial)
        for item, amount in list(sorted(self.targets.items()))[:32]:
            if self.snapshot.inventory.get(item, 0) >= amount:
                continue
            worker = self._candidate_worker()
            try:
                candidate = worker._need(item, amount)
            except (KeyError, ValueError):
                continue
            candidate = self._shared_bill_candidate(candidate, item, amount)
            if candidate and not worker._buffer_service and candidate.steps[0].action in {
                "factory_gather", "factory_insert", "factory_extract", "factory_craft"
            }:
                candidates.append(candidate)
        unique = {}
        for candidate in candidates:
            if candidate.steps[0].allowed(self.snapshot) and not candidate.steps[0].satisfied(self.snapshot):
                unique.setdefault(candidate.id, candidate)
        ready = [candidate for candidate in unique.values() if candidate.steps[0].action != "factory_wait"]
        item, amount = self.focus
        return [service_visit(self, replace(candidate, description=f"Next production batch: {amount} {item}. "
                        + candidate.description)) for candidate in (ready or list(unique.values()))[:self.max_candidates]]
