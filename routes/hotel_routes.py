"""
routes/hotel_routes.py — Hotel owner/manager/staff dashboard API
Each hotel: /api/hotel/{slug}/*
Permissions enforced per role. Zero hardcoding.
"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from services import database as db
from services.auth import require_hotel_access, require_perm, get_current_user
from services.cache import get_room as cache_room, delete_session, delete_room, delete_pending
import secrets

router = APIRouter(prefix="/api/hotel")

# ── Dashboard stats ────────────────────────────────────────────────
@router.get("/{slug}/dashboard")
async def hotel_dashboard(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    hid = hotel["id"]
    rev = await db.get_daily_revenue(hid)
    rooms = await db.get_all_rooms(hid)
    occupied = 0
    for r in rooms:
        if await cache_room(r["room_number"]): occupied += 1
    pending_srs = await db.get_pending_service_requests(hid)
    return JSONResponse({
        "hotel_name": hotel["hotel_name"],
        "active_guests": int(rev.get("active_guests",0)),
        "total_rooms": len(rooms), "occupied_rooms": occupied, "vacant_rooms": len(rooms)-occupied,
        "today_revenue": float(rev.get("total_revenue",0)),
        "pending_balance": float(rev.get("pending_revenue",0)),
        "cash_collected": float(rev.get("cash_collected",0)),
        "online_collected": float(rev.get("online_collected",0)),
        "checkins_today": int(rev.get("checkins_today",0)),
        "checkouts_today": int(rev.get("checkouts_today",0)),
        "room_revenue": float(rev.get("room_revenue",0)),
        "food_revenue": float(rev.get("food_revenue",0)),
        "service_revenue": float(rev.get("service_revenue",0)),
        "pending_service_requests": len(pending_srs)
    })

# ── Rooms ─────────────────────────────────────────────────────────
@router.get("/{slug}/rooms")
async def hotel_rooms(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    rooms = await db.get_all_rooms(hotel["id"])
    for r in rooms:
        p = await cache_room(r["room_number"])
        r["live_status"] = "Occupied" if p else "Vacant"
        r["current_phone"] = p.replace("PENDING:","") if p else None
    return JSONResponse({"rooms": rooms})

@router.put("/{slug}/rooms/{room_number}/rate")
async def update_room_rate(slug: str, room_number: str, request: Request):
    """Owner/Manager can update room rate. Instant — no redeploy."""
    await require_perm(request, slug, "can_manage_rooms")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json()
    new_rate = float(d.get("rate",0))
    if new_rate <= 0: raise HTTPException(400, "Rate must be positive")
    await db.update_room_rate(room_number, new_rate, hotel["id"])
    return JSONResponse({"success": True, "room": room_number, "new_rate": new_rate})

@router.post("/{slug}/rooms/free")
async def force_free(slug: str, request: Request):
    await require_perm(request, slug, "can_manage_rooms")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json()
    room = d.get("room","")
    phone = await cache_room(room)
    if phone:
        p = phone.replace("PENDING:","")
        await delete_session(p); await delete_room(room); await delete_pending(p)
    await db.set_room_vacant(room, hotel["id"])
    return JSONResponse({"success": True})

# ── Bookings ──────────────────────────────────────────────────────
@router.get("/{slug}/bookings")
async def hotel_bookings(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    status = request.query_params.get("status","")
    limit = int(request.query_params.get("limit",50))
    bks = await db.get_bookings_list(hotel["id"], status or None, limit)
    return JSONResponse({"bookings": bks})

@router.get("/{slug}/bookings/{bid}")
async def hotel_booking_detail(slug: str, bid: str, request: Request):
    user = await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    bk = await db.get_booking_by_id(bid)
    if not bk: raise HTTPException(404)
    charges = await db.get_charges_for_booking(bid)
    extra = await db.fetch("SELECT * FROM additional_booking_guests WHERE booking_id=$1", bid)
    # Hide ID proof photos from staff who can't view them
    if not user.get("can_view_id_proofs") and user.get("role") != "superadmin":
        bk["id_proof_photo"] = "🔒 Hidden"
        bk["id_proof_photo_back"] = "🔒 Hidden"
        for ag in extra:
            ag["id_proof_photo"] = "🔒 Hidden"
            ag["id_proof_photo_back"] = "🔒 Hidden"
    return JSONResponse({"booking": bk, "charges": charges, "additional_guests": extra})

# ── Revenue (owner/manager only) ──────────────────────────────────
@router.get("/{slug}/revenue/today")
async def revenue_today(slug: str, request: Request):
    await require_perm(request, slug, "can_view_revenue")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    return JSONResponse({"revenue": await db.get_daily_revenue(hotel["id"])})

@router.get("/{slug}/revenue/range")
async def revenue_range(slug: str, request: Request):
    await require_perm(request, slug, "can_view_revenue")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    from_d = request.query_params.get("from","")
    to_d   = request.query_params.get("to","")
    if not from_d or not to_d:
        from datetime import datetime, timedelta
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        to_d   = datetime.now(ist).strftime("%Y-%m-%d")
        from_d = (datetime.now(ist)-timedelta(days=30)).strftime("%Y-%m-%d")
    data = await db.get_revenue_range(hotel["id"], from_d, to_d)
    return JSONResponse({"data": data, "from": from_d, "to": to_d})

# ── Services — owner/manager can CRUD ────────────────────────────
@router.get("/{slug}/services")
async def list_services(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    return JSONResponse({"services": await db.get_all_services_admin(hotel["id"])})

@router.post("/{slug}/services")
async def add_service(slug: str, request: Request):
    """Add service. Instant — guest menu updates immediately."""
    await require_perm(request, slug, "can_manage_services")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json()
    svc = await db.create_service(hotel["id"], d)
    return JSONResponse({"success": True, "service": svc})

@router.put("/{slug}/services/{sid}")
async def edit_service(slug: str, sid: int, request: Request):
    """Edit service/price. Instant — no redeploy."""
    await require_perm(request, slug, "can_manage_services")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json()
    svc = await db.update_service(sid, hotel["id"], d)
    return JSONResponse({"success": True, "service": svc})

@router.delete("/{slug}/services/{sid}")
async def remove_service(slug: str, sid: int, request: Request):
    await require_perm(request, slug, "can_manage_services")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    await db.delete_service(sid, hotel["id"])
    return JSONResponse({"success": True})

# ── Staff / Users — owner only ────────────────────────────────────
@router.get("/{slug}/users")
async def list_users(slug: str, request: Request):
    await require_perm(request, slug, "can_manage_staff")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    return JSONResponse({"users": await db.get_hotel_users(hotel["id"])})

@router.post("/{slug}/users")
async def add_user(slug: str, request: Request):
    """Add owner/manager/staff. Instant — WhatsApp commands work immediately."""
    await require_perm(request, slug, "can_manage_staff")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json()
    user = await db.create_hotel_user(hotel["id"], d)
    return JSONResponse({"success": True, "user": user})

@router.put("/{slug}/users/{uid}")
async def edit_user(slug: str, uid: int, request: Request):
    """Update WhatsApp number, role, password etc. Instant — no redeploy."""
    await require_perm(request, slug, "can_manage_staff")
    d = await request.json()
    user = await db.update_hotel_user(uid, d)
    return JSONResponse({"success": True, "user": user})

@router.delete("/{slug}/users/{uid}")
async def remove_user(slug: str, uid: int, request: Request):
    await require_perm(request, slug, "can_manage_staff")
    await db.delete_hotel_user(uid)
    return JSONResponse({"success": True})

# ── Hotel settings — owner only ───────────────────────────────────
@router.get("/{slug}/settings")
async def get_settings(slug: str, request: Request):
    await require_perm(request, slug, "can_edit_hotel")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    # Don't expose payment secrets to non-superadmin
    safe = dict(hotel)
    safe.pop("razorpay_secret", None)
    return JSONResponse({"hotel": safe})

@router.put("/{slug}/settings")
async def update_settings(slug: str, request: Request):
    """Owner can update hotel settings. Instant — no redeploy."""
    await require_perm(request, slug, "can_edit_hotel")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json()
    updated = await db.update_hotel(hotel["id"], d)
    return JSONResponse({"success": True, "hotel": updated})

# ── Service requests ───────────────────────────────────────────────
@router.get("/{slug}/service-requests")
async def service_requests(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    srs = await db.get_pending_service_requests(hotel["id"])
    return JSONResponse({"requests": srs})

@router.put("/{slug}/service-requests/{rid}/done")
async def mark_done(slug: str, rid: str, request: Request):
    await require_hotel_access(request, slug)
    row = await db.mark_service_done(rid)
    return JSONResponse({"success": True, "request": row})

# ── Broadcast ─────────────────────────────────────────────────────
@router.post("/{slug}/broadcast")
async def broadcast(slug: str, request: Request):
    await require_perm(request, slug, "can_broadcast")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json()
    msg = d.get("message","").strip()
    if not msg: raise HTTPException(400, "Message required")
    from services.whatsapp import send_text
    guests = await db.get_active_guests_for_broadcast(hotel["id"])
    for g in guests:
        full = (f"📢 *Message from {hotel['hotel_name']}*\n━━━━━━━━━━━━━━━━━━\n\n"
                f"{msg}\n\n━━━━━━━━━━━━━━━━━━")
        await send_text(hotel["instance_name"], g["guest_phone"], full)
    return JSONResponse({"success": True, "sent_to": len(guests)})

# ── Guest lookup ───────────────────────────────────────────────────
@router.get("/{slug}/guests/search")
async def search_guest(slug: str, request: Request):
    await require_hotel_access(request, slug)
    phone = request.query_params.get("phone","").strip()
    id_num = request.query_params.get("id_number","").strip().upper()
    if not phone and not id_num: raise HTTPException(400, "Provide phone or id_number")
    guest = await db.lookup_guest_by_phone(phone) if phone else await db.lookup_guest_by_id(id_num)
    if not guest: return JSONResponse({"found": False})
    return JSONResponse({"found": True, "guest": guest})

# ── QR Code generator ─────────────────────────────────────────────
@router.get("/{slug}/rooms/{room_number}/qr")
async def room_qr(slug: str, room_number: str, request: Request):
    """Generate QR code for room registration page."""
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    room = await db.get_room(room_number, hotel["id"])
    if not room: raise HTTPException(404, "Room not found")
    from config.settings import BASE_URL
    import urllib.parse, httpx, base64
    reg_url = f"{BASE_URL}/register/{slug}?room={room_number}&secret={room['qr_secret']}"
    qr_api = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&format=png&data={urllib.parse.quote(reg_url)}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(qr_api)
            if r.status_code == 200:
                return JSONResponse({"qr_base64": base64.b64encode(r.content).decode(), "url": reg_url})
    except: pass
    return JSONResponse({"url": reg_url})

# ── Approve/Reject checkin from dashboard ─────────────────────────
@router.post("/{slug}/bookings/{bid}/approve")
async def approve_checkin(slug: str, bid: str, request: Request):
    await require_perm(request, slug, "can_approve_checkin")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    bk = await db.get_booking_by_id(bid)
    if not bk: raise HTTPException(404)
    phone = bk["guest_phone"]
    from services.cache import get_session, set_session, set_room, calc_ttl
    from services.whatsapp import send_text
    import secrets as sec
    session = await get_session(phone)
    if not session: raise HTTPException(400, "No session found")
    session["status"] = "ORDERING"
    session["menuToken"] = sec.token_urlsafe(8)
    checkout_date = bk.get("checkout_date","")
    if hasattr(checkout_date, "strftime"): checkout_date = checkout_date.strftime("%Y-%m-%d")
    else: checkout_date = str(checkout_date).split("T")[0]
    ttl = calc_ttl(checkout_date)
    await set_session(phone, session, ttl)
    await set_room(bk["room_number"], phone, ttl)
    await db.set_room_occupied(bk["room_number"], hotel["id"])
    await db.execute("UPDATE bookings SET status='Active',updated_at=NOW() WHERE booking_id=$1", bid)
    await send_text(hotel["instance_name"], phone,
        f"✅ *Check-in Approved!*\n━━━━━━━━━━━━━━━━━━\n"
        f"🏨 Welcome to *{hotel['hotel_name']}*!\n\n"
        f"🛏️ Room: *{bk['room_number']}*\n"
        f"📅 Checkout: {checkout_date}\n\n"
        f"Type *hi* for hotel services. 🙏\n"
        f"📶 WiFi: {hotel.get('wifi_name','')} / {hotel.get('wifi_password','')}")
    return JSONResponse({"success": True})

@router.post("/{slug}/bookings/{bid}/reject")
async def reject_checkin(slug: str, bid: str, request: Request):
    await require_perm(request, slug, "can_reject_checkin")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    bk = await db.get_booking_by_id(bid)
    if not bk: raise HTTPException(404)
    from services.cache import delete_session, delete_room
    from services.whatsapp import send_text
    await delete_session(bk["guest_phone"])
    await delete_room(bk["room_number"])
    await db.execute("UPDATE bookings SET status='Rejected',updated_at=NOW() WHERE booking_id=$1", bid)
    await send_text(hotel["instance_name"], bk["guest_phone"],
        "❌ *Check-in Request Rejected*\n\nPlease contact reception for assistance. 🙏")
    return JSONResponse({"success": True})
