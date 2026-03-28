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
        "sort": request.query_params.get("sort") or "updated",
    }


def _sort_items(items: list, sort: str) -> list:
    if sort == "created":
        return sorted(items, key=lambda x: x.created_at, reverse=True)
    if sort == "confidence" and items and hasattr(items[0], "merge_confidence"):
        return sorted(items, key=lambda x: x.merge_confidence, reverse=True)
    # default: updated
    return sorted(items, key=lambda x: x.updated_at, reverse=True)


def _filter_prs(prs: list, filters: dict) -> list:
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


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    state = request.app.state.poller.state
    filters = _get_filters(request)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "state": state,
            "filters": filters,
            "filter_qs": _filter_query_string(filters),
            "prs": _filter_prs(state.my_prs, filters),
            "reviews": _filter_reviews(state.review_requests, filters),
        },
    )


@router.get("/partials/my-prs", response_class=HTMLResponse)
async def my_prs_partial(request: Request):
    state = request.app.state.poller.state
    filters = _get_filters(request)
    return templates.TemplateResponse(request, "partials/my_prs.html", {"prs": _filter_prs(state.my_prs, filters)})


@router.get("/partials/reviews", response_class=HTMLResponse)
async def reviews_partial(request: Request):
    state = request.app.state.poller.state
    filters = _get_filters(request)
    return templates.TemplateResponse(
        request, "partials/review_requests.html", {"reviews": _filter_reviews(state.review_requests, filters)}
    )


@router.get("/api/state")
async def api_state(request: Request):
    return request.app.state.poller.state


@router.post("/api/poll")
async def trigger_poll(request: Request):
    import asyncio

    poller = request.app.state.poller
    asyncio.create_task(asyncio.to_thread(poller._poll_once))
    return {"status": "polling"}
