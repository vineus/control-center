from datetime import datetime, timedelta, timezone

from control_center.models import (
    AutofixAttempt,
    AutofixStatus,
    CIStatus,
    DashboardState,
    FixType,
    PRStatus,
    ReviewRequest,
    ReviewStatus,
)


def _make_pr(**overrides) -> PRStatus:
    defaults = {
        "number": 1,
        "title": "Test PR",
        "url": "https://github.com/org/repo/pull/1",
        "repo": "org/repo",
        "head_ref": "feat/test",
        "author": "user",
        "ci_status": CIStatus.SUCCESS,
        "review_status": ReviewStatus.APPROVED,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return PRStatus(**defaults)


def _make_review_request(**overrides) -> ReviewRequest:
    defaults = {
        "number": 1,
        "title": "Review this",
        "url": "https://github.com/org/repo/pull/1",
        "repo": "org/repo",
        "author": "other",
        "review_status": ReviewStatus.PENDING,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ReviewRequest(**defaults)


# --- pr_key ---


class TestPRKey:
    def test_format(self):
        pr = _make_pr(repo="vibe-ad/api", number=42)
        assert pr.pr_key == "vibe-ad/api#42"


# --- merge_confidence ---


class TestMergeConfidence:
    def test_perfect_pr(self):
        """CI success + approved + not draft = 1.0"""
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.APPROVED, is_draft=False)
        assert pr.merge_confidence == 1.0

    def test_ci_success_no_review(self):
        """CI success + pending review = 0.4 (ci) + 0.2 (no changes_requested) = 0.6"""
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.PENDING)
        assert pr.merge_confidence == 0.6

    def test_ci_failure_approved(self):
        """CI failure + approved = 0.4 (review) + 0.2 (no changes_requested) = 0.6"""
        pr = _make_pr(ci_status=CIStatus.FAILURE, review_status=ReviewStatus.APPROVED)
        assert pr.merge_confidence == 0.6

    def test_changes_requested(self):
        """Changes requested removes the 0.2 bonus"""
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.CHANGES_REQUESTED)
        assert pr.merge_confidence == 0.4

    def test_draft_halves_score(self):
        """Draft PRs get their score halved"""
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.APPROVED, is_draft=True)
        assert pr.merge_confidence == 0.5

    def test_nothing_passing(self):
        """CI pending + no review = 0.2 (no changes_requested bonus only)"""
        pr = _make_pr(ci_status=CIStatus.PENDING, review_status=ReviewStatus.PENDING)
        assert pr.merge_confidence == 0.2

    def test_worst_case(self):
        """CI failure + changes_requested + draft = 0.0"""
        pr = _make_pr(ci_status=CIStatus.FAILURE, review_status=ReviewStatus.CHANGES_REQUESTED, is_draft=True)
        assert pr.merge_confidence == 0.0


# --- ready_to_merge ---


class TestReadyToMerge:
    def test_all_green(self):
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.APPROVED, is_draft=False)
        assert pr.ready_to_merge is True

    def test_ci_failure_blocks(self):
        pr = _make_pr(ci_status=CIStatus.FAILURE, review_status=ReviewStatus.APPROVED)
        assert pr.ready_to_merge is False

    def test_not_approved_blocks(self):
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.PENDING)
        assert pr.ready_to_merge is False

    def test_draft_blocks(self):
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.APPROVED, is_draft=True)
        assert pr.ready_to_merge is False

    def test_conflict_blocks(self):
        pr = _make_pr(ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.APPROVED, mergeable="CONFLICTING")
        assert pr.ready_to_merge is False

    def test_ci_pending_blocks(self):
        pr = _make_pr(ci_status=CIStatus.PENDING, review_status=ReviewStatus.APPROVED)
        assert pr.ready_to_merge is False


# --- needs_fix ---


class TestNeedsFix:
    def test_ci_failure(self):
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        assert pr.needs_fix is True

    def test_merge_conflict(self):
        pr = _make_pr(ci_status=CIStatus.SUCCESS, mergeable="CONFLICTING")
        assert pr.needs_fix is True

    def test_both_broken(self):
        pr = _make_pr(ci_status=CIStatus.FAILURE, mergeable="CONFLICTING")
        assert pr.needs_fix is True

    def test_all_good(self):
        pr = _make_pr(ci_status=CIStatus.SUCCESS, mergeable="MERGEABLE")
        assert pr.needs_fix is False

    def test_ci_pending_no_conflict(self):
        pr = _make_pr(ci_status=CIStatus.PENDING, mergeable="MERGEABLE")
        assert pr.needs_fix is False

    def test_unknown_ci_no_conflict(self):
        pr = _make_pr(ci_status=CIStatus.UNKNOWN, mergeable="UNKNOWN")
        assert pr.needs_fix is False


# --- ReviewRequest.priority ---


class TestReviewPriority:
    def test_high_priority_old(self):
        rr = _make_review_request(created_at=datetime.now(timezone.utc) - timedelta(hours=49))
        assert rr.priority == "high"

    def test_medium_priority(self):
        rr = _make_review_request(created_at=datetime.now(timezone.utc) - timedelta(hours=25))
        assert rr.priority == "medium"

    def test_low_priority_recent(self):
        rr = _make_review_request(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        assert rr.priority == "low"

    def test_boundary_48h(self):
        """At 48h the age_hours >= 48 due to computation time, so it's high"""
        rr = _make_review_request(created_at=datetime.now(timezone.utc) - timedelta(hours=48, seconds=1))
        assert rr.priority == "high"

    def test_boundary_24h(self):
        """At 24h the age_hours >= 24 due to computation time, so it's medium"""
        rr = _make_review_request(created_at=datetime.now(timezone.utc) - timedelta(hours=24, seconds=1))
        assert rr.priority == "medium"


# --- DashboardState.orgs ---


class TestDashboardOrgs:
    def test_extracts_orgs_from_prs(self):
        state = DashboardState(
            my_prs=[_make_pr(repo="alpha/repo1"), _make_pr(repo="beta/repo2", number=2)],
        )
        assert state.orgs == ["alpha", "beta"]

    def test_extracts_orgs_from_reviews(self):
        state = DashboardState(
            review_requests=[_make_review_request(repo="gamma/repo3")],
        )
        assert state.orgs == ["gamma"]

    def test_deduplicates(self):
        state = DashboardState(
            my_prs=[_make_pr(repo="org/repo1"), _make_pr(repo="org/repo2", number=2)],
            review_requests=[_make_review_request(repo="org/repo3")],
        )
        assert state.orgs == ["org"]

    def test_empty(self):
        state = DashboardState()
        assert state.orgs == []

    def test_sorted(self):
        state = DashboardState(
            my_prs=[_make_pr(repo="zebra/repo"), _make_pr(repo="alpha/repo", number=2)],
        )
        assert state.orgs == ["alpha", "zebra"]
