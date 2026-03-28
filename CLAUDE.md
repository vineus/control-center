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
- `gh` CLI timeout: 60s (not 30s) — GraphQL search queries can be slow
- GraphQL queries return archived repos — filter with `repository.isArchived`
- Git worktree paths: replace `/` in branch names with `_` — branches like `vdl/feat-foo` create subdirectories otherwise
- Worktree creation: use `-b branch` (local tracking branch), NOT `--detach` + `git checkout` (fails with "already checked out")
- Claude Agent SDK: uses Claude Code CLI auth (OAuth), no API key needed. Package is `claude-agent-sdk` (not `claude-code-sdk`)
- Cards are `<div>` not `<a>` — so buttons inside cards work. Only the title is a link
- Client-side search (no page reload) — use `data-pr-*` attributes on cards, `autocomplete="off"` on search input
- Search input: NEVER use `window.location.href` for search — causes page reloads and history spam
- In-progress autofix can get stuck if SDK hangs — `reconcile_status()` force-stops tasks when PR no longer needs fixing
- Never auto-mark draft PRs as ready (`gh pr ready`) — that's the user's decision

## Structure
- `src/control_center/github/` — GraphQL client, queries, polling loop
- `src/control_center/web/` — FastAPI routes, Jinja2 templates (HTMX partials)
- `src/control_center/agent/` — Claude Agent SDK auto-fix (autofix.py: worktrees/prompts, manager.py: orchestration)
- Config lives at `~/.control-center/config.toml`, loaded via `Settings.load()` — NOT `Settings()`
- Settings page at `/settings` — changes are persisted to TOML file via `settings.save()`
- Filters are query-param based, passed through to HTMX partial refreshes
