"""Persistent storage for review suggestions with history."""

import json
import logging
from datetime import datetime
from pathlib import Path

from control_center.models import ReviewComment, ReviewSuggestion

logger = logging.getLogger(__name__)

REVIEWS_DIR = Path.home() / ".control-center" / "reviews"


def _pr_path(repo: str, number: int) -> Path:
    """File path for a PR's review suggestions."""
    safe_repo = repo.replace("/", "_")
    return REVIEWS_DIR / f"{safe_repo}_{number}.json"


def _serialize_suggestion(s: ReviewSuggestion) -> dict:
    return {
        "summary": s.summary,
        "verdict": s.verdict,
        "comments": [c.model_dump() for c in s.comments],
        "generated_at": s.generated_at.isoformat() if s.generated_at else None,
    }


def _deserialize_suggestion(data: dict) -> ReviewSuggestion:
    comments = [ReviewComment(**c) for c in data.get("comments", [])]
    gen_at = data.get("generated_at")
    if gen_at and isinstance(gen_at, str):
        gen_at = datetime.fromisoformat(gen_at)
    return ReviewSuggestion(
        summary=data.get("summary", ""),
        verdict=data.get("verdict", "comment"),
        comments=comments,
        generated_at=gen_at,
    )


def save_suggestion(repo: str, number: int, suggestion: ReviewSuggestion) -> None:
    """Append a suggestion to the PR's history file."""
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = _pr_path(repo, number)

    history = _load_raw(path)
    history.append(_serialize_suggestion(suggestion))
    path.write_text(json.dumps(history, indent=2))
    logger.info("Saved review suggestion for %s#%d (%d total)", repo, number, len(history))


def load_latest(repo: str, number: int) -> ReviewSuggestion | None:
    """Load the most recent suggestion for a PR."""
    path = _pr_path(repo, number)
    history = _load_raw(path)
    if not history:
        return None
    return _deserialize_suggestion(history[-1])


def load_history(repo: str, number: int) -> list[ReviewSuggestion]:
    """Load all suggestions for a PR (oldest first)."""
    path = _pr_path(repo, number)
    history = _load_raw(path)
    return [_deserialize_suggestion(h) for h in history]


def delete_suggestions(repo: str, number: int) -> bool:
    """Delete all suggestions for a PR."""
    path = _pr_path(repo, number)
    if path.exists():
        path.unlink()
        logger.info("Deleted review suggestions for %s#%d", repo, number)
        return True
    return False


def load_all_latest() -> dict[str, ReviewSuggestion]:
    """Load the latest suggestion for every PR. Returns {repo#number: suggestion}."""
    result = {}
    if not REVIEWS_DIR.exists():
        return result
    for path in REVIEWS_DIR.glob("*.json"):
        try:
            history = _load_raw(path)
            if not history:
                continue
            # Parse repo and number from filename: org_repo_123.json
            stem = path.stem  # e.g. "vibe-ad_some-repo_77"
            parts = stem.rsplit("_", 1)
            if len(parts) != 2:
                continue
            number = int(parts[1])
            repo = parts[0].replace("_", "/", 1)  # first _ back to /
            key = f"{repo}#{number}"
            result[key] = _deserialize_suggestion(history[-1])
        except Exception:
            logger.warning("Failed to load review file %s", path, exc_info=True)
    return result


def _load_raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
        # Migrate from single-object format
        return [data]
    except (json.JSONDecodeError, OSError):
        return []
