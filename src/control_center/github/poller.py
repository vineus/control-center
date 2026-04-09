import logging
import subprocess
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
        if node.get("repository", {}).get("isArchived", False):
            continue
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
                author=(node.get("author") or {}).get("login", "unknown"),
                ci_status=_parse_ci_status(rollup_state),
                checks=_parse_checks(last_commit),
                review_status=_parse_review_decision(node.get("reviewDecision")),
                reviews=reviews,
                mergeable=node.get("mergeable", "UNKNOWN"),
                is_draft=node.get("isDraft", False),
                base_ref=node.get("baseRefName", "staging"),
                created_at=datetime.fromisoformat(node["createdAt"]),
                updated_at=datetime.fromisoformat(node["updatedAt"]),
            )
        )
    return prs


def _parse_review_requests(data: dict, username: str, review_source: str = "personal") -> list[ReviewRequest]:
    requests = []
    for node in data.get("data", {}).get("search", {}).get("nodes", []):
        if not node.get("number"):
            continue
        if node.get("repository", {}).get("isArchived", False):
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
                review_source=review_source,
                created_at=datetime.fromisoformat(node["createdAt"]),
                updated_at=datetime.fromisoformat(node["updatedAt"]),
            )
        )
    return requests


def _fetch_team_members(teams: list[str]) -> set[str]:
    """Fetch all members of the configured teams via gh CLI."""
    members = set()
    for team_slug in teams:
        # team_slug is "org/team-name"
        parts = team_slug.split("/", 1)
        if len(parts) != 2:
            continue
        org, team = parts
        try:
            result = subprocess.run(
                ["gh", "api", f"orgs/{org}/teams/{team}/members", "--jq", ".[].login"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    login = line.strip()
                    if login:
                        members.add(login)
        except Exception:
            logger.warning("Failed to fetch members for team %s", team_slug)
    return members


class GitHubPoller:
    def __init__(self, settings: Settings, state: DashboardState):
        self.settings = settings
        self.state = state

    def _poll_once(self):
        logger.info("Polling GitHub for PR statuses...")
        self.state.log("Polling GitHub...")
        try:
            my_prs_data = gh_graphql(MY_PRS_QUERY)
            my_prs = _parse_my_prs(my_prs_data)

            # Merge pinned PRs and review requests
            from control_center.github.pinned import (
                fetch_pr,
                fetch_review_request,
                load_pinned,
            )

            pinned = load_pinned()
            existing_pr_keys = {f"{p.repo}#{p.number}" for p in my_prs}
            for key, target in pinned.items():
                parts = key.rsplit("#", 1)
                if len(parts) != 2:
                    continue
                repo, number = parts[0], int(parts[1])
                if target == "review":
                    # Handled below after all_reviews is built
                    continue
                if key in existing_pr_keys:
                    for p in my_prs:
                        if p.pr_key == key:
                            p.is_pinned = True
                    continue
                pr = fetch_pr(repo, number)
                if pr:
                    pr.is_pinned = True
                    my_prs.append(pr)

            self.state.my_prs = my_prs

            # Personal review requests
            search_query = f"is:pr is:open review-requested:{self.settings.github_username}"
            review_data = gh_graphql(REVIEW_REQUESTS_QUERY, variables={"searchQuery": search_query})
            all_reviews = _parse_review_requests(review_data, self.settings.github_username, "personal")

            # Team review requests
            seen_keys = {f"{rr.repo}#{rr.number}" for rr in all_reviews}
            for team in self.settings.github_teams:
                team_query = f"is:pr is:open team-review-requested:{team}"
                team_data = gh_graphql(REVIEW_REQUESTS_QUERY, variables={"searchQuery": team_query})
                team_reviews = _parse_review_requests(team_data, self.settings.github_username, f"team:{team}")
                for rr in team_reviews:
                    key = f"{rr.repo}#{rr.number}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_reviews.append(rr)

            # Fetch team members and tag review requests
            if self.settings.github_teams:
                self.state.team_members = _fetch_team_members(self.settings.github_teams)
            for rr in all_reviews:
                if rr.author in self.state.team_members:
                    rr.is_team_member = True

            # Restore suggestions from disk (survives restarts)
            from control_center.agent.review_store import load_all_latest

            stored = load_all_latest()
            for rr in all_reviews:
                key = f"{rr.repo}#{rr.number}"
                if key in stored:
                    rr.suggestion = stored[key]
            for pr in self.state.my_prs:
                if pr.pr_key in stored:
                    pr.suggestion = stored[pr.pr_key]

            # Merge pinned review requests
            existing_rr_keys = {f"{rr.repo}#{rr.number}" for rr in all_reviews}
            for key, target in pinned.items():
                if target != "review":
                    continue
                if key in existing_rr_keys:
                    for rr in all_reviews:
                        if f"{rr.repo}#{rr.number}" == key:
                            rr.is_pinned = True
                    continue
                parts = key.rsplit("#", 1)
                if len(parts) != 2:
                    continue
                repo, number = parts[0], int(parts[1])
                rr = fetch_review_request(repo, number, self.settings.github_username)
                if rr:
                    if rr.author in self.state.team_members:
                        rr.is_team_member = True
                    rr_key = f"{rr.repo}#{rr.number}"
                    if rr_key in stored:
                        rr.suggestion = stored[rr_key]
                    all_reviews.append(rr)

            self.state.review_requests = all_reviews

            self.state.last_poll = datetime.now(timezone.utc)
            self.state.poll_error = None
            msg = f"Poll complete: {len(self.state.my_prs)} PRs, {len(self.state.review_requests)} reviews"
            logger.info(msg)
            self.state.log(msg)
        except Exception as e:
            self.state.poll_error = str(e)
            self.state.last_poll = datetime.now(timezone.utc)
            self.state.log(f"Poll error: {str(e)[:200]}")
            raise
