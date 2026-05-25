import os
import asyncpg, logging
from config.settings import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS
from typing import Optional, List, Dict
from services.auth import hash_password, verify_password, apply_role_defaults

logger = logging.getLogger(__name__)
_pool: Optional[asyncpg.Pool] = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASS, min_size=2, max_size=15, command_timeout=30)
    return _pool

async def close_pool():
    global _pool
    if _pool: await _pool.close(); _pool = None

async def fetchrow(q, *a) -> Optional[Dict]:
    p = await get_pool()
    async with p.acquire() as c:
        r = await c.fetchrow(q, *a)
        return dict(r) if r else None

async def fetch(q, *a) -> List[Dict]:
    p = await get_pool()
    async with p.acquire() as c:
        rows = await c.fetch(q, *a)
        return [dict(r) for r in rows]

async def execute(q, *a):
    p = await get_pool()
    async with p.acquire() as c:
        return await c.execute(q, *a)

async def fetchval(q, *a):
    p = await get_pool()
    async with p.acquire() as c:
        return await c.fetchval(q, *a)

# ══════════════════════════════════════════════════════════════════
# ADMIN USERS (master admin — you)
# ══════════════════════════════════════════════════════════════════
async def verify_admin_login(username: str, password: str) -> Optional[Dict]:
    row = await fetchrow("SELECT * FROM admin_users WHERE username=$1 AND is_active=TRUE", username)
    if not row: return None
    return row if verify_password(password, row["password_hash"]) else None

async def update_admin_password(admin_id: int, new_password: str):
    await execute("UPDATE admin_users SET password_hash=$1 WHERE id=$2",
                  hash_password(new_password), admin_id)

# Known broken hash from older migration.sql seed — sha256("123")
# It will never verify under sha256(salt||pw), so we auto-repair it on startup.
_BROKEN_ADMIN_HASH = "a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3:defaultsalt"


def _truthy(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


async def ensure_admin_seed():
    """
    Boot-time self-heal for the master admin login.

    Behaviour, in order:

    1. Ensure the admin_users table exists (in case migration.sql was never run).
    2. Read the desired credentials from env:
         - ADMIN_USERNAME       (default: "admin")
         - ADMIN_PASSWORD       (default: "admin123" — weak, only used if env not set)
         - ADMIN_PASSWORD_RESET (default: "0"; if truthy, force-reset password on boot)
    3. If no admin user exists at all, create one with those credentials.
    4. If the existing admin still has the known-broken legacy seed hash, repair it
       using the env-provided password.
    5. If ADMIN_PASSWORD is set AND the current password is still the weak default
       'admin123', auto-upgrade to the env password. This is the common path when an
       operator sets a strong password after the row was seeded with the default.
    6. If ADMIN_PASSWORD_RESET is truthy, force-reset the password to ADMIN_PASSWORD
       on boot. This is for emergency recovery — set it once, redeploy, log in,
       then unset it.
    7. Otherwise leave the existing user alone (user-managed password wins).
    """
    username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    password = os.getenv("ADMIN_PASSWORD", "admin123")
    force_reset = _truthy(os.getenv("ADMIN_PASSWORD_RESET"))

    is_default_pw = (password == "admin123")

    try:
        await execute("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id            SERIAL PRIMARY KEY,
                username      VARCHAR(60)  NOT NULL UNIQUE,
                password_hash VARCHAR(300) NOT NULL,
                name          VARCHAR(100) DEFAULT 'Admin',
                is_active     BOOLEAN DEFAULT TRUE,
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)

        row = await fetchrow("SELECT id, password_hash FROM admin_users WHERE username=$1", username)

        if not row:
            await execute(
                "INSERT INTO admin_users (username, password_hash, name) VALUES ($1, $2, 'Super Admin')",
                username, hash_password(password),
            )
            if is_default_pw:
                logger.warning(
                    "Seeded default admin '%s' with WEAK password 'admin123'. "
                    "Set ADMIN_PASSWORD env var to a strong password and redeploy.",
                    username,
                )
            else:
                logger.info("Seeded admin user '%s' from ADMIN_PASSWORD env var.", username)
            return

        if row["password_hash"] == _BROKEN_ADMIN_HASH:
            await execute(
                "UPDATE admin_users SET password_hash=$1 WHERE id=$2",
                hash_password(password), row["id"],
            )
            logger.warning(
                "Detected broken legacy admin seed; reset '%s' to %s.",
                username,
                "ADMIN_PASSWORD env" if not is_default_pw else "default 'admin123' (please set ADMIN_PASSWORD)",
            )
            return

        # Auto-upgrade weak default password whenever a custom one is provided.
        # This handles the case where the admin row was previously seeded with
        # 'admin123' and the operator later sets ADMIN_PASSWORD in their env.
        if not is_default_pw and verify_password("admin123", row["password_hash"]):
            await execute(
                "UPDATE admin_users SET password_hash=$1 WHERE id=$2",
                hash_password(password), row["id"],
            )
            logger.warning(
                "Detected weak default password 'admin123' for admin '%s'; "
                "auto-upgraded to ADMIN_PASSWORD from env.",
                username,
            )
            return

        if force_reset:
            await execute(
                "UPDATE admin_users SET password_hash=$1 WHERE id=$2",
                hash_password(password), row["id"],
            )
            logger.warning(
                "ADMIN_PASSWORD_RESET=1 — force-reset password for '%s'. "
                "Unset ADMIN_PASSWORD_RESET after logging in.",
                username,
            )
            return

    except Exception as e:
        # Never block startup over the seed; just log it loudly.
        logger.exception("ensure_admin_seed failed: %s", e)

# ══════════════════════════════════════════════════════════════════
# HOTELS
# ══════════════════════════════════════════════════════════════════
async def get_all_hotels() -> List[Dict]:
    return await fetch("SELECT * FROM hotels ORDER BY id")

async def get_hotel_by_id(hid: int) -> Optional[Dict]:
    return await fetchrow("SELECT * FROM hotels WHERE id=$1", hid)

async def get_hotel_by_slug(slug: str) -> Optional[Dict]:
    return await fetchrow("SELECT * FROM hotels WHERE slug=$1", slug)

async def get_hotel_by_instance(instance: str) -> Optional[Dict]:
    return await fetchrow("SELECT * FROM hotels WHERE instance_name=$1 AND is_active=TRUE", instance)

async def create_hotel(data: Dict) -> Dict:
    p = await get_pool()
    async with p.acquire() as c:
        row = await c.fetchrow("""
            INSERT INTO hotels (hotel_name,slug,instance_name,
                logo_url,primary_color,secondary_color,background_color,button_color,text_color,font_choice,
                tagline,address,city,google_maps_url,hotel_email,hotel_whatsapp,
                check_in_time,checkout_time_display,welcome_message,footer_text,
                google_review_url,menu_url,emergency_number,wifi_name,wifi_password,
                payment_mode,razorpay_key_id,razorpay_secret,upi_id,upi_display_name,
                gotenberg_url,cloudinary_cloud_name,cloudinary_upload_preset,
                staff_phones,report_phones,
                checkout_hour,late_charge_flat,late_fee_per_hour,max_late_fee,
                svc_open_hour,svc_close_hour,
                sched_daily_report_hour,sched_monthly_report_hour,
                sched_reminder1_hour,sched_reminder2_hour,sched_reminder2_min,
                sched_late_alert_hour,sched_late_alert_min,
                sched_auto_charge_hour,sched_auto_charge_min,is_active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                    $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,
                    $31,$32,$33,$34,$35,$36,$37,$38,$39,$40,$41,$42,$43,$44,
                    $45,$46,$47,$48,$49,$50,$51)
            RETURNING *""",
            data.get("hotel_name"), data.get("slug"), data.get("instance_name"),
            data.get("logo_url",""), data.get("primary_color","#c8a84b"),
            data.get("secondary_color","#1a2942"), data.get("background_color","#0d1117"),
            data.get("button_color","#c8a84b"), data.get("text_color","#ffffff"),
            data.get("font_choice","Outfit"),
            data.get("tagline","Your home away from home"),
            data.get("address",""), data.get("city",""), data.get("google_maps_url",""),
            data.get("hotel_email",""), data.get("hotel_whatsapp",""),
            data.get("check_in_time","2:00 PM"), data.get("checkout_time_display","11:00 AM"),
            data.get("welcome_message","Welcome!"), data.get("footer_text",""),
            data.get("google_review_url",""), data.get("menu_url",""),
            data.get("emergency_number",""), data.get("wifi_name",""), data.get("wifi_password",""),
            data.get("payment_mode","razorpay"), data.get("razorpay_key_id",""),
            data.get("razorpay_secret",""), data.get("upi_id",""), data.get("upi_display_name",""),
            data.get("gotenberg_url","http://localhost:3000"),
            data.get("cloudinary_cloud_name",""), data.get("cloudinary_upload_preset",""),
            data.get("staff_phones",[]), data.get("report_phones",[]),
            int(data.get("checkout_hour",11)), float(data.get("late_charge_flat",500)),
            float(data.get("late_fee_per_hour",0)), float(data.get("max_late_fee",1500)),
            int(data.get("svc_open_hour",7)), int(data.get("svc_close_hour",23)),
            int(data.get("sched_daily_report_hour",7)), int(data.get("sched_monthly_report_hour",9)),
            int(data.get("sched_reminder1_hour",21)),
            int(data.get("sched_reminder2_hour",10)), int(data.get("sched_reminder2_min",30)),
            int(data.get("sched_late_alert_hour",11)), int(data.get("sched_late_alert_min",30)),
            int(data.get("sched_auto_charge_hour",12)), int(data.get("sched_auto_charge_min",0)),
            data.get("is_active", True)
        )
        return dict(row)

async def update_hotel(hid: int, data: Dict) -> Optional[Dict]:
    allowed = [
        "hotel_name","logo_url","primary_color","secondary_color","background_color",
        "button_color","text_color","font_choice","tagline","address","city",
        "google_maps_url","hotel_email","hotel_whatsapp","check_in_time","checkout_time_display",
        "welcome_message","footer_text","google_review_url","menu_url","emergency_number",
        "wifi_name","wifi_password","payment_mode","razorpay_key_id","razorpay_secret",
        "upi_id","upi_display_name","gotenberg_url","cloudinary_cloud_name","cloudinary_upload_preset",
        "staff_phones","report_phones","checkout_hour","late_charge_flat","late_fee_per_hour",
        "max_late_fee","svc_open_hour","svc_close_hour","sched_daily_report_hour",
        "sched_monthly_report_hour","sched_reminder1_hour","sched_reminder2_hour",
        "sched_reminder2_min","sched_late_alert_hour","sched_late_alert_min",
        "sched_auto_charge_hour","sched_auto_charge_min","is_active"
    ]
    fields, vals, i = [], [], 1
    for k, v in data.items():
        if k in allowed:
            fields.append(f"{k}=${i}"); vals.append(v); i += 1
    if not fields: return await get_hotel_by_id(hid)
    vals.append(hid)
    return await fetchrow(f"UPDATE hotels SET {','.join(fields)} WHERE id=${i} RETURNING *", *vals)

# ══════════════════════════════════════════════════════════════════
# HOTEL USERS — Owners / Managers / Staff
# KEY: Bot reads whatsapp_number from here. Change number in DB = instant effect.
# ══════════════════════════════════════════════════════════════════
async def get_hotel_users(hid: int) -> List[Dict]:
    return await fetch("SELECT id,hotel_id,name,whatsapp_number,role,username,is_active,created_at, can_approve_checkin,can_reject_checkin,can_checkout_guest,can_manage_services,can_manage_rooms,can_view_revenue,can_view_id_proofs,can_broadcast,can_manage_staff,can_edit_hotel FROM hotel_users WHERE hotel_id=$1 ORDER BY role,name", hid)

async def get_hotel_user_by_id(uid: int) -> Optional[Dict]:
    return await fetchrow("SELECT * FROM hotel_users WHERE id=$1", uid)

async def get_hotel_users_by_role(hid: int, role: str) -> List[Dict]:
    return await fetch("SELECT * FROM hotel_users WHERE hotel_id=$1 AND role=$2 AND is_active=TRUE ORDER BY name", hid, role)

async def identify_staff_by_whatsapp(phone: str, hotel_id: int) -> Optional[Dict]:
    """Bot calls this to check if incoming WhatsApp number is a staff member.
    Change their number in DB → bot recognizes new number instantly. No redeploy."""
    return await fetchrow("""
        SELECT hu.*, h.instance_name, h.hotel_name, h.staff_phones,
               h.checkout_hour, h.late_charge_flat, h.max_late_fee,
               h.gotenberg_url, h.google_review_url, h.menu_url,
               h.payment_mode, h.upi_id, h.upi_display_name,
               h.razorpay_key_id, h.razorpay_secret
        FROM hotel_users hu
        JOIN hotels h ON h.id=hu.hotel_id
        WHERE hu.whatsapp_number=$1 AND hu.hotel_id=$2 AND hu.is_active=TRUE
        LIMIT 1""", phone, hotel_id)

async def identify_any_staff_by_whatsapp(phone: str) -> Optional[Dict]:
    """Identify staff across ALL hotels by WhatsApp number."""
    return await fetchrow("""
        SELECT hu.*, h.slug, h.instance_name, h.hotel_name,
               h.checkout_hour, h.late_charge_flat, h.max_late_fee,
               h.gotenberg_url, h.google_review_url, h.payment_mode,
               h.upi_id, h.upi_display_name, h.razorpay_key_id, h.razorpay_secret
        FROM hotel_users hu
        JOIN hotels h ON h.id=hu.hotel_id
        WHERE hu.whatsapp_number=$1 AND hu.is_active=TRUE AND h.is_active=TRUE
        LIMIT 1""", phone)

async def create_hotel_user(hid: int, data: Dict) -> Dict:
    role = data.get("role", "staff")
    perms = apply_role_defaults(role, {
        "can_approve_checkin": data.get("can_approve_checkin"),
        "can_reject_checkin": data.get("can_reject_checkin"),
        "can_checkout_guest": data.get("can_checkout_guest"),
        "can_manage_services": data.get("can_manage_services"),
        "can_manage_rooms": data.get("can_manage_rooms"),
        "can_view_revenue": data.get("can_view_revenue"),
        "can_view_id_proofs": data.get("can_view_id_proofs"),
        "can_broadcast": data.get("can_broadcast"),
        "can_manage_staff": data.get("can_manage_staff"),
        "can_edit_hotel": data.get("can_edit_hotel"),
    })
    pw_hash = hash_password(data.get("password", "changeme123"))
    p = await get_pool()
    async with p.acquire() as c:
        row = await c.fetchrow("""
            INSERT INTO hotel_users (hotel_id,name,whatsapp_number,role,username,password_hash,
                can_approve_checkin,can_reject_checkin,can_checkout_guest,
                can_manage_services,can_manage_rooms,can_view_revenue,
                can_view_id_proofs,can_broadcast,can_manage_staff,can_edit_hotel,is_active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,TRUE)
            RETURNING id,hotel_id,name,whatsapp_number,role,username,is_active,created_at,
                can_approve_checkin,can_reject_checkin,can_checkout_guest,
                can_manage_services,can_manage_rooms,can_view_revenue,
                can_view_id_proofs,can_broadcast,can_manage_staff,can_edit_hotel""",
            hid, data.get("name"), data.get("whatsapp_number",""),
            role, data.get("username"), pw_hash,
            perms["can_approve_checkin"], perms["can_reject_checkin"],
            perms["can_checkout_guest"], perms["can_manage_services"],
            perms["can_manage_rooms"], perms["can_view_revenue"],
            perms["can_view_id_proofs"], perms["can_broadcast"],
            perms["can_manage_staff"], perms["can_edit_hotel"]
        )
        return dict(row)

async def update_hotel_user(uid: int, data: Dict) -> Optional[Dict]:
    """Update user details. Changing whatsapp_number takes effect instantly in bot."""
    allowed = ["name","whatsapp_number","role","username","is_active",
               "can_approve_checkin","can_reject_checkin","can_checkout_guest",
               "can_manage_services","can_manage_rooms","can_view_revenue",
               "can_view_id_proofs","can_broadcast","can_manage_staff","can_edit_hotel"]
    fields, vals, i = [], [], 1
    for k, v in data.items():
        if k in allowed:
            fields.append(f"{k}=${i}"); vals.append(v); i += 1
    if "password" in data and data["password"]:
        fields.append(f"password_hash=${i}"); vals.append(hash_password(data["password"])); i += 1
    if not fields: return await get_hotel_user_by_id(uid)
    vals.append(uid)
    return await fetchrow(f"UPDATE hotel_users SET {','.join(fields)} WHERE id=${i} RETURNING id,hotel_id,name,whatsapp_number,role,username,is_active", *vals)

async def verify_hotel_user_login(hid: int, username: str, password: str) -> Optional[Dict]:
    row = await fetchrow("SELECT * FROM hotel_users WHERE hotel_id=$1 AND username=$2 AND is_active=TRUE", hid, username)
    if not row: return None
    return row if verify_password(password, row["password_hash"]) else None

async def delete_hotel_user(uid: int):
    await execute("UPDATE hotel_users SET is_active=FALSE WHERE id=$1", uid)

# Get all staff WhatsApp numbers for a hotel (for notifications)
async def get_staff_phones(hid: int, roles: list = None) -> List[str]:
    if roles:
        placeholders = ",".join(f"${i+2}" for i in range(len(roles)))
        rows = await fetch(f"SELECT whatsapp_number FROM hotel_users WHERE hotel_id=$1 AND role IN ({placeholders}) AND is_active=TRUE AND whatsapp_number!=''", hid, *roles)
    else:
        rows = await fetch("SELECT whatsapp_number FROM hotel_users WHERE hotel_id=$1 AND is_active=TRUE AND whatsapp_number!=''", hid)
    return [r["whatsapp_number"] for r in rows if r.get("whatsapp_number")]

# ══════════════════════════════════════════════════════════════════
# ROOMS
# ══════════════════════════════════════════════════════════════════
async def get_all_rooms(hid: int) -> List[Dict]:
    return await fetch("SELECT * FROM rooms WHERE hotel_id=$1 ORDER BY room_number", hid)

async def get_room(room_number: str, hid: int) -> Optional[Dict]:
    return await fetchrow("SELECT * FROM rooms WHERE room_number=$1 AND hotel_id=$2", room_number, hid)

async def upsert_room(hid: int, room_number: str, room_type: str, floor: int, rate: float, qr_secret: str):
    await execute("""
        INSERT INTO rooms (room_number,room_type,floor,room_rate,qr_secret,status,hotel_id)
        VALUES ($1,$2,$3,$4,$5,'Vacant',$6)
        ON CONFLICT (room_number) DO UPDATE
        SET room_type=$2,floor=$3,room_rate=$4,qr_secret=$5,hotel_id=$6,updated_at=NOW()""",
        room_number, room_type, floor, rate, qr_secret, hid)

async def update_room_rate(room_number: str, new_rate: float, hid: int):
    """Owner/Manager can update room rate anytime. Instant effect."""
    await execute("UPDATE rooms SET room_rate=$1,updated_at=NOW() WHERE room_number=$2 AND hotel_id=$3",
                  new_rate, room_number, hid)

async def set_room_occupied(room: str, hid: int):
    await execute("UPDATE rooms SET status='Occupied',updated_at=NOW() WHERE room_number=$1 AND hotel_id=$2", room, hid)

async def set_room_vacant(room: str, hid: int = 1):
    await execute("UPDATE rooms SET status='Vacant',updated_at=NOW() WHERE room_number=$1 AND hotel_id=$2", room, hid)

async def get_room_rate(room: str, hid: int) -> float:
    v = await fetchval("SELECT room_rate FROM rooms WHERE room_number=$1 AND hotel_id=$2", room, hid)
    return float(v or 0)

# ══════════════════════════════════════════════════════════════════
# SERVICES (hotel_id separated)
# ══════════════════════════════════════════════════════════════════
async def get_services(hid: int) -> List[Dict]:
    return await fetch("SELECT * FROM services WHERE hotel_id=$1 AND is_active=TRUE ORDER BY category,service_name", hid)

async def get_all_services_admin(hid: int) -> List[Dict]:
    return await fetch("SELECT * FROM services WHERE hotel_id=$1 ORDER BY category,service_name", hid)

async def create_service(hid: int, data: Dict) -> Dict:
    row = await fetchrow("""
        INSERT INTO services (service_name,category,price,department,description,is_active,hotel_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
        data["service_name"], data.get("category","Other"), float(data.get("price",0)),
        data.get("department","Reception"), data.get("description",""),
        data.get("is_active",True), hid)
    return dict(row)

async def update_service(svc_id: int, hid: int, data: Dict) -> Optional[Dict]:
    """Update service. Instant effect — no redeploy."""
    allowed = ["service_name","category","price","department","description","is_active"]
    fields, vals, i = [], [], 1
    for k, v in data.items():
        if k in allowed:
            fields.append(f"{k}=${i}"); vals.append(v); i += 1
    if not fields: return None
    vals += [svc_id, hid]
    return await fetchrow(f"UPDATE services SET {','.join(fields)} WHERE id=${i} AND hotel_id=${i+1} RETURNING *", *vals)

async def delete_service(svc_id: int, hid: int):
    await execute("DELETE FROM services WHERE id=$1 AND hotel_id=$2", svc_id, hid)

async def get_dept_phone(dept: str, hid: int) -> Optional[str]:
    # First check hotel_users table (live, no redeploy needed)
    val = await fetchval("SELECT whatsapp_number FROM hotel_users WHERE hotel_id=$1 AND whatsapp_number!='' AND is_active=TRUE AND (role='staff' OR role='manager') LIMIT 1", hid)
    if val: return val
    # Fallback to staff_departments
    return await fetchval("SELECT whatsapp_number FROM staff_departments WHERE department=$1 AND hotel_id=$2 AND is_active=TRUE LIMIT 1", dept, hid)

# ══════════════════════════════════════════════════════════════════
# BOOKINGS
# ══════════════════════════════════════════════════════════════════
async def get_active_booking_by_phone(phone: str, hid: int) -> Optional[Dict]:
    return await fetchrow("""
        SELECT b.*, COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending'),0) AS balance_due
        FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id
        WHERE b.guest_phone=$1 AND b.status='Active' AND b.hotel_id=$2
        GROUP BY b.id LIMIT 1""", phone, hid)

async def get_active_booking_by_room(room: str, hid: int) -> Optional[Dict]:
    return await fetchrow("""
        SELECT b.*, COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending'),0) AS balance_due
        FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id
        WHERE b.room_number=$1 AND b.status='Active' AND b.hotel_id=$2
        GROUP BY b.id LIMIT 1""", room, hid)

async def get_booking_by_id(bid: str) -> Optional[Dict]:
    return await fetchrow("SELECT * FROM bookings WHERE booking_id=$1", bid)

async def insert_booking(d: Dict):
    await execute("""
        INSERT INTO bookings (booking_id,room_number,guest_name,guest_phone,
            checkin_date,checkout_date,status,payment_mode,
            id_proof_type,id_proof_number,id_proof_photo,id_proof_photo_back,
            guest_count,alternate_phone,hotel_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        ON CONFLICT (booking_id) DO NOTHING""",
        d["booking_id"],d["room_number"],d["guest_name"],d["guest_phone"],
        d["checkin_date"],d["checkout_date"],d.get("status","Active"),
        d.get("payment_mode","Pay at checkout"),
        d.get("id_proof_type",""),d.get("id_proof_number",""),
        d.get("id_proof_photo",""),d.get("id_proof_photo_back",""),
        d.get("guest_count",1),d.get("alternate_phone",""),d.get("hotel_id",1))

async def checkout_booking(room: str, hid: int):
    await execute("UPDATE bookings SET status='CheckedOut',updated_at=NOW() WHERE room_number=$1 AND status='Active' AND hotel_id=$2", room, hid)
    await execute("UPDATE rooms SET status='Vacant',updated_at=NOW() WHERE room_number=$1 AND hotel_id=$2", room, hid)

async def get_bookings_list(hid: int, status: str = None, limit: int = 50, offset: int = 0) -> List[Dict]:
    w = "WHERE b.hotel_id=$1"
    a = [hid]
    if status:
        w += f" AND b.status=$2"; a.append(status)
    return await fetch(f"""
        SELECT b.*, COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending'),0) AS balance_due
        FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id
        {w} GROUP BY b.id ORDER BY b.created_at DESC LIMIT ${len(a)+1} OFFSET ${len(a)+2}""",
        *a, limit, offset)

# ══════════════════════════════════════════════════════════════════
# STAY CHARGES & PAYMENTS
# ══════════════════════════════════════════════════════════════════
async def get_charges_for_booking(bid: str) -> List[Dict]:
    return await fetch("SELECT * FROM stay_charges WHERE booking_id=$1 AND payment_status NOT IN ('Cancelled','Waived') ORDER BY charge_date ASC,id ASC", bid)

async def get_balance_due(bid: str) -> float:
    v = await fetchval("SELECT COALESCE(SUM(total) FILTER(WHERE payment_status='Pending'),0) FROM stay_charges WHERE booking_id=$1", bid)
    return float(v or 0)

async def insert_stay_charge(d: Dict):
    from datetime import date
    cd = d.get("charge_date") or date.today()
    await execute("""
        INSERT INTO stay_charges (booking_id,charge_date,service_type,description,amount,tax,total,payment_status,order_ref,hotel_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
        d["booking_id"],cd,d["service_type"],d["description"],
        d["amount"],d.get("tax",0),d["total"],
        d.get("payment_status","Pending"),d.get("order_ref"),d.get("hotel_id",1))

async def mark_charges_paid(bid: str, method: str, ref: str):
    await execute("UPDATE stay_charges SET payment_status='Paid',payment_method=$2,order_ref=$3 WHERE booking_id=$1 AND payment_status='Pending'", bid, method, ref)

async def insert_payment_log(d: Dict):
    await execute("""
        INSERT INTO payment_logs (booking_id,guest_phone,room_number,guest_name,amount,payment_method,reference,payment_date,hotel_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8)""",
        d["booking_id"],d["guest_phone"],d["room_number"],d["guest_name"],
        d["amount"],d["payment_method"],d.get("reference",""),d.get("hotel_id",1))

async def insert_additional_guests(bid: str, guests: list, hid: int):
    if not guests: return
    p = await get_pool()
    async with p.acquire() as c:
        await c.executemany("""
            INSERT INTO additional_booking_guests (booking_id,guest_name,id_proof_type,id_proof_number,id_proof_photo,id_proof_photo_back,hotel_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            [(bid,g.get("name",""),g.get("id_proof_type",""),
              g.get("id_proof_number","").upper(),g.get("id_proof_photo",""),
              g.get("id_proof_photo_back",""),hid) for g in guests])

# ══════════════════════════════════════════════════════════════════
# SERVICE REQUESTS
# ══════════════════════════════════════════════════════════════════
async def insert_service_request(d: Dict):
    bid = d.get("booking_id") or await fetchval(
        "SELECT booking_id FROM bookings WHERE guest_phone=$1 AND status='Active' LIMIT 1", d.get("phone",""))
    await execute("""
        INSERT INTO service_requests (request_id,booking_id,service_name,category,status,priority,guest_note,department,charge_amount)
        VALUES ($1,$2,$3,$4,'Pending','Normal',$5,$6,$7)
        ON CONFLICT (request_id) DO NOTHING""",
        d["request_id"],bid,d["service_name"],d["category"],
        d.get("note",""),d["department"],d.get("price",0))

async def mark_service_done(rid: str) -> Optional[Dict]:
    return await fetchrow("UPDATE service_requests SET status='Completed',completed_at=NOW() WHERE request_id=$1 RETURNING *", rid)

async def get_pending_service_requests(hid: int) -> List[Dict]:
    return await fetch("""
        SELECT sr.*, b.room_number, b.guest_name, b.guest_phone
        FROM service_requests sr
        JOIN bookings b ON b.booking_id=sr.booking_id
        WHERE b.hotel_id=$1 AND sr.status='Pending'
        ORDER BY sr.requested_at DESC""", hid)

# ══════════════════════════════════════════════════════════════════
# REVENUE & REPORTS
# ══════════════════════════════════════════════════════════════════
async def get_daily_revenue(hid: int) -> Dict:
    return await fetchrow("""
        WITH t AS (SELECT date_trunc('day',NOW() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata' AS s)
        SELECT
          COALESCE(SUM(sc.total),0) AS total_revenue,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type='Room Rent'),0) AS room_revenue,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type='Food'),0) AS food_revenue,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type NOT IN ('Room Rent','Food')),0) AS service_revenue,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending'),0) AS pending_revenue,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Paid' AND sc.payment_method='Cash'),0) AS cash_collected,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Paid' AND sc.payment_method!='Cash'),0) AS online_collected,
          COUNT(DISTINCT b.booking_id) FILTER(WHERE b.status='Active') AS active_guests,
          COUNT(DISTINCT b.booking_id) FILTER(WHERE b.checkin_date>=t.s) AS checkins_today,
          COUNT(DISTINCT b.booking_id) FILTER(WHERE b.status='CheckedOut' AND b.updated_at>=t.s) AS checkouts_today
        FROM stay_charges sc JOIN bookings b ON b.booking_id=sc.booking_id CROSS JOIN t
        WHERE b.hotel_id=$1 AND sc.charge_date>=t.s""", hid) or {}

async def get_revenue_range(hid: int, from_date: str, to_date: str) -> List[Dict]:
    return await fetch("""
        SELECT charge_date::date AS date,
          COALESCE(SUM(total),0) AS total,
          COALESCE(SUM(total) FILTER(WHERE service_type='Room Rent'),0) AS room,
          COALESCE(SUM(total) FILTER(WHERE service_type='Food'),0) AS food,
          COALESCE(SUM(total) FILTER(WHERE service_type NOT IN ('Room Rent','Food')),0) AS services
        FROM stay_charges sc JOIN bookings b ON b.booking_id=sc.booking_id
        WHERE b.hotel_id=$1 AND charge_date::date BETWEEN $2::date AND $3::date
        GROUP BY charge_date::date ORDER BY date""", hid, from_date, to_date)

async def get_tomorrow_checkouts(hid: int) -> List[Dict]:
    return await fetch("""
        SELECT b.booking_id,b.guest_phone,b.guest_name,b.room_number,b.checkout_date,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending'),0) AS total_pending
        FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id
        WHERE b.status='Active' AND b.hotel_id=$1 AND b.checkout_date::date=CURRENT_DATE+INTERVAL '1 day'
        GROUP BY b.id""", hid)

async def get_today_checkouts(hid: int) -> List[Dict]:
    return await fetch("""
        SELECT b.booking_id,b.guest_phone,b.guest_name,b.room_number,b.checkout_date,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending'),0) AS total_pending
        FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id
        WHERE b.status='Active' AND b.hotel_id=$1 AND b.checkout_date::date=CURRENT_DATE
        GROUP BY b.id""", hid)

async def get_active_guests_for_broadcast(hid: int) -> List[Dict]:
    return await fetch("SELECT guest_phone,guest_name,room_number FROM bookings WHERE status='Active' AND hotel_id=$1", hid)

async def lookup_guest_by_phone(phone: str) -> Optional[Dict]:
    return await fetchrow("""
        SELECT b.guest_name,b.guest_phone,b.alternate_phone,b.id_proof_type,b.id_proof_number,
          b.id_proof_photo,b.id_proof_photo_back,b.guest_count,
          COUNT(*) OVER() AS total_visits,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Paid'),0) AS total_spent,
          MAX(b.checkin_date) AS last_checkin
        FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id
        WHERE b.guest_phone=$1 GROUP BY b.id ORDER BY last_checkin DESC LIMIT 1""", phone)

async def lookup_guest_by_id(id_num: str) -> Optional[Dict]:
    return await fetchrow("""
        SELECT b.guest_name,b.guest_phone,b.alternate_phone,b.id_proof_type,b.id_proof_number,
          b.id_proof_photo,b.id_proof_photo_back,b.guest_count,MAX(b.checkin_date) AS last_checkin
        FROM bookings b WHERE b.id_proof_number=$1 GROUP BY b.id ORDER BY last_checkin DESC LIMIT 1""", id_num)

async def get_monthly_stats(hid: int) -> Dict:
    return await fetchrow("""
        SELECT TO_CHAR(DATE_TRUNC('month',CURRENT_DATE-INTERVAL '1 day'),'Month YYYY') AS report_month,
          COUNT(DISTINCT b.booking_id) AS total_bookings,
          COUNT(DISTINCT b.guest_phone) AS unique_guests,
          COALESCE(SUM(sc.total),0) AS total_revenue,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type='Food'),0) AS food_revenue,
          COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type='Room Rent'),0) AS room_revenue,
          ROUND(AVG(b.checkout_date-b.checkin_date)::numeric,1) AS avg_stay_days,
          COUNT(DISTINCT b.booking_id) FILTER(WHERE b.status='CheckedOut') AS completed_stays
        FROM bookings b LEFT JOIN stay_charges sc ON sc.booking_id=b.booking_id
        WHERE b.hotel_id=$1 AND DATE_TRUNC('month',b.checkin_date)=DATE_TRUNC('month',CURRENT_DATE-INTERVAL '1 day')""", hid) or {}

async def get_top_food(hid: int) -> List[Dict]:
    return await fetch("""
        SELECT description AS item_name,COUNT(*) AS order_count
        FROM stay_charges sc JOIN bookings b ON b.booking_id=sc.booking_id
        WHERE sc.service_type='Food' AND b.hotel_id=$1
          AND DATE_TRUNC('month',sc.created_at)=DATE_TRUNC('month',CURRENT_DATE-INTERVAL '1 day')
        GROUP BY description ORDER BY order_count DESC LIMIT 3""", hid)
