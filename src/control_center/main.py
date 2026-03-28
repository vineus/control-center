import asyncio
import logging
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
    settings = Settings()
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


def run():
    settings = Settings()
    uvicorn.run("control_center.main:app", host=settings.host, port=settings.port)
