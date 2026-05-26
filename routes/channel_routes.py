"""
routes/channel_routes.py — Channel-manager (OTA aggregator) API.

Mounts under /api/hotel/{slug}/channel/* and /api/admin/channel/*.

Hotel-owner endpoints (require can_edit_hotel):
    GET    /account                        current account state (secrets masked)
    POST   /connect                        save provider + credentials
    DELETE /disconnect                     deactivate + wipe secrets
    GET    /room-types                     list mapped room types
    POST   /room-types                     create or update mapping
    DELETE /room-types/{id}                remove a mapping
    GET    /rate-plans                     list rate plans
    POST   /rate-plans                     create or update
    DELETE /rate-plans/{id}                remove
    GET    /inventory                      preview the next-N-days inventory
    POST   /sync/inventory                 manual inventory push
    POST   /sync/bookings                  manual booking pull
    GET    /sync-log                       recent sync attempts
    GET    /bookings                       OTA reservations list
    POST   /bookings/{id}/ingest           convert OTA reservation → booking

Superadmin endpoints (require_superadmin):
    GET    /api/admin/channel/overview     status across every hotel
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services import database as db
from services import channel_manager as cm
from services.audit import audit
from services.auth import require_perm, require_superadmin

logger = logging.getLogger(__name__)
router = APIRouter()


def _actor(user) -> str:
    if not user:
        return "system"
    return str(user.get("username") or user.get("name") or user.get("user_id") or "owner")


def _mask(account: Optional[dict]) -> Optional[dict]:
    """Never leak secrets to the dashboard. Just show whether they're set."""
    if not account:
        return None
    out = dict(account)
    for k in ("api_key", "api_secret", "password", "webhook_secret"):
        v = out.get(k) or ""
        out[f"has_{k}"] = bool(v)
        out[k] = ""
    return out


# ══════════════════════════════════════════════════════════════════
# Per-hotel endpoints — /api/hotel/{slug}/channel/...
# ══════════════════════════════════════════════════════════════════
hotel_router = APIRouter(prefix="/api/hotel")


async def _hotel(slug: str, request: Request, perm: str = "can_edit_hotel") -> dict:
    """Authorize and resolve the hotel row from slug."""
    await require_perm(request, slug, perm)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    return hotel


# ── Account ───────────────────────────────────────────────────────
@hotel_router.get("/{slug}/channel/account")
async def get_account(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    account = await db.get_channel_account(hotel["id"])
    return JSONResponse({"account": _mask(account)})


@hotel_router.post("/{slug}/channel/connect")
async def connect_account(slug: str, request: Request):
    """
    Save provider + credentials. To rotate credentials, just POST again
    with the new values. Empty/missing secret fields preserve the
    existing values rather than wiping them.
    """
    hotel = await _hotel(slug, request)
    user = await require_perm(request, slug, "can_edit_hotel")
    data = await request.json()
    provider = (data.get("provider") or "").lower().strip()
    if provider not in cm.ADAPTERS:
        raise HTTPException(400,
            f"Unsupported provider '{provider}'. "
            f"Supported: {sorted(set(cm.ADAPTERS.keys()))}")

    account = await db.upsert_channel_account(hotel["id"], data)
    await audit("channel.connect",
                actor=_actor(user), actor_role="owner",
                hotel_id=hotel["id"], target=str(hotel["id"]),
                payload={"provider": provider, "dry_run": account.get("dry_run"),
                         "is_active": account.get("is_active")},
                request=request)
    await db.insert_sync_log(hotel["id"], provider, "connect",
                             status="ok", payload_summary="account upserted")
    return JSONResponse({"success": True, "account": _mask(account)})


@hotel_router.delete("/{slug}/channel/disconnect")
async def disconnect_account(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    user = await require_perm(request, slug, "can_edit_hotel")
    await db.disconnect_channel_account(hotel["id"])
    await audit("channel.disconnect",
                actor=_actor(user), actor_role="owner",
                hotel_id=hotel["id"], target=str(hotel["id"]),
                request=request)
    await db.insert_sync_log(hotel["id"], "", "disconnect",
                             status="ok", payload_summary="account disabled")
    return JSONResponse({"success": True})


# ── Room types & rate plans ───────────────────────────────────────
@hotel_router.get("/{slug}/channel/room-types")
async def list_room_types(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    return JSONResponse({"room_types": await db.list_channel_room_types(hotel["id"])})


@hotel_router.post("/{slug}/channel/room-types")
async def upsert_room_type(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    data = await request.json()
    if not (data.get("provider_code") or "").strip():
        raise HTTPException(400, "provider_code is required")
    if not (data.get("room_type") or "").strip():
        raise HTTPException(400, "room_type is required")
    rt = await db.upsert_channel_room_type(hotel["id"], data)
    return JSONResponse({"success": True, "room_type": rt})


@hotel_router.delete("/{slug}/channel/room-types/{rt_id}")
async def delete_room_type(slug: str, rt_id: int, request: Request):
    hotel = await _hotel(slug, request)
    await db.delete_channel_room_type(hotel["id"], rt_id)
    return JSONResponse({"success": True})


@hotel_router.get("/{slug}/channel/rate-plans")
async def list_rate_plans(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    return JSONResponse({"rate_plans": await db.list_channel_rate_plans(hotel["id"])})


@hotel_router.post("/{slug}/channel/rate-plans")
async def upsert_rate_plan(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    data = await request.json()
    if not (data.get("code") or "").strip():
        raise HTTPException(400, "code is required (e.g. BAR, NRR)")
    if not int(data.get("room_type_id") or 0):
        raise HTTPException(400, "room_type_id is required")
    rp = await db.upsert_channel_rate_plan(hotel["id"], data)
    return JSONResponse({"success": True, "rate_plan": rp})


@hotel_router.delete("/{slug}/channel/rate-plans/{rp_id}")
async def delete_rate_plan(slug: str, rp_id: int, request: Request):
    hotel = await _hotel(slug, request)
    await db.delete_channel_rate_plan(hotel["id"], rp_id)
    return JSONResponse({"success": True})


# ── Inventory preview & manual sync ───────────────────────────────
@hotel_router.get("/{slug}/channel/inventory")
async def preview_inventory(slug: str, request: Request):
    """
    Return what the inventory snapshot looks like for the next N days
    (defaults to 30). Useful for the dashboard so the operator sees
    exactly what we'd push to OTAs *before* clicking sync.
    """
    hotel = await _hotel(slug, request)
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    rows = await db.list_channel_inventory(hotel["id"], days=days)
    return JSONResponse({"days": days, "rows": rows})


@hotel_router.post("/{slug}/channel/sync/inventory")
async def manual_push_inventory(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    user = await require_perm(request, slug, "can_edit_hotel")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    days = int(body.get("days") or 0) or None
    result = await cm.push_inventory_for_hotel(hotel["id"], days=days)
    await audit("channel.sync.inventory",
                actor=_actor(user), actor_role="owner",
                hotel_id=hotel["id"], target=str(hotel["id"]),
                payload=result.as_dict(), request=request)
    return JSONResponse({"success": result.ok, "result": result.as_dict()})


@hotel_router.post("/{slug}/channel/sync/bookings")
async def manual_pull_bookings(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    user = await require_perm(request, slug, "can_edit_hotel")
    result = await cm.pull_bookings_for_hotel(hotel["id"])
    await audit("channel.sync.bookings",
                actor=_actor(user), actor_role="owner",
                hotel_id=hotel["id"], target=str(hotel["id"]),
                payload=result.as_dict(), request=request)
    return JSONResponse({"success": result.ok, "result": result.as_dict()})


# ── Sync log ──────────────────────────────────────────────────────
@hotel_router.get("/{slug}/channel/sync-log")
async def get_sync_log(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    return JSONResponse({"log": await db.list_sync_log(hotel["id"], limit=limit)})


# ── OTA bookings ──────────────────────────────────────────────────
@hotel_router.get("/{slug}/channel/bookings")
async def list_ota_bookings(slug: str, request: Request):
    hotel = await _hotel(slug, request)
    status = request.query_params.get("status") or None
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    return JSONResponse({
        "bookings": await db.list_channel_bookings(hotel["id"], status=status, limit=limit)
    })


@hotel_router.post("/{slug}/channel/bookings/{cb_id}/ingest")
async def ingest_ota_booking(slug: str, cb_id: int, request: Request):
    """
    Convert an OTA reservation into a real booking. Operator picks a
    room. We block the action if the room is occupied for overlapping
    dates so we can never double-book.
    """
    hotel = await _hotel(slug, request)
    user = await require_perm(request, slug, "can_approve_checkin")
    data = {}
    try:
        data = await request.json()
    except Exception:
        pass
    room_number = (data.get("room_number") or "").strip()
    if not room_number:
        raise HTTPException(400, "room_number is required")
    try:
        result = await cm.ingest_channel_booking(
            hotel["id"], cb_id, room_number,
            actor=_actor(user), request=request,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"success": True, **result})


# ══════════════════════════════════════════════════════════════════
# Superadmin overview — /api/admin/channel/overview
# ══════════════════════════════════════════════════════════════════
admin_router = APIRouter(prefix="/api/admin")


@admin_router.get("/channel/overview")
async def channel_overview(request: Request):
    """
    Cross-hotel summary for the master admin: which hotels are connected,
    which provider, last push/pull timestamps, last error.
    """
    await require_superadmin(request)
    rows = await db.fetch("""
        SELECT h.id AS hotel_id, h.hotel_name, h.slug, h.is_active AS hotel_active,
               ca.provider, ca.is_active AS account_active, ca.dry_run,
               ca.last_inventory_push_at, ca.last_booking_pull_at,
               ca.last_error, ca.updated_at
        FROM hotels h
        LEFT JOIN channel_accounts ca ON ca.hotel_id=h.id
        ORDER BY h.id
    """)
    return JSONResponse({"hotels": rows})


# Aggregation router so main.py only includes one symbol.
def get_routers():
    return [hotel_router, admin_router]
