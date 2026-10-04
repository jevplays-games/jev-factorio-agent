"""Reject negated GitHub closing keywords that can close issues on merge."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


_NEGATED_CLOSING_REFERENCE = re.compile(
    r"\b(?:"
    r"does\s+not|doesn't|do\s+not|don't|did\s+not|didn't|"
    r"will\s+not|won't|would\s+not|wouldn't|should\s+not|shouldn't|"
    r"must\s+not|mustn't|cannot|can't|never|not"
    r")\s+(?:\w[\w'-]*\s+){0,2}"
    r"(?:close|closes|closed|closing|fix|fixes|fixed|fixing|"
    r"resolve|resolves|resolved|resolving)\s+"
    r"(?:issue\s+)?(?:#\d+|[\w.-]+/[\w.-]+#\d+|"
    r"https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+)\b",
    re.IGNORECASE,
)


def find_negated_closing_references(body: str) -> list[str]:
    """Return each numbered closing reference placed in a negated phrase."""
    return [match.group(0) for match in _NEGATED_CLOSING_REFERENCE.finditer(body)]


def main(event_path: str) -> int:
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Unable to read the pull request event: {exc}", file=sys.stderr)
        return 2

    if not isinstance(event, dict):
        print("The event must be a JSON object.", file=sys.stderr)
        return 2

    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        print("The event does not contain a pull request body.", file=sys.stderr)
        return 2

    body = pull_request.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str):
        print("The pull request body must be text or null.", file=sys.stderr)
        return 2

    references = find_negated_closing_references(body)
    if references:
        print("Negated closing references can still close issues on merge:")
        for reference in references:
            print(f"  - {reference}")
        print("Use a neutral `Refs #...` reference or remove the issue number.")
        return 1

    print("No negated numbered closing references found.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {Path(sys.argv[0]).name} <github-event.json>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
