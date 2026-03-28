import asyncio
import logging
from datetime import datetime, timezone

from control_center.config import Settings
from control_center.github.client import gh_graphql
from control_center.github.queries import MY_PRS_QUERY, REVIEW_REQUESTS_QUERY
from control_center.models import (
    CheckRun,
    CIStatus,
    DashboardState,
    PRStatus,
    Review,
    ReviewRequest,
    ReviewStatus,
)

logger = logging.getLogger(__name__)


def _parse_ci_status(rollup_state: str | None) -> CIStatus:
    if rollup_state is None:
        return CIStatus.UNKNOWN
    mapping = {
        "SUCCESS": CIStatus.SUCCESS,
        "FAILURE": CIStatus.FAILURE,
        "ERROR": CIStatus.FAILURE,
        "PENDING": CIStatus.PENDING,
        "EXPECTED": CIStatus.PENDING,
    }
    return mapping.get(rollup_state, CIStatus.UNKNOWN)


def _parse_review_decision(decision: str | None) -> ReviewStatus:
    if decision is None:
        return ReviewStatus.PENDING
    mapping = {
        "APPROVED": ReviewStatus.APPROVED,
        "CHANGES_REQUESTED": ReviewStatus.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewStatus.PENDING,
    }
    return mapping.get(decision, ReviewStatus.PENDING)


def _parse_checks(commit_node: dict) -> list[CheckRun]:
    checks = []
    rollup = commit_node.get("commit", {}).get("statusCheckRollup")
    if not rollup:
        return checks
    for ctx in rollup.get("contexts", {}).get("nodes", []):
        if "name" in ctx:
            checks.append(CheckRun(name=ctx["name"], status=ctx.get("status", ""), conclusion=ctx.get("conclusion")))
        elif "context" in ctx:
            checks.append(CheckRun(name=ctx["context"], status=ctx.get("state", ""), conclusion=ctx.get("state")))
    return checks


def _parse_my_prs(data: dict) -> list[PRStatus]:
    prs = []
    for node in data.get("data", {}).get("viewer", {}).get("pullRequests", {}).get("nodes", []):
        commits = node.get("commits", {}).get("nodes", [])
        last_commit = commits[-1] if commits else {}
        rollup = last_commit.get("commit", {}).get("statusCheckRollup")
        rollup_state = rollup.get("state") if rollup else None

        reviews = [
            Review(
                author=r.get("author", {}).get("login", "unknown"),
                state=r.get("state", ""),
                body=r.get("body", ""),
            )
            for r in node.get("reviews", {}).get("nodes", [])
        ]

        prs.append(
            PRStatus(
                number=node["number"],
                title=node["title"],
                url=node["url"],
                repo=node["repository"]["nameWithOwner"],
                head_ref=node["headRefName"],
                author=node.get("author", {}).get("login", "unknown"),
                ci_status=_parse_ci_status(rollup_state),
                checks=_parse_checks(last_commit),
                review_status=_parse_review_decision(node.get("reviewDecision")),
                reviews=reviews,
                is_draft=node.get("isDraft", False),
                created_at=datetime.fromisoformat(node["createdAt"]),
                updated_at=datetime.fromisoformat(node["updatedAt"]),
            )
        )
    return prs


def _parse_review_requests(data: dict, username: str) -> list[ReviewRequest]:
    requests = []
    for node in data.get("data", {}).get("search", {}).get("nodes", []):
        if not node.get("number"):
            continue

        reviews = node.get("reviews", {}).get("nodes", [])
        has_other_approvals = any(
            r.get("state") == "APPROVED" and r.get("author", {}).get("login") != username for r in reviews
        )

        requests.append(
            ReviewRequest(
                number=node["number"],
                title=node["title"],
                url=node["url"],
                repo=node["repository"]["nameWithOwner"],
                author=node.get("author", {}).get("login", "unknown"),
                review_status=_parse_review_decision(node.get("reviewDecision")),
                has_other_approvals=has_other_approvals,
                created_at=datetime.fromisoformat(node["createdAt"]),
                updated_at=datetime.fromisoformat(node["updatedAt"]),
            )
        )
    return requests


class GitHubPoller:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = DashboardState()

    async def poll_loop(self):
        while True:
            try:
                await asyncio.to_thread(self._poll_once)
            except Exception:
                logger.exception("Poll cycle failed")
            await asyncio.sleep(self.settings.poll_interval_seconds)

    def _poll_once(self):
        logger.info("Polling GitHub for PR statuses...")
        try:
            my_prs_data = gh_graphql(MY_PRS_QUERY)
            self.state.my_prs = _parse_my_prs(my_prs_data)

            search_query = f"is:pr is:open review-requested:{self.settings.github_username}"
            review_data = gh_graphql(REVIEW_REQUESTS_QUERY, variables={"searchQuery": search_query})
            self.state.review_requests = _parse_review_requests(review_data, self.settings.github_username)

            self.state.last_poll = datetime.now(timezone.utc)
            self.state.poll_error = None
            logger.info(
                "Poll complete: %d open PRs, %d review requests",
                len(self.state.my_prs),
                len(self.state.review_requests),
            )
        except Exception as e:
            self.state.poll_error = str(e)
            self.state.last_poll = datetime.now(timezone.utc)
            raise
