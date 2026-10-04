import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_references.py"


def run_event_cli(tmp_path, event_text):
    event_path = tmp_path / "event.json"
    event_path.write_text(event_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(event_path)],
        capture_output=True,
        check=False,
        text=True,
    )


def test_cli_rejects_historical_negated_closing_forms(tmp_path):
    event = {
        "pull_request": {
            "body": (
                "Does not close #103 or #245. "
                "This does not resolve #96's remaining attribution work. "
                "Don't fix CompleteDotTech/jev-factorio-agent#95. "
                "Do not close https://github.com/jevplays-games/jev-factorio-agent/issues/94."
            )
        }
    }

    result = run_event_cli(tmp_path, json.dumps(event))

    assert result.returncode == 1
    assert "Does not close #103" in result.stdout
    assert "does not resolve #96" in result.stdout
    assert "Don't fix CompleteDotTech/jev-factorio-agent#95" in result.stdout
    assert "Do not close https://github.com/jevplays-games/jev-factorio-agent/issues/94" in result.stdout


def test_cli_accepts_null_or_missing_body(tmp_path):
    for event in ('{"pull_request":{"body":null}}', '{"pull_request":{}}'):
        result = run_event_cli(tmp_path, event)
        assert result.returncode == 0
        assert "No negated" in result.stdout


def test_cli_fails_closed_for_malformed_event_or_body(tmp_path):
    for event in ("{", "[]", "{}", '{"pull_request":{"body":[]}}'):
        result = run_event_cli(tmp_path, event)
        assert result.returncode == 2


def test_cli_accepts_neutral_refs_and_positive_closing_references(tmp_path):
    event = {
        "pull_request": {
            "body": (
                "Refs #103 and CompleteDotTech/jev-factorio-agent#245. "
                "This does not complete the broader rollout. "
                "Fixes https://github.com/jevplays-games/jev-factorio-agent/issues/251."
            )
        }
    }

    result = run_event_cli(tmp_path, json.dumps(event))

    assert result.returncode == 0
    assert "No negated" in result.stdout
