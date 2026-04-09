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
- Agent SDK completion ≠ fix succeeded — `ResultMessage` returns when turns/budget exhausted. Check `is_error` field. Status flow: IN_PROGRESS → COMPLETED (agent finished) → SUCCEEDED (reconciliation confirms PR fixed)
- Autofix only triggers on concrete issues (CI failure, merge conflicts) — never on draft status alone. Manual "continue work" was removed; drafts get fixed only when they have CI/conflict issues
- Worktree cleanup: always normalize branch names with `.replace("/", "_")` before comparing against worktree dir names — they use different separators
- Never auto-mark draft PRs as ready (`gh pr ready`) — that's the user's decision
- FastAPI `{repo:path}` routes are greedy — `/api/autofix/stop/{repo:path}/{pr_number}` captures `stop/` as part of repo. Use JSON body endpoints for actions instead
- Stopping the Claude Agent SDK: `task.cancel()` does NOT kill the subprocess. Must `pgrep -f claude_agent_sdk/_bundled/claude` + `SIGTERM` to actually stop it
- Stop endpoint must be synchronous (non-blocking) — don't `await task`, just cancel + kill + update state immediately
- CSS themes: all colors must be CSS variables (`var(--bg)` etc.), never hardcoded hex. `:root` block must NOT come after `[data-theme]` blocks (overrides them by cascade)
- Static CSS caching: use cache-busting query param on `style.css` link, or browsers show stale themes
- Settings auto-save via `POST /api/settings` with JSON body — no form submission needed
- After triggering autofix, reload page after 2s so the PR card re-renders with log section
- CSS: inner `<span>` elements need `display: block` to respect `width`/`height` — common issue with progress bars and fills
- Templates use `{% block filter_bar %}` and `{% block body_class %}` in base.html for per-page customization (e.g., settings hides filter bar)
- Keyboard shortcuts defined in base.html: `/` search, `r` refresh, `t` cycle theme, `g+h` dashboard, `g+s` settings, `?` help overlay
- Daemon mode: `start` (or `-d`), `stop`, `restart`, `status`, `logs` — PID file at `~/.control-center/daemon.pid`, logs at `daemon.log`
- Daemon double-fork: flush stdout/stderr before `os.fork()` — unflushed buffers get duplicated to both parent and child processes

## Structure
- `src/control_center/github/` — GraphQL client, queries, polling loop
- `src/control_center/web/` — FastAPI routes, Jinja2 templates (HTMX partials)
- `src/control_center/agent/` — Claude Agent SDK auto-fix (autofix.py: worktrees/prompts, manager.py: orchestration)
- Config lives at `~/.control-center/config.toml`, loaded via `Settings.load()` — NOT `Settings()`
- Settings page at `/settings` — changes are persisted to TOML file via `settings.save()`
- Filters are query-param based, passed through to HTMX partial refreshes
