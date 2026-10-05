"""Strict contracts for opt-in, inventory-paid furnace output buffers.

A build receipt proves a paid component, not material flow. Commissioning is
separate bounded, sampled conservation evidence, not an anti-tamper guarantee.
"""
from __future__ import annotations

import math
from copy import deepcopy

COMMAND = "factory_buffer_build"
FIELDS = {"source", "layout", "part", "receipt"}
PARTS = {"chest": "wooden-chest", "inserter": "burner-inserter"}
EFFECTS = {"buffer_component", "buffer_flow"}
SOURCES = {"recipe:iron-plate", "recipe:copper-plate", "recipe:steel-plate"}
SUCCESSOR_SOURCES = {"growth:iron-plate", "growth:copper-plate"}


def validate_commitments(owned: dict, *, successors: bool = False) -> None:
    """Validate retained paid owners independently of live topology or flow."""
    roles = SOURCES | (SUCCESSOR_SOURCES if successors else set())
    if not isinstance(owned, dict) or not set(owned) <= roles:
        raise ValueError("Invalid output-buffer ownership map")
    units, paid_roles, receipts = set(), set(), set()
    for entry in owned.values():
        if (not isinstance(entry, dict) or set(entry) != {"source_unit", "layout", "parts"}
                or type(entry["source_unit"]) is not int or not 0 < entry["source_unit"] <= 2**53 - 1
                or entry["source_unit"] in units
                or not isinstance(entry["layout"], str) or not 0 < len(entry["layout"]) <= 128
                or not isinstance(entry["parts"], dict) or not set(entry["parts"]) <= PARTS.keys()
                or "inserter" in entry["parts"] and "chest" not in entry["parts"]):
            raise ValueError("Invalid output-buffer commitment")
        units.add(entry["source_unit"])
        for paid in entry["parts"].values():
            if (not isinstance(paid, dict) or set(paid) != {"role", "unit_number", "receipt", "paid"}
                    or type(paid["paid"]) is not int or paid["paid"] != 1
                    or type(paid["unit_number"]) is not int or not 0 < paid["unit_number"] <= 2**53 - 1
                    or paid["unit_number"] in units
                    or any(not isinstance(paid[k], str) or not 0 < len(paid[k]) <= 128
                           for k in ("role", "receipt"))
                    or paid["role"] in paid_roles or paid["role"] in roles
                    or paid["receipt"] in receipts):
                raise ValueError("Invalid or aliased output-buffer paid identity")
            units.add(paid["unit_number"]); paid_roles.add(paid["role"]); receipts.add(paid["receipt"])


def _validate_composed_paid_identities(memory, output_owners: dict | None = None) -> None:
    """Reject one paid component claimed by distinct retained owner families.

    Route endpoints and ``source_unit`` values are references to existing
    entities, not newly paid parts.  Only each family's retained ``parts`` map
    participates.  Successor input receipts intentionally mirror the input
    route owner's same source/part; that exact mirror is indexed once.
    """
    if output_owners is None:
        output_owners = {}
        if hasattr(memory, "output_commitments"):
            if not isinstance(memory.output_commitments, dict):
                raise ValueError("Invalid retained output owner map")
            output_owners.update(memory.output_commitments)
        if hasattr(memory, "successor_receipts"):
            if not isinstance(memory.successor_receipts, dict):
                raise ValueError("Invalid retained successor owner map")
            for source, retained in memory.successor_receipts.items():
                if not isinstance(retained, dict) or not isinstance(retained.get("output"), dict):
                    raise ValueError("Invalid retained successor output owners")
                if retained["output"]:
                    if source in output_owners:
                        raise ValueError("Output and successor output share a retained owner key")
                    output_owners[source] = {"parts": retained["output"]}

    seen = {"unit_number": {}, "receipt": {}, "role": {}}

    def add(family, source, part, paid, mirror=None):
        if (not isinstance(paid, dict)
                or type(paid.get("unit_number")) is not int
                or not 0 < paid["unit_number"] <= 2**53 - 1
                or not isinstance(paid.get("receipt"), str)
                or not 0 < len(paid["receipt"]) <= 128
                or not isinstance(paid.get("role"), str)
                or not 0 < len(paid["role"]) <= 128
                or type(paid.get("paid")) is not int or paid["paid"] != 1):
            raise ValueError("Invalid retained paid owner identity")
        identity = (paid["unit_number"], paid["receipt"], paid["role"])
        owner = (family, source, part)
        for dimension in ("unit_number", "receipt", "role"):
            value = paid[dimension]
            previous = seen[dimension].get(value)
            if previous is not None:
                previous_identity, previous_mirror, previous_owner = previous
                if not (mirror is not None and mirror == previous_mirror
                        and identity == previous_identity):
                    raise ValueError(
                        f"Aliased retained paid owner {dimension}: {previous_owner!r} and {owner!r}")
            else:
                seen[dimension][value] = (identity, mirror, owner)

    def add_map(family, mapping, *, mirror_family=None):
        if not isinstance(mapping, dict):
            raise ValueError(f"Invalid retained {family} owner map")
        for source, entry in mapping.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("parts"), dict):
                raise ValueError(f"Invalid retained {family} owner entry")
            for part, paid in entry["parts"].items():
                mirror = None
                if mirror_family == "input":
                    retained = getattr(memory, "successor_receipts", {}).get(source, {})
                    mirrored = retained.get("input", {}) if isinstance(retained, dict) else {}
                    if isinstance(mirrored, dict) and mirrored.get(part) == paid:
                        mirror = ("input-successor", source, part)
                add(family, source, part, paid, mirror)

    # `output_owners` includes the output parts mirrored through successor
    # receipts, preserving the existing composed output validation.
    for source, entry in output_owners.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("parts"), dict):
            raise ValueError("Invalid composed output owner entry")
        family = "successor_output" if source in SUCCESSOR_SOURCES else "output"
        for part, paid in entry["parts"].items():
            add(family, source, part, paid)

    if hasattr(memory, "input_commitments"):
        add_map("input", memory.input_commitments, mirror_family="input")

    if hasattr(memory, "successor_receipts"):
        receipts = memory.successor_receipts
        if not isinstance(receipts, dict):
            raise ValueError("Invalid retained successor owner map")
        for source, retained in receipts.items():
            if not isinstance(retained, dict) or not isinstance(retained.get("input"), dict):
                raise ValueError("Invalid retained successor input owners")
            for part, paid in retained["input"].items():
                mirror = None
                input_entry = getattr(memory, "input_commitments", {}).get(source, {})
                input_parts = input_entry.get("parts", {}) if isinstance(input_entry, dict) else {}
                if isinstance(input_parts, dict) and input_parts.get(part) == paid:
                    mirror = ("input-successor", source, part)
                add("successor_input", source, part, paid, mirror)

    if hasattr(memory, "outpost_commitments"):
        add_map("outpost", memory.outpost_commitments)

    if hasattr(memory, "solid_commitments"):
        add_map("solid", memory.solid_commitments)

    if hasattr(memory, "coal_commitments"):
        add_map("coal", memory.coal_commitments)


def expected_commitments(memory) -> dict:
    """Combine disjoint durable ordinary and successor output owners."""
    owned = deepcopy(memory.output_commitments)
    successors = hasattr(memory, 'successor_schema')
    validate_commitments(owned)
    if successors:
        if not isinstance(memory.successor_projects, dict) or not isinstance(memory.successor_receipts, dict):
            raise ValueError('Invalid successor ownership maps')
        for source, project in memory.successor_projects.items():
            retained = memory.successor_receipts.get(source, {})
            if not isinstance(project, dict) or not isinstance(retained, dict):
                raise ValueError('Invalid retained successor owner')
            if retained.get('output'):
                known = {'source_unit': project.get('source_unit'), 'layout': retained.get('output_layout'),
                         'parts': deepcopy(retained['output'])}
                validate_commitments({source: known}, successors=True)
                if source not in SUCCESSOR_SOURCES or any(
                        p['unit_number'] == project.get('predecessor_unit') for p in known['parts'].values()):
                    raise ValueError('Invalid or aliased successor output ownership')
                owned[source] = known
    validate_commitments(owned, successors=successors)
    _validate_composed_paid_identities(memory, owned)
    return owned


def validate(parameters: dict) -> None:
    if (not isinstance(parameters, dict) or set(parameters) != FIELDS
            or any(not isinstance(value, str) or not value or len(value) > 128
                   for value in parameters.values())
            or parameters["part"] not in PARTS):
        raise ValueError("Invalid furnace-buffer command")


def sources(snapshot) -> dict:
    data = snapshot.factory.get("output_buffers", {})
    if (not isinstance(data, dict) or type(data.get("protocol")) is not int
            or data["protocol"] != 1 or data.get("session_id") != snapshot.session_id
            or type(data.get("tick")) is not int or data["tick"] != snapshot.tick
            or not isinstance(data.get("sources"), dict) or len(data["sources"]) > (5 if "successors" in snapshot.factory else 3)):
        raise ValueError("Missing or stale furnace-buffer telemetry")
    return data["sources"]


def current(row: dict, snapshot) -> bool:
    if not isinstance(row, dict):
        return False
    source = snapshot.factory.get("entities", {}).get(row.get("source", ""), {})
    return (type(row.get("source_unit")) is int and row["source_unit"] > 0
            and source.get("unit_number") == row["source_unit"]
            and row.get("state") in {"proposed", "building", "ready"})


def allowed(parameters: dict, snapshot) -> bool:
    validate(parameters)
    try:
        row = sources(snapshot).get(parameters["source"], {})
    except ValueError:
        return False
    parts = row.get("parts", {})
    return bool(current(row, snapshot) and row.get("layout") == parameters["layout"]
                and not parts.get(parameters["part"])
                and (parameters["part"] == "chest" or parts.get("chest"))
                and snapshot.factory.get("player_bound") is True
                and snapshot.factory.get("player_connected") is True
                and snapshot.factory.get("crafting_queue") == 0
                and snapshot.inventory.get(PARTS[parameters["part"]], 0) >= 1)


def component_complete(parameters: dict, snapshot) -> bool:
    validate(parameters)
    try:
        row = sources(snapshot).get(parameters["source"], {})
    except ValueError:
        return False
    part = row.get("parts", {}).get(parameters["part"], {})
    entity = snapshot.factory.get("entities", {}).get(part.get("role", ""), {})
    return bool(current(row, snapshot) and row.get("layout") == parameters["layout"]
                and part.get("receipt") == parameters["receipt"]
                and part.get("paid") == 1 and type(part.get("paid")) is int
                and type(part.get("unit_number")) is int and part["unit_number"] > 0
                and entity.get("unit_number") == part["unit_number"]
                and entity.get("name") == PARTS[parameters["part"]])


def flow_complete(source: str, layout: str, snapshot) -> bool:
    try:
        row = sources(snapshot).get(source, {})
    except ValueError:
        return False
    if (not current(row, snapshot) or row.get("state") != "ready"
            or row.get("layout") != layout or row.get("topology") is not True):
        return False
    for name, prototype in PARTS.items():
        part = row.get("parts", {}).get(name, {})
        entity = snapshot.factory.get("entities", {}).get(part.get("role", ""), {})
        if (type(part.get("unit_number")) is not int or part["unit_number"] <= 0
                or entity.get("unit_number") != part["unit_number"]
                or entity.get("name") != prototype):
            return False
    proof = row.get("flow", {})
    keys = ("first_tick", "last_tick", "positive_samples", "received")
    if (not isinstance(proof, dict) or any(type(proof.get(key)) is not int
                                         or proof[key] < 0 for key in keys)):
        return False
    return (proof.get("layout") == layout and type(proof.get("source_unit")) is int
            and proof["source_unit"] == row["source_unit"]
            and proof.get("conservation") is True
            and proof["last_tick"] <= snapshot.tick
            and proof["last_tick"] - proof["first_tick"] >= 120
            and proof["positive_samples"] >= 3 and proof["received"] >= 3)


def permits(action: str, parameters: dict, snapshot) -> bool:
    """Never let the actor race the inserter or seed its output evidence."""
    rows = sources(snapshot)
    for source, row in rows.items():
        if row.get("state") == "fault":
            return False
        parts = row.get("parts", {})
        chest = parts.get("chest", {}).get("role")
        role = parameters.get("role")
        if role == chest and action == "factory_insert":
            return False
        if (role == source and parts.get("inserter")
                and action in {"factory_extract", "factory_configure"}):
            return False
        if (role == chest and action == "factory_extract"
                and not flow_complete(source, row.get("layout", ""), snapshot)):
            return False
    return True


def potential(row: dict, snapshot, recipe: dict) -> int:
    """Bound collection by actual outputs and already-paid native ingredients."""
    entity = snapshot.factory["entities"][row["source"]]
    chest = snapshot.factory["entities"].get(row.get("chest_role", ""), {})
    item = row["item"]
    ingredients = recipe.get("ingredients", [])
    if not ingredients or any(entry["type"] != "item" or entry["amount"] <= 0
                              for entry in ingredients):
        return 0
    buffered = min(math.floor(entity.get("input", {}).get(entry["name"], 0) / entry["amount"])
                   for entry in ingredients)
    output = next(entry["amount"] for entry in recipe["products"] if entry["name"] == item)
    return math.floor(chest.get("output", {}).get(item, 0)
                      + entity.get("output", {}).get(item, 0) + row.get("held", 0)
                      + output * (buffered + int(entity.get("crafting") is True)))
