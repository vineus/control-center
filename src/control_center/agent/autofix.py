import json
import logging
import subprocess
from pathlib import Path

from control_center.config import Settings
from control_center.models import FixType, PRStatus

logger = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)


def prepare_worktree(repo: str, branch: str, settings: Settings) -> Path:
    base = Path(settings.repos_base_dir).expanduser()
    repo_dir = base / repo.replace("/", "_")
    worktrees_dir = base / "worktrees"
    worktree_dir = worktrees_dir / f"{repo.replace('/', '_')}_{branch}"

    # Ensure directories exist
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Clone repo if needed
    if not repo_dir.exists():
        logger.info("Cloning %s into %s", repo, repo_dir)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        result = _run(["gh", "repo", "clone", repo, str(repo_dir)], timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"Clone failed: {result.stderr}")

    # Prune stale worktrees (e.g. from previous crashed runs)
    _run(["git", "worktree", "prune"], cwd=str(repo_dir))

    # If worktree dir exists but is broken, remove it
    if worktree_dir.exists():
        check = _run(["git", "rev-parse", "--git-dir"], cwd=str(worktree_dir))
        if check.returncode != 0:
            logger.warning("Removing broken worktree at %s", worktree_dir)
            _run(["git", "worktree", "remove", str(worktree_dir), "--force"], cwd=str(repo_dir))
            import shutil

            if worktree_dir.exists():
                shutil.rmtree(worktree_dir)

    # Create worktree if it doesn't exist
    if not worktree_dir.exists():
        fetch = _run(["git", "fetch", "origin", branch], cwd=str(repo_dir), timeout=60)
        if fetch.returncode != 0:
            raise RuntimeError(f"Fetch failed: {fetch.stderr}")

        # Use detached HEAD to avoid "already checked out" errors
        result = _run(
            ["git", "worktree", "add", "--detach", str(worktree_dir), f"origin/{branch}"],
            cwd=str(repo_dir),
        )
        if result.returncode != 0:
            raise RuntimeError(f"Worktree creation failed: {result.stderr}")

    # Checkout the branch and pull latest
    _run(["git", "checkout", branch], cwd=str(worktree_dir))
    _run(["git", "pull", "--rebase", "origin", branch], cwd=str(worktree_dir), timeout=60)

    return worktree_dir


def cleanup_worktree(repo: str, worktree_path: Path, settings: Settings) -> None:
    base = Path(settings.repos_base_dir).expanduser()
    repo_dir = base / repo.replace("/", "_")
    if repo_dir.exists():
        _run(["git", "worktree", "remove", str(worktree_path), "--force"], cwd=str(repo_dir))
    import shutil

    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)


def cleanup_stale_worktrees(
    open_pr_branches: set[str],
    running_pr_keys: set[str],
    settings: Settings,
) -> list[str]:
    """Remove worktrees for PRs that are closed/merged or have passing CI.
    Returns list of cleaned up worktree paths."""
    base = Path(settings.repos_base_dir).expanduser()
    worktrees_dir = base / "worktrees"
    if not worktrees_dir.exists():
        return []

    cleaned = []
    for wt in worktrees_dir.iterdir():
        if not wt.is_dir():
            continue

        # Parse worktree name: {org}_{repo}_{branch}
        # The branch is everything after the second underscore-separated repo part
        # e.g. "vibe-ad_clear-bidder_vdl_fix-something" -> branch = "vdl/fix-something"
        # But we stored it as repo.replace("/","_") + "_" + branch
        # Easiest: check if any open PR branch matches
        wt_name = wt.name

        # Check if this worktree belongs to a currently running fix
        is_running = any(wt_name in key.replace("/", "_").replace("#", "_") for key in running_pr_keys)
        if is_running:
            continue

        # Check if the branch is still in the open PRs set
        is_open = any(branch in wt_name for branch in open_pr_branches)
        if is_open:
            continue

        # This worktree is for a PR that's no longer open — clean it up
        logger.info("Cleaning up stale worktree: %s", wt)
        # Find the parent repo dir to run git worktree remove
        # Try all repo dirs
        for repo_dir in base.iterdir():
            if repo_dir.is_dir() and repo_dir.name != "worktrees":
                result = _run(["git", "worktree", "remove", str(wt), "--force"], cwd=str(repo_dir))
                if result.returncode == 0:
                    break
        # Fallback: just delete the directory
        import shutil

        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        cleaned.append(str(wt))

    # Prune all repos
    for repo_dir in base.iterdir():
        if repo_dir.is_dir() and repo_dir.name != "worktrees":
            _run(["git", "worktree", "prune"], cwd=str(repo_dir))

    return cleaned


def get_ci_failure_logs(pr: PRStatus) -> str:
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
        if result.returncode != 0 or not result.stdout.strip():
            return "Could not fetch CI run list."

        runs = json.loads(result.stdout)
        if not runs:
            return "No failed CI runs found."

        run_id = str(runs[0]["databaseId"])
        log_result = _run(
            ["gh", "run", "view", run_id, "--repo", pr.repo, "--log-failed"],
            timeout=60,
        )
        if log_result.returncode != 0:
            return f"Could not fetch logs for run {run_id}: {log_result.stderr}"

        # Return last 8000 chars to stay within context limits
        return log_result.stdout[-8000:]
    except Exception as e:
        logger.exception("Failed to fetch CI logs for %s#%d", pr.repo, pr.number)
        return f"Error fetching CI logs: {e}"


def detect_fix_type(pr: PRStatus) -> FixType | None:
    if pr.mergeable == "CONFLICTING":
        return FixType.MERGE_CONFLICT
    if pr.ci_status.value == "failure" and not pr.is_draft:
        return FixType.CI_FAILURE
    if pr.is_draft:
        return FixType.DRAFT
    return None


def build_prompt(pr: PRStatus, fix_type: FixType, ci_logs: str = "") -> str:
    if fix_type == FixType.CI_FAILURE:
        return f"""You are fixing CI failures in {pr.repo} (#{pr.number}: {pr.title}).
Branch: {pr.head_ref}

Here are the failing CI logs (last 8000 chars):
```
{ci_logs}
```

Instructions:
1. Read the relevant code and understand what's failing
2. Make the minimal fix needed to pass CI
3. Run `make format` if a Makefile exists
4. Commit with a message like: fix(ci): <describe what you fixed>
5. Push with `git push`

Do NOT make unrelated changes. Focus only on making CI green."""

    if fix_type == FixType.MERGE_CONFLICT:
        return f"""PR #{pr.number} in {pr.repo} ("{pr.title}") has merge conflicts with {pr.base_ref}.
Branch: {pr.head_ref}

Instructions:
1. Run: git fetch origin {pr.base_ref}
2. Run: git rebase origin/{pr.base_ref}
3. Resolve any merge conflicts by reading both versions and choosing the correct resolution
4. Run `make format` if a Makefile exists
5. Run: git push --force-with-lease

Be careful with conflict resolution — understand the intent of both sides before resolving."""

    if fix_type == FixType.DRAFT:
        return f"""You are working on draft PR #{pr.number} in {pr.repo}: "{pr.title}".
Branch: {pr.head_ref}, target: {pr.base_ref}

Instructions:
1. Read the existing code changes on this branch (use git diff origin/{pr.base_ref}...HEAD)
2. Read any PR description for context (use: gh pr view {pr.number} --repo {pr.repo})
3. Continue the implementation — fix issues, add missing pieces
4. Run `make format` if a Makefile exists
5. Commit and push your changes

Do NOT mark the PR as ready — that is the user's decision.
Do NOT make changes unrelated to the PR's purpose."""

    raise ValueError(f"Unknown fix type: {fix_type}")
