import pytest
from datetime import datetime, timedelta, timezone

from control_center.agent.autofix import detect_fix_type
from control_center.agent.manager import AutofixManager
from control_center.config import Settings
from control_center.models import (
    AutofixAttempt,
    AutofixStatus,
    CIStatus,
    DashboardState,
    FixType,
    PRStatus,
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


def _make_settings(**overrides) -> Settings:
    defaults = {
        "github_username": "testuser",
        "autofix_enabled": True,
        "autofix_cooldown_minutes": 60,
    }
    defaults.update(overrides)
    return Settings(**defaults)


# --- _should_fix ---


class TestShouldFix:
    def test_returns_ci_failure(self):
        state = DashboardState()
        manager = AutofixManager(_make_settings(), state)
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        assert manager._should_fix(pr) == FixType.CI_FAILURE

    def test_returns_merge_conflict(self):
        state = DashboardState()
        manager = AutofixManager(_make_settings(), state)
        pr = _make_pr(mergeable="CONFLICTING")
        assert manager._should_fix(pr) == FixType.MERGE_CONFLICT

    def test_returns_none_for_healthy_pr(self):
        state = DashboardState()
        manager = AutofixManager(_make_settings(), state)
        pr = _make_pr(ci_status=CIStatus.SUCCESS, mergeable="MERGEABLE")
        assert manager._should_fix(pr) is None

    def test_skips_running_pr(self):
        state = DashboardState()
        manager = AutofixManager(_make_settings(), state)
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        manager._running.add(pr.pr_key)
        assert manager._should_fix(pr) is None

    def test_skips_skipped_pr(self):
        state = DashboardState()
        manager = AutofixManager(_make_settings(), state)
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        manager.skipped.add(pr.pr_key)
        assert manager._should_fix(pr) is None

    def test_skips_in_progress_attempt(self):
        state = DashboardState()
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        state.autofix_attempts[pr.pr_key] = AutofixAttempt(
            pr_key=pr.pr_key,
            fix_type=FixType.CI_FAILURE,
            status=AutofixStatus.IN_PROGRESS,
            started_at=datetime.now(timezone.utc),
        )
        manager = AutofixManager(_make_settings(), state)
        assert manager._should_fix(pr) is None

    def test_skips_during_cooldown(self):
        state = DashboardState()
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        state.autofix_attempts[pr.pr_key] = AutofixAttempt(
            pr_key=pr.pr_key,
            fix_type=FixType.CI_FAILURE,
            status=AutofixStatus.FAILED,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            finished_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        manager = AutofixManager(_make_settings(autofix_cooldown_minutes=60), state)
        assert manager._should_fix(pr) is None

    def test_allows_after_cooldown(self):
        state = DashboardState()
        pr = _make_pr(ci_status=CIStatus.FAILURE)
        state.autofix_attempts[pr.pr_key] = AutofixAttempt(
            pr_key=pr.pr_key,
            fix_type=FixType.CI_FAILURE,
            status=AutofixStatus.FAILED,
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            finished_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        manager = AutofixManager(_make_settings(autofix_cooldown_minutes=60), state)
        assert manager._should_fix(pr) == FixType.CI_FAILURE


# --- skip / unskip ---


class TestSkipUnskip:
    def test_skip(self):
        manager = AutofixManager(_make_settings(), DashboardState())
        manager.skip_pr("org/repo#1")
        assert "org/repo#1" in manager.skipped

    def test_unskip(self):
        manager = AutofixManager(_make_settings(), DashboardState())
        manager.skip_pr("org/repo#1")
        manager.unskip_pr("org/repo#1")
        assert "org/repo#1" not in manager.skipped

    def test_unskip_nonexistent_is_safe(self):
        manager = AutofixManager(_make_settings(), DashboardState())
        manager.unskip_pr("org/repo#999")  # should not raise


# --- reconcile_status ---


@pytest.mark.asyncio
async def test_reconcile_completed_to_succeeded():
    """When a COMPLETED attempt's PR no longer needs fixing, upgrade to SUCCEEDED."""
    state = DashboardState()
    pr = _make_pr(ci_status=CIStatus.SUCCESS, mergeable="MERGEABLE")  # no longer needs fix
    state.autofix_attempts[pr.pr_key] = AutofixAttempt(
        pr_key=pr.pr_key,
        fix_type=FixType.CI_FAILURE,
        status=AutofixStatus.COMPLETED,
        started_at=datetime.now(timezone.utc),
    )
    manager = AutofixManager(_make_settings(), state)
    await manager.reconcile_status([pr])
    assert state.autofix_attempts[pr.pr_key].status == AutofixStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconcile_keeps_status_if_still_broken():
    """If PR still needs fixing, don't change status."""
    state = DashboardState()
    pr = _make_pr(ci_status=CIStatus.FAILURE)
    state.autofix_attempts[pr.pr_key] = AutofixAttempt(
        pr_key=pr.pr_key,
        fix_type=FixType.CI_FAILURE,
        status=AutofixStatus.COMPLETED,
        started_at=datetime.now(timezone.utc),
    )
    manager = AutofixManager(_make_settings(), state)
    await manager.reconcile_status([pr])
    assert state.autofix_attempts[pr.pr_key].status == AutofixStatus.COMPLETED


@pytest.mark.asyncio
async def test_reconcile_ignores_failed():
    """FAILED attempts should not be reconciled."""
    state = DashboardState()
    pr = _make_pr(ci_status=CIStatus.SUCCESS)
    state.autofix_attempts[pr.pr_key] = AutofixAttempt(
        pr_key=pr.pr_key,
        fix_type=FixType.CI_FAILURE,
        status=AutofixStatus.FAILED,
        started_at=datetime.now(timezone.utc),
    )
    manager = AutofixManager(_make_settings(), state)
    await manager.reconcile_status([pr])
    assert state.autofix_attempts[pr.pr_key].status == AutofixStatus.FAILED


# --- stop_fix ---


class TestStopFix:
    def test_stop_marks_failed_and_skipped(self):
        state = DashboardState()
        pr_key = "org/repo#1"
        state.autofix_attempts[pr_key] = AutofixAttempt(
            pr_key=pr_key,
            fix_type=FixType.CI_FAILURE,
            status=AutofixStatus.IN_PROGRESS,
            started_at=datetime.now(timezone.utc),
        )
        manager = AutofixManager(_make_settings(), state)
        manager._running.add(pr_key)
        manager.stop_fix(pr_key)

        assert state.autofix_attempts[pr_key].status == AutofixStatus.FAILED
        assert state.autofix_attempts[pr_key].error == "Stopped by user"
        assert pr_key in manager.skipped
        assert pr_key not in manager._running

    def test_stop_nonexistent_is_safe(self):
        manager = AutofixManager(_make_settings(), DashboardState())
        manager.stop_fix("org/repo#999")  # should not raise
