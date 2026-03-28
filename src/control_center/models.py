from datetime import datetime
from enum import Enum

from pydantic import BaseModel, computed_field


class CIStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    UNKNOWN = "unknown"


class ReviewStatus(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    PENDING = "pending"
    COMMENTED = "commented"


class CheckRun(BaseModel):
    name: str
    status: str
    conclusion: str | None = None


class Review(BaseModel):
    author: str
    state: str
    body: str = ""


class PRStatus(BaseModel):
    number: int
    title: str
    url: str
    repo: str
    head_ref: str
    author: str
    ci_status: CIStatus
    checks: list[CheckRun] = []
    review_status: ReviewStatus
    reviews: list[Review] = []
    is_draft: bool = False
    created_at: datetime
    updated_at: datetime
    autofix_in_progress: bool = False

    @computed_field
    @property
    def merge_confidence(self) -> float:
        score = 0.0
        if self.ci_status == CIStatus.SUCCESS:
            score += 0.4
        if self.review_status == ReviewStatus.APPROVED:
            score += 0.4
        if self.review_status != ReviewStatus.CHANGES_REQUESTED:
            score += 0.2
        if self.is_draft:
            score *= 0.5
        return round(score, 2)

    @computed_field
    @property
    def ready_to_merge(self) -> bool:
        return self.ci_status == CIStatus.SUCCESS and self.review_status == ReviewStatus.APPROVED and not self.is_draft


class ReviewRequest(BaseModel):
    number: int
    title: str
    url: str
    repo: str
    author: str
    review_status: ReviewStatus
    has_other_approvals: bool = False
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def priority(self) -> str:
        age_hours = (datetime.now(self.created_at.tzinfo) - self.created_at).total_seconds() / 3600
        if age_hours > 48:
            return "high"
        elif age_hours > 24:
            return "medium"
        return "low"


class DashboardState(BaseModel):
    my_prs: list[PRStatus] = []
    review_requests: list[ReviewRequest] = []
    last_poll: datetime | None = None
    poll_error: str | None = None

    @computed_field
    @property
    def orgs(self) -> list[str]:
        repos = {pr.repo.split("/")[0] for pr in self.my_prs}
        repos.update(rr.repo.split("/")[0] for rr in self.review_requests)
        return sorted(repos)
