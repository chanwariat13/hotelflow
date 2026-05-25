from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from services import database as db
from services.cache import (get_session, set_session, set_room, delete_session,
                             delete_room, delete_pending, is_blocked, block_user,
                             unblock_user, calc_ttl, get_room as cache_get_room)
from services.whatsapp import (send_text, send_to_phones, send_media_b64,
                                send_image_b64, fetch_upi_qr, create_razorpay_link)
from services.helpers import (request_id as gen_sr, fmt_date, ist_now,
                               categorize_service, html_to_pdf_b64, build_bill_html)
import asyncio, logging, re
from datetime import datetime, timedelta
import pytz

router = APIRouter()
logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def extract_msg(body: dict):
    event = body.get("event","")
    if event and event != "messages.upsert":
        return None, None
    data = body.get("data",{})
    if data.get("key",{}).get("fromMe"):
        return None, None
    msg = data.get("message",{})
    text = (msg.get("conversation") or
            msg.get("extendedTextMessage",{}).get("text") or
            msg.get("imageMessage",{}).get("caption") or "")
    if not text: return None, None
    phone = data.get("key",{}).get("remoteJid","").replace("@s.whatsapp.net","").replace("@g.us","")
    if not phone or "-" in phone: return None, None
    return phone, text.strip()


async def handle_message(body: dict, instance_name: str):
    phone, text = extract_msg(body)
    if not phone or not text: return

    # Load hotel by instance
    hotel = await db.get_hotel_by_instance(instance_name)
    if not hotel: return

    hid        = hotel["id"]
    slug       = hotel["slug"]
    h_name     = hotel["hotel_name"]
    instance   = hotel["instance_name"]
    checkout_h = hotel.get("checkout_hour", 11)
    late_flat  = float(hotel.get("late_charge_flat", 500) or 500)
    pay_mode   = hotel.get("payment_mode","razorpay")
    review_url = hotel.get("google_review_url","")
    gotenberg  = hotel.get("gotenberg_url","http://localhost:3000")

    # Blocked?
    if await is_blocked(phone): return

    UP = text.upper().strip()

    # ── Check if this phone belongs to a staff/manager/owner ─────
    # Bot reads hotel_users table — change number in DB = instant effect, no redeploy
    staff_user = await db.identify_staff_by_whatsapp(phone, hid)

    if staff_user:
        await handle_staff(phone, text, UP, staff_user, hotel, instance, hid, h_name,
                           slug, checkout_h, late_flat, review_url, gotenberg, pay_mode)
        return

    # ── Guest flow ────────────────────────────────────────────────
    session = await get_session(phone)
    if not session:
        await handle_unknown(phone, hotel, instance)
        return

    await handle_guest(phone, text, UP, session, hotel, instance, hid, h_name,
                       checkout_h, late_flat, review_url, gotenberg, pay_mode)


# ══════════════════════════════════════════════════════════════════
# STAFF COMMANDS
# ══════════════════════════════════════════════════════════════════
async def handle_staff(phone, text, UP, su, hotel, instance, hid, h_name,
                       slug, checkout_h, late_flat, review_url, gotenberg, pay_mode):

    # Permission helper
    def can(perm): return bool(su.get(perm))

    # APPROVE <phone>
    m = re.match(r"^APPROVE\s+(\d+)$", UP)
    if m:
        if not can("can_approve_checkin"):
            await send_text(instance, phone, "⛔ You don't have permission to approve check-ins."); return
        await approve_guest(m.group(1), phone, hotel, instance, hid, h_name)
        return

    # REJECT <phone>
    m = re.match(r"^REJECT\s+(\d+)$", UP)
    if m:
        target = m.group(1)
        sess = await get_session(target)
        room = sess.get("room","") if sess else ""
        await delete_session(target)
        if room: await delete_room(room)
        await db.execute("UPDATE bookings SET status='Rejected',updated_at=NOW() WHERE guest_phone=$1 AND status='Active'", target)
        await send_text(instance, phone, f"❌ Rejected: {target} | Room: {room}")
        await send_text(instance, target, "❌ *Check-in Rejected*\nPlease contact reception. 🙏")
        return

    # CASH RECEIVED <phone>
    m = re.match(r"^CASH\s+RECEIVED\s+(\d+)$", UP)
    if m:
        await process_cash(m.group(1), phone, hotel, instance, hid)
        return

    # PAY CONFIRM R<room> <amount>
    m = re.match(r"^PAY\s+CONFIRM\s+R(\w+)\s+(\d+(?:\.\d+)?)$", UP)
    if m:
        await confirm_payment(m.group(1), float(m.group(2)), phone, hotel, instance, hid)
        return

    # FREE R<room>
    m = re.match(r"^FREE\s+R(\w+)$", UP)
    if m:
        room = m.group(1)
        rph = await cache_get_room(room)
        if rph:
            p = rph.replace("PENDING:","")
            await delete_session(p); await delete_room(room); await delete_pending(p)
        await db.set_room_vacant(room, hid)
        await send_text(instance, phone, f"✅ Room {room} is now FREE!")
        return

    # BILL R<room>
    m = re.match(r"^BILL\s+R(\w+)$", UP)
    if m:
        bk = await db.get_active_booking_by_room(m.group(1), hid)
        if bk:
            await send_bill(bk, hotel, instance, gotenberg)
            await send_text(instance, phone, f"📄 Bill sent to {bk['guest_name']}")
        else:
            await send_text(instance, phone, f"⚠️ No active booking for Room {m.group(1)}")
        return

    # CHECKOUT R<room>
    m = re.match(r"^CHECKOUT\s+R(\w+)$", UP)
    if m:
        bk = await db.get_active_booking_by_room(m.group(1), hid)
        if not bk:
            await send_text(instance, phone, f"⚠️ No active booking for Room {m.group(1)}"); return
        staff_phones = await db.get_staff_phones(hid)
        await do_checkout(bk, hotel, instance, review_url, checkout_h, late_flat, hid, gotenberg, staff_phones)
        return

    # STATUS R<room>
    m = re.match(r"^STATUS\s+R(\w+)$", UP)
    if m:
        bk = await db.get_active_booking_by_room(m.group(1), hid)
        if bk:
            bal = float(bk.get("balance_due",0))
            msg = (f"🏨 *Room {m.group(1)}*\n👤 {bk['guest_name']}\n📱 {bk['guest_phone']}\n"
                   f"📅 {fmt_date(bk.get('checkin_date'))} → {fmt_date(bk.get('checkout_date'))}\n"
                   f"💰 Balance: ₹{bal:.0f}")
        else:
            msg = f"🟢 Room {m.group(1)} is VACANT"
        await send_text(instance, phone, msg)
        return

    # ROOMS
    if UP in ("ROOMS","ROOM STATUS","ALL ROOMS"):
        rooms = await db.get_all_rooms(hid)
        now = datetime.now(IST).strftime("%I:%M %p")
        msg = f"🏨 *ROOM STATUS* · {now}\n━━━━━━━━━━━━━━━━━━\n\n"
        free = occ = 0
        for r in rooms:
            val = await cache_get_room(r["room_number"])
            if val: occ += 1; msg += f"🔴 *R{r['room_number']}* — Occupied\n"
            else: free += 1; msg += f"🟢 R{r['room_number']} — Vacant\n"
        msg += f"\n━━━━━━━━━━━━━━━━━━\n🟢 Free: {free}  🔴 Occupied: {occ}"
        await send_text(instance, phone, msg)
        return

    # SALES
    if UP == "SALES":
        r = await db.get_daily_revenue(hid)
        msg = (f"📊 *TODAY'S REVENUE*\n━━━━━━━━━━━━━━━━━━\n"
               f"🏠 Room Rent: ₹{float(r.get('room_revenue',0)):.0f}\n"
               f"🍽️ Food:      ₹{float(r.get('food_revenue',0)):.0f}\n"
               f"🛎️ Services:  ₹{float(r.get('service_revenue',0)):.0f}\n"
               f"━━━━━━━━━━━━━━━━━━\n*Total: ₹{float(r.get('total_revenue',0)):.0f}*\n"
               f"⏳ Pending: ₹{float(r.get('pending_revenue',0)):.0f}\n"
               f"💵 Cash: ₹{float(r.get('cash_collected',0)):.0f}\n"
               f"🌐 Online: ₹{float(r.get('online_collected',0)):.0f}")
        await send_text(instance, phone, msg)
        return

    # QR R<room>
    m = re.match(r"^QR\s+R(\w+)$", UP)
    if m:
        room = m.group(1)
        room_row = await db.get_room(room, hid)
        if not room_row:
            await send_text(instance, phone, f"⚠️ Room {room} not found"); return
        from config.settings import BASE_URL
        import urllib.parse, httpx, base64
        reg_url = f"{BASE_URL}/register/{hotel['slug']}?room={room}&secret={room_row['qr_secret']}"
        qr_api = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&format=png&data={urllib.parse.quote(reg_url)}"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.get(qr_api)
                if resp.status_code == 200:
                    await send_image_b64(instance, phone, base64.b64encode(resp.content).decode(),
                                         f"🏨 *Room {room} QR Code*\n\n📲 Guest scans to register.\n🔗 {reg_url}")
                    return
        except: pass
        await send_text(instance, phone, f"🔗 Room {room}:\n{reg_url}")
        return

    # BLOCK / UNBLOCK
    m = re.match(r"^BLOCK\s+(\d+)$", UP)
    if m: await block_user(m.group(1)); await send_text(instance, phone, f"🚫 {m.group(1)} blocked."); return
    m = re.match(r"^UNBLOCK\s+(\d+)$", UP)
    if m: await unblock_user(m.group(1)); await send_text(instance, phone, f"✅ {m.group(1)} unblocked."); return

    # BROADCAST <msg>
    m = re.match(r"^BROADCAST\s+(.+)$", text, re.DOTALL | re.IGNORECASE)
    if m:
        if not can("can_broadcast"):
            await send_text(instance, phone, "⛔ No broadcast permission."); return
        guests = await db.get_active_guests_for_broadcast(hid)
        bcast_msg = m.group(1).strip()
        for g in guests:
            await send_text(instance, g["guest_phone"],
                f"📢 *{h_name}*\n━━━━━━━━━━━━━━━━━━\n\n{bcast_msg}")
        await send_text(instance, phone, f"✅ Broadcast sent to {len(guests)} guests.")
        return

    # DONE <SR_ID>
    m = re.match(r"^DONE\s+(SR\w+)$", UP)
    if m:
        row = await db.mark_service_done(m.group(1))
        if row: await send_text(instance, phone, f"✅ {m.group(1)} marked DONE!\n🛎️ {row.get('service_name','')}")
        else: await send_text(instance, phone, f"⚠️ Request {m.group(1)} not found.")
        return

    # EXTEND CONFIRM <bid> <date>
    m = re.match(r"^EXTEND\s+CONFIRM\s+(\w+)\s+(\d{4}-\d{2}-\d{2})$", UP)
    if m:
        bid, new_date = m.group(1), m.group(2)
        bk = await db.get_booking_by_id(bid)
        if bk:
            await db.execute("UPDATE bookings SET checkout_date=$1,updated_at=NOW() WHERE booking_id=$2",
                             datetime.strptime(new_date,"%Y-%m-%d"), bid)
            sess = await get_session(bk["guest_phone"])
            if sess:
                sess["checkoutDate"] = new_date
                await set_session(bk["guest_phone"], sess, calc_ttl(new_date))
            await send_text(instance, phone, f"✅ Extension approved!\n📅 New checkout: {new_date}")
            await send_text(instance, bk["guest_phone"],
                f"✅ *Stay Extended!*\n📅 New checkout: *{new_date}*\n💰 Additional charges apply. 🙏")
        return

    # FOOD DONE R<room>
    m = re.match(r"^FOOD\s+DONE\s+R(\w+)$", UP)
    if m:
        bk = await db.get_active_booking_by_room(m.group(1), hid)
        if bk:
            await send_text(instance, bk["guest_phone"],
                f"🍽️ *Your order has been delivered!*\n🏨 Room {m.group(1)}\n\nEnjoy your meal! 😊")
            await send_text(instance, phone, f"✅ Food delivered confirmation sent to Room {m.group(1)}")
        return

    # ADMIN / HELP
    if UP in ("ADMIN","HELP","COMMANDS","?"):
        role = su.get("role","staff").upper()
        await send_text(instance, phone,
            f"🏨 *{h_name} — {role} Commands*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ APPROVE <phone>\n❌ REJECT <phone>\n"
            f"💵 CASH RECEIVED <phone>\n💳 PAY CONFIRM R<room> <amount>\n"
            f"🚪 CHECKOUT R<room>\n📄 BILL R<room>\n🔓 FREE R<room>\n"
            f"📊 STATUS R<room>\n🏨 ROOMS\n💰 SALES\n📱 QR R<room>\n"
            f"🚫 BLOCK <phone>\n✅ UNBLOCK <phone>\n"
            f"📢 BROADCAST <message>\n✓ DONE <SR_ID>\n"
            f"🍽️ FOOD DONE R<room>\n"
            f"📅 EXTEND CONFIRM <bkid> <date>\n"
            f"━━━━━━━━━━━━━━━━━━\n🖥️ Dashboard: /hotel/{hotel['slug']}")
        return

    await send_text(instance, phone, f"⚠️ Unknown command. Type *ADMIN* to see all commands.")


# ══════════════════════════════════════════════════════════════════
# GUEST COMMANDS
# ══════════════════════════════════════════════════════════════════
async def handle_guest(phone, text, UP, session, hotel, instance, hid, h_name,
                       checkout_h, late_flat, review_url, gotenberg, pay_mode):
    status    = session.get("status","")
    room      = session.get("room","")
    bid       = session.get("bookingId","")
    name      = session.get("name","Guest")
    fname     = name.split()[0]
    co_date   = session.get("checkoutDate","")
    menu_url  = hotel.get("menu_url","")
    emergency = hotel.get("emergency_number","")
    wifi_name = hotel.get("wifi_name","")
    wifi_pw   = hotel.get("wifi_password","")
    from config.settings import BASE_URL
    slug = hotel.get("slug","")

    if status == "AWAITING_APPROVAL":
        await send_text(instance, phone,
            f"⏳ *Waiting for approval...*\n\nOur reception team will approve your check-in shortly. 🙏")
        return

    # HI / HELLO / HELP / MENU
    if UP in ("HI","HELLO","HEY","START","HELP","MENU","HOME"):
        svc_url = f"{BASE_URL}/menu/{slug}?r={room}&p={phone}"
        bill_url = f"{BASE_URL}/bill/{slug}?phone={phone}"
        await send_text(instance, phone,
            f"👋 *Namaste {fname}!*\n\n🏨 Room: *{room}*\n📅 Checkout: {fmt_date(co_date)}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n*How can I help you?*\n\n"
            f"1️⃣ *menu* — Food & room service\n2️⃣ *my bill* — View charges\n"
            f"3️⃣ *pay online* — Pay bill online\n4️⃣ *upi* — Pay via UPI QR\n"
            f"5️⃣ *pay cash* — Pay cash\n6️⃣ *checkout* — Checkout\n"
            f"7️⃣ *extend* — Extend stay\n\n"
            f"🛎️ Services: {svc_url}\n💰 Bill: {bill_url}\n\n"
            f"📞 Emergency: {emergency}\n📶 WiFi: {wifi_name} / {wifi_pw}")
        return

    # FOOD / ORDER / MENU
    if any(UP.startswith(k) for k in ("MENU","FOOD","ORDER","1 ")):
        svc_url = f"{BASE_URL}/menu/{slug}?r={room}&p={phone}&n={fname}&b={bid}"
        await send_text(instance, phone,
            f"🍽️ *Room Service Menu*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"🏨 Room: *{room}*\n\n👆 Open to browse & order:\n{svc_url}\n\n"
            f"Orders go straight to our kitchen! 🍳")
        return

    # SERVICES
    if UP in ("SERVICES","SERVICE","2"):
        svcs = await db.get_services(hid)
        if not svcs:
            await send_text(instance, phone, "🛎️ Contact reception for services."); return
        msg = f"🛎️ *Services — Room {room}*\n━━━━━━━━━━━━━━━━━━\n"
        cat = ""
        for s in svcs:
            if s["category"] != cat:
                cat = s["category"]; msg += f"\n*{cat}*\n"
            p = float(s.get("price",0))
            msg += f"  • {s['service_name']} — {'₹'+str(int(p)) if p>0 else 'Free'}\n"
        msg += "\n━━━━━━━━━━━━━━━━━━\nJust type the service name! 😊"
        await send_text(instance, phone, msg)
        return

    # MY BILL
    if any(UP.startswith(k) for k in ("MY BILL","BILL","CHARGES","MYBILL")):
        bk = await db.get_active_booking_by_room(room, hid)
        if not bk:
            await send_text(instance, phone, "⚠️ No active booking. Contact reception."); return
        charges = await db.get_charges_for_booking(bid)
        total = sum(float(c.get("total",0)) for c in charges)
        paid  = sum(float(c.get("total",0)) for c in charges if c.get("payment_status")=="Paid")
        pend  = total - paid
        msg   = (f"💰 *Your Bill — Room {room}*\n━━━━━━━━━━━━━━━━━━\n"
                 f"🔖 {bid}\n📅 {fmt_date(bk.get('checkin_date'))} → {fmt_date(bk.get('checkout_date'))}\n\n")
        for c in charges[-8:]:
            ic = "✓" if c.get("payment_status")=="Paid" else "●"
            msg += f"{ic} {c.get('service_type','')}: {c.get('description','')} — ₹{float(c.get('total',0)):.0f}\n"
        msg += f"\n━━━━━━━━━━━━━━━━━━\n💰 Total: ₹{total:.0f}\n✅ Paid: ₹{paid:.0f}\n⏳ *Balance: ₹{pend:.0f}*"
        await send_text(instance, phone, msg)
        return

    # CHECKOUT
    if any(UP.startswith(k) for k in ("CHECKOUT","CHECK OUT","3")):
        bk = await db.get_active_booking_by_room(room, hid)
        if not bk:
            await send_text(instance, phone, "⚠️ No active booking."); return
        staff_phones = await db.get_staff_phones(hid)
        await do_checkout(bk, hotel, instance, review_url, checkout_h, late_flat, hid, gotenberg, staff_phones)
        return

    # PAY ONLINE / RAZORPAY
    if any(UP.startswith(k) for k in ("PAY ONLINE","RAZORPAY","ONLINE PAY","PAY")):
        if pay_mode == "razorpay":
            await do_razorpay(phone, room, bid, name, hotel, instance, hid)
        else:
            await do_upi(phone, room, bid, name, hotel, instance, hid)
        return

    # UPI
    if any(UP.startswith(k) for k in ("UPI","QR CODE","SCAN QR","PAY UPI")):
        await do_upi(phone, room, bid, name, hotel, instance, hid)
        return

    # PAY CASH
    if any(UP.startswith(k) for k in ("PAY CASH","CASH","CASH PAYMENT")):
        bal = await db.get_balance_due(bid)
        staff_phones = await db.get_staff_phones(hid)
        await send_to_phones(instance, staff_phones,
            f"💵 *CASH PAYMENT — COLLECT NOW*\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 {name}\n📱 {phone}\n🏨 Room: *{room}*\n💰 ₹{bal:.0f}\n\n"
            f"✅ After collecting:\n*CASH RECEIVED {phone}*")
        await send_text(instance, phone,
            f"💵 *Cash Payment*\n━━━━━━━━━━━━━━━━━━\n"
            f"Our staff will come to Room *{room}* to collect *₹{bal:.0f}*.\nKeep amount ready. 🙏")
        return

    # EXTEND
    if any(UP.startswith(k) for k in ("EXTEND","STAY LONGER","EXTENSION")):
        staff_phones = await db.get_staff_phones(hid)
        try:
            old_dt = datetime.strptime(co_date[:10],"%Y-%m-%d")
            new_dt = old_dt + timedelta(days=1)
            new_date = new_dt.strftime("%Y-%m-%d")
        except: new_date = co_date
        bk = await db.get_booking_by_id(bid)
        await send_to_phones(instance, staff_phones,
            f"🔔 *STAY EXTENSION REQUEST*\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 {name}\n📱 {phone}\n🏨 Room: *{room}*\n🔖 {bid}\n\n"
            f"📅 Current: {fmt_date(co_date)}\n📅 Requested: *{fmt_date(new_date)}*\n\n"
            f"✅ EXTEND CONFIRM {bid} {new_date}")
        await send_text(instance, phone,
            f"🔔 *Extension Request Sent!*\n━━━━━━━━━━━━━━━━━━\n"
            f"📅 Current: {fmt_date(co_date)}\n📅 Requested: {fmt_date(new_date)}\n\n"
            f"Reception will confirm shortly. 💰 Extra charges apply. 🙏")
        return

    # Unknown → treat as service request
    await do_service_request(phone, text, room, bid, name, hotel, instance, hid)


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════
async def handle_unknown(phone, hotel, instance):
    from config.settings import BASE_URL
    slug = hotel.get("slug","")
    await send_text(instance, phone,
        f"👋 *Welcome to {hotel['hotel_name']}!*\n\n"
        f"To access services, please scan the QR code in your room.\n\n"
        f"🔗 Or register: {BASE_URL}/register/{slug}\n\n"
        f"📞 Need help? Contact reception.")

async def approve_guest(target, staff_phone, hotel, instance, hid, h_name):
    import secrets as sec
    sess = await get_session(target)
    if not sess:
        await send_text(instance, staff_phone, f"⚠️ No pending session for {target}"); return
    if sess.get("status") != "AWAITING_APPROVAL":
        await send_text(instance, staff_phone, f"ℹ️ {sess.get('name',target)} is already {sess.get('status')}"); return
    sess["status"] = "ORDERING"
    sess["menuToken"] = sec.token_urlsafe(8)
    sess["approvedAt"] = ist_now().isoformat()
    co = sess.get("checkoutDate","")
    ttl = calc_ttl(co)
    room = sess.get("room","")
    name = sess.get("name","Guest")
    bid = sess.get("bookingId","")
    await set_session(target, sess, ttl)
    await set_room(room, target, ttl)
    await db.set_room_occupied(room, hid)
    await db.execute("UPDATE bookings SET status='Active',updated_at=NOW() WHERE booking_id=$1", bid)
    from config.settings import BASE_URL
    slug = hotel.get("slug","")
    await send_text(instance, staff_phone,
        f"✅ *Approved!*\n👤 {name}\n🏨 Room {room}\n📱 {target}")
    await send_text(instance, target,
        f"✅ *Check-in Approved!*\n━━━━━━━━━━━━━━━━━━\n"
        f"🏨 Welcome to *{h_name}*, {name.split()[0]}!\n\n"
        f"🛏️ Room: *{room}*\n📅 Checkout: {fmt_date(co)}\n🔖 {bid}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛎️ Services: {BASE_URL}/menu/{slug}?r={room}&p={target}\n"
        f"💰 Bill: {BASE_URL}/bill/{slug}?phone={target}\n\n"
        f"Type *hi* anytime for help! 🙏\n"
        f"📞 Emergency: {hotel.get('emergency_number','')}\n"
        f"📶 WiFi: {hotel.get('wifi_name','')} / {hotel.get('wifi_password','')}")

async def do_checkout(bk, hotel, instance, review_url, checkout_h, late_flat, hid, gotenberg, staff_phones):
    phone = bk["guest_phone"]; room = bk["room_number"]; bid = bk["booking_id"]; name = bk["guest_name"]
    h_name = hotel["hotel_name"]
    bal = await db.get_balance_due(bid)
    now = datetime.now(IST)
    co_str = str(bk.get("checkout_date","")).split("T")[0]
    try:
        deadline = IST.localize(datetime.strptime(f"{co_str}T{checkout_h:02d}:00:00","%Y-%m-%dT%H:%M:%S"))
        is_late = now > deadline
        hrs_late = max(int((now-deadline).total_seconds()/3600),0) if is_late else 0
    except: is_late = False; hrs_late = 0
    if is_late and hrs_late > 0:
        existing = await db.fetchval("SELECT COUNT(*) FROM stay_charges WHERE booking_id=$1 AND service_type='Late Checkout' AND charge_date=CURRENT_DATE", bid)
        if not existing:
            from datetime import date
            await db.insert_stay_charge({"booking_id":bid,"charge_date":date.today(),"service_type":"Late Checkout",
                "description":f"Late checkout — {hrs_late}hr past {checkout_h}:00 AM","amount":late_flat,
                "total":late_flat,"payment_status":"Pending","hotel_id":hid})
            bal += late_flat
    if bal > 0:
        await send_text(instance, phone,
            f"⚠️ *Pending Balance: ₹{bal:.0f}*\n━━━━━━━━━━━━━━━━━━\n"
            f"Please clear dues before checkout.\n\nType *pay online*, *upi* or *pay cash*. 🙏")
        return
    await db.checkout_booking(room, hid)
    await delete_session(phone); await delete_room(room); await delete_pending(phone)
    charges = await db.get_charges_for_booking(bid)
    bill_h = build_bill_html(bk, charges, hotel)
    pdf = await html_to_pdf_b64(bill_h, gotenberg)
    if pdf: await send_media_b64(instance, phone, pdf, f"📄 Bill from {h_name}", "document", f"bill_{room}.pdf")
    await send_text(instance, phone,
        f"🚪 *Checkout Complete!*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"Thank you for staying at *{h_name}*! 🙏\n\n"
        f"We hope you had a wonderful experience.\n\n"
        f"⭐ *Please share your Google review:*\n{review_url}\n\n"
        f"Your review helps us grow! 🌟")
    await send_to_phones(instance, staff_phones,
        f"✅ *Checkout Complete*\n━━━━━━━━━━━━━━━━━━\n"
        f"🏨 Room {room} — {name}\n📱 {phone}\n"
        f"{'⚠️ Late: '+str(hrs_late)+'hr'+chr(10) if is_late else ''}"
        f"🟢 Room is now VACANT")

async def process_cash(target, staff_phone, hotel, instance, hid):
    sess = await get_session(target)
    if not sess: await send_text(instance, staff_phone, f"⚠️ No session for {target}"); return
    bid = sess.get("bookingId",""); room = sess.get("room",""); name = sess.get("name","Guest")
    bal = await db.get_balance_due(bid)
    if bal <= 0: await send_text(instance, staff_phone, f"ℹ️ No dues for {name} Room {room}"); return
    await db.mark_charges_paid(bid, "Cash", "CASH RECEIVED")
    await db.insert_payment_log({"booking_id":bid,"guest_phone":target,"room_number":room,
        "guest_name":name,"amount":bal,"payment_method":"Cash","hotel_id":hid})
    await db.execute("UPDATE bookings SET total_paid=total_paid+$1,updated_at=NOW() WHERE booking_id=$2", bal, bid)
    await send_text(instance, staff_phone, f"✅ Cash ₹{bal:.0f} recorded\n👤 {name} · Room {room}")
    await send_text(instance, target, f"✅ *Cash Payment Confirmed!*\n💰 ₹{bal:.0f} received\n🏨 Room: {room}\nThank you! 🙏")

async def confirm_payment(room, amount, staff_phone, hotel, instance, hid):
    bk = await db.get_active_booking_by_room(room, hid)
    if not bk: await send_text(instance, staff_phone, f"⚠️ No active booking for Room {room}"); return
    bid = bk["booking_id"]; phone = bk["guest_phone"]; name = bk["guest_name"]
    await db.mark_charges_paid(bid, "Online", "PAY CONFIRM")
    await db.insert_payment_log({"booking_id":bid,"guest_phone":phone,"room_number":room,
        "guest_name":name,"amount":amount,"payment_method":"Online","hotel_id":hid})
    await db.execute("UPDATE bookings SET total_paid=total_paid+$1,updated_at=NOW() WHERE booking_id=$2", amount, bid)
    await send_text(instance, staff_phone, f"✅ ₹{amount:.0f} confirmed · Room {room}")
    await send_text(instance, phone, f"✅ *Payment Confirmed!*\n💰 ₹{amount:.0f} recorded.\nThank you! 🙏")

async def do_razorpay(phone, room, bid, name, hotel, instance, hid):
    bal = await db.get_balance_due(bid)
    if bal <= 0: await send_text(instance, phone, "✅ No pending dues!"); return
    key_id = hotel.get("razorpay_key_id",""); secret = hotel.get("razorpay_secret","")
    if not key_id: await send_text(instance, phone, "⚠️ Online payment not configured. Please pay cash or UPI."); return
    link = await create_razorpay_link(key_id, secret, bal, f"Hotel Stay - Room {room} - {bid}", phone)
    if link:
        await send_text(instance, phone,
            f"💳 *Online Payment*\n━━━━━━━━━━━━━━━━━━\n🏨 Room: *{room}*\n"
            f"💰 Amount: *₹{bal:.0f}*\n\n🔗 Pay securely:\n{link}\n\n✅ Powered by Razorpay.")
    else:
        await send_text(instance, phone, "⚠️ Could not generate link. Please contact reception.")

async def do_upi(phone, room, bid, name, hotel, instance, hid):
    bal = await db.get_balance_due(bid)
    if bal <= 0: await send_text(instance, phone, "✅ No pending dues!"); return
    upi_id = hotel.get("upi_id",""); upi_name = hotel.get("upi_display_name","") or hotel.get("hotel_name","Hotel")
    if not upi_id: await send_text(instance, phone, "⚠️ UPI not configured. Please pay cash."); return
    qr_b64 = await fetch_upi_qr(upi_id, upi_name, bal, room, bid)
    msg = (f"💳 *UPI Payment QR*\n━━━━━━━━━━━━━━━━━━\n🏨 Room: *{room}*\n💰 Amount: *₹{bal:.0f}*\n\n"
           f"📱 Screenshot & scan in GPay/PhonePe/Paytm\n\n"
           f"⚠️ Please pay at reception in front of staff.\nStaff confirms when soundbox says 'Payment Received'")
    if qr_b64: await send_image_b64(instance, phone, qr_b64, msg)
    else: await send_text(instance, phone, msg)
    staff_phones = await db.get_staff_phones(hid)
    await send_to_phones(instance, staff_phones,
        f"💳 *UPI PAYMENT REQUEST*\n👤 {name}\n🏨 Room: *{room}*\n💰 ₹{bal:.0f}\n\n"
        f"✅ After soundbox:\n*PAY CONFIRM R{room} {int(bal)}*")

async def do_service_request(phone, text, room, bid, name, hotel, instance, hid):
    cat, dept = categorize_service(text)
    sr_id = gen_sr()
    price = await db.fetchval(
        "SELECT price FROM services WHERE hotel_id=$1 AND LOWER(service_name) LIKE LOWER($2) AND is_active=TRUE LIMIT 1",
        hid, f"%{text[:20]}%") or 0
    price = float(price)
    await db.insert_service_request({"request_id":sr_id,"phone":phone,"booking_id":bid,
        "service_name":text[:100],"category":cat,"department":dept,"price":price})
    if price > 0:
        from datetime import date
        await db.insert_stay_charge({"booking_id":bid,"charge_date":date.today(),"service_type":cat,
            "description":text[:100],"amount":price,"total":price,"payment_status":"Pending",
            "order_ref":sr_id,"hotel_id":hid})
    dept_phone = await db.get_dept_phone(dept, hid)
    staff_phones = await db.get_staff_phones(hid)
    notify = [dept_phone] if dept_phone else staff_phones
    await send_to_phones(instance, notify,
        f"🛎️ *SERVICE REQUEST*\n━━━━━━━━━━━━━━━━━━\n🏨 Room: *{room}*\n📱 {phone}\n🔖 {sr_id}\n\n"
        f"📋 *{text[:100]}*\n{'💰 ₹'+str(int(price)) if price>0 else ''}\n\nReply *DONE {sr_id}* when done.")
    await send_text(instance, phone,
        f"✅ *Request Received!*\n\n🛎️ {text[:60]}\n🏨 Room: {room}\n🔖 {sr_id}\n\n"
        f"Our team will attend shortly. 🙏\n{'💰 ₹'+str(int(price))+' added to bill.' if price>0 else ''}")

async def send_bill(bk, hotel, instance, gotenberg):
    charges = await db.get_charges_for_booking(bk["booking_id"])
    pdf = await html_to_pdf_b64(build_bill_html(bk, charges, hotel), gotenberg)
    if pdf:
        await send_media_b64(instance, bk["guest_phone"], pdf,
                              f"📄 Bill — Room {bk['room_number']}", "document", f"bill_{bk['room_number']}.pdf")

# ── Webhook entry point ────────────────────────────────────────────
@router.post("/webhook/whatsapp")
async def webhook(request: Request, bg: BackgroundTasks):
    try: body = await request.json()
    except: return JSONResponse({"status":"ok"})
    instance = (body.get("instance") or body.get("instanceName") or
                request.headers.get("X-Instance-Name",""))
    bg.add_task(handle_message, body, instance)
    return JSONResponse({"status":"ok"})
