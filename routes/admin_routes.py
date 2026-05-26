"""
routes/admin_routes.py — Master admin API (/api/admin/*)
Only superadmin can access these.
"""
from fastapi import APIRouter, Request, HTTPException, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from services import database as db
from services.auth import require_superadmin
from services.audit import audit, list_audit
from services.cache import get_room as cache_room
import json
import logging
import secrets

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")

def sa(request: Request): return require_superadmin(request)


def _actor(user) -> str:
    if not user: return "system"
    return str(user.get("username") or user.get("name") or user.get("user_id") or "admin")

# ── Hotels CRUD ───────────────────────────────────────────────────
@router.get("/hotels")
async def list_hotels(request: Request):
    await require_superadmin(request)
    hotels = await db.get_all_hotels()
    return JSONResponse({"hotels": hotels})

@router.post("/hotels")
async def create_hotel(request: Request):
    user = await require_superadmin(request)
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
    await audit("hotel.create", actor=_actor(user), actor_role="superadmin",
                hotel_id=hotel["id"], target=str(hotel["id"]),
                payload={"name": hotel.get("hotel_name"), "slug": hotel.get("slug")},
                request=request)
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
    user = await require_superadmin(request)
    data = await request.json()
    # Mask secrets in audit payload
    audit_payload = {
        k: ("***" if "secret" in k or "password" in k else v)
        for k, v in (data or {}).items()
    }
    hotel = await db.update_hotel(hid, data)
    await audit("hotel.update", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(hid),
                payload={"changed": list((data or {}).keys()), "values": audit_payload},
                request=request)
    return JSONResponse({"success": True, "hotel": hotel})

@router.delete("/hotels/{hid}")
async def delete_hotel(hid: int, request: Request):
    """Soft delete: set is_active=FALSE, keep all history."""
    user = await require_superadmin(request)
    await db.execute("UPDATE hotels SET is_active=FALSE WHERE id=$1", hid)
    await audit("hotel.deactivate", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(hid), request=request)
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
    """Soft delete via is_active=FALSE."""
    user = await require_superadmin(request)
    await db.delete_hotel_user(uid)
    await audit("hotel_user.delete", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(uid), request=request)
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
    user = await require_superadmin(request)
    d = await request.json()
    room = d.get("room","")
    from services.cache import delete_session, delete_room, delete_pending, get_room
    phone = await get_room(room)
    if phone:
        p = phone.replace("PENDING:","")
        await delete_session(p); await delete_room(room); await delete_pending(p)
    await db.set_room_vacant(room, hid)
    await audit("ops.free_room", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=room, payload={"phone_was": phone or ""},
                request=request)
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

@router.put("/password")
async def change_admin_password(request: Request):
    """
    Change the master admin password. Mounted as `PUT /api/admin/password`
    to match the frontend (`admin.html` calls PUT /api/admin/password).
    Previously this was POST /admin/password under the same prefix, which
    resolved to `/api/admin/admin/password` and 404'd silently — operators
    "saved" a new password but kept the old one.
    """
    user = await require_superadmin(request)
    d = await request.json()
    new_pw = (d.get("new_password") or "").strip()
    if not new_pw or len(new_pw) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    await db.update_admin_password(user["user_id"], new_pw)
    await audit("admin.password.change", actor=_actor(user), actor_role="superadmin",
                target=str(user.get("user_id")), request=request)
    return JSONResponse({"success": True, "message": "Password updated"})


# ── Audit Log read API ────────────────────────────────────────────
@router.get("/audit-log")
async def audit_log_read(request: Request):
    await require_superadmin(request)
    qp = request.query_params
    hid = None
    try:
        hid = int(qp.get("hotel_id")) if qp.get("hotel_id") else None
    except Exception:
        hid = None
    prefix = qp.get("action_prefix") or None
    limit = int(qp.get("limit") or 200)
    rows = await list_audit(hotel_id=hid, action_prefix=prefix, limit=limit)
    # Stringify timestamps so JSON serializes
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
    return JSONResponse({"entries": rows})


# ══════════════════════════════════════════════════════════════════
# HOUSEKEEPING
# ══════════════════════════════════════════════════════════════════
@router.get("/hotels/{hid}/housekeeping")
async def hk_list(hid: int, request: Request):
    await require_superadmin(request)
    rooms = await db.list_housekeeping(hid)
    log = await db.list_housekeeping_log(hid, limit=100)
    # JSON-safe timestamps
    for r in rooms:
        if r.get("last_cleaned_at"):
            r["last_cleaned_at"] = r["last_cleaned_at"].isoformat()
    for e in log:
        if e.get("created_at"):
            e["created_at"] = e["created_at"].isoformat()
    return JSONResponse({"rooms": rooms, "log": log})


@router.post("/hotels/{hid}/housekeeping")
async def hk_set(hid: int, request: Request):
    user = await require_superadmin(request)
    body = await request.json()
    room = (body.get("room_number") or "").strip()
    status = (body.get("status") or "").strip()
    if not room or not status:
        raise HTTPException(400, "room_number and status are required")
    try:
        await db.set_housekeeping_status(
            hid, room, status,
            cleaned_by=(body.get("cleaned_by") or _actor(user)),
            notes=(body.get("notes") or ""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await audit("housekeeping.set", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=room,
                payload={"status": status, "notes": body.get("notes", "")},
                request=request)
    return JSONResponse({"success": True})


# ══════════════════════════════════════════════════════════════════
# MAINTENANCE TICKETS
# ══════════════════════════════════════════════════════════════════
@router.get("/hotels/{hid}/maintenance")
async def mt_list(hid: int, request: Request):
    await require_superadmin(request)
    status = request.query_params.get("status") or None
    rows = await db.list_maintenance(hid, status=status, limit=300)
    for r in rows:
        for k in ("reported_at", "resolved_at", "created_at", "updated_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
    return JSONResponse({"tickets": rows})


@router.post("/hotels/{hid}/maintenance")
async def mt_create(hid: int, request: Request):
    user = await require_superadmin(request)
    body = await request.json()
    if not (body.get("title") or "").strip():
        raise HTTPException(400, "title is required")
    body.setdefault("reported_by", _actor(user))
    ticket = await db.create_maintenance(hid, body)
    await audit("maintenance.create", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(ticket.get("id", "")),
                payload={"title": ticket.get("title"), "room": ticket.get("room_number"),
                         "priority": ticket.get("priority")}, request=request)
    if ticket.get("created_at"):
        ticket["created_at"] = ticket["created_at"].isoformat()
    if ticket.get("reported_at"):
        ticket["reported_at"] = ticket["reported_at"].isoformat()
    if ticket.get("updated_at"):
        ticket["updated_at"] = ticket["updated_at"].isoformat()
    return JSONResponse({"success": True, "ticket": ticket})


@router.patch("/hotels/{hid}/maintenance/{tid}")
async def mt_update(hid: int, tid: int, request: Request):
    user = await require_superadmin(request)
    body = await request.json()
    ticket = await db.update_maintenance(tid, body)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    await audit("maintenance.update", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(tid),
                payload={k: v for k, v in body.items() if k in ("status", "priority", "assigned_to")},
                request=request)
    for k in ("reported_at", "resolved_at", "created_at", "updated_at"):
        if ticket.get(k):
            ticket[k] = ticket[k].isoformat()
    return JSONResponse({"success": True, "ticket": ticket})


@router.delete("/hotels/{hid}/maintenance/{tid}")
async def mt_delete(hid: int, tid: int, request: Request):
    """Soft delete via status='cancelled'."""
    user = await require_superadmin(request)
    await db.delete_maintenance(tid)
    await audit("maintenance.cancel", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(tid), request=request)
    return JSONResponse({"success": True})


# ══════════════════════════════════════════════════════════════════
# RAZORPAY WEBHOOK (signature verified)
# ══════════════════════════════════════════════════════════════════
@router.post("/razorpay-webhook/{slug}")
async def razorpay_webhook(slug: str, request: Request, bg: BackgroundTasks):
    """
    Razorpay POSTs payment events here. We verify
        HMAC_SHA256(hotels.razorpay_webhook_secret, raw_body)
    against the X-Razorpay-Signature header before doing anything.

    Until a hotel sets razorpay_webhook_secret in admin settings, we return 503
    rather than process unauthenticated payment events. Configure it at
    Settings → Razorpay Webhook Secret on the master admin dashboard.
    """
    from services.security import verify_razorpay_signature

    raw = await request.body()
    sig = request.headers.get("X-Razorpay-Signature", "") or \
          request.headers.get("x-razorpay-signature", "")

    hotel = await db.get_hotel_by_slug(slug)
    if not hotel or not hotel.get("is_active"):
        # Don't leak which slugs exist — uniform 401 for unknown / inactive too
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    secret = (hotel.get("razorpay_webhook_secret") or "").strip()
    if not secret:
        await audit("razorpay.webhook.rejected_no_secret", actor_role="system",
                    hotel_id=hotel["id"], target="razorpay",
                    payload={"reason": "razorpay_webhook_secret not configured"},
                    request=request)
        return JSONResponse(
            {"error": "Webhook secret not configured for this hotel."},
            status_code=503,
        )

    if not verify_razorpay_signature(raw, sig, secret):
        await audit("razorpay.webhook.rejected_bad_signature", actor_role="system",
                    hotel_id=hotel["id"], target="razorpay",
                    payload={"sig_present": bool(sig)}, request=request)
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    await audit("razorpay.webhook.accepted", actor_role="system",
                hotel_id=hotel["id"], target=str(body.get("event", "")),
                payload={"id": body.get("id", "")}, request=request)
    bg.add_task(_handle_razorpay_event, hotel, body)
    return JSONResponse({"status": "ok"})


async def _handle_razorpay_event(hotel: dict, body: dict):
    """
    Process an authenticated Razorpay event in the background.
    Currently handles `payment_link.paid` — marks the matching booking's
    pending charges paid and notifies guest + staff.
    """
    try:
        event = body.get("event", "")
        if event != "payment_link.paid":
            return
        link = (body.get("payload", {}).get("payment_link") or {}).get("entity", {})
        payment = (body.get("payload", {}).get("payment") or {}).get("entity", {})

        notes = link.get("notes") or {}
        bid = notes.get("booking_id") or ""
        room = notes.get("room") or notes.get("room_number") or ""
        phone = (link.get("customer") or {}).get("contact") or notes.get("phone", "")
        amount = float(payment.get("amount", 0)) / 100.0
        ref = payment.get("id") or link.get("id") or ""

        if not bid:
            # Try to find the active booking from room_number / phone
            if phone:
                row = await db.fetchrow(
                    "SELECT booking_id, room_number, guest_name FROM bookings "
                    "WHERE guest_phone=$1 AND status='Active' AND hotel_id=$2 LIMIT 1",
                    phone, hotel["id"],
                )
                if row:
                    bid = row["booking_id"]
                    room = room or row["room_number"]

        if not bid:
            logger.warning("razorpay event missing booking ref: %s", body.get("id", ""))
            return

        # Idempotency: Razorpay retries the same event on any non-2xx response.
        # If we've already booked this `payment.id` against payment_logs, the
        # mark_charges_paid below is itself idempotent but the bookings.total_paid
        # increment was NOT, so a retried event used to double-credit the guest.
        # Short-circuit here when we've seen this reference before.
        if ref and await db.is_payment_logged(ref):
            logger.info("razorpay duplicate event ignored: ref=%s", ref)
            return

        await db.mark_charges_paid(bid, "Online", ref)
        inserted = await db.insert_payment_log({
            "booking_id": bid, "guest_phone": phone, "room_number": room,
            "guest_name": "", "amount": amount, "payment_method": "Online",
            "reference": ref, "hotel_id": hotel["id"],
        })
        if not inserted:
            # Lost a race against another worker handling the same event; the
            # earlier worker already credited bookings.total_paid. Skip the
            # post-payment side-effects so the guest doesn't get a double
            # WhatsApp confirmation either.
            logger.info("razorpay event lost idempotency race: ref=%s", ref)
            return
        await db.execute(
            "UPDATE bookings SET total_paid = COALESCE(total_paid,0) + $1, updated_at=NOW() "
            "WHERE booking_id=$2",
            amount, bid,
        )

        # Notify guest + staff
        from services.whatsapp import send_text, send_to_phones
        if phone:
            await send_text(hotel["instance_name"], phone,
                f"✅ *Payment Received!*\n💰 ₹{amount:.0f} captured.\n"
                f"🔖 {bid}\nThank you! 🙏")
        staff_phones = await db.get_staff_phones(hotel["id"])
        await send_to_phones(hotel["instance_name"], staff_phones,
            f"✅ *RAZORPAY PAID*\n🔖 {bid} | Room {room}\n💰 ₹{amount:.0f}\nRef: {ref}")
    except Exception as e:
        logger.exception("razorpay handler failed: %s", e)




# ══════════════════════════════════════════════════════════════════
# REAL FOOD / RESTAURANT MODULE — superadmin endpoints
# Replaces the old `menu_url` placeholder. Each hotel can manage its own
# in-room dining menu and view incoming food orders. Food orders auto-create
# matching stay_charge rows so they flow through the existing bill / revenue
# pipeline without changes.
# ══════════════════════════════════════════════════════════════════
@router.get("/hotels/{hid}/food/items")
async def admin_list_food_items(hid: int, request: Request):
    await require_superadmin(request)
    items = await db.list_food_items(hid, available_only=False)
    cats  = await db.list_food_categories(hid)
    return JSONResponse({"items": items, "categories": cats})


@router.post("/hotels/{hid}/food/items")
async def admin_create_food_item(hid: int, request: Request):
    user = await require_superadmin(request)
    data = await request.json()
    if not (data.get("name") or "").strip():
        raise HTTPException(400, "name is required")
    item = await db.create_food_item(hid, data)
    await audit("food.item.create", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(item.get("id", "")),
                payload={"name": item.get("name"), "price": item.get("price")},
                request=request)
    return JSONResponse({"success": True, "item": item})


@router.put("/hotels/{hid}/food/items/{item_id}")
async def admin_update_food_item(hid: int, item_id: int, request: Request):
    user = await require_superadmin(request)
    data = await request.json()
    item = await db.update_food_item(item_id, hid, data)
    if not item:
        raise HTTPException(404, "Food item not found")
    await audit("food.item.update", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(item_id),
                payload={k: v for k, v in (data or {}).items()
                         if k in ("name", "price", "is_available", "category")},
                request=request)
    return JSONResponse({"success": True, "item": item})


@router.delete("/hotels/{hid}/food/items/{item_id}")
async def admin_delete_food_item(hid: int, item_id: int, request: Request):
    user = await require_superadmin(request)
    await db.delete_food_item(item_id, hid)
    await audit("food.item.delete", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(item_id), request=request)
    return JSONResponse({"success": True})


@router.get("/hotels/{hid}/food/orders")
async def admin_list_food_orders(hid: int, request: Request):
    await require_superadmin(request)
    status = request.query_params.get("status") or None
    orders = await db.list_food_orders(hid, status=status, limit=200)
    for o in orders:
        for k in ("created_at", "updated_at", "delivered_at"):
            if o.get(k):
                o[k] = o[k].isoformat()
    return JSONResponse({"orders": orders})


@router.patch("/hotels/{hid}/food/orders/{order_id}")
async def admin_update_food_order(hid: int, order_id: int, request: Request):
    user = await require_superadmin(request)
    data = await request.json()
    new_status = (data.get("status") or "").strip()
    if not new_status:
        raise HTTPException(400, "status is required")
    try:
        order = await db.update_food_order_status(order_id, hid, new_status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not order:
        raise HTTPException(404, "Food order not found")
    await audit("food.order.status", actor=_actor(user), actor_role="superadmin",
                hotel_id=hid, target=str(order_id),
                payload={"status": new_status}, request=request)
    for k in ("created_at", "updated_at", "delivered_at"):
        if order.get(k):
            order[k] = order[k].isoformat()
    return JSONResponse({"success": True, "order": order})
