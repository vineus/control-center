import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from control_center.agent.manager import AutofixManager
from control_center.config import Settings
from control_center.github.poller import GitHubPoller
from control_center.models import DashboardState
from control_center.web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.load()
    state = DashboardState()
    poller = GitHubPoller(settings, state)
    autofix_manager = AutofixManager(settings, state)

    app.state.poller = poller
    app.state.settings = settings
    app.state.autofix_manager = autofix_manager

    # Run first poll immediately
    try:
        await asyncio.to_thread(poller._poll_once)
    except Exception:
        logger.exception("Initial poll failed — dashboard will show error")

    # Start background poll loop with auto-fix
    async def poll_and_fix():
        while True:
            try:
                await asyncio.to_thread(poller._poll_once)
                await autofix_manager.reconcile_status(state.my_prs)
                await autofix_manager.check_and_fix(state.my_prs)
                await autofix_manager.cleanup_worktrees(state.my_prs)
            except Exception:
                logger.exception("Poll/fix cycle failed")
            await asyncio.sleep(settings.poll_interval_seconds)

    task = asyncio.create_task(poll_and_fix())
    logger.info(
        "Control Center started — polling every %ds, autofix=%s",
        settings.poll_interval_seconds,
        settings.autofix_enabled,
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Control Center", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")


PID_FILE = Path.home() / ".control-center" / "daemon.pid"
LOG_FILE = Path.home() / ".control-center" / "daemon.log"


def _is_running(pid: int) -> bool:
    """Check if a process with the given PID is alive."""

    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _daemon_start(host: str, port: int):
    """Fork into background, redirect output to log file, write PID file."""
    import sys

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Check if already running
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if _is_running(old_pid):
                print(f"Already running (pid {old_pid}). Use 'control-center stop' first.")
                sys.exit(1)
        except (ValueError, FileNotFoundError):
            pass
        PID_FILE.unlink(missing_ok=True)

    # Double-fork to detach
    pid = os.fork()
    if pid > 0:
        # Parent — wait briefly then confirm child started
        import time

        time.sleep(0.5)
        if PID_FILE.exists():
            daemon_pid = int(PID_FILE.read_text().strip())
            print(f"Control Center started (pid {daemon_pid}) on http://{host}:{port}")
            print(f"  log: {LOG_FILE}")
            print("  stop: control-center stop")
        else:
            print("Started in background")
        sys.exit(0)

    # Child — new session
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        sys.exit(0)

    # Grandchild — the actual daemon
    PID_FILE.write_text(str(os.getpid()))

    # Redirect stdout/stderr to log file
    log_fd = open(LOG_FILE, "a")
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())
    sys.stdin.close()

    uvicorn.run("control_center.main:app", host=host, port=port, log_level="info")


def _daemon_stop():
    """Stop the daemon process."""
    import signal
    import sys

    if not PID_FILE.exists():
        print("Not running (no pid file)")
        sys.exit(1)

    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, FileNotFoundError):
        print("Invalid pid file")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    if not _is_running(pid):
        print(f"Process {pid} not running, cleaning up pid file")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    os.kill(pid, signal.SIGTERM)
    print(f"Stopped (pid {pid})")
    PID_FILE.unlink(missing_ok=True)


def _daemon_status():
    """Print daemon status."""
    if not PID_FILE.exists():
        print("Not running")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, FileNotFoundError):
        print("Not running (invalid pid file)")
        return

    if _is_running(pid):
        print(f"Running (pid {pid})")
        print(f"  log: {LOG_FILE}")
    else:
        print(f"Not running (stale pid {pid})")
        PID_FILE.unlink(missing_ok=True)


def run():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Control Center — GitHub PR monitor & auto-fix dashboard")
    parser.add_argument("-p", "--port", type=int, default=None, help="Port to listen on (default: from config or 8000)")
    parser.add_argument("--host", default=None, help="Host to bind to (default: from config or 0.0.0.0)")
    parser.add_argument("-d", "--daemon", action="store_true", help="Run as background daemon")
    parser.add_argument(
        "command", nargs="?", choices=["stop", "status", "logs"], help="Daemon control: stop, status, logs"
    )
    args = parser.parse_args()

    # Handle daemon control commands
    if args.command == "stop":
        _daemon_stop()
        return
    if args.command == "status":
        _daemon_status()
        return
    if args.command == "logs":
        if LOG_FILE.exists():
            os.execvp("tail", ["tail", "-f", str(LOG_FILE)])
        else:
            print(f"No log file at {LOG_FILE}")
            sys.exit(1)

    settings = Settings.load()
    host = args.host or settings.host
    port = args.port or settings.port

    if args.daemon:
        _daemon_start(host, port)
    else:
        uvicorn.run("control_center.main:app", host=host, port=port)
