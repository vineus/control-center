# Control Center

A dashboard that monitors your GitHub pull requests and automatically fixes CI failures, merge conflicts, and draft PRs using the Claude Agent SDK.

![Dashboard](https://img.shields.io/badge/stack-FastAPI%20%2B%20HTMX%20%2B%20Tailwind-blue)

## Features

- **PR monitoring** — polls GitHub for your open PRs and review requests
- **Auto-fix agent** — uses Claude to fix failing CI, resolve merge conflicts, and continue draft PRs
- **Live dashboard** — dark-mode UI with status cards, filters, search, and sorting
- **Agent logs** — per-PR and global log panels showing what the agent is doing
- **Per-PR controls** — start, stop, or skip auto-fix on individual PRs
- **Browser notifications** — alerts when fixes complete or PRs become ready to merge
- **Settings UI** — configure everything from the dashboard at `/settings`

## Quick start

### With uvx (no clone needed)

```bash
uvx --from git+https://github.com/vineus/control-center.git control-center
```

### From source

```bash
git clone https://github.com/vineus/control-center.git
cd control-center
uv sync
make dev
```

Open http://localhost:8000

## Prerequisites

- Python 3.12+
- [`gh` CLI](https://cli.github.com/) — authenticated (`gh auth login`)
- [`uv`](https://docs.astral.sh/uv/) — Python package manager
- [Claude Code CLI](https://claude.ai/code) — for the auto-fix agent (optional)

## Configuration

Config is stored at `~/.control-center/config.toml` and auto-created on first run. GitHub username is detected from `gh` CLI.

You can edit it from the UI at http://localhost:8000/settings or directly:

```toml
[github]
username = "your-username"
default_org = "your-org"

[server]
poll_interval_seconds = 180

[autofix]
enabled = false
max_budget_usd = 2.0
max_turns = 30
cooldown_minutes = 60
model = "sonnet"
```

Environment variables with `CC_` prefix override config file values (e.g. `CC_AUTOFIX_ENABLED=true`).

## How the auto-fix agent works

When enabled, the agent runs after each poll cycle:

1. Scans your open PRs for actionable issues
2. For each PR, creates an isolated git worktree
3. Invokes Claude via the Agent SDK with a tailored prompt:
   - **CI failure** — fetches logs, analyzes, fixes code, commits and pushes
   - **Merge conflict** — rebases on target branch, resolves conflicts, force-pushes
   - **Draft PR** — reads the PR description, continues implementation
4. Tracks status with per-PR logs visible in the dashboard

Safety: budget cap per fix, 10-minute timeout, 60-minute cooldown between retries, opt-in only.

## Development

```bash
make dev      # hot-reload server on :8000
make format   # ruff format + fix
make lint     # ruff check
```
