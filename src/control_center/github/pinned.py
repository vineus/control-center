"""Pinned PRs — manually added by URL, persisted to disk."""

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from control_center.models import (
    CheckRun,
    CIStatus,
    PRStatus,
    Review,
    ReviewRequest,
    ReviewStatus,
)

logger = logging.getLogger(__name__)

PINNED_FILE = Path.home() / ".control-center" / "pinned.json"

# https://github.com/org/repo/pull/123
_PR_URL_RE = re.compile(r"https?://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)")


def parse_pr_url(url: str) -> tuple[str, int] | None:
    m = _PR_URL_RE.search(url.strip())
    if not m:
        return None
    return m.group("repo"), int(m.group("number"))


def load_pinned() -> dict[str, str]:
    """Load pinned PRs. Returns {key: target} where target is 'pr' or 'review'."""
    if not PINNED_FILE.exists():
        return {}
    try:
        data = json.loads(PINNED_FILE.read_text())
        # Migrate from old list format
        if isinstance(data, list):
            return {k: "pr" for k in data}
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_pinned(pinned: dict[str, str]) -> None:
    PINNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PINNED_FILE.write_text(json.dumps(pinned, indent=2))


def add_pinned(repo: str, number: int, target: str = "pr") -> None:
    pinned = load_pinned()
    pinned[f"{repo}#{number}"] = target
    save_pinned(pinned)
    logger.info("Pinned %s#%d as %s", repo, number, target)


def remove_pinned(repo: str, number: int) -> None:
    pinned = load_pinned()
    pinned.pop(f"{repo}#{number}", None)
    save_pinned(pinned)
    logger.info("Unpinned %s#%d", repo, number)


def fetch_pr(repo: str, number: int) -> PRStatus | None:
    """Fetch a single PR's data via gh CLI."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,url,isDraft,mergeable,headRefName,baseRefName,"
                "createdAt,updatedAt,author,reviewDecision,reviews,statusCheckRollup",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning("Failed to fetch %s#%d: %s", repo, number, result.stderr)
            return None
        data = json.loads(result.stdout)
    except Exception:
        logger.warning("Failed to fetch %s#%d", repo, number, exc_info=True)
        return None

    # Parse CI status
    rollup = data.get("statusCheckRollup", [])
    ci = CIStatus.UNKNOWN
    checks = []
    if rollup:
        # statusCheckRollup is a list of check/status objects
        has_fail = any(r.get("conclusion") == "FAILURE" or r.get("state") == "FAILURE" for r in rollup)
        has_pending = any(r.get("status") == "IN_PROGRESS" or r.get("state") == "PENDING" for r in rollup)
        all_pass = all(r.get("conclusion") == "SUCCESS" or r.get("state") == "SUCCESS" for r in rollup)
        if has_fail:
            ci = CIStatus.FAILURE
        elif has_pending:
            ci = CIStatus.PENDING
        elif all_pass and rollup:
            ci = CIStatus.SUCCESS
        for r in rollup:
            name = r.get("name") or r.get("context", "")
            if name:
                checks.append(
                    CheckRun(
                        name=name,
                        status=r.get("status", r.get("state", "")),
                        conclusion=r.get("conclusion", r.get("state")),
                    )
                )

    # Parse review decision
    decision = data.get("reviewDecision")
    review_map = {
        "APPROVED": ReviewStatus.APPROVED,
        "CHANGES_REQUESTED": ReviewStatus.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewStatus.PENDING,
    }
    review_status = review_map.get(decision, ReviewStatus.PENDING)

    reviews = []
    for r in data.get("reviews", []):
        reviews.append(
            Review(
                author=(r.get("author") or {}).get("login", "unknown"),
                state=r.get("state", ""),
                body=r.get("body", ""),
            )
        )

    return PRStatus(
        number=data["number"],
        title=data["title"],
        url=data["url"],
        repo=repo,
        head_ref=data.get("headRefName", ""),
        base_ref=data.get("baseRefName", "main"),
        author=(data.get("author") or {}).get("login", "unknown"),
        ci_status=ci,
        checks=checks,
        review_status=review_status,
        reviews=reviews,
        mergeable=data.get("mergeable", "UNKNOWN"),
        is_draft=data.get("isDraft", False),
        created_at=datetime.fromisoformat(data["createdAt"]),
        updated_at=datetime.fromisoformat(data["updatedAt"]),
    )


def fetch_review_request(repo: str, number: int, username: str = "") -> ReviewRequest | None:
    """Fetch a PR as a ReviewRequest via gh CLI."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,url,createdAt,updatedAt,author,reviewDecision,reviews",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning("Failed to fetch %s#%d: %s", repo, number, result.stderr)
            return None
        data = json.loads(result.stdout)
    except Exception:
        logger.warning("Failed to fetch %s#%d", repo, number, exc_info=True)
        return None

    decision = data.get("reviewDecision")
    review_map = {
        "APPROVED": ReviewStatus.APPROVED,
        "CHANGES_REQUESTED": ReviewStatus.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewStatus.PENDING,
    }
    has_other_approvals = any(
        r.get("state") == "APPROVED" and (r.get("author") or {}).get("login") != username
        for r in data.get("reviews", [])
    )

    return ReviewRequest(
        number=data["number"],
        title=data["title"],
        url=data["url"],
        repo=repo,
        author=(data.get("author") or {}).get("login", "unknown"),
        review_status=review_map.get(decision, ReviewStatus.PENDING),
        has_other_approvals=has_other_approvals,
        review_source="pinned",
        is_pinned=True,
        created_at=datetime.fromisoformat(data["createdAt"]),
        updated_at=datetime.fromisoformat(data["updatedAt"]),
    )
