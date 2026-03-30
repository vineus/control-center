import pytest

from control_center.agent.autofix import build_prompt, detect_fix_type
from control_center.models import CIStatus, FixType, PRStatus, ReviewStatus

from datetime import datetime, timezone


def _make_pr(**overrides) -> PRStatus:
    defaults = {
        "number": 1,
        "title": "Test PR",
        "url": "https://github.com/org/repo/pull/1",
        "repo": "org/repo",
        "head_ref": "feat/test",
        "base_ref": "staging",
        "author": "user",
        "ci_status": CIStatus.SUCCESS,
        "review_status": ReviewStatus.APPROVED,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return PRStatus(**defaults)


# --- detect_fix_type ---


class TestDetectFixType:
    def test_merge_conflict(self):
        pr = _make_pr(mergeable="CONFLICTING")
        assert detect_fix_type(pr) == FixType.MERGE_CONFLICT

    def test_ci_failure(self):
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        assert detect_fix_type(pr) == FixType.CI_FAILURE

    def test_conflict_takes_priority_over_ci(self):
        """When both conflict and CI failure, merge conflict is returned first."""
        pr = _make_pr(ci_status=CIStatus.FAILURE, mergeable="CONFLICTING")
        assert detect_fix_type(pr) == FixType.MERGE_CONFLICT

    def test_no_issue(self):
        pr = _make_pr(ci_status=CIStatus.SUCCESS, mergeable="MERGEABLE")
        assert detect_fix_type(pr) is None

    def test_pending_ci_no_conflict(self):
        pr = _make_pr(ci_status=CIStatus.PENDING, mergeable="UNKNOWN")
        assert detect_fix_type(pr) is None


# --- build_prompt ---


class TestBuildPrompt:
    def test_ci_failure_prompt_contains_logs(self):
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        prompt = build_prompt(pr, FixType.CI_FAILURE, ci_logs="ERROR: test failed")
        assert "ERROR: test failed" in prompt
        assert "org/repo" in prompt
        assert "#1" in prompt
        assert "make format" in prompt

    def test_ci_failure_prompt_contains_branch(self):
        pr = _make_pr(head_ref="fix/broken-test")
        prompt = build_prompt(pr, FixType.CI_FAILURE, ci_logs="")
        assert "fix/broken-test" in prompt

    def test_merge_conflict_prompt(self):
        pr = _make_pr(base_ref="main", mergeable="CONFLICTING")
        prompt = build_prompt(pr, FixType.MERGE_CONFLICT)
        assert "merge conflict" in prompt.lower()
        assert "origin/main" in prompt
        assert "rebase" in prompt.lower()
        assert "force-with-lease" in prompt

    def test_unknown_fix_type_raises(self):
        pr = _make_pr()
        with pytest.raises(ValueError, match="Unknown fix type"):
            build_prompt(pr, "bogus_type")
