from datetime import datetime, timedelta, timezone

from control_center.models import (
    AutofixAttempt,
    AutofixStatus,
    CIStatus,
    FixType,
    PRStatus,
    ReviewRequest,
    ReviewStatus,
)
from control_center.web.routes import (
    _filter_prs,
    _filter_query_string,
    _filter_reviews,
    _sort_items,
    _timeago,
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


def _make_review(**overrides) -> ReviewRequest:
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


def _default_filters(**overrides) -> dict:
    f = {"org": None, "ci": None, "review": None, "search": None, "draft": None, "fixing": None, "sort": "updated"}
    f.update(overrides)
    return f


# --- _timeago ---


class TestTimeago:
    def test_just_now(self):
        assert _timeago(datetime.now(timezone.utc)) == "just now"

    def test_minutes(self):
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _timeago(dt) == "5m ago"

    def test_hours(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=3)
        assert _timeago(dt) == "3h ago"

    def test_days(self):
        dt = datetime.now(timezone.utc) - timedelta(days=7)
        assert _timeago(dt) == "7d ago"

    def test_months(self):
        dt = datetime.now(timezone.utc) - timedelta(days=60)
        assert _timeago(dt) == "2mo ago"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
        assert _timeago(dt) == "10m ago"


# --- _sort_items ---


class TestSortItems:
    def test_sort_by_updated(self):
        pr1 = _make_pr(number=1, updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        pr2 = _make_pr(number=2, updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
        result = _sort_items([pr1, pr2], "updated")
        assert result[0].number == 2

    def test_sort_by_created(self):
        pr1 = _make_pr(number=1, created_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
        pr2 = _make_pr(number=2, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        result = _sort_items([pr1, pr2], "created")
        assert result[0].number == 1

    def test_sort_by_confidence(self):
        pr1 = _make_pr(number=1, ci_status=CIStatus.FAILURE, review_status=ReviewStatus.PENDING)
        pr2 = _make_pr(number=2, ci_status=CIStatus.SUCCESS, review_status=ReviewStatus.APPROVED)
        result = _sort_items([pr1, pr2], "confidence")
        assert result[0].number == 2  # higher confidence first

    def test_sort_defaults_to_updated(self):
        pr1 = _make_pr(number=1, updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        pr2 = _make_pr(number=2, updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
        result = _sort_items([pr1, pr2], "unknown_sort")
        assert result[0].number == 2

    def test_empty_list(self):
        assert _sort_items([], "updated") == []


# --- _filter_prs ---


class TestFilterPrs:
    def test_no_filters(self):
        prs = [_make_pr(number=1), _make_pr(number=2)]
        result = _filter_prs(prs, _default_filters())
        assert len(result) == 2

    def test_filter_by_org(self):
        prs = [_make_pr(repo="alpha/repo1"), _make_pr(repo="beta/repo2", number=2)]
        result = _filter_prs(prs, _default_filters(org="alpha"))
        assert len(result) == 1
        assert result[0].repo == "alpha/repo1"

    def test_filter_by_ci_status(self):
        prs = [_make_pr(ci_status=CIStatus.SUCCESS), _make_pr(ci_status=CIStatus.FAILURE, number=2)]
        result = _filter_prs(prs, _default_filters(ci="failure"))
        assert len(result) == 1
        assert result[0].ci_status == CIStatus.FAILURE

    def test_filter_by_review_status(self):
        prs = [_make_pr(review_status=ReviewStatus.APPROVED), _make_pr(review_status=ReviewStatus.PENDING, number=2)]
        result = _filter_prs(prs, _default_filters(review="approved"))
        assert len(result) == 1

    def test_hide_drafts(self):
        prs = [_make_pr(is_draft=False), _make_pr(is_draft=True, number=2)]
        result = _filter_prs(prs, _default_filters(draft="hide"))
        assert len(result) == 1
        assert result[0].is_draft is False

    def test_only_drafts(self):
        prs = [_make_pr(is_draft=False), _make_pr(is_draft=True, number=2)]
        result = _filter_prs(prs, _default_filters(draft="only"))
        assert len(result) == 1
        assert result[0].is_draft is True

    def test_filter_fixing(self):
        prs = [_make_pr(number=1, repo="org/repo"), _make_pr(number=2, repo="org/repo")]
        attempts = {
            "org/repo#1": AutofixAttempt(
                pr_key="org/repo#1",
                fix_type=FixType.CI_FAILURE,
                status=AutofixStatus.IN_PROGRESS,
                started_at=datetime.now(timezone.utc),
            )
        }
        result = _filter_prs(prs, _default_filters(fixing="true"), autofix_attempts=attempts)
        assert len(result) == 1
        assert result[0].number == 1

    def test_search_by_title(self):
        prs = [_make_pr(title="Fix auth bug"), _make_pr(title="Add feature", number=2)]
        result = _filter_prs(prs, _default_filters(search="auth"))
        assert len(result) == 1
        assert result[0].title == "Fix auth bug"

    def test_search_by_repo(self):
        prs = [_make_pr(repo="org/api"), _make_pr(repo="org/web", number=2)]
        result = _filter_prs(prs, _default_filters(search="api"))
        assert len(result) == 1

    def test_search_by_branch(self):
        prs = [_make_pr(head_ref="feat/auth"), _make_pr(head_ref="fix/typo", number=2)]
        result = _filter_prs(prs, _default_filters(search="auth"))
        assert len(result) == 1

    def test_search_case_insensitive(self):
        prs = [_make_pr(title="Fix AUTH Bug")]
        result = _filter_prs(prs, _default_filters(search="auth"))
        assert len(result) == 1

    def test_combined_filters(self):
        prs = [
            _make_pr(number=1, repo="org/repo", ci_status=CIStatus.SUCCESS),
            _make_pr(number=2, repo="org/repo", ci_status=CIStatus.FAILURE),
            _make_pr(number=3, repo="other/repo", ci_status=CIStatus.FAILURE),
        ]
        result = _filter_prs(prs, _default_filters(org="org", ci="failure"))
        assert len(result) == 1
        assert result[0].number == 2


# --- _filter_reviews ---


class TestFilterReviews:
    def test_no_filters(self):
        reviews = [_make_review()]
        result = _filter_reviews(reviews, _default_filters())
        assert len(result) == 1

    def test_filter_by_org(self):
        reviews = [_make_review(repo="alpha/repo"), _make_review(repo="beta/repo", number=2)]
        result = _filter_reviews(reviews, _default_filters(org="alpha"))
        assert len(result) == 1

    def test_search_by_title(self):
        reviews = [_make_review(title="Fix bug"), _make_review(title="Add feature", number=2)]
        result = _filter_reviews(reviews, _default_filters(search="bug"))
        assert len(result) == 1

    def test_search_by_author(self):
        reviews = [_make_review(author="alice"), _make_review(author="bob", number=2)]
        result = _filter_reviews(reviews, _default_filters(search="alice"))
        assert len(result) == 1


# --- _filter_query_string ---


class TestFilterQueryString:
    def test_empty_filters(self):
        assert _filter_query_string({"org": None, "ci": None}) == ""

    def test_single_filter(self):
        assert _filter_query_string({"org": "vibe", "ci": None}) == "?org=vibe"

    def test_multiple_filters(self):
        qs = _filter_query_string({"org": "vibe", "ci": "failure"})
        assert "org=vibe" in qs
        assert "ci=failure" in qs
        assert qs.startswith("?")
