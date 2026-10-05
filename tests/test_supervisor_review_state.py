"""Offline public-method controls for GitHub's current review opinion state.

All Git, GitHub, reviewer and check data in this module are modeled.  The tests
exercise Supervisor.verify_code without contacting GitHub or launching a child.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_factorio.supervisor import Supervisor


MERGE = "a" * 40
HEAD = "c" * 40
OLD_HEAD = "b" * 40
PR_URL = "https://github.com/jevplays-games/jev-factorio-agent/pull/1"


def _review(state, revision=HEAD, author="reviewer"):
    return {
        "author": {"login": author},
        "state": state,
        "commit": {"oid": revision},
    }


def _pull(raw_reviews):
    return {
        "url": PR_URL,
        "baseRefName": "main",
        "state": "MERGED",
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE},
        "reviews": raw_reviews,
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }


def _opinion_page(nodes, *, has_next=False, cursor=None, review_decision=None,
                  pr_overrides=None, connection_overrides=None):
    pull = {
        "number": 1,
        "url": PR_URL,
        "baseRefName": "main",
        "state": "MERGED",
        "headRefOid": HEAD,
        "reviewDecision": review_decision,
        "mergeCommit": {"oid": MERGE},
        "latestOpinionatedReviews": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        },
    }
    if pr_overrides:
        pull.update(pr_overrides)
    if connection_overrides:
        pull["latestOpinionatedReviews"].update(connection_overrides)
    return {
        "data": {
            "repository": {
                "pullRequest": pull,
            }
        }
    }


def _instance(tmp_path, monkeypatch, *, raw_reviews, opinion_pages=None,
              pull_overrides=None):
    instance = object.__new__(Supervisor)
    instance.config = SimpleNamespace(state_dir=tmp_path, python=sys.executable)
    pull = _pull(raw_reviews)
    if pull_overrides:
        pull.update(pull_overrides)
    pages = opinion_pages or {None: _opinion_page([])}
    calls = []

    def capture(command):
        calls.append(command)
        if command == ["git", "rev-parse", "HEAD"]:
            return 0, MERGE
        if command == ["git", "status", "--porcelain"]:
            return 0, ""
        if command == ["git", "remote"]:
            return 0, "origin"
        if command == ["git", "remote", "get-url", "--all", "origin"]:
            return 0, "https://github.com/jevplays-games/jev-factorio-agent.git"
        if command == ["git", "remote", "get-url", "--push", "--all", "origin"]:
            return 0, "https://github.com/jevplays-games/jev-factorio-agent.git"
        if command == ["git", "ls-remote", "origin", "refs/heads/main"]:
            return 0, MERGE + "\trefs/heads/main"
        if command[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps(pull)
        if command[:3] == ["gh", "api", "graphql"]:
            after = None
            for argument in command:
                if argument.startswith("after="):
                    after = argument.removeprefix("after=")
            page = pages.get(after)
            if page is None:
                return 1, "modeled missing page"
            if isinstance(page, tuple):
                return page
            return 0, json.dumps(page)
        if command == [sys.executable, "-m", "pytest", "tests/"]:
            return 0, "modeled test pass"
        pytest.fail(f"unexpected verification command: {command}")

    monkeypatch.setattr(instance, "capture", capture)
    return instance, calls


def _independent_artifact(tmp_path, result):
    artifact = tmp_path / "independent-review.json"
    artifact.write_text(json.dumps({
        "head": HEAD,
        "verdict": "approved",
        "reviewer": "independent-reviewer",
        "source_evidence": ["modeled evidence"],
    }))
    result.update(independent_review=str(artifact), repair_agent="repair-agent")


@pytest.mark.parametrize(
    "name,raw,opinions,independent,expected",
    [
        (
            "exact_head_approval",
            [_review("APPROVED")],
            [_review("APPROVED")],
            False,
            True,
        ),
        (
            "changes_request_veto_with_independent",
            [_review("CHANGES_REQUESTED")],
            [_review("CHANGES_REQUESTED")],
            True,
            False,
        ),
        (
            "comment_after_approval_preserves_opinion",
            [_review("APPROVED"), _review("COMMENTED")],
            [_review("APPROVED")],
            False,
            True,
        ),
        (
            "comment_after_change_request_preserves_veto",
            [_review("CHANGES_REQUESTED"), _review("COMMENTED")],
            [_review("CHANGES_REQUESTED")],
            True,
            False,
        ),
        (
            "new_approval_replaces_change_request",
            [_review("CHANGES_REQUESTED"), _review("APPROVED")],
            [_review("APPROVED")],
            False,
            True,
        ),
        (
            "dismissed_approval_does_not_approve",
            [_review("DISMISSED")],
            [_review("DISMISSED")],
            False,
            False,
        ),
        (
            "stale_head_approval_does_not_approve",
            [_review("APPROVED", OLD_HEAD)],
            [_review("APPROVED", OLD_HEAD)],
            False,
            False,
        ),
        (
            "comment_only_does_not_approve",
            [_review("COMMENTED")],
            [],
            False,
            False,
        ),
    ],
)
def test_verify_code_uses_preserved_opinion_across_raw_comment_events(
        tmp_path, monkeypatch, name, raw, opinions, independent, expected):
    del name  # The case label is retained by pytest's parameter ID.
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=raw,
        opinion_pages={None: _opinion_page(opinions)},
    )
    result = {"commit": MERGE, "pr_url": PR_URL}
    if independent:
        _independent_artifact(tmp_path, result)

    assert instance.verify_code(result) is expected
    assert ([sys.executable, "-m", "pytest", "tests/"] in calls) is expected


def test_authoritative_opinion_wins_when_raw_review_history_disagrees(tmp_path, monkeypatch):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("COMMENTED")],
        opinion_pages={None: _opinion_page([_review("APPROVED")])},
    )

    assert instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    graphql_calls = [call for call in calls if call[:3] == ["gh", "api", "graphql"]]
    assert len(graphql_calls) == 1
    query_argument = next(arg for arg in graphql_calls[0] if arg.startswith("query="))
    assert "latestOpinionatedReviews" in query_argument
    assert "reviewDecision" in query_argument
    assert "commit { oid }" in query_argument


def test_raw_approval_cannot_override_authoritative_change_request(tmp_path, monkeypatch):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: _opinion_page(
            [_review("CHANGES_REQUESTED")], review_decision="CHANGES_REQUESTED")},
    )
    result = {"commit": MERGE, "pr_url": PR_URL}
    _independent_artifact(tmp_path, result)

    assert not instance.verify_code(result)
    assert not any("pytest" in call for call in calls)


def test_null_review_decision_preserves_independent_artifact_path(tmp_path, monkeypatch):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("COMMENTED")],
        opinion_pages={None: _opinion_page([], review_decision=None)},
    )
    result = {"commit": MERGE, "pr_url": PR_URL}
    _independent_artifact(tmp_path, result)

    assert instance.verify_code(result)
    assert [sys.executable, "-m", "pytest", "tests/"] in calls


def test_review_decision_change_request_is_veto_even_without_opinion_node(tmp_path, monkeypatch):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[],
        opinion_pages={None: _opinion_page([], review_decision="CHANGES_REQUESTED")},
    )
    result = {"commit": MERGE, "pr_url": PR_URL}
    _independent_artifact(tmp_path, result)

    assert not instance.verify_code(result)
    assert not any("pytest" in call for call in calls)


def test_global_approved_decision_does_not_replace_exact_head_opinion(tmp_path, monkeypatch):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: _opinion_page(
            [_review("APPROVED", OLD_HEAD)], review_decision="APPROVED")},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("missing", ["reviewDecision", "hasNextPage", "endCursor"])
def test_missing_required_graphql_fields_fail_closed(tmp_path, monkeypatch, missing):
    page = _opinion_page([_review("APPROVED")], review_decision=None)
    pull = page["data"]["repository"]["pullRequest"]
    if missing == "reviewDecision":
        del pull["reviewDecision"]
    else:
        del pull["latestOpinionatedReviews"]["pageInfo"][missing]
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages={None: page})

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    graphql_calls = [call for call in calls if call[:3] == ["gh", "api", "graphql"]]
    assert len(graphql_calls) == 1
    assert not any("pytest" in call for call in calls)


def test_complete_opinion_pagination_finds_later_veto_with_stable_decision(
        tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([_review("APPROVED", author="first")],
                            has_next=True, cursor="page-2", review_decision=None),
        "page-2": _opinion_page([_review("CHANGES_REQUESTED", author="second")],
                                 review_decision=None),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages=pages)
    result = {"commit": MERGE, "pr_url": PR_URL}
    _independent_artifact(tmp_path, result)

    assert not instance.verify_code(result)
    graphql_calls = [call for call in calls if call[:3] == ["gh", "api", "graphql"]]
    assert len(graphql_calls) == 2
    assert any("after=page-2" in call for call in graphql_calls)
    assert not any("pytest" in call for call in calls)


def test_review_decision_change_between_pages_fails_closed(tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([_review("APPROVED", author="first")],
                            has_next=True, cursor="page-2", review_decision=None),
        "page-2": _opinion_page([_review("CHANGES_REQUESTED", author="second")],
                                 review_decision="CHANGES_REQUESTED"),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages=pages)
    result = {"commit": MERGE, "pr_url": PR_URL}
    _independent_artifact(tmp_path, result)

    assert not instance.verify_code(result)
    graphql_calls = [call for call in calls if call[:3] == ["gh", "api", "graphql"]]
    assert len(graphql_calls) == 2
    assert not any("pytest" in call for call in calls)


def test_complete_opinion_pagination_accepts_later_exact_head_approval(tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([], has_next=True, cursor="page-2"),
        "page-2": _opinion_page([_review("APPROVED", author="second")]),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages=pages)

    assert instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert len([call for call in calls if call[:3] == ["gh", "api", "graphql"]]) == 2


def test_failed_later_opinion_page_cannot_accept_first_page_approval(tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([_review("APPROVED")], has_next=True, cursor="page-2"),
        "page-2": (1, "modeled GraphQL page failure"),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages=pages)

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert len([call for call in calls if call[:3] == ["gh", "api", "graphql"]]) == 2
    assert not any("pytest" in call for call in calls)


def test_review_decision_cannot_change_between_opinion_pages(tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([_review("APPROVED")], has_next=True, cursor="page-2",
                            review_decision="REVIEW_REQUIRED"),
        "page-2": _opinion_page([], review_decision="APPROVED"),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages=pages)

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert len([call for call in calls if call[:3] == ["gh", "api", "graphql"]]) == 2
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("state", ["COMMENTED", "PENDING", "UNKNOWN"])
def test_nonopinionated_state_in_opinion_connection_fails_closed(tmp_path, monkeypatch, state):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: _opinion_page([_review(state)])},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("node", [
    {"state": "APPROVED", "author": {"login": "reviewer"}, "commit": {"oid": "not-an-oid"}},
    {"state": "APPROVED", "author": None, "commit": {"oid": HEAD}},
    {"state": "APPROVED", "author": {"login": " "}, "commit": {"oid": HEAD}},
    {"state": "APPROVED", "author": {"login": "reviewer"}, "commit": None},
    {"state": "APPROVED", "author": {"login": "reviewer"}, "commit": {"oid": None}},
    {"state": None, "author": {"login": "reviewer"}, "commit": {"oid": HEAD}},
])
def test_malformed_opinion_nodes_fail_closed(tmp_path, monkeypatch, node):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: _opinion_page([node])},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("nodes", [
    [_review("APPROVED", author="reviewer"), _review("CHANGES_REQUESTED", author="REVIEWER")],
    [_review("APPROVED", author="reviewer"), _review("APPROVED", author="reviewer")],
])
def test_duplicate_authors_across_current_opinions_fail_closed(tmp_path, monkeypatch, nodes):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[],
        opinion_pages={None: _opinion_page(nodes)},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("field,value", [
    ("number", 2),
    ("number", True),
    ("url", "https://github.com/jevplays-games/jev-factorio-agent/pull/2"),
    ("baseRefName", "release"),
    ("state", "OPEN"),
    ("headRefOid", OLD_HEAD),
    ("mergeCommit", {"oid": "d" * 40}),
])
def test_graphql_pull_identity_must_match_cli_metadata(tmp_path, monkeypatch, field, value):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: _opinion_page(
            [_review("APPROVED")], pr_overrides={field: value})},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("payload", [
    "not-json",
    json.dumps({"errors": [{"message": "partial response"}], "data": {}}),
    json.dumps({"data": {"repository": {"pullRequest": None}}}),
    json.dumps({"data": {"repository": {"pullRequest": {
        "number": 1, "url": PR_URL, "baseRefName": "main", "state": "MERGED",
        "headRefOid": HEAD, "reviewDecision": None, "mergeCommit": {"oid": MERGE},
    }}}}),
])
def test_invalid_or_incomplete_graphql_response_fails_closed(tmp_path, monkeypatch, payload):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: (0, payload)},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("connection", [
    {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}},
    {"nodes": [], "pageInfo": {"hasNextPage": "false", "endCursor": None}},
    {"nodes": "not-a-list", "pageInfo": {"hasNextPage": False, "endCursor": None}},
    {"nodes": [], "pageInfo": None},
])
def test_malformed_page_info_or_nodes_fail_closed(tmp_path, monkeypatch, connection):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: _opinion_page([], connection_overrides=connection)},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)


def test_repeated_page_cursor_fails_closed(tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([], has_next=True, cursor="same"),
        "same": _opinion_page([], has_next=True, cursor="same"),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[_review("APPROVED")], opinion_pages=pages)

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert len([call for call in calls if call[:3] == ["gh", "api", "graphql"]]) == 2
    assert not any("pytest" in call for call in calls)


def test_cross_page_duplicate_author_fails_closed(tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([_review("APPROVED", author="reviewer")],
                            has_next=True, cursor="page-2"),
        "page-2": _opinion_page([_review("CHANGES_REQUESTED", author="REVIEWER")]),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages=pages)

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert len([call for call in calls if call[:3] == ["gh", "api", "graphql"]]) == 2
    assert not any("pytest" in call for call in calls)


def test_later_page_pull_head_drift_fails_closed(tmp_path, monkeypatch):
    pages = {
        None: _opinion_page([], has_next=True, cursor="page-2"),
        "page-2": _opinion_page([_review("APPROVED")],
                                pr_overrides={"headRefOid": OLD_HEAD}),
    }
    instance, calls = _instance(
        tmp_path, monkeypatch, raw_reviews=[], opinion_pages=pages)

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert len([call for call in calls if call[:3] == ["gh", "api", "graphql"]]) == 2
    assert not any("pytest" in call for call in calls)


def test_dismissed_change_request_clears_veto_for_valid_independent_artifact(tmp_path, monkeypatch):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("CHANGES_REQUESTED"), _review("DISMISSED")],
        opinion_pages={None: _opinion_page([_review("DISMISSED")],
                                           review_decision="REVIEW_REQUIRED")},
    )
    result = {"commit": MERGE, "pr_url": PR_URL}
    _independent_artifact(tmp_path, result)

    assert instance.verify_code(result)
    assert [sys.executable, "-m", "pytest", "tests/"] in calls


def test_unknown_review_decision_fails_closed_even_with_independent_artifact(tmp_path, monkeypatch):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[],
        opinion_pages={None: _opinion_page([], review_decision="UNRECOGNIZED")},
    )
    result = {"commit": MERGE, "pr_url": PR_URL}
    _independent_artifact(tmp_path, result)

    assert not instance.verify_code(result)
    assert not any("pytest" in call for call in calls)


@pytest.mark.parametrize("checks", [None, {}, [None], [{"conclusion": {"result": "SUCCESS"}}]])
def test_malformed_status_check_metadata_fails_closed(tmp_path, monkeypatch, checks):
    instance, calls = _instance(
        tmp_path,
        monkeypatch,
        raw_reviews=[_review("APPROVED")],
        opinion_pages={None: _opinion_page([_review("APPROVED")])},
        pull_overrides={"statusCheckRollup": checks},
    )

    assert not instance.verify_code({"commit": MERGE, "pr_url": PR_URL})
    assert not any("pytest" in call for call in calls)
