import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
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
    return _sort_items(result, filters["sort"])


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


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request):
    settings = request.app.state.settings
    form = await request.form()

    settings.github_username = form.get("github_username", settings.github_username).strip()
    settings.default_org = form.get("default_org", settings.default_org).strip()
    settings.theme = form.get("theme", settings.theme).strip()
    settings.poll_interval_seconds = int(form.get("poll_interval_seconds", settings.poll_interval_seconds))
    settings.autofix_enabled = form.get("autofix_enabled") == "on"
    settings.autofix_max_budget_usd = float(form.get("autofix_max_budget_usd", settings.autofix_max_budget_usd))
    settings.autofix_max_turns = int(form.get("autofix_max_turns", settings.autofix_max_turns))
    settings.autofix_cooldown_minutes = int(form.get("autofix_cooldown_minutes", settings.autofix_cooldown_minutes))
    settings.autofix_model = form.get("autofix_model", settings.autofix_model).strip()
    settings.repos_base_dir = form.get("repos_base_dir", settings.repos_base_dir).strip()

    settings.save()

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "state": request.app.state.poller.state,
            "filters": {},
            "autofix_enabled": settings.autofix_enabled,
            "theme": settings.theme,
            "saved": True,
        },
    )


THEMES = ["dark", "light", "phosphor", "blueprint", "paper", "concrete", "amber"]


@router.post("/api/theme/set")
async def set_theme(request: Request):
    settings = request.app.state.settings
    theme = request.query_params.get("theme", "dark")
    if theme in THEMES:
        settings.theme = theme
        settings.save()
    return {"theme": settings.theme}


@router.post("/api/theme/toggle")
async def toggle_theme(request: Request):
    settings = request.app.state.settings
    idx = THEMES.index(settings.theme) if settings.theme in THEMES else 0
    settings.theme = THEMES[(idx + 1) % len(THEMES)]
    settings.save()
    return {"theme": settings.theme}


@router.post("/api/autofix/toggle")
async def toggle_autofix(request: Request):
    settings = request.app.state.settings
    settings.autofix_enabled = not settings.autofix_enabled
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


@router.post("/api/autofix/{repo:path}/{pr_number}")
async def trigger_autofix(repo: str, pr_number: int, request: Request):
    state = _get_state(request)
    manager = request.app.state.autofix_manager

    pr = next((p for p in state.my_prs if p.repo == repo and p.number == pr_number), None)
    if pr is None:
        return {"error": f"PR {repo}#{pr_number} not found"}

    attempt = await manager.trigger_fix(pr)
    return {"status": attempt.status.value, "pr_key": attempt.pr_key}


@router.post("/api/autofix/stop/{repo:path}/{pr_number}")
async def stop_autofix(repo: str, pr_number: int, request: Request):
    manager = request.app.state.autofix_manager
    pr_key = f"{repo}#{pr_number}"
    await manager.stop_fix(pr_key)
    return {"status": "stopped", "pr_key": pr_key}


@router.post("/api/autofix/skip/{repo:path}/{pr_number}")
async def skip_autofix(repo: str, pr_number: int, request: Request):
    manager = request.app.state.autofix_manager
    pr_key = f"{repo}#{pr_number}"
    manager.skip_pr(pr_key)
    return {"status": "skipped", "pr_key": pr_key}


@router.post("/api/autofix/unskip/{repo:path}/{pr_number}")
async def unskip_autofix(repo: str, pr_number: int, request: Request):
    manager = request.app.state.autofix_manager
    pr_key = f"{repo}#{pr_number}"
    manager.unskip_pr(pr_key)
    return {"status": "unskipped", "pr_key": pr_key}
