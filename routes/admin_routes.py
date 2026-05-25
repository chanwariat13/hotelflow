"""
routes/admin_routes.py — Master admin API (/api/admin/*)
Only superadmin can access these.
"""
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from services import database as db
from services.auth import require_superadmin
from services.cache import get_room as cache_room
import secrets

router = APIRouter(prefix="/api/admin")

def sa(request: Request): return require_superadmin(request)

# ── Hotels CRUD ───────────────────────────────────────────────────
@router.get("/hotels")
async def list_hotels(request: Request):
    await require_superadmin(request)
    hotels = await db.get_all_hotels()
    return JSONResponse({"hotels": hotels})

@router.post("/hotels")
async def create_hotel(request: Request):
    await require_superadmin(request)
    data = await request.json()
    if not data.get("hotel_name") or not data.get("slug") or not data.get("instance_name"):
        raise HTTPException(400, "hotel_name, slug and instance_name are required")
    # Slug: lowercase, hyphens only
    data["slug"] = data["slug"].lower().replace(" ","-").replace("_","-")
    hotel = await db.create_hotel(data)
    # Create rooms if provided
    for r in data.get("rooms",[]):
        if r.get("room_number"):
            await db.upsert_room(hotel["id"], r["room_number"], r.get("room_type","Standard"),
                                  int(r.get("floor",1)), float(r.get("room_rate",0)),
                                  r.get("qr_secret", secrets.token_urlsafe(12).upper()[:16]))
    # Create staff departments if provided
    for d in data.get("departments",[]):
        if d.get("department"):
            await db.upsert_dept(hotel["id"], d["department"], d.get("display_name",""), d.get("whatsapp_number",""))
    # Create services if provided
    for s in data.get("services",[]):
        if s.get("service_name"):
            await db.create_service(hotel["id"], s)
    # Create owner user if provided
    if data.get("owner_username") and data.get("owner_password"):
        await db.create_hotel_user(hotel["id"], {
            "name": data.get("owner_name", data.get("hotel_name") + " Owner"),
            "whatsapp_number": data.get("owner_whatsapp",""),
            "role": "owner", "username": data["owner_username"],
            "password": data["owner_password"]
        })
    return JSONResponse({"success": True, "hotel": hotel})

@router.get("/hotels/{hid}")
async def get_hotel(hid: int, request: Request):
    await require_superadmin(request)
    hotel = await db.get_hotel_by_id(hid)
    if not hotel: raise HTTPException(404, "Not found")
    rooms = await db.get_all_rooms(hid)
    users = await db.get_hotel_users(hid)
    svcs  = await db.get_all_services_admin(hid)
    return JSONResponse({"hotel": hotel, "rooms": rooms, "users": users, "services": svcs})

@router.put("/hotels/{hid}")
async def update_hotel(hid: int, request: Request):
    await require_superadmin(request)
    data = await request.json()
    hotel = await db.update_hotel(hid, data)
    return JSONResponse({"success": True, "hotel": hotel})

@router.delete("/hotels/{hid}")
async def delete_hotel(hid: int, request: Request):
    await require_superadmin(request)
    await db.execute("UPDATE hotels SET is_active=FALSE WHERE id=$1", hid)
    return JSONResponse({"success": True})

# ── Hotel Users (staff management from master admin) ──────────────
@router.get("/hotels/{hid}/users")
async def list_users(hid: int, request: Request):
    await require_superadmin(request)
    return JSONResponse({"users": await db.get_hotel_users(hid)})

@router.post("/hotels/{hid}/users")
async def create_user(hid: int, request: Request):
    await require_superadmin(request)
    data = await request.json()
    user = await db.create_hotel_user(hid, data)
    return JSONResponse({"success": True, "user": user})

@router.put("/hotels/{hid}/users/{uid}")
async def update_user(hid: int, uid: int, request: Request):
    await require_superadmin(request)
    data = await request.json()
    user = await db.update_hotel_user(uid, data)
    return JSONResponse({"success": True, "user": user})

@router.delete("/hotels/{hid}/users/{uid}")
async def delete_user(hid: int, uid: int, request: Request):
    await require_superadmin(request)
    await db.delete_hotel_user(uid)
    return JSONResponse({"success": True})

# ── Rooms ─────────────────────────────────────────────────────────
@router.get("/hotels/{hid}/rooms")
async def list_rooms(hid: int, request: Request):
    await require_superadmin(request)
    rooms = await db.get_all_rooms(hid)
    for r in rooms:
        p = await cache_room(r["room_number"])
        r["live_status"] = "Occupied" if p else "Vacant"
        r["current_phone"] = p
    return JSONResponse({"rooms": rooms})

@router.post("/hotels/{hid}/rooms")
async def upsert_room(hid: int, request: Request):
    await require_superadmin(request)
    d = await request.json()
    await db.upsert_room(hid, d["room_number"], d.get("room_type","Standard"),
                          int(d.get("floor",1)), float(d.get("room_rate",0)),
                          d.get("qr_secret", secrets.token_urlsafe(12).upper()[:16]))
    return JSONResponse({"success": True})

@router.put("/hotels/{hid}/rooms/{room_number}/rate")
async def update_room_rate(hid: int, room_number: str, request: Request):
    await require_superadmin(request)
    d = await request.json()
    await db.update_room_rate(room_number, float(d.get("rate",0)), hid)
    return JSONResponse({"success": True})

@router.post("/hotels/{hid}/rooms/free")
async def force_free_room(hid: int, request: Request):
    await require_superadmin(request)
    d = await request.json()
    room = d.get("room","")
    from services.cache import delete_session, delete_room, delete_pending, get_room
    phone = await get_room(room)
    if phone:
        p = phone.replace("PENDING:","")
        await delete_session(p); await delete_room(room); await delete_pending(p)
    await db.set_room_vacant(room, hid)
    return JSONResponse({"success": True})

# ── Services from admin ────────────────────────────────────────────
@router.get("/hotels/{hid}/services")
async def list_services(hid: int, request: Request):
    await require_superadmin(request)
    return JSONResponse({"services": await db.get_all_services_admin(hid)})

@router.post("/hotels/{hid}/services")
async def create_svc(hid: int, request: Request):
    await require_superadmin(request)
    d = await request.json()
    svc = await db.create_service(hid, d)
    return JSONResponse({"success": True, "service": svc})

@router.put("/hotels/{hid}/services/{sid}")
async def update_svc(hid: int, sid: int, request: Request):
    await require_superadmin(request)
    d = await request.json()
    svc = await db.update_service(sid, hid, d)
    return JSONResponse({"success": True, "service": svc})

@router.delete("/hotels/{hid}/services/{sid}")
async def delete_svc(hid: int, sid: int, request: Request):
    await require_superadmin(request)
    await db.delete_service(sid, hid)
    return JSONResponse({"success": True})

# ── Dashboard stats ────────────────────────────────────────────────
@router.get("/dashboard")
async def admin_dashboard(request: Request):
    await require_superadmin(request)
    hotels = await db.get_all_hotels()
    total_rev = 0.0; total_guests = 0; active_hotels = 0
    for h in hotels:
        if not h.get("is_active"): continue
        active_hotels += 1
        rev = await db.get_daily_revenue(h["id"])
        total_rev += float(rev.get("total_revenue",0))
        total_guests += int(rev.get("active_guests",0))
    return JSONResponse({"active_hotels": active_hotels, "total_hotels": len(hotels),
                          "total_guests_today": total_guests, "total_revenue_today": total_rev})

# ── Bookings (all hotels) ─────────────────────────────────────────
@router.get("/bookings")
async def all_bookings(request: Request):
    await require_superadmin(request)
    hid = int(request.query_params.get("hotel_id",0))
    status = request.query_params.get("status","")
    limit = int(request.query_params.get("limit",50))
    if hid:
        bks = await db.get_bookings_list(hid, status or None, limit)
    else:
        bks = await db.fetch("SELECT b.*,COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending'),0) AS balance_due FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id GROUP BY b.id ORDER BY b.created_at DESC LIMIT $1", limit)
    return JSONResponse({"bookings": bks})

@router.get("/admin/password")
async def change_admin_password(request: Request):
    await require_superadmin(request)
    d = await request.json()
    user = await get_current_user(request)
    await db.update_admin_password(user["user_id"], d.get("new_password",""))
    return JSONResponse({"success": True})
