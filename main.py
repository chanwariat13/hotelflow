from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import logging, os

from config.settings import HOST, PORT
from services.database import get_pool, close_pool, ensure_admin_seed, ensure_schema_v2
from services.cache import get_redis, close_redis
from scheduler.setup import start_scheduler, stop_scheduler
from routes.bot import router as bot_router
from routes.guest_pages import router as guest_router
from routes.auth_routes import router as auth_router
from routes.admin_routes import router as admin_router
from routes.hotel_routes import router as hotel_router
from routes.formc_routes import router as formc_router

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

FRONTEND = os.path.join(os.path.dirname(__file__), "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🏨 HotelFlow v2 starting...")
    await get_pool()
    await get_redis()
    await ensure_schema_v2()
    await ensure_admin_seed()
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
