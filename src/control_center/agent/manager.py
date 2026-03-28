import asyncio
import logging
from datetime import datetime, timedelta, timezone

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from control_center.agent.autofix import (
    build_prompt,
    detect_fix_type,
    get_ci_failure_logs,
    prepare_worktree,
)
from control_center.config import Settings
from control_center.models import AgentLogEntry, AutofixAttempt, AutofixStatus, DashboardState, FixType, PRStatus

logger = logging.getLogger(__name__)


class AutofixManager:
    def __init__(self, settings: Settings, state: DashboardState):
        self.settings = settings
        self.state = state
        self._running: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self.skipped: set[str] = set()  # PRs excluded from auto-fix

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
        if pr_key in self.skipped:
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
            fix_type = FixType.CI_FAILURE

        pr_key = pr.pr_key
        self.skipped.discard(pr_key)  # un-skip if manually triggered

        if pr_key in self._running:
            return self.state.autofix_attempts[pr_key]

        task = asyncio.create_task(self._run_fix(pr, fix_type))
        self._tasks[pr_key] = task
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

    async def stop_fix(self, pr_key: str) -> None:
        task = self._tasks.get(pr_key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._running.discard(pr_key)
        attempt = self.state.autofix_attempts.get(pr_key)
        if attempt and attempt.status == AutofixStatus.IN_PROGRESS:
            attempt.status = AutofixStatus.FAILED
            attempt.error = "Stopped by user"
            attempt.finished_at = datetime.now(timezone.utc)
            self.state.autofix_attempts[pr_key] = attempt

    def skip_pr(self, pr_key: str) -> None:
        self.skipped.add(pr_key)

    def unskip_pr(self, pr_key: str) -> None:
        self.skipped.discard(pr_key)

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

        def _log(msg: str) -> None:
            entry = AgentLogEntry(
                timestamp=datetime.now(timezone.utc),
                pr_key=pr_key,
                message=msg,
            )
            attempt.log.append(entry)
            self.state.global_log.append(entry)
            if len(attempt.log) > 200:
                attempt.log = attempt.log[-200:]
            if len(self.state.global_log) > 500:
                self.state.global_log = self.state.global_log[-500:]

        _log(f"Starting {fix_type.value} fix for {pr.repo}#{pr.number}")

        try:
            # Prepare worktree (blocking I/O)
            _log("Preparing worktree...")
            worktree = await asyncio.to_thread(prepare_worktree, pr.repo, pr.head_ref, self.settings)
            attempt.worktree_path = str(worktree)
            self.state.autofix_attempts[pr_key] = attempt
            _log(f"Worktree ready: {worktree}")
            logger.info("Auto-fixing %s (%s) in %s", pr_key, fix_type.value, worktree)

            # Get CI logs if needed
            ci_logs = ""
            if fix_type == FixType.CI_FAILURE:
                _log("Fetching CI failure logs...")
                ci_logs = await asyncio.to_thread(get_ci_failure_logs, pr)
                _log(f"Got {len(ci_logs)} chars of CI logs")

            # Build prompt
            prompt = build_prompt(pr, fix_type, ci_logs)
            _log("Invoking Claude agent...")

            # Run Claude Agent SDK with timeout
            cost = 0.0
            try:
                async with asyncio.timeout(600):  # 10 minute max
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
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock) and block.text.strip():
                                    _log(block.text[:300])
                                elif isinstance(block, ToolUseBlock):
                                    _log(f"[{block.name}] {str(block.input)[:200]}")
                        elif isinstance(message, ResultMessage):
                            cost = getattr(message, "total_cost_usd", 0.0) or 0.0
            except TimeoutError:
                _log("Timed out after 10 minutes")
                raise RuntimeError("Auto-fix timed out after 10 minutes")

            attempt.status = AutofixStatus.SUCCEEDED
            attempt.cost_usd = cost
            _log(f"Succeeded (cost: ${cost:.3f})")
            logger.info("Auto-fix succeeded for %s (cost: $%.4f)", pr_key, cost)

        except Exception as e:
            attempt.status = AutofixStatus.FAILED
            attempt.error = str(e)[:500]
            _log(f"Failed: {str(e)[:200]}")
            logger.exception("Auto-fix failed for %s", pr_key)

        finally:
            attempt.finished_at = datetime.now(timezone.utc)
            self.state.autofix_attempts[pr_key] = attempt
            self._running.discard(pr_key)
