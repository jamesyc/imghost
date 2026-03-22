from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .app_state import AppState
from .config import load_settings
from .web.admin_api import router as admin_api_router
from .web.auth_context import clear_stale_session_cookie
from .web.auth import router as auth_router
from .web.csrf import enforce_session_csrf
from .web.health import router as health_router
from .web.media import router as media_router
from .web.pages import router as pages_router
from .web.public_api import router as public_api_router
from .web.request_context import assign_request_context
from .web.security_headers import add_security_headers
from .web.user_api import router as user_api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.imghost = AppState(settings)
    await app.state.imghost.start()
    yield
    await app.state.imghost.stop()


app = FastAPI(title="imghost V1", lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.middleware("http")(assign_request_context)
app.middleware("http")(add_security_headers)
app.middleware("http")(enforce_session_csrf)
app.middleware("http")(clear_stale_session_cookie)

app.include_router(pages_router)
app.include_router(auth_router)
app.include_router(public_api_router)
app.include_router(user_api_router)
app.include_router(admin_api_router)
app.include_router(media_router)
app.include_router(health_router)
