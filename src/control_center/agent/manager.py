import asyncio
import logging
from datetime import datetime, timedelta, timezone

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from control_center.agent.autofix import (
    build_prompt,
    detect_fix_type,
    get_ci_failure_logs,
    prepare_worktree,
)
from control_center.config import Settings
from control_center.models import AutofixAttempt, AutofixStatus, DashboardState, FixType, PRStatus

logger = logging.getLogger(__name__)


class AutofixManager:
    def __init__(self, settings: Settings, state: DashboardState):
        self.settings = settings
        self.state = state
        self._running: set[str] = set()

    async def check_and_fix(self, prs: list[PRStatus]) -> None:
        if not self.settings.autofix_enabled:
            return

        for pr in prs:
            fix_type = self._should_fix(pr)
            if fix_type is None:
                continue
            asyncio.create_task(self._run_fix(pr, fix_type))

    def _should_fix(self, pr: PRStatus) -> FixType | None:
        fix_type = detect_fix_type(pr)
        if fix_type is None:
            return None

        pr_key = pr.pr_key
        if pr_key in self._running:
            return None

        attempt = self.state.autofix_attempts.get(pr_key)
        if attempt and attempt.status == AutofixStatus.IN_PROGRESS:
            return None

        # Cooldown: don't re-attempt within the configured window
        if attempt and attempt.finished_at:
            cooldown = timedelta(minutes=self.settings.autofix_cooldown_minutes)
            if datetime.now(timezone.utc) - attempt.finished_at < cooldown:
                return None

        return fix_type

    async def trigger_fix(self, pr: PRStatus, fix_type: FixType | None = None) -> AutofixAttempt:
        if fix_type is None:
            fix_type = detect_fix_type(pr)
        if fix_type is None:
            fix_type = FixType.CI_FAILURE  # default for manual triggers

        pr_key = pr.pr_key
        if pr_key in self._running:
            return self.state.autofix_attempts[pr_key]

        asyncio.create_task(self._run_fix(pr, fix_type))
        # Wait briefly to return the in-progress attempt
        await asyncio.sleep(0.1)
        return self.state.autofix_attempts.get(
            pr_key,
            AutofixAttempt(
                pr_key=pr_key,
                fix_type=fix_type,
                status=AutofixStatus.IN_PROGRESS,
                started_at=datetime.now(timezone.utc),
            ),
        )

    async def _run_fix(self, pr: PRStatus, fix_type: FixType) -> None:
        pr_key = pr.pr_key
        self._running.add(pr_key)

        attempt = AutofixAttempt(
            pr_key=pr_key,
            fix_type=fix_type,
            status=AutofixStatus.IN_PROGRESS,
            started_at=datetime.now(timezone.utc),
        )
        self.state.autofix_attempts[pr_key] = attempt

        try:
            # Prepare worktree (blocking I/O)
            worktree = await asyncio.to_thread(prepare_worktree, pr.repo, pr.head_ref, self.settings)
            attempt.worktree_path = str(worktree)
            logger.info("Auto-fixing %s (%s) in %s", pr_key, fix_type.value, worktree)

            # Get CI logs if needed
            ci_logs = ""
            if fix_type == FixType.CI_FAILURE:
                ci_logs = await asyncio.to_thread(get_ci_failure_logs, pr)

            # Build prompt
            prompt = build_prompt(pr, fix_type, ci_logs)

            # Run Claude Agent SDK
            cost = 0.0
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    cwd=str(worktree),
                    allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
                    permission_mode="bypassPermissions",
                    max_turns=self.settings.autofix_max_turns,
                    max_budget_usd=self.settings.autofix_max_budget_usd,
                    model=self.settings.autofix_model,
                ),
            ):
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0

            attempt.status = AutofixStatus.SUCCEEDED
            attempt.cost_usd = cost
            logger.info("Auto-fix succeeded for %s (cost: $%.4f)", pr_key, cost)

        except Exception as e:
            attempt.status = AutofixStatus.FAILED
            attempt.error = str(e)[:500]
            logger.exception("Auto-fix failed for %s", pr_key)

        finally:
            attempt.finished_at = datetime.now(timezone.utc)
            self.state.autofix_attempts[pr_key] = attempt
            self._running.discard(pr_key)
