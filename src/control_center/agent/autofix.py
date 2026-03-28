import json
import logging
import subprocess
from pathlib import Path

from control_center.config import Settings
from control_center.models import PRStatus

logger = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)


def prepare_worktree(repo: str, branch: str, settings: Settings) -> Path:
    base = Path(settings.repos_base_dir).expanduser()
    repo_dir = base / repo.replace("/", "_")

    if not repo_dir.exists():
        logger.info("Cloning %s into %s", repo, repo_dir)
        _run(["gh", "repo", "clone", repo, str(repo_dir)], timeout=120)

    worktree_dir = base / "worktrees" / f"{repo.replace('/', '_')}_{branch}"
    if not worktree_dir.exists():
        _run(["git", "fetch", "origin", branch], cwd=str(repo_dir))
        _run(["git", "worktree", "add", str(worktree_dir), f"origin/{branch}"], cwd=str(repo_dir))

    return worktree_dir


def get_failure_context(pr: PRStatus) -> str:
    parts = []

    failed_checks = [c for c in pr.checks if c.conclusion == "FAILURE"]
    if failed_checks:
        parts.append(f"## Failed CI checks: {', '.join(c.name for c in failed_checks)}")
        try:
            result = _run(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    pr.repo,
                    "--branch",
                    pr.head_ref,
                    "--status",
                    "failure",
                    "--limit",
                    "1",
                    "--json",
                    "databaseId",
                ],
                timeout=15,
            )
            if result.returncode == 0:
                runs = json.loads(result.stdout)
                if runs:
                    run_id = str(runs[0]["databaseId"])
                    log_result = _run(
                        ["gh", "run", "view", run_id, "--repo", pr.repo, "--log-failed"],
                        timeout=60,
                    )
                    if log_result.returncode == 0:
                        log_text = log_result.stdout[-5000:]  # last 5k chars
                        parts.append(f"## CI log (last 5000 chars):\n```\n{log_text}\n```")
        except Exception:
            logger.exception("Failed to fetch CI logs")

    change_reviews = [r for r in pr.reviews if r.state == "CHANGES_REQUESTED"]
    if change_reviews:
        parts.append("## Review comments requesting changes:")
        for r in change_reviews:
            parts.append(f"- **{r.author}**: {r.body}")

    return "\n\n".join(parts) if parts else "No specific failure context found."


async def attempt_autofix(pr: PRStatus, settings: Settings) -> str:
    """Attempt to auto-fix a PR using the Claude Agent SDK. Returns a status message."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError:
        return "claude-agent-sdk not installed. Install with: uv add claude-agent-sdk"

    worktree_path = prepare_worktree(pr.repo, pr.head_ref, settings)
    failure_context = get_failure_context(pr)

    prompt = f"""You are fixing a PR in {pr.repo} (#{pr.number}: {pr.title}).

The following issues need to be addressed:

{failure_context}

Instructions:
1. Read the relevant code and understand the failures
2. Make the minimal necessary fixes
3. Run any available tests to verify
4. Commit the fix with a clear message
5. Push the changes

Do NOT make unnecessary changes beyond what's needed to fix the issues.
"""

    logger.info("Starting auto-fix for %s#%d in %s", pr.repo, pr.number, worktree_path)

    result_text = ""
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            cwd=str(worktree_path),
            allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
            permission_mode="acceptEdits",
            max_turns=30,
        ),
    ):
        if hasattr(message, "result"):
            result_text = message.result

    return result_text or "Auto-fix completed"
