from control_center.github.poller import (
    _parse_checks,
    _parse_ci_status,
    _parse_my_prs,
    _parse_review_decision,
    _parse_review_requests,
)
from control_center.models import CIStatus, ReviewStatus


# --- _parse_ci_status ---


class TestParseCIStatus:
    def test_success(self):
        assert _parse_ci_status("SUCCESS") == CIStatus.SUCCESS

    def test_failure(self):
        assert _parse_ci_status("FAILURE") == CIStatus.FAILURE

    def test_error_maps_to_failure(self):
        assert _parse_ci_status("ERROR") == CIStatus.FAILURE

    def test_pending(self):
        assert _parse_ci_status("PENDING") == CIStatus.PENDING

    def test_expected_maps_to_pending(self):
        assert _parse_ci_status("EXPECTED") == CIStatus.PENDING

    def test_none_is_unknown(self):
        assert _parse_ci_status(None) == CIStatus.UNKNOWN

    def test_unknown_string(self):
        assert _parse_ci_status("SOMETHING_ELSE") == CIStatus.UNKNOWN


# --- _parse_review_decision ---


class TestParseReviewDecision:
    def test_approved(self):
        assert _parse_review_decision("APPROVED") == ReviewStatus.APPROVED

    def test_changes_requested(self):
        assert _parse_review_decision("CHANGES_REQUESTED") == ReviewStatus.CHANGES_REQUESTED

    def test_review_required(self):
        assert _parse_review_decision("REVIEW_REQUIRED") == ReviewStatus.PENDING

    def test_none_is_pending(self):
        assert _parse_review_decision(None) == ReviewStatus.PENDING

    def test_unknown_string(self):
        assert _parse_review_decision("SOMETHING_ELSE") == ReviewStatus.PENDING


# --- _parse_checks ---


class TestParseChecks:
    def test_check_runs(self):
        commit_node = {
            "commit": {
                "statusCheckRollup": {
                    "contexts": {
                        "nodes": [
                            {"name": "CI / test", "status": "COMPLETED", "conclusion": "SUCCESS"},
                            {"name": "CI / lint", "status": "COMPLETED", "conclusion": "FAILURE"},
                        ]
                    }
                }
            }
        }
        checks = _parse_checks(commit_node)
        assert len(checks) == 2
        assert checks[0].name == "CI / test"
        assert checks[0].conclusion == "SUCCESS"
        assert checks[1].conclusion == "FAILURE"

    def test_status_contexts(self):
        """StatusContext uses 'context' instead of 'name' and 'state' instead of 'status'."""
        commit_node = {
            "commit": {
                "statusCheckRollup": {
                    "contexts": {
                        "nodes": [
                            {"context": "ci/circleci", "state": "SUCCESS"},
                        ]
                    }
                }
            }
        }
        checks = _parse_checks(commit_node)
        assert len(checks) == 1
        assert checks[0].name == "ci/circleci"
        assert checks[0].status == "SUCCESS"
        assert checks[0].conclusion == "SUCCESS"

    def test_no_rollup(self):
        assert _parse_checks({"commit": {}}) == []
        assert _parse_checks({}) == []

    def test_empty_contexts(self):
        commit_node = {"commit": {"statusCheckRollup": {"contexts": {"nodes": []}}}}
        assert _parse_checks(commit_node) == []


# --- _parse_my_prs ---


def _make_gql_pr_node(**overrides):
    """Build a minimal GraphQL PR node for testing."""
    node = {
        "number": 1,
        "title": "Test PR",
        "url": "https://github.com/org/repo/pull/1",
        "repository": {"nameWithOwner": "org/repo", "isArchived": False},
        "headRefName": "feat/test",
        "baseRefName": "staging",
        "author": {"login": "user"},
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": "SUCCESS",
                            "contexts": {"nodes": []},
                        }
                    }
                }
            ]
        },
        "reviews": {"nodes": []},
    }
    node.update(overrides)
    return node


def _wrap_prs_response(nodes):
    return {"data": {"viewer": {"pullRequests": {"nodes": nodes}}}}


class TestParseMyPrs:
    def test_basic_pr(self):
        prs = _parse_my_prs(_wrap_prs_response([_make_gql_pr_node()]))
        assert len(prs) == 1
        assert prs[0].number == 1
        assert prs[0].repo == "org/repo"
        assert prs[0].ci_status == CIStatus.SUCCESS

    def test_skips_archived_repos(self):
        node = _make_gql_pr_node()
        node["repository"]["isArchived"] = True
        prs = _parse_my_prs(_wrap_prs_response([node]))
        assert len(prs) == 0

    def test_missing_author(self):
        node = _make_gql_pr_node()
        node["author"] = None
        prs = _parse_my_prs(_wrap_prs_response([node]))
        assert prs[0].author == "unknown"

    def test_draft_pr(self):
        node = _make_gql_pr_node(isDraft=True)
        prs = _parse_my_prs(_wrap_prs_response([node]))
        assert prs[0].is_draft is True

    def test_ci_failure(self):
        node = _make_gql_pr_node()
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
        prs = _parse_my_prs(_wrap_prs_response([node]))
        assert prs[0].ci_status == CIStatus.FAILURE

    def test_no_commits(self):
        node = _make_gql_pr_node()
        node["commits"]["nodes"] = []
        prs = _parse_my_prs(_wrap_prs_response([node]))
        assert prs[0].ci_status == CIStatus.UNKNOWN

    def test_reviews_parsed(self):
        node = _make_gql_pr_node()
        node["reviews"]["nodes"] = [
            {"author": {"login": "reviewer"}, "state": "APPROVED", "body": "LGTM"},
        ]
        prs = _parse_my_prs(_wrap_prs_response([node]))
        assert len(prs[0].reviews) == 1
        assert prs[0].reviews[0].author == "reviewer"
        assert prs[0].reviews[0].state == "APPROVED"

    def test_empty_response(self):
        assert _parse_my_prs({"data": {"viewer": {"pullRequests": {"nodes": []}}}}) == []
        assert _parse_my_prs({}) == []

    def test_multiple_prs(self):
        nodes = [_make_gql_pr_node(number=1), _make_gql_pr_node(number=2)]
        prs = _parse_my_prs(_wrap_prs_response(nodes))
        assert len(prs) == 2


# --- _parse_review_requests ---


def _make_gql_review_request_node(**overrides):
    node = {
        "number": 10,
        "title": "Review this",
        "url": "https://github.com/org/repo/pull/10",
        "repository": {"nameWithOwner": "org/repo", "isArchived": False},
        "author": {"login": "other"},
        "reviewDecision": None,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "reviews": {"nodes": []},
    }
    node.update(overrides)
    return node


def _wrap_review_response(nodes):
    return {"data": {"search": {"nodes": nodes}}}


class TestParseReviewRequests:
    def test_basic_review_request(self):
        reqs = _parse_review_requests(_wrap_review_response([_make_gql_review_request_node()]), "me")
        assert len(reqs) == 1
        assert reqs[0].number == 10
        assert reqs[0].author == "other"

    def test_skips_archived(self):
        node = _make_gql_review_request_node()
        node["repository"]["isArchived"] = True
        reqs = _parse_review_requests(_wrap_review_response([node]), "me")
        assert len(reqs) == 0

    def test_skips_nodes_without_number(self):
        node = _make_gql_review_request_node()
        del node["number"]
        reqs = _parse_review_requests(_wrap_review_response([node]), "me")
        assert len(reqs) == 0

    def test_detects_other_approvals(self):
        node = _make_gql_review_request_node()
        node["reviews"]["nodes"] = [
            {"state": "APPROVED", "author": {"login": "someone_else"}},
        ]
        reqs = _parse_review_requests(_wrap_review_response([node]), "me")
        assert reqs[0].has_other_approvals is True

    def test_own_approval_not_counted(self):
        node = _make_gql_review_request_node()
        node["reviews"]["nodes"] = [
            {"state": "APPROVED", "author": {"login": "me"}},
        ]
        reqs = _parse_review_requests(_wrap_review_response([node]), "me")
        assert reqs[0].has_other_approvals is False

    def test_non_approval_not_counted(self):
        node = _make_gql_review_request_node()
        node["reviews"]["nodes"] = [
            {"state": "COMMENTED", "author": {"login": "someone"}},
        ]
        reqs = _parse_review_requests(_wrap_review_response([node]), "me")
        assert reqs[0].has_other_approvals is False
