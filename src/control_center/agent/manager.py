import asyncio
import logging
import os
import signal
import subprocess
from datetime import datetime, timedelta, timezone

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from control_center.agent.autofix import (
    build_prompt,
    cleanup_stale_worktrees,
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

        # Only auto-fix PRs in the selected org
        if self.settings.default_org:
            prs = [p for p in prs if p.repo.split("/")[0] == self.settings.default_org]

        for pr in prs:
            fix_type = self._should_fix(pr)
            if fix_type is None:
                continue
            task = asyncio.create_task(self._run_fix(pr, fix_type))
            self._tasks[pr.pr_key] = task

    async def reconcile_status(self, prs: list[PRStatus]) -> None:
        """Reconcile autofix attempts with current PR state.

        - IN_PROGRESS + PR no longer needs fixing → stop agent, mark SUCCEEDED
        - COMPLETED + PR no longer needs fixing → upgrade to SUCCEEDED (confirmed fix)
        """
        pr_map = {pr.pr_key: pr for pr in prs}
        for pr_key, attempt in list(self.state.autofix_attempts.items()):
            if attempt.status not in (AutofixStatus.IN_PROGRESS, AutofixStatus.COMPLETED):
                continue
            pr = pr_map.get(pr_key)
            if pr is not None and pr.needs_fix:
                continue

            # PR no longer needs fixing
            if attempt.status == AutofixStatus.IN_PROGRESS and pr_key in self._running:
                logger.info("Stopping agent for %s: PR no longer needs fixing", pr_key)
                self.stop_fix(pr_key)
                attempt = self.state.autofix_attempts.get(pr_key)
                if attempt:
                    attempt.status = AutofixStatus.SUCCEEDED
                    attempt.error = None
                    self.state.autofix_attempts[pr_key] = attempt
            else:
                attempt.status = AutofixStatus.SUCCEEDED
                if not attempt.finished_at:
                    attempt.finished_at = datetime.now(timezone.utc)
                self.state.autofix_attempts[pr_key] = attempt

            logger.info("Reconciled %s: PR no longer needs fixing", pr_key)

    async def cleanup_worktrees(self, prs: list[PRStatus]) -> None:
        """Remove worktrees for PRs that no longer need fixing (closed, merged, CI green)."""
        try:
            # Collect all open PR branches (keep their worktrees)
            open_branches = {pr.head_ref for pr in prs}

            # Collect worktree paths actively used by autofix
            active_paths = set()
            for attempt in self.state.autofix_attempts.values():
                if attempt.worktree_path and attempt.status == AutofixStatus.IN_PROGRESS:
                    active_paths.add(attempt.worktree_path)

            cleaned = await asyncio.to_thread(
                cleanup_stale_worktrees,
                active_paths,
                open_branches,
                self.settings,
            )
            if cleaned:
                logger.info("Cleaned up %d stale worktrees: %s", len(cleaned), cleaned)
        except Exception:
            logger.exception("Worktree cleanup failed")

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
            fix_type = FixType.DRAFT if pr.is_draft else FixType.CI_FAILURE

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

    def stop_fix(self, pr_key: str) -> None:
        # Update state immediately (non-blocking)
        attempt = self.state.autofix_attempts.get(pr_key)
        if attempt and attempt.status == AutofixStatus.IN_PROGRESS:
            attempt.status = AutofixStatus.FAILED
            attempt.error = "Stopped by user"
            attempt.finished_at = datetime.now(timezone.utc)
            self.state.autofix_attempts[pr_key] = attempt

        self._running.discard(pr_key)
        self.skipped.add(pr_key)

        # Kill claude subprocesses
        self._kill_claude_subprocesses()

        # Cancel the asyncio task (fire and forget)
        task = self._tasks.pop(pr_key, None)
        if task and not task.done():
            task.cancel()

        logger.info("Stopped fix for %s", pr_key)

    @staticmethod
    def _kill_claude_subprocesses() -> None:
        """Kill all claude_agent_sdk bundled subprocess spawned by this server."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "claude_agent_sdk/_bundled/claude"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return
            my_pid = os.getpid()
            for line in result.stdout.strip().split("\n"):
                pid_str = line.strip()
                if not pid_str:
                    continue
                pid = int(pid_str)
                if pid == my_pid:
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                    logger.info("Killed claude subprocess PID %d", pid)
                except ProcessLookupError:
                    pass
        except Exception:
            logger.exception("Failed to kill claude subprocesses")

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
            if len(attempt.log) > 250:
                attempt.log = attempt.log[-200:]
            if len(self.state.global_log) > 600:
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
            agent_error = False
            agent_result = ""
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
                            agent_error = getattr(message, "is_error", False)
                            agent_result = getattr(message, "result", "") or ""
            except TimeoutError:
                _log("Timed out after 10 minutes")
                raise RuntimeError("Auto-fix timed out after 10 minutes")

            attempt.cost_usd = cost
            if agent_error:
                attempt.status = AutofixStatus.FAILED
                attempt.error = agent_result[:500] or "Agent reported an error"
                _log(f"Failed (cost: ${cost:.3f}): {attempt.error}")
                logger.info("Auto-fix failed for %s (cost: $%.4f): %s", pr_key, cost, attempt.error)
            else:
                # Agent finished but we don't know if the fix worked yet —
                # reconcile_status() will upgrade to SUCCEEDED if PR no longer needs fixing
                attempt.status = AutofixStatus.COMPLETED
                _log(f"Completed (cost: ${cost:.3f})")
                logger.info("Auto-fix completed for %s (cost: $%.4f)", pr_key, cost)

        except asyncio.CancelledError:
            # stop_fix() already set status to FAILED — only update if still in progress
            if attempt.status == AutofixStatus.IN_PROGRESS:
                attempt.status = AutofixStatus.FAILED
                attempt.error = "Stopped by user"
            _log("Stopped by user")
            logger.info("Auto-fix cancelled for %s", pr_key)

        except Exception as e:
            attempt.status = AutofixStatus.FAILED
            attempt.error = str(e)[:500]
            _log(f"Failed: {str(e)[:200]}")
            logger.exception("Auto-fix failed for %s", pr_key)

        finally:
            attempt.finished_at = datetime.now(timezone.utc)
            self.state.autofix_attempts[pr_key] = attempt
            self._running.discard(pr_key)
            self._tasks.pop(pr_key, None)
