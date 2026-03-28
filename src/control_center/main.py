import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from control_center.config import Settings
from control_center.github.poller import GitHubPoller
from control_center.web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    poller = GitHubPoller(settings)
    app.state.poller = poller
    app.state.settings = settings

    # Run first poll immediately
    try:
        await asyncio.to_thread(poller._poll_once)
    except Exception:
        logger.exception("Initial poll failed — dashboard will show error")

    task = asyncio.create_task(poller.poll_loop())
    logger.info("Control Center started — polling every %ds", settings.poll_interval_seconds)
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
