import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _timeago(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


templates.env.filters["timeago"] = _timeago

_REPO_TAG_COUNT = 24


def _repo_color(repo_name: str) -> int:
    """Deterministic color index for a repo name (stable across restarts)."""
    return int(hashlib.md5(repo_name.encode()).hexdigest(), 16) % _REPO_TAG_COUNT


templates.env.filters["repo_color"] = _repo_color


def _days_ago(dt: datetime) -> int:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((now - dt).total_seconds() / 86400)


templates.env.filters["days_ago"] = _days_ago


def _get_filters(request: Request) -> dict:
    return {
        "org": request.query_params.get("org") or None,
        "ci": request.query_params.get("ci") or None,
        "review": request.query_params.get("review") or None,
        "search": request.query_params.get("search") or None,
        "draft": request.query_params.get("draft") or None,
        "fixing": request.query_params.get("fixing") or None,
        "sort": request.query_params.get("sort") or "updated",
    }


def _sort_items(items: list, sort: str) -> list:
    if sort == "created":
        return sorted(items, key=lambda x: x.created_at, reverse=True)
    if sort == "confidence" and items and hasattr(items[0], "merge_confidence"):
        return sorted(items, key=lambda x: x.merge_confidence, reverse=True)
    return sorted(items, key=lambda x: x.updated_at, reverse=True)


def _filter_prs(prs: list, filters: dict, autofix_attempts: dict | None = None) -> list:
    result = prs
    if filters["org"]:
        result = [p for p in result if p.repo.split("/")[0] == filters["org"]]
    if filters["ci"]:
        result = [p for p in result if p.ci_status.value == filters["ci"]]
    if filters["review"]:
        result = [p for p in result if p.review_status.value == filters["review"]]
    if filters["draft"] == "hide":
        result = [p for p in result if not p.is_draft]
    elif filters["draft"] == "only":
        result = [p for p in result if p.is_draft]
    if filters["fixing"]:
        result = [
            p
            for p in result
            if p.pr_key in autofix_attempts and autofix_attempts[p.pr_key].status.value == "in_progress"
        ]
    if filters["search"]:
        q = filters["search"].lower()
        result = [p for p in result if q in p.title.lower() or q in p.repo.lower() or q in p.head_ref.lower()]
    return _sort_items(result, filters["sort"])


def _filter_reviews(reviews: list, filters: dict) -> list:
    result = reviews
    if filters["org"]:
        result = [r for r in result if r.repo.split("/")[0] == filters["org"]]
    if filters["search"]:
        q = filters["search"].lower()
        result = [r for r in result if q in r.title.lower() or q in r.repo.lower() or q in r.author.lower()]
    sorted_result = _sort_items(result, filters["sort"])
    # Team members first (stable sort preserves updated_at order within each group)
    return sorted(sorted_result, key=lambda r: (not r.is_team_member, r.has_other_approvals))


def _filter_query_string(filters: dict) -> str:
    parts = [f"{k}={v}" for k, v in filters.items() if v]
    return "?" + "&".join(parts) if parts else ""


def _get_state(request: Request):
    return request.app.state.poller.state


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    state = _get_state(request)
    filters = _get_filters(request)
    # Apply default org if no org filter set and no other filters active
    settings = request.app.state.settings
    if not filters["org"] and settings.default_org:
        filters["org"] = settings.default_org
    fixing_count = sum(1 for a in state.autofix_attempts.values() if a.status.value == "in_progress")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "state": state,
            "filters": filters,
            "filter_qs": _filter_query_string(filters),
            "prs": _filter_prs(state.my_prs, filters, state.autofix_attempts),
            "reviews": _filter_reviews(state.review_requests, filters),
            "autofix": state.autofix_attempts,
            "autofix_enabled": request.app.state.settings.autofix_enabled,
            "skipped_prs": request.app.state.autofix_manager.skipped,
            "theme": request.app.state.settings.theme,
            "fixing_count": fixing_count,
        },
    )


@router.get("/partials/my-prs", response_class=HTMLResponse)
async def my_prs_partial(request: Request):
    state = _get_state(request)
    filters = _get_filters(request)
    return templates.TemplateResponse(
        request,
        "partials/my_prs.html",
        {
            "prs": _filter_prs(state.my_prs, filters, state.autofix_attempts),
            "autofix": state.autofix_attempts,
            "autofix_enabled": request.app.state.settings.autofix_enabled,
            "skipped_prs": request.app.state.autofix_manager.skipped,
        },
    )


@router.get("/partials/reviews", response_class=HTMLResponse)
async def reviews_partial(request: Request):
    state = _get_state(request)
    filters = _get_filters(request)
    return templates.TemplateResponse(
        request, "partials/review_requests.html", {"reviews": _filter_reviews(state.review_requests, filters)}
    )


@router.get("/api/state")
async def api_state(request: Request):
    return _get_state(request)


@router.post("/api/pin")
async def pin_pr(request: Request):
    from control_center.github.pinned import (
        add_pinned,
        fetch_pr,
        fetch_review_request,
        parse_pr_url,
    )

    data = await request.json()
    url = data.get("url", "").strip()
    target = data.get("target", "pr")  # "pr" or "review"
    parsed = parse_pr_url(url)
    if not parsed:
        return JSONResponse(
            {"error": "Invalid PR URL. Expected: https://github.com/org/repo/pull/123"},
            status_code=400,
        )
    repo, number = parsed
    state = _get_state(request)
    settings = request.app.state.settings
    pr_key = f"{repo}#{number}"

    # Load any existing suggestion from disk
    from control_center.agent.review_store import load_latest

    existing_suggestion = await asyncio.to_thread(load_latest, repo, number)

    if target == "review":
        # Check if already in reviews
        if any(f"{r.repo}#{r.number}" == pr_key for r in state.review_requests):
            return {"status": "already_tracked", "pr_key": pr_key}
        rr = await asyncio.to_thread(fetch_review_request, repo, number, settings.github_username)
        if not rr:
            return JSONResponse({"error": f"Could not fetch {pr_key}"}, status_code=404)
        if existing_suggestion:
            rr.suggestion = existing_suggestion
        state.review_requests.append(rr)
        await asyncio.to_thread(add_pinned, repo, number, "review")
        state.log(f"Pinned {pr_key} as review: {rr.title}")
        return {"status": "pinned", "pr_key": pr_key, "title": rr.title}
    else:
        if any(p.pr_key == pr_key for p in state.my_prs):
            return {"status": "already_tracked", "pr_key": pr_key}
        pr = await asyncio.to_thread(fetch_pr, repo, number)
        if not pr:
            return JSONResponse({"error": f"Could not fetch {pr_key}"}, status_code=404)
        pr.is_pinned = True
        if existing_suggestion:
            pr.suggestion = existing_suggestion
        state.my_prs.append(pr)
        await asyncio.to_thread(add_pinned, repo, number, "pr")
        state.log(f"Pinned {pr_key} as PR: {pr.title}")
        return {"status": "pinned", "pr_key": pr_key, "title": pr.title}


@router.post("/api/unpin")
async def unpin_pr(request: Request):
    from control_center.github.pinned import remove_pinned

    data = await request.json()
    repo = data.get("repo", "")
    number = data.get("number", 0)
    state = _get_state(request)
    pr_key = f"{repo}#{number}"

    # Remove from both PR and review lists if pinned
    state.my_prs = [p for p in state.my_prs if not (p.pr_key == pr_key and p.is_pinned)]
    state.review_requests = [r for r in state.review_requests if not (f"{r.repo}#{r.number}" == pr_key and r.is_pinned)]
    await asyncio.to_thread(remove_pinned, repo, number)
    state.log(f"Unpinned {pr_key}")
    return {"status": "unpinned", "pr_key": pr_key}


@router.post("/api/poll")
async def trigger_poll(request: Request):
    async def poll_and_reconcile():
        poller = request.app.state.poller
        manager = request.app.state.autofix_manager
        await asyncio.to_thread(poller._poll_once)
        state = _get_state(request)
        await manager.reconcile_status(state.my_prs)

    asyncio.create_task(poll_and_reconcile())
    return {"status": "polling"}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "state": request.app.state.poller.state,
            "filters": {},
            "autofix_enabled": settings.autofix_enabled,
            "theme": settings.theme,
        },
    )


@router.post("/api/settings")
async def save_settings_api(request: Request):
    settings = request.app.state.settings
    data = await request.json()

    field_map = {
        "github_username": str,
        "default_org": str,
        "theme": str,
        "poll_interval_seconds": int,
        "autofix_enabled": bool,
        "autofix_max_budget_usd": float,
        "autofix_max_turns": int,
        "autofix_cooldown_minutes": int,
        "autofix_model": str,
        "repos_base_dir": str,
    }
    # List fields handled separately
    list_fields = {"github_teams"}

    for key, cast in field_map.items():
        if key in data:
            setattr(settings, key, cast(data[key]))
    for key in list_fields:
        if key in data:
            setattr(settings, key, list(data[key]))

    settings.save()
    return {"status": "saved"}


@router.post("/api/autofix/toggle")
async def toggle_autofix(request: Request):
    settings = request.app.state.settings
    settings.autofix_enabled = not settings.autofix_enabled
    settings.save()
    return {"autofix_enabled": settings.autofix_enabled}


@router.get("/partials/global-log", response_class=HTMLResponse)
async def global_log_partial(request: Request):
    state = _get_state(request)
    return templates.TemplateResponse(request, "partials/global_log.html", {"log": state.global_log[-100:]})


@router.get("/api/autofix/logs/{pr_key:path}")
async def get_autofix_logs(pr_key: str, request: Request):
    state = _get_state(request)
    attempt = state.autofix_attempts.get(pr_key)
    if not attempt:
        return {"log": [], "status": "idle"}
    return {"log": [e.model_dump() for e in attempt.log[-50:]], "status": attempt.status.value}


@router.get("/partials/autofix-log", response_class=HTMLResponse)
async def autofix_log_partial(request: Request):
    pr_key = request.query_params.get("key", "")
    state = _get_state(request)
    attempt = state.autofix_attempts.get(pr_key)
    log = attempt.log[-50:] if attempt else []
    return templates.TemplateResponse(
        request, "partials/autofix_log.html", {"log": log, "pr_key": pr_key, "attempt": attempt}
    )


def _get_review_jobs(request: Request) -> dict:
    """Get or init the review jobs dict on app state."""
    if not hasattr(request.app.state, "review_jobs"):
        request.app.state.review_jobs = {}
    return request.app.state.review_jobs


@router.post("/api/review/suggest")
async def suggest_review(request: Request):
    from control_center.agent.reviewer import generate_review_suggestion
    from control_center.models import AgentLogEntry

    data = await request.json()
    repo = data.get("repo", "")
    number = data.get("number", 0)
    state = _get_state(request)
    settings = request.app.state.settings
    pr_key = f"{repo}#{number}"
    jobs = _get_review_jobs(request)

    # Don't start duplicate jobs
    if pr_key in jobs and jobs[pr_key]["status"] == "running":
        return {"status": "running", "pr_key": pr_key}

    # Look in both review requests and own PRs
    rr = next((r for r in state.review_requests if r.repo == repo and r.number == number), None)
    if rr is None:
        rr = next((p for p in state.my_prs if p.repo == repo and p.number == number), None)
    if rr is None:
        return JSONResponse({"error": f"PR {repo}#{number} not found"}, status_code=404)

    # Init job tracking
    jobs[pr_key] = {"status": "running", "logs": [], "error": None}

    def _log(msg: str) -> None:
        entry = AgentLogEntry(
            timestamp=datetime.now(timezone.utc),
            pr_key=pr_key,
            message=msg,
        )
        state.global_log.append(entry)
        if len(state.global_log) > 600:
            state.global_log = state.global_log[-500:]
        # Also track in the job for polling
        if pr_key in jobs:
            jobs[pr_key]["logs"].append(msg)
            if len(jobs[pr_key]["logs"]) > 50:
                jobs[pr_key]["logs"] = jobs[pr_key]["logs"][-30:]

    async def _run():
        try:
            suggestion = await generate_review_suggestion(rr, settings, log_fn=_log)
            rr.suggestion = suggestion
            from control_center.agent.review_store import save_suggestion

            await asyncio.to_thread(save_suggestion, repo, number, suggestion)
            jobs[pr_key]["status"] = "done"
            jobs[pr_key]["suggestion"] = {
                "summary": suggestion.summary,
                "verdict": suggestion.verdict,
                "comments": [c.model_dump() for c in suggestion.comments],
                "generated_at": suggestion.generated_at.isoformat() if suggestion.generated_at else None,
            }
        except Exception as e:
            err_msg = str(e).strip() or f"{type(e).__name__} (no details)"
            import traceback

            tb = traceback.format_exception(e)
            tb_short = "".join(tb[-3:])[:500]
            _log(f"Review suggestion failed: {err_msg[:200]}")
            _log(f"Traceback: {tb_short}")
            jobs[pr_key]["status"] = "error"
            jobs[pr_key]["error"] = f"{type(e).__name__}: {err_msg[:500]}"

    asyncio.create_task(_run())
    return {"status": "started", "pr_key": pr_key}


@router.get("/api/review/status")
async def review_status(request: Request):
    repo = request.query_params.get("repo", "")
    number = int(request.query_params.get("number", 0))
    pr_key = f"{repo}#{number}"
    jobs = _get_review_jobs(request)
    job = jobs.get(pr_key)
    if not job:
        return {"status": "idle"}
    result = {
        "status": job["status"],
        "logs": job.get("logs", []),
        "error": job.get("error"),
    }
    if job["status"] == "done" and "suggestion" in job:
        result["suggestion"] = job["suggestion"]
    return result


@router.post("/api/review/delete")
async def delete_review_suggestion(request: Request):
    from control_center.agent.review_store import delete_suggestions

    data = await request.json()
    repo = data.get("repo", "")
    number = data.get("number", 0)
    state = _get_state(request)

    for item in list(state.review_requests) + list(state.my_prs):
        if item.repo == repo and item.number == number:
            item.suggestion = None

    await asyncio.to_thread(delete_suggestions, repo, number)
    return {"status": "deleted"}


@router.get("/api/review/history")
async def review_history(request: Request):
    from control_center.agent.review_store import load_history

    repo = request.query_params.get("repo", "")
    number = int(request.query_params.get("number", 0))
    history = await asyncio.to_thread(load_history, repo, number)
    return {
        "history": [
            {
                "summary": s.summary,
                "verdict": s.verdict,
                "comments": [c.model_dump() for c in s.comments],
                "generated_at": s.generated_at.isoformat() if s.generated_at else None,
            }
            for s in history
        ]
    }


@router.post("/api/autofix/{repo:path}/{pr_number}")
async def trigger_autofix(repo: str, pr_number: int, request: Request):
    state = _get_state(request)
    manager = request.app.state.autofix_manager

    pr = next((p for p in state.my_prs if p.repo == repo and p.number == pr_number), None)
    if pr is None:
        return JSONResponse({"error": f"PR {repo}#{pr_number} not found"}, status_code=404)

    attempt = await manager.trigger_fix(pr)
    return {"status": attempt.status.value, "pr_key": attempt.pr_key}


@router.post("/api/autofix/action")
async def autofix_action(request: Request):
    data = await request.json()
    action = data.get("action", "")
    pr_key = data.get("pr_key", "")
    manager = request.app.state.autofix_manager

    if action == "stop":
        manager.stop_fix(pr_key)
        return {"status": "stopped", "pr_key": pr_key}
    elif action == "skip":
        manager.skip_pr(pr_key)
        return {"status": "skipped", "pr_key": pr_key}
    elif action == "unskip":
        manager.unskip_pr(pr_key)
        return {"status": "unskipped", "pr_key": pr_key}
    return JSONResponse({"error": "unknown action"}, status_code=400)
