"""
routes/hotel_routes.py — Hotel owner/manager/staff dashboard API
Each hotel: /api/hotel/{slug}/*
Permissions enforced per role. Zero hardcoding.
"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from services import database as db
from services.auth import require_hotel_access, require_perm
from services.cache import get_room as cache_room, delete_session, delete_room, delete_pending
from services.cloudinary_signing import (
    verify_id_proof_token,
    is_cloudinary_url,
    wrap_booking_id_proofs,
    wrap_additional_guest_id_proofs,
    wrap_guest_lookup_id_proofs,
)

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
    bk = await db.get_booking_by_id(bid, hotel_id=hotel["id"])
    if not bk: raise HTTPException(404)
    charges = await db.get_charges_for_booking(bid, hotel_id=hotel["id"])
    extra = await db.fetch(
        "SELECT * FROM additional_booking_guests WHERE booking_id=$1 AND hotel_id=$2",
        bid, hotel["id"],
    )
    # Hide ID proof photos from staff who can't view them
    if not user.get("can_view_id_proofs") and user.get("role") != "superadmin":
        bk["id_proof_photo"] = "🔒 Hidden"
        bk["id_proof_photo_back"] = "🔒 Hidden"
        for ag in extra:
            ag["id_proof_photo"] = "🔒 Hidden"
            ag["id_proof_photo_back"] = "🔒 Hidden"
    # Replace any remaining real Cloudinary URLs with short-lived signed proxy
    # URLs so a leaked link expires in ~10 minutes and permission is re-checked
    # at fetch time. The "🔒 Hidden" sentinel above is preserved by the wrappers.
    wrap_booking_id_proofs(bk, slug)
    for ag in extra:
        wrap_additional_guest_id_proofs(ag, slug)
    return JSONResponse({"booking": bk, "charges": charges, "additional_guests": extra})

# ── ID-proof photo proxy (signed, short-lived) ────────────────────
@router.get("/{slug}/id-proof/{token}")
async def fetch_id_proof(slug: str, token: str, request: Request):
    """Resolve a signed ID-proof token to the actual Cloudinary URL.

    Frontend never sees `https://res.cloudinary.com/...` directly anymore —
    every endpoint that used to emit that URL now emits a path of the form
    `/api/hotel/{slug}/id-proof/{token}` whose token expires in ~10 minutes.
    See `services/cloudinary_signing` for the token format and rationale.

    Defense-in-depth checks (in this order, fail-closed):
      1. Caller has a valid hotel session AND `can_view_id_proofs` for `slug`.
         Permission is re-checked HERE — at fetch time — so revoking
         `can_view_id_proofs` instantly invalidates already-issued tokens
         instead of waiting for them to expire naturally.
      2. Token signature is valid and not expired.
      3. The token's row belongs to THIS hotel (blocks cross-tenant access
         even if a token leaks between hotels — e.g. a manager who works at
         hotel A and B and has tokens for both in the same browser).
      4. The stored URL points at Cloudinary (allow-list). Without this the
         endpoint would be an open redirect — an attacker who somehow stored
         `https://evil.example/...` in `id_proof_photo` could use a valid
         staff session to redirect logged-in browsers.

    On success: 302 to Cloudinary with `Cache-Control: private, no-store` so
    intermediaries don't cache the redirect target.
    """
    user = await require_perm(request, slug, "can_view_id_proofs")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        raise HTTPException(404)

    payload = verify_id_proof_token(token)
    if not payload:
        # Same response for forged / expired / malformed — don't help an
        # attacker distinguish those cases.
        raise HTTPException(403, "Invalid or expired ID-proof token")

    # Resolve the row. We fetch only the columns we need so a row that has
    # other sensitive data isn't touched.
    if payload["kind"] == "b":
        row = await db.fetchrow(
            "SELECT id, hotel_id, id_proof_photo, id_proof_photo_back "
            "FROM bookings WHERE id=$1",
            payload["row_id"],
        )
    else:  # "a" — additional_booking_guests
        row = await db.fetchrow(
            "SELECT id, hotel_id, id_proof_photo, id_proof_photo_back "
            "FROM additional_booking_guests WHERE id=$1",
            payload["row_id"],
        )
    if not row:
        raise HTTPException(404, "Photo not found")

    # Tenant scope re-check. Even with a valid signature + permission, the
    # token must point at a row owned by THIS hotel. Without this, a manager
    # with `can_view_id_proofs` at hotel A could swap the slug in the URL
    # to hotel B and still resolve A's tokens.
    if int(row["hotel_id"]) != int(hotel["id"]):
        raise HTTPException(403, "Cross-tenant access denied")

    col = "id_proof_photo" if payload["which"] == "f" else "id_proof_photo_back"
    url = (row.get(col) or "").strip()
    if not url:
        raise HTTPException(404, "No photo on this row")

    # Open-redirect guard. We only ever stored Cloudinary URLs (the upload
    # path in `routes/guest_pages` POSTs to `api.cloudinary.com` and stores
    # `secure_url` from the response), so anything else is suspicious.
    if not is_cloudinary_url(url):
        raise HTTPException(502, "Refusing to redirect to non-Cloudinary URL")

    resp = RedirectResponse(url, status_code=302)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp

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
    """Owner can update hotel settings. Instant — no redeploy.

    Payment credentials (razorpay_key_id / razorpay_secret /
    razorpay_webhook_secret) are *not* editable from the per-hotel API to
    keep them in the superadmin's hands. The owner can configure UPI and
    branding here, but rotating Razorpay keys must go through the master
    admin (POST /api/admin/hotels/{hid}). This avoids a hotel owner
    accidentally — or maliciously — pointing payouts at another account.
    """
    await require_perm(request, slug, "can_edit_hotel")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    d = await request.json() or {}
    # Strip protected keys before forwarding to the DB layer.
    blocked = {
        "razorpay_secret", "razorpay_key_id", "razorpay_webhook_secret",
        # Slug / instance_name uniqueness is owned by superadmin too.
        "slug", "instance_name", "is_active",
    }
    rejected = [k for k in d.keys() if k in blocked]
    for k in rejected:
        d.pop(k, None)
    updated = await db.update_hotel(hotel["id"], d)
    resp = {"success": True, "hotel": updated}
    if rejected:
        resp["ignored_fields"] = rejected
        resp["message"] = (
            "Some fields require master-admin access and were ignored: "
            + ", ".join(rejected)
        )
    return JSONResponse(resp)

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
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    row = await db.mark_service_done(rid, hotel_id=hotel["id"])
    if not row:
        raise HTTPException(404, "Service request not found in this hotel")
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
    user = await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        raise HTTPException(404)
    phone = request.query_params.get("phone","").strip()
    id_num = request.query_params.get("id_number","").strip().upper()
    if not phone and not id_num: raise HTTPException(400, "Provide phone or id_number")
    # SECURITY: Always scope guest lookups to the caller's hotel — without
    # this, hotel A staff could pull guest_name, ID type/number, photo URLs,
    # alternate_phone, total_visits and total_spent for any guest of any
    # hotel in the system by phone or ID number (cross-tenant PII leak).
    guest = (
        await db.lookup_guest_by_phone(phone, hotel_id=hotel["id"])
        if phone else
        await db.lookup_guest_by_id(id_num, hotel_id=hotel["id"])
    )
    if not guest: return JSONResponse({"found": False})
    # Same permission gate as the booking detail endpoint: staff without
    # `can_view_id_proofs` can still look up name / phone / visit count
    # (legitimate operational need at reception) but the photo URLs are
    # masked. Previously this endpoint exposed them to every hotel user
    # regardless of permission — silent gap, fixed here.
    guest_dict = dict(guest)  # asyncpg Records are immutable; we mutate this
    if not user.get("can_view_id_proofs") and user.get("role") != "superadmin":
        guest_dict["id_proof_photo"] = "🔒 Hidden"
        guest_dict["id_proof_photo_back"] = "🔒 Hidden"
    # Sign any remaining real URLs (`b.id AS id` is selected by the lookup
    # query so the wrapper has the row id it needs).
    wrap_guest_lookup_id_proofs(guest_dict, slug)
    return JSONResponse({"found": True, "guest": guest_dict})

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
    bk = await db.get_booking_by_id(bid, hotel_id=hotel["id"])
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
    await db.execute("UPDATE bookings SET status='Active',updated_at=NOW() WHERE booking_id=$1 AND hotel_id=$2", bid, hotel["id"])
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
    bk = await db.get_booking_by_id(bid, hotel_id=hotel["id"])
    if not bk: raise HTTPException(404)
    from services.cache import delete_session, delete_room
    from services.whatsapp import send_text
    await delete_session(bk["guest_phone"])
    await delete_room(bk["room_number"])
    await db.execute("UPDATE bookings SET status='Rejected',updated_at=NOW() WHERE booking_id=$1 AND hotel_id=$2", bid, hotel["id"])
    await send_text(hotel["instance_name"], bk["guest_phone"],
        "❌ *Check-in Request Rejected*\n\nPlease contact reception for assistance. 🙏")
    return JSONResponse({"success": True})




# ── Payment settings (read-only, non-secret) ──────────────────────
@router.get("/{slug}/payment-settings")
async def payment_settings(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    return JSONResponse({
        "payment_mode": hotel.get("payment_mode", "razorpay"),
        "razorpay_key_id": hotel.get("razorpay_key_id", ""),
        "razorpay_configured": bool(hotel.get("razorpay_key_id") and hotel.get("razorpay_secret")),
        "upi_id": hotel.get("upi_id", ""),
        "upi_display_name": hotel.get("upi_display_name", ""),
        "upi_configured": bool(hotel.get("upi_id")),
    })

# ── Confirm UPI payment (staff endpoint) ──────────────────────────
@router.post("/{slug}/confirm-upi-payment")
async def confirm_upi_payment(slug: str, request: Request):
    """Staff confirms a UPI payment was received (e.g. soundbox notification)."""
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    hid = hotel["id"]
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    booking_id = (body.get("booking_id") or "").strip()
    amount = float(body.get("amount") or 0)
    if not booking_id or amount <= 0:
        raise HTTPException(400, "booking_id and positive amount required")
    bk = await db.get_booking_by_id(booking_id, hotel_id=hid)
    if not bk:
        raise HTTPException(404, "Booking not found")
    phone = bk.get("guest_phone", "")
    room = bk.get("room_number", "")
    name = bk.get("guest_name", "")
    await db.insert_payment_log({
        "booking_id": booking_id,
        "guest_phone": phone,
        "room_number": room,
        "guest_name": name,
        "amount": amount,
        "payment_method": "UPI",
        "reference": "UPI-STAFF-CONFIRM",
        "hotel_id": hid,
    })
    await db.mark_charges_paid(booking_id, "UPI", "UPI-STAFF-CONFIRM", hotel_id=hid)
    await db.execute(
        "UPDATE bookings SET total_paid=total_paid+$1,updated_at=NOW() WHERE booking_id=$2 AND hotel_id=$3",
        amount, booking_id, hid,
    )
    # Notify guest
    try:
        from services.whatsapp import send_text
        await send_text(hotel["instance_name"], phone,
            f"✅ *UPI Payment Confirmed!*\n💰 ₹{amount:.0f} received.\n"
            f"🏨 Room: {room}\nThank you! 🙏")
    except Exception:
        pass
    return JSONResponse({"success": True, "message": "UPI payment confirmed"})


# ══════════════════════════════════════════════════════════════════
# REAL FOOD / RESTAURANT MODULE — per-hotel endpoints
# The hotel owner / manager uses these from their dashboard. The menu they
# build here is what guests see at /food/{slug}, what the bot serves when
# guests type "menu", and what powers the food revenue line on the dashboard.
# ══════════════════════════════════════════════════════════════════
@router.get("/{slug}/food/items")
async def hotel_list_food_items(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    items = await db.list_food_items(hotel["id"], available_only=False)
    cats  = await db.list_food_categories(hotel["id"])
    return JSONResponse({"items": items, "categories": cats})


@router.post("/{slug}/food/items")
async def hotel_create_food_item(slug: str, request: Request):
    """Owners/managers with can_manage_services can edit the food menu."""
    await require_perm(request, slug, "can_manage_services")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    data = await request.json()
    if not (data.get("name") or "").strip():
        raise HTTPException(400, "name is required")
    item = await db.create_food_item(hotel["id"], data)
    return JSONResponse({"success": True, "item": item})


@router.put("/{slug}/food/items/{item_id}")
async def hotel_update_food_item(slug: str, item_id: int, request: Request):
    await require_perm(request, slug, "can_manage_services")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    data = await request.json()
    item = await db.update_food_item(item_id, hotel["id"], data)
    if not item:
        raise HTTPException(404, "Food item not found")
    return JSONResponse({"success": True, "item": item})


@router.delete("/{slug}/food/items/{item_id}")
async def hotel_delete_food_item(slug: str, item_id: int, request: Request):
    await require_perm(request, slug, "can_manage_services")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    await db.delete_food_item(item_id, hotel["id"])
    return JSONResponse({"success": True})


@router.get("/{slug}/food/orders")
async def hotel_list_food_orders(slug: str, request: Request):
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    status = request.query_params.get("status") or None
    orders = await db.list_food_orders(hotel["id"], status=status, limit=200)
    for o in orders:
        for k in ("created_at", "updated_at", "delivered_at"):
            if o.get(k):
                o[k] = o[k].isoformat()
    return JSONResponse({"orders": orders})


@router.patch("/{slug}/food/orders/{order_id}")
async def hotel_update_food_order(slug: str, order_id: int, request: Request):
    """Mark an order Preparing / Ready / Delivered / Cancelled.

    Cancelling soft-cancels the linked stay_charge so the bill total updates
    automatically.
    """
    await require_hotel_access(request, slug)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    data = await request.json()
    new_status = (data.get("status") or "").strip()
    if not new_status:
        raise HTTPException(400, "status is required")
    try:
        order = await db.update_food_order_status(order_id, hotel["id"], new_status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not order:
        raise HTTPException(404, "Food order not found")

    # Notify the guest on key transitions so the bot stays useful even when the
    # guest doesn't hit "/food" again.
    try:
        from services.whatsapp import send_text
        if new_status == "Ready" and order.get("guest_phone"):
            await send_text(hotel["instance_name"], order["guest_phone"],
                f"🍽️ *Your food is ready!*\n🏨 Room: {order.get('room_number','')}\n"
                f"It's on its way to your room. Bon appétit! 🙏")
        elif new_status == "Delivered" and order.get("guest_phone"):
            await send_text(hotel["instance_name"], order["guest_phone"],
                f"✅ *Order delivered to Room {order.get('room_number','')}*\n"
                f"Enjoy your meal! 😊")
    except Exception:
        pass

    for k in ("created_at", "updated_at", "delivered_at"):
        if order.get(k):
            order[k] = order[k].isoformat()
    return JSONResponse({"success": True, "order": order})
