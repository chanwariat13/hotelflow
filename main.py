from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import logging, os

from config.settings import HOST, PORT, SECRET_KEY
from services.database import (
    get_pool, close_pool,
    ensure_admin_seed, ensure_schema_v2,
    purge_pristine_seed_hotel,
)
from services.cache import get_redis, close_redis
from scheduler.setup import start_scheduler, stop_scheduler
from routes.bot import router as bot_router
from routes.guest_pages import router as guest_router
from routes.auth_routes import router as auth_router
from routes.admin_routes import router as admin_router
from routes.hotel_routes import router as hotel_router
from routes.formc_routes import router as formc_router
from routes.channel_routes import get_routers as channel_routers
from routes.reports_routes import get_routers as reports_routers

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Refuse to start with the placeholder SECRET_KEY ──────────────────────────
# SECRET_KEY is reserved for session/cookie/JWT signing. Booting with the
# placeholder "changeme" (or with no value) would silently weaken any future
# crypto that picks it up — so we fail fast here, before the app starts
# accepting traffic. Set a strong unique value via the env var.
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
if not SECRET_KEY or SECRET_KEY == "changeme":
    logger.critical(
        "SECRET_KEY is not set (or still the default 'changeme'). "
        "Refusing to start — set a strong, unique SECRET_KEY env var."
    )
    raise SystemExit(
        "SECRET_KEY must be set to a strong, unique value before starting "
        "HotelFlow. Generate one with: "
        "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

# ── Refuse to start without a WhatsApp webhook key ───────────────────────────
# routes/bot.py historically logged-and-allowed when WEBHOOK_API_KEY was
# unset. Anyone who learnt the public webhook URL could then POST a forged
# `messages.upsert` payload from a phone matching a staff `whatsapp_number`
# and drive APPROVE / REJECT / CASH RECEIVED / FREE / CHECKOUT / BLOCK from
# the outside. We now require a key at boot, matching the SECRET_KEY guard
# above. Operators who haven't yet configured Evolution to send the
# matching `apikey` header can opt OUT explicitly by setting
# `WEBHOOK_AUTH_OPTOUT=1` (intended ONLY for short migrations; the bot
# still logs a WARNING on every accepted hit).
_WEBHOOK_KEY_SETTING = (
    os.getenv("WEBHOOK_API_KEY") or os.getenv("EVOLUTION_API_KEY") or ""
).strip()
_WEBHOOK_OPTOUT = (os.getenv("WEBHOOK_AUTH_OPTOUT") or "").strip() in {"1", "true", "yes"}
if not _WEBHOOK_KEY_SETTING and not _WEBHOOK_OPTOUT:
    logger.critical(
        "WEBHOOK_API_KEY (or EVOLUTION_API_KEY) is not set. The WhatsApp "
        "inbound webhook would otherwise accept forged messages.upsert "
        "payloads from anyone. Refusing to start. Configure Evolution API "
        "to send a shared `apikey` header, set WEBHOOK_API_KEY to the "
        "matching value, and restart. For a short migration window only, "
        "set WEBHOOK_AUTH_OPTOUT=1."
    )
    raise SystemExit(
        "WEBHOOK_API_KEY (or EVOLUTION_API_KEY) must be set, or "
        "WEBHOOK_AUTH_OPTOUT=1 for a short migration window, before "
        "starting HotelFlow."
    )

FRONTEND = os.path.join(os.path.dirname(__file__), "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🏨 HotelFlow v2 starting...")
    await get_pool()
    await get_redis()
    await ensure_schema_v2()
    await ensure_admin_seed()
    # One-shot cleanup of the legacy "Grand Stay Hotel" seed row that the
    # old migration.sql Section 7 used to insert on every fresh deploy.
    # No-op if the row is gone or has been customised.
    await purge_pristine_seed_hotel()
    await start_scheduler()
    logger.info("✅ HotelFlow is ready!")
    yield
    stop_scheduler()
    await close_pool()
    await close_redis()
    logger.info("HotelFlow shutdown complete.")


app = FastAPI(title="HotelFlow v2", version="2.0.0", lifespan=lifespan)

# CORS: lock down to a configured allow-list. Set CORS_ORIGINS to a comma-
# separated list of allowed frontends (e.g. "https://hotel.example.com").
# Use "*" only for local development; "*" disables credentialed requests.
_cors_raw = os.getenv("CORS_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()] or ["*"]
_allow_credentials = _cors_origins != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routes
app.include_router(bot_router)
app.include_router(guest_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(hotel_router)
app.include_router(formc_router)
for _r in channel_routers():
    app.include_router(_r)
for _r in reports_routers():
    app.include_router(_r)


@app.get("/")
async def health():
    return JSONResponse({"status": "ok", "service": "HotelFlow v2",
                         "admin": "/admin", "version": "2.0.0"})


# ── Serve frontend HTML files ─────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/{path:path}", response_class=HTMLResponse)
async def serve_admin(path: str = ""):
    f = os.path.join(FRONTEND, "admin.html")
    return FileResponse(f) if os.path.exists(f) else JSONResponse({"error": "Admin panel not found"}, 404)


@app.get("/hotel/{slug}", response_class=HTMLResponse)
@app.get("/hotel/{slug}/{path:path}", response_class=HTMLResponse)
async def serve_hotel_dashboard(slug: str, path: str = ""):
    f = os.path.join(FRONTEND, "hotel_dashboard.html")
    return FileResponse(f) if os.path.exists(f) else JSONResponse({"error": "Dashboard not found"}, 404)


@app.get("/login", response_class=HTMLResponse)
async def serve_login():
    f = os.path.join(FRONTEND, "login.html")
    return FileResponse(f) if os.path.exists(f) else JSONResponse({"error": "Login page not found"}, 404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, workers=1)
