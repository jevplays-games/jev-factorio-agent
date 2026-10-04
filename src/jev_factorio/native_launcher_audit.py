"""Read-only structural audit of launcher/maintenance receipt field contracts.

This does not import either script, verify its signature, or authorize a launch.
It catches missing literal receipt fields before a maintenance signal is sent.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

MAX_SOURCE_BYTES = 1024 * 1024


class UnsupportedContract(ValueError):
    """The script does not use the literal receipt shape this audit understands."""


def _function(tree: ast.Module, qualified_name: str) -> ast.FunctionDef:
    scope = tree
    for name in qualified_name.split("."):
        # Reviewed native launchers retain historical definitions. Python uses
        # the last definition, so auditing the first can falsely report success.
        matches = [node for node in scope.body
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name]
        if not matches:
            raise UnsupportedContract(f"Function not found: {qualified_name}")
        scope = matches[-1]
    if not isinstance(scope, ast.FunctionDef):
        raise UnsupportedContract("The selected contract must be a function")
    return scope


def audit(launcher: str, maintenance: str, *, producer: str = "launch",
          consumer: str) -> dict:
    """Compare literal durable(RESULT, {...}) exits with strict result[key] reads.

    Exception-only receipts are reported separately: they cannot stand in for a
    terminal child exit. Dynamic receipt construction is unsupported, not a pass.
    This is a field-shape check, not a control-flow or provenance proof.
    """
    for source in (launcher, maintenance):
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise UnsupportedContract("Source exceeds the audit size bound")
    writer = _function(ast.parse(launcher), producer)
    reader = _function(ast.parse(maintenance), consumer)
    required = set()
    for node in ast.walk(reader):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "result"):
            if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
                raise UnsupportedContract("Dynamic result field lookup")
            required.add(node.slice.value)
    if not required:
        raise UnsupportedContract("No strict result field reads found")

    exits, other = [], []
    for node in ast.walk(writer):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "durable" and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "RESULT"):
            continue
        if len(node.args) != 2 or node.keywords or not isinstance(node.args[1], ast.Dict):
            raise UnsupportedContract("Nonliteral result receipt construction")
        keys = node.args[1].keys
        if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in keys):
            raise UnsupportedContract("Dynamic result receipt fields")
        fields = {key.value for key in keys}
        row = {"line": node.lineno, "fields": sorted(fields)}
        if "exit_code" in fields:
            row["missing_fields"] = sorted(required - fields)
            exits.append(row)
        else:
            other.append(row)
    if not exits:
        raise UnsupportedContract("No literal terminal exit receipt found")
    return {
        "schema": 1,
        "scope": "static_literal_field_contract_only",
        "launch_authorized": False,
        "compatible": all(not row["missing_fields"] for row in exits),
        "launcher_sha256": hashlib.sha256(launcher.encode()).hexdigest(),
        "maintenance_sha256": hashlib.sha256(maintenance.encode()).hexdigest(),
        "producer": producer, "consumer": consumer,
        "required_fields": sorted(required),
        "terminal_receipts": exits, "nonterminal_receipts": other,
    }


def _read(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise UnsupportedContract("Source exceeds the audit size bound")
    return raw.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--maintenance", type=Path, required=True)
    parser.add_argument("--producer", default="launch")
    parser.add_argument("--consumer", required=True,
                        help="Function or Class.method containing strict result field reads")
    args = parser.parse_args(argv)
    try:
        report = audit(_read(args.launcher), _read(args.maintenance),
                       producer=args.producer, consumer=args.consumer)
    except (UnsupportedContract, SyntaxError, UnicodeError, OSError) as error:
        # Do not echo source fragments from SyntaxError or private script paths.
        print(json.dumps({"schema": 1, "compatible": False, "launch_authorized": False,
                          "status": "unsupported_or_unreadable", "error_type": type(error).__name__}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
