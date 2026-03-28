# Control Center

GitHub PR monitor dashboard with auto-fix agent capabilities.

## Stack
- Python 3.12+, FastAPI, Jinja2, HTMX, Tailwind CDN (no build step)
- `gh` CLI (subprocess) for GitHub GraphQL API — reuses local auth
- `uv` for dependency management

## Commands
- `make dev` — run with hot reload on :8000
- `make run` — production mode
- `make format` — ruff format + fix
- `make lint` — ruff check

## Gotchas
- Starlette TemplateResponse: use `templates.TemplateResponse(request, "name.html", context)` — request is a positional arg, NOT inside context dict
- `gh` CLI calls are blocking — wrap in `asyncio.to_thread()` for FastAPI
- GraphQL queries return archived repos — filter with `repository.isArchived`

## Structure
- `src/control_center/github/` — GraphQL client, queries, polling loop
- `src/control_center/web/` — FastAPI routes, Jinja2 templates (HTMX partials)
- `src/control_center/agent/` — Claude Agent SDK auto-fix (scaffolded, not yet active)
- Filters are query-param based, passed through to HTMX partial refreshes
