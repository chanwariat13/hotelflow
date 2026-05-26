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

    Reads from env:
      - ADMIN_USERNAME       (default "admin")
      - ADMIN_PASSWORD       (default "admin123" — weak, only used if env not set)
      - ADMIN_PASSWORD_RESET (truthy = force-reset password on this boot)

    Behaviour:
      1. Ensure admin_users table exists.
      2. Create the admin user with env credentials if it doesn't exist.
      3. Repair the known-broken legacy seed hash to the env password.
      4. If the row currently uses the weak default 'admin123' AND a
         non-default ADMIN_PASSWORD is set in env, auto-upgrade to the env
         password (so operators don't have to do anything special after
         setting ADMIN_PASSWORD in Coolify).
      5. ADMIN_PASSWORD_RESET=1 force-resets even a custom password.
      6. Otherwise leave the existing user alone (UI-managed password wins).
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


async def ensure_schema_v2():
    """
    Idempotent schema upgrade. Runs on every startup. Creates new tables and
    adds new columns if they don't already exist. Never drops or renames anything.

    Adds:
      - audit_log:           append-only privileged-action log
      - housekeeping_log:    cleaning history per room
      - maintenance_tickets: room/area maintenance tracking
      - hotel_food_items:    real food/restaurant menu (replaces menu_url string)
      - hotel_food_orders:   guest food orders linked to bookings
      - hotels.gstin                    (B2B tax invoice)
      - hotels.razorpay_webhook_secret  (for signed webhook)
      - bookings.customer_gstin         (B2B guest GSTIN on bill)
      - rooms.housekeeping_status / last_cleaned_by / last_cleaned_at
    """
    try:
        # 1. New tables
        await execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          SERIAL PRIMARY KEY,
                hotel_id    INTEGER,
                actor       VARCHAR(200) DEFAULT '',
                actor_role  VARCHAR(50)  DEFAULT '',
                action      VARCHAR(100) DEFAULT '',
                target      VARCHAR(200) DEFAULT '',
                payload     TEXT         DEFAULT '',
                ip          VARCHAR(100) DEFAULT '',
                user_agent  VARCHAR(300) DEFAULT '',
                created_at  TIMESTAMP    DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_audit_hotel_created ON audit_log(hotel_id, created_at DESC)")
        await execute("CREATE INDEX IF NOT EXISTS idx_audit_action        ON audit_log(action)")

        await execute("""
            CREATE TABLE IF NOT EXISTS housekeeping_log (
                id            SERIAL PRIMARY KEY,
                hotel_id      INTEGER NOT NULL,
                room_number   VARCHAR(20) NOT NULL,
                status        VARCHAR(30) NOT NULL,   -- dirty / cleaning / clean / inspected / maintenance
                cleaned_by    VARCHAR(100) DEFAULT '',
                notes         TEXT DEFAULT '',
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_hk_hotel_room ON housekeeping_log(hotel_id, room_number, created_at DESC)")

        await execute("""
            CREATE TABLE IF NOT EXISTS maintenance_tickets (
                id            SERIAL PRIMARY KEY,
                hotel_id      INTEGER NOT NULL,
                room_number   VARCHAR(20) DEFAULT '',
                title         VARCHAR(200) NOT NULL,
                description   TEXT DEFAULT '',
                priority      VARCHAR(20) DEFAULT 'normal',   -- low / normal / high / urgent
                status        VARCHAR(20) DEFAULT 'open',     -- open / in_progress / resolved / cancelled
                assigned_to   VARCHAR(100) DEFAULT '',
                reported_by   VARCHAR(100) DEFAULT '',
                reported_at   TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP,
                resolution    TEXT DEFAULT '',
                created_at    TIMESTAMP DEFAULT NOW(),
                updated_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_mt_hotel_status ON maintenance_tickets(hotel_id, status)")
        await execute("CREATE INDEX IF NOT EXISTS idx_mt_room         ON maintenance_tickets(room_number)")

        # 2. New columns on existing tables
        await execute("ALTER TABLE hotels   ADD COLUMN IF NOT EXISTS gstin                   VARCHAR(20)  DEFAULT ''")
        await execute("ALTER TABLE hotels   ADD COLUMN IF NOT EXISTS razorpay_webhook_secret VARCHAR(200) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_gstin          VARCHAR(20)  DEFAULT ''")
        await execute("ALTER TABLE rooms    ADD COLUMN IF NOT EXISTS housekeeping_status     VARCHAR(20)  DEFAULT 'clean'")
        await execute("ALTER TABLE rooms    ADD COLUMN IF NOT EXISTS last_cleaned_by         VARCHAR(100) DEFAULT ''")
        await execute("ALTER TABLE rooms    ADD COLUMN IF NOT EXISTS last_cleaned_at         TIMESTAMP")

        # 2b. India compliance — GST place-of-supply (CGST/SGST vs IGST split)
        # `state_code` is the GSTIN 2-digit state code of the hotel itself; we
        # also persist legal_name + PAN for the printed tax invoice header.
        await execute("ALTER TABLE hotels        ADD COLUMN IF NOT EXISTS state_code      VARCHAR(2)   DEFAULT ''")
        await execute("ALTER TABLE hotels        ADD COLUMN IF NOT EXISTS legal_name      VARCHAR(200) DEFAULT ''")
        await execute("ALTER TABLE hotels        ADD COLUMN IF NOT EXISTS pan             VARCHAR(20)  DEFAULT ''")
        await execute("ALTER TABLE hotels        ADD COLUMN IF NOT EXISTS default_gst_rate NUMERIC(5,2) DEFAULT 12.00")
        await execute("ALTER TABLE bookings      ADD COLUMN IF NOT EXISTS guest_state_code VARCHAR(2)  DEFAULT ''")
        await execute("ALTER TABLE stay_charges  ADD COLUMN IF NOT EXISTS hsn_code        VARCHAR(10)  DEFAULT ''")
        await execute("ALTER TABLE stay_charges  ADD COLUMN IF NOT EXISTS tax_rate        NUMERIC(5,2) DEFAULT 0")
        await execute("ALTER TABLE stay_charges  ADD COLUMN IF NOT EXISTS cgst_amount     NUMERIC(10,2) DEFAULT 0")
        await execute("ALTER TABLE stay_charges  ADD COLUMN IF NOT EXISTS sgst_amount     NUMERIC(10,2) DEFAULT 0")
        await execute("ALTER TABLE stay_charges  ADD COLUMN IF NOT EXISTS igst_amount     NUMERIC(10,2) DEFAULT 0")
        await execute("ALTER TABLE stay_charges  ADD COLUMN IF NOT EXISTS is_inter_state  BOOLEAN      DEFAULT FALSE")

        # 2c. India compliance — Form C (FRRO) for foreign guests.
        # Fields mirror what the indianfrro.gov.in portal asks for. We keep them
        # flat on the bookings row; a separate `formc_filings` audit table
        # below records each filing event for legal-defensibility.
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS is_foreign_guest      BOOLEAN     DEFAULT FALSE")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS nationality           VARCHAR(80) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS sex                   VARCHAR(10) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS date_of_birth         VARCHAR(20) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS passport_no           VARCHAR(40) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS passport_place_of_issue VARCHAR(80) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS passport_issue_date   VARCHAR(20) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS passport_expiry_date  VARCHAR(20) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS visa_no               VARCHAR(40) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS visa_type             VARCHAR(40) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS visa_issue_place      VARCHAR(80) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS visa_issue_date       VARCHAR(20) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS visa_expiry_date      VARCHAR(20) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS arrival_in_india_date VARCHAR(20) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS arrival_in_india_port VARCHAR(80) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS last_country_visited  VARCHAR(80) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS next_destination      VARCHAR(120) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS purpose_of_visit      VARCHAR(80) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS formc_status          VARCHAR(20) DEFAULT 'NotRequired'") # NotRequired/Pending/Filed/Failed
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS formc_filed_at        TIMESTAMP")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS formc_reference       VARCHAR(80) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS formc_filed_by        VARCHAR(100) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS formc_remarks         TEXT        DEFAULT ''")
        await execute("CREATE INDEX IF NOT EXISTS idx_bookings_formc_pending ON bookings(hotel_id, formc_status) WHERE is_foreign_guest=TRUE")

        await execute("""
            CREATE TABLE IF NOT EXISTS formc_filings (
                id            SERIAL PRIMARY KEY,
                hotel_id      INTEGER NOT NULL,
                booking_id    VARCHAR(40) NOT NULL,
                action        VARCHAR(20) NOT NULL,   -- generated / filed / failed / amended
                reference     VARCHAR(80) DEFAULT '',
                filed_by      VARCHAR(100) DEFAULT '',
                payload       TEXT DEFAULT '',
                notes         TEXT DEFAULT '',
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_formc_filings_booking ON formc_filings(booking_id, created_at DESC)")
        await execute("CREATE INDEX IF NOT EXISTS idx_formc_filings_hotel   ON formc_filings(hotel_id, created_at DESC)")

        # 3. Real food/restaurant module — replaces the "menu_url is just a string"
        #    placeholder with actual menu storage and order tracking. Each food
        #    order is mirrored as a stay_charge of service_type='Food' so the
        #    existing bill / revenue / payment flows pick it up unchanged.
        await execute("""
            CREATE TABLE IF NOT EXISTS hotel_food_items (
                id              SERIAL PRIMARY KEY,
                hotel_id        INTEGER      NOT NULL,
                category        VARCHAR(80)  DEFAULT 'Other',
                name            VARCHAR(150) NOT NULL,
                description     TEXT         DEFAULT '',
                price           NUMERIC(10,2) NOT NULL DEFAULT 0,
                image_url       TEXT         DEFAULT '',
                type            VARCHAR(20)  DEFAULT 'veg',         -- veg / nonveg / egg
                is_available    BOOLEAN      DEFAULT TRUE,
                is_bestseller   BOOLEAN      DEFAULT FALSE,
                spice_level     VARCHAR(20)  DEFAULT '',            -- mild / medium / spicy / ''
                serving_hours   VARCHAR(50)  DEFAULT '',            -- e.g. 'breakfast', '7am-11pm'
                sort_order      INTEGER      DEFAULT 0,
                created_at      TIMESTAMP    DEFAULT NOW(),
                updated_at      TIMESTAMP    DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_food_items_hotel ON hotel_food_items(hotel_id, is_available)")
        await execute("CREATE INDEX IF NOT EXISTS idx_food_items_cat   ON hotel_food_items(hotel_id, category)")

        await execute("""
            CREATE TABLE IF NOT EXISTS hotel_food_orders (
                id              SERIAL PRIMARY KEY,
                hotel_id        INTEGER      NOT NULL,
                booking_id      VARCHAR(40)  DEFAULT '',
                room_number     VARCHAR(20)  DEFAULT '',
                guest_phone     VARCHAR(20)  DEFAULT '',
                guest_name      VARCHAR(150) DEFAULT '',
                items_json      JSONB        NOT NULL DEFAULT '[]'::jsonb, -- [{food_item_id,name,price,qty}]
                subtotal        NUMERIC(10,2) DEFAULT 0,
                tax             NUMERIC(10,2) DEFAULT 0,
                total           NUMERIC(10,2) DEFAULT 0,
                notes           TEXT         DEFAULT '',
                status          VARCHAR(20)  DEFAULT 'Placed',     -- Placed / Preparing / Ready / Delivered / Cancelled
                stay_charge_id  INTEGER,                            -- link back to stay_charges row
                created_at      TIMESTAMP    DEFAULT NOW(),
                updated_at      TIMESTAMP    DEFAULT NOW(),
                delivered_at    TIMESTAMP
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_food_orders_hotel  ON hotel_food_orders(hotel_id, status)")
        await execute("CREATE INDEX IF NOT EXISTS idx_food_orders_booking ON hotel_food_orders(booking_id)")
        await execute("CREATE INDEX IF NOT EXISTS idx_food_orders_room    ON hotel_food_orders(room_number, status)")

        # 4. Channel Manager — OTA aggregator integration
        # We integrate with one channel-manager aggregator per hotel (AxisRooms,
        # STAAH, RateGain, etc.) which itself fans out to MMT / Booking.com /
        # Goibibo / Agoda / Expedia. So one connection here = 50+ OTAs live.
        #
        # Tables:
        #   channel_accounts       — provider, credentials, hotel_code per hotel
        #   channel_room_types     — map our rooms to provider room_type_codes
        #   channel_rate_plans     — map our rate plans to provider rate_plan_codes
        #   channel_inventory      — daily inventory snapshot we last pushed
        #   channel_sync_log       — append-only log of every sync attempt
        #   channel_bookings       — OTA reservations pulled from the aggregator
        await execute("""
            CREATE TABLE IF NOT EXISTS channel_accounts (
                id              SERIAL PRIMARY KEY,
                hotel_id        INTEGER      NOT NULL UNIQUE,
                provider        VARCHAR(40)  NOT NULL DEFAULT 'axisrooms', -- axisrooms / staah / rategain / siteminder
                base_url        VARCHAR(300) DEFAULT '',
                hotel_code      VARCHAR(80)  DEFAULT '',
                api_key         VARCHAR(300) DEFAULT '',
                api_secret      VARCHAR(300) DEFAULT '',
                username        VARCHAR(120) DEFAULT '',
                password        VARCHAR(300) DEFAULT '',
                webhook_secret  VARCHAR(200) DEFAULT '',
                push_inventory_minutes INTEGER DEFAULT 30,
                pull_bookings_minutes  INTEGER DEFAULT 15,
                inventory_horizon_days INTEGER DEFAULT 60,
                dry_run         BOOLEAN      DEFAULT TRUE,  -- safety: don't hit live API until operator flips off
                is_active       BOOLEAN      DEFAULT FALSE,
                last_inventory_push_at TIMESTAMP,
                last_booking_pull_at   TIMESTAMP,
                last_error      TEXT         DEFAULT '',
                created_at      TIMESTAMP    DEFAULT NOW(),
                updated_at      TIMESTAMP    DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_accounts_hotel ON channel_accounts(hotel_id)")

        await execute("""
            CREATE TABLE IF NOT EXISTS channel_room_types (
                id                  SERIAL PRIMARY KEY,
                hotel_id            INTEGER      NOT NULL,
                room_type           VARCHAR(80)  NOT NULL,        -- our internal room_type label (matches rooms.room_type)
                provider_code       VARCHAR(80)  NOT NULL,        -- e.g. "DLX", "STD" — what the aggregator calls it
                provider_label      VARCHAR(150) DEFAULT '',      -- human label for ops UI
                total_units         INTEGER      DEFAULT 0,       -- how many of this type we sell on OTAs
                base_rate           NUMERIC(10,2) DEFAULT 0,
                is_active           BOOLEAN      DEFAULT TRUE,
                created_at          TIMESTAMP    DEFAULT NOW(),
                updated_at          TIMESTAMP    DEFAULT NOW(),
                UNIQUE(hotel_id, provider_code)
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_room_types_hotel ON channel_room_types(hotel_id)")

        await execute("""
            CREATE TABLE IF NOT EXISTS channel_rate_plans (
                id              SERIAL PRIMARY KEY,
                hotel_id        INTEGER      NOT NULL,
                room_type_id    INTEGER      NOT NULL,            -- FK channel_room_types.id (loose)
                code            VARCHAR(40)  NOT NULL,            -- BAR / NRR / EP / CP / MAP / AP
                name            VARCHAR(150) DEFAULT '',
                meal_plan       VARCHAR(20)  DEFAULT 'EP',        -- EP / CP / MAP / AP
                rate_modifier   NUMERIC(6,3) DEFAULT 1.000,        -- multiplier on base_rate (e.g. 0.9 for NRR)
                is_default      BOOLEAN      DEFAULT FALSE,
                is_active       BOOLEAN      DEFAULT TRUE,
                created_at      TIMESTAMP    DEFAULT NOW(),
                updated_at      TIMESTAMP    DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_rate_plans_hotel ON channel_rate_plans(hotel_id)")
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_rate_plans_rt    ON channel_rate_plans(room_type_id)")

        await execute("""
            CREATE TABLE IF NOT EXISTS channel_inventory (
                id              SERIAL PRIMARY KEY,
                hotel_id        INTEGER      NOT NULL,
                room_type_id    INTEGER      NOT NULL,
                stay_date       DATE         NOT NULL,
                available_units INTEGER      DEFAULT 0,
                base_rate       NUMERIC(10,2) DEFAULT 0,
                stop_sell       BOOLEAN      DEFAULT FALSE,
                last_pushed_at  TIMESTAMP,
                last_push_status VARCHAR(20) DEFAULT 'pending',   -- pending / ok / failed
                created_at      TIMESTAMP    DEFAULT NOW(),
                updated_at      TIMESTAMP    DEFAULT NOW(),
                UNIQUE(hotel_id, room_type_id, stay_date)
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_inv_hotel_date ON channel_inventory(hotel_id, stay_date)")

        await execute("""
            CREATE TABLE IF NOT EXISTS channel_sync_log (
                id              SERIAL PRIMARY KEY,
                hotel_id        INTEGER      NOT NULL,
                provider        VARCHAR(40)  DEFAULT '',
                operation       VARCHAR(40)  NOT NULL,            -- push_inventory / push_rates / pull_bookings / connect
                status          VARCHAR(20)  DEFAULT 'ok',        -- ok / failed / dry_run
                records         INTEGER      DEFAULT 0,
                duration_ms     INTEGER      DEFAULT 0,
                error           TEXT         DEFAULT '',
                payload_summary TEXT         DEFAULT '',           -- short summary, never raw secrets
                created_at      TIMESTAMP    DEFAULT NOW()
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_sync_log_hotel ON channel_sync_log(hotel_id, created_at DESC)")

        await execute("""
            CREATE TABLE IF NOT EXISTS channel_bookings (
                id                  SERIAL PRIMARY KEY,
                hotel_id            INTEGER      NOT NULL,
                provider            VARCHAR(40)  DEFAULT '',
                provider_ref        VARCHAR(120) NOT NULL,        -- the aggregator's reservation id
                ota_source          VARCHAR(60)  DEFAULT '',      -- mmt / goibibo / booking_com / agoda / expedia / direct
                ota_booking_id      VARCHAR(120) DEFAULT '',      -- the OTA's own reservation number, if available
                guest_name          VARCHAR(200) DEFAULT '',
                guest_email         VARCHAR(200) DEFAULT '',
                guest_phone         VARCHAR(40)  DEFAULT '',
                guest_country       VARCHAR(80)  DEFAULT '',
                checkin_date        DATE,
                checkout_date       DATE,
                nights              INTEGER      DEFAULT 0,
                guests              INTEGER      DEFAULT 1,
                room_type_code      VARCHAR(80)  DEFAULT '',
                rate_plan_code      VARCHAR(40)  DEFAULT '',
                room_count          INTEGER      DEFAULT 1,
                currency            VARCHAR(8)   DEFAULT 'INR',
                total_amount        NUMERIC(12,2) DEFAULT 0,
                ota_commission      NUMERIC(12,2) DEFAULT 0,
                payment_terms       VARCHAR(40)  DEFAULT 'pay_at_hotel', -- pay_at_hotel / prepaid
                status              VARCHAR(20)  DEFAULT 'new',          -- new / confirmed / modified / cancelled / ingested
                special_requests    TEXT         DEFAULT '',
                raw_payload         TEXT         DEFAULT '',
                mapped_booking_id   VARCHAR(40)  DEFAULT '',             -- bookings.booking_id once ingested
                received_at         TIMESTAMP    DEFAULT NOW(),
                ingested_at         TIMESTAMP,
                cancelled_at        TIMESTAMP,
                created_at          TIMESTAMP    DEFAULT NOW(),
                updated_at          TIMESTAMP    DEFAULT NOW(),
                UNIQUE(hotel_id, provider, provider_ref)
            )
        """)
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_bookings_hotel  ON channel_bookings(hotel_id, status)")
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_bookings_dates  ON channel_bookings(hotel_id, checkin_date)")
        await execute("CREATE INDEX IF NOT EXISTS idx_channel_bookings_phone  ON channel_bookings(guest_phone)")
        # Tag native bookings that originated from a channel-manager pull, so
        # the dashboard can show a small "OTA · MMT" badge next to them.
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS ota_source     VARCHAR(60) DEFAULT ''")
        await execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS channel_ref    VARCHAR(120) DEFAULT ''")

        logger.info("✅ schema_v2 ensured (audit_log, housekeeping, maintenance, food, formc, channel_manager)")
    except Exception as e:
        logger.exception("ensure_schema_v2 failed: %s", e)

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
        "razorpay_webhook_secret","gstin","state_code","legal_name","pan","default_gst_rate",
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
            guest_count,alternate_phone,hotel_id,customer_gstin,
            guest_state_code,is_foreign_guest,nationality,sex,date_of_birth,
            passport_no,passport_place_of_issue,passport_issue_date,passport_expiry_date,
            visa_no,visa_type,visa_issue_place,visa_issue_date,visa_expiry_date,
            arrival_in_india_date,arrival_in_india_port,last_country_visited,
            next_destination,purpose_of_visit,formc_status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,
                $31,$32,$33,$34,$35,$36)
        ON CONFLICT (booking_id) DO NOTHING""",
        d["booking_id"],d["room_number"],d["guest_name"],d["guest_phone"],
        d["checkin_date"],d["checkout_date"],d.get("status","Active"),
        d.get("payment_mode","Pay at checkout"),
        d.get("id_proof_type",""),d.get("id_proof_number",""),
        d.get("id_proof_photo",""),d.get("id_proof_photo_back",""),
        d.get("guest_count",1),d.get("alternate_phone",""),d.get("hotel_id",1),
        d.get("customer_gstin",""),
        d.get("guest_state_code",""),
        bool(d.get("is_foreign_guest", False)),
        d.get("nationality",""), d.get("sex",""), d.get("date_of_birth",""),
        d.get("passport_no","").upper(), d.get("passport_place_of_issue",""),
        d.get("passport_issue_date",""), d.get("passport_expiry_date",""),
        d.get("visa_no","").upper(), d.get("visa_type",""),
        d.get("visa_issue_place",""), d.get("visa_issue_date",""),
        d.get("visa_expiry_date",""),
        d.get("arrival_in_india_date",""), d.get("arrival_in_india_port",""),
        d.get("last_country_visited",""), d.get("next_destination",""),
        d.get("purpose_of_visit",""),
        "Pending" if d.get("is_foreign_guest") else "NotRequired",
    )

# ── FormC / FRRO helpers ────────────────────────────────────────────
async def list_formc_bookings(hid: int, status: Optional[str] = None,
                              limit: int = 100, offset: int = 0) -> List[Dict]:
    """Foreign-guest bookings, optionally filtered by formc_status."""
    sql = """
        SELECT booking_id, hotel_id, room_number, guest_name, guest_phone,
               checkin_date, checkout_date, status,
               nationality, sex, date_of_birth, passport_no,
               passport_place_of_issue, passport_issue_date, passport_expiry_date,
               visa_no, visa_type, visa_issue_place, visa_issue_date, visa_expiry_date,
               arrival_in_india_date, arrival_in_india_port,
               last_country_visited, next_destination, purpose_of_visit,
               is_foreign_guest, formc_status, formc_filed_at, formc_reference,
               formc_filed_by, formc_remarks, created_at
        FROM bookings
        WHERE hotel_id=$1 AND is_foreign_guest=TRUE
    """
    args: list = [hid]
    if status:
        sql += " AND formc_status=$2"
        args.append(status)
        sql += f" ORDER BY checkin_date DESC LIMIT ${len(args)+1} OFFSET ${len(args)+2}"
    else:
        sql += f" ORDER BY checkin_date DESC LIMIT ${len(args)+1} OFFSET ${len(args)+2}"
    args += [limit, offset]
    return await fetch(sql, *args)


async def update_booking_foreign_fields(booking_id: str, data: Dict) -> Optional[Dict]:
    """
    Update foreign-guest / FormC fields on a booking. Used when an operator
    edits the captured passport/visa details before filing.
    """
    allowed = [
        "is_foreign_guest","nationality","sex","date_of_birth",
        "passport_no","passport_place_of_issue","passport_issue_date","passport_expiry_date",
        "visa_no","visa_type","visa_issue_place","visa_issue_date","visa_expiry_date",
        "arrival_in_india_date","arrival_in_india_port","last_country_visited",
        "next_destination","purpose_of_visit","formc_remarks",
    ]
    fields, vals, i = [], [], 1
    for k, v in data.items():
        if k in allowed:
            if isinstance(v, str) and k in ("passport_no", "visa_no"):
                v = v.upper()
            fields.append(f"{k}=${i}"); vals.append(v); i += 1
    # If is_foreign_guest just flipped on and status is empty/NotRequired, set Pending.
    if "is_foreign_guest" in data:
        if data["is_foreign_guest"]:
            fields.append(f"formc_status=COALESCE(NULLIF(formc_status,''),'Pending')")
        else:
            fields.append(f"formc_status='NotRequired'")
    if not fields:
        return await get_booking_by_id(booking_id)
    vals.append(booking_id)
    return await fetchrow(
        f"UPDATE bookings SET {','.join(fields)} WHERE booking_id=${i} RETURNING *", *vals)


async def mark_formc_filed(booking_id: str, reference: str, filed_by: str,
                           hotel_id: int, payload: str = "") -> Optional[Dict]:
    """Operator clicks 'Mark Filed' after uploading the CSV to FRRO portal."""
    row = await fetchrow("""
        UPDATE bookings
        SET formc_status='Filed',
            formc_filed_at=NOW(),
            formc_reference=$2,
            formc_filed_by=$3
        WHERE booking_id=$1
        RETURNING *""", booking_id, reference[:80], filed_by[:100])
    await execute("""
        INSERT INTO formc_filings (hotel_id, booking_id, action, reference, filed_by, payload)
        VALUES ($1,$2,'filed',$3,$4,$5)""",
        hotel_id, booking_id, reference[:80], filed_by[:100], (payload or "")[:8000])
    return row


async def log_formc_event(hotel_id: int, booking_id: str, action: str,
                          filed_by: str = "", payload: str = "", notes: str = ""):
    await execute("""
        INSERT INTO formc_filings (hotel_id, booking_id, action, filed_by, payload, notes)
        VALUES ($1,$2,$3,$4,$5,$6)""",
        hotel_id, booking_id, action[:20], filed_by[:100],
        (payload or "")[:8000], (notes or "")[:1000])


async def count_formc_pending(hid: int) -> int:
    """Count of foreign-guest bookings still awaiting FormC filing."""
    v = await fetchval("""
        SELECT COUNT(*) FROM bookings
        WHERE hotel_id=$1 AND is_foreign_guest=TRUE AND formc_status='Pending'""", hid)
    return int(v or 0)

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
    """
    Insert a stay charge with GST split.

    The caller may either provide pre-computed CGST/SGST/IGST values, or just
    `tax_rate` + `is_inter_state` and let this function derive the split.
    Falls back to the legacy single `tax` column if no rate is supplied — so
    older callers (insert a flat-tax row from the dashboard) continue to work.
    """
    from datetime import date
    from services.gst import compute_split, hsn_for_service, split_existing_tax

    cd = d.get("charge_date") or date.today()
    amount = float(d.get("amount", 0))
    rate   = float(d.get("tax_rate", 0))
    inter  = d.get("is_inter_state")

    # If caller provided cgst/sgst/igst directly, trust them.
    cgst = d.get("cgst_amount")
    sgst = d.get("sgst_amount")
    igst = d.get("igst_amount")

    if cgst is None and sgst is None and igst is None:
        if rate:
            split = compute_split(amount, rate, inter_state=inter)
            cgst = split["cgst"]; sgst = split["sgst"]; igst = split["igst"]
            tax  = split["total_tax"]
            total = split["total"]
        else:
            # Legacy path — caller knows total only.
            tax  = float(d.get("tax", 0))
            cgst, sgst, igst = split_existing_tax(amount, tax, inter_state=bool(inter))
            total = float(d.get("total", amount + tax))
    else:
        cgst = float(cgst or 0); sgst = float(sgst or 0); igst = float(igst or 0)
        tax  = cgst + sgst + igst
        total = float(d.get("total", amount + tax))

    hsn = (d.get("hsn_code") or hsn_for_service(d.get("service_type", "")))[:10]

    await execute("""
        INSERT INTO stay_charges (booking_id,charge_date,service_type,description,amount,tax,total,
            payment_status,order_ref,hotel_id,
            hsn_code,tax_rate,cgst_amount,sgst_amount,igst_amount,is_inter_state)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
        d["booking_id"], cd, d["service_type"], d["description"],
        amount, tax, total,
        d.get("payment_status", "Pending"), d.get("order_ref"), d.get("hotel_id", 1),
        hsn, rate, cgst, sgst, igst, bool(inter or False))

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



# ══════════════════════════════════════════════════════════════════
# HOUSEKEEPING — room cleaning / inspection workflow
# ══════════════════════════════════════════════════════════════════
HK_STATUSES = ("dirty", "cleaning", "clean", "inspected", "maintenance")


async def list_housekeeping(hid: int) -> List[Dict]:
    """Snapshot of every room with its current housekeeping status."""
    return await fetch(
        """SELECT id, room_number, room_type, floor, status AS room_status,
                  COALESCE(housekeeping_status,'clean') AS housekeeping_status,
                  last_cleaned_by, last_cleaned_at
           FROM rooms WHERE hotel_id=$1 ORDER BY room_number""",
        hid,
    )


async def list_housekeeping_log(hid: int, limit: int = 200) -> List[Dict]:
    return await fetch(
        """SELECT id, room_number, status, cleaned_by, notes, created_at
           FROM housekeeping_log WHERE hotel_id=$1
           ORDER BY id DESC LIMIT $2""",
        hid, limit,
    )


async def set_housekeeping_status(
    hid: int, room_number: str, status: str,
    cleaned_by: str = "", notes: str = ""
):
    """
    Update a room's housekeeping_status and append to history. status must be
    one of HK_STATUSES — invalid values are rejected so the UI can't smuggle
    arbitrary strings into the DB.
    """
    if status not in HK_STATUSES:
        raise ValueError(f"invalid housekeeping status '{status}' (allowed: {HK_STATUSES})")
    await execute(
        """UPDATE rooms
           SET housekeeping_status=$1,
               last_cleaned_by = CASE WHEN $1 IN ('clean','inspected') THEN $2 ELSE last_cleaned_by END,
               last_cleaned_at = CASE WHEN $1 IN ('clean','inspected') THEN NOW() ELSE last_cleaned_at END,
               updated_at = NOW()
           WHERE room_number=$3 AND hotel_id=$4""",
        status, cleaned_by, room_number, hid,
    )
    await execute(
        """INSERT INTO housekeeping_log (hotel_id, room_number, status, cleaned_by, notes)
           VALUES ($1, $2, $3, $4, $5)""",
        hid, room_number, status, cleaned_by, notes,
    )


# ══════════════════════════════════════════════════════════════════
# MAINTENANCE TICKETS
# ══════════════════════════════════════════════════════════════════
MT_STATUSES   = ("open", "in_progress", "resolved", "cancelled")
MT_PRIORITIES = ("low", "normal", "high", "urgent")


async def list_maintenance(hid: int, status: Optional[str] = None,
                            limit: int = 200) -> List[Dict]:
    if status:
        if status not in MT_STATUSES:
            return []
        return await fetch(
            """SELECT * FROM maintenance_tickets
               WHERE hotel_id=$1 AND status=$2
               ORDER BY CASE priority
                          WHEN 'urgent' THEN 1 WHEN 'high' THEN 2
                          WHEN 'normal' THEN 3 ELSE 4 END,
                        id DESC LIMIT $3""",
            hid, status, limit,
        )
    return await fetch(
        """SELECT * FROM maintenance_tickets
           WHERE hotel_id=$1
           ORDER BY CASE WHEN status IN ('open','in_progress') THEN 0 ELSE 1 END,
                    CASE priority
                      WHEN 'urgent' THEN 1 WHEN 'high' THEN 2
                      WHEN 'normal' THEN 3 ELSE 4 END,
                    id DESC LIMIT $2""",
        hid, limit,
    )


async def get_maintenance(tid: int) -> Optional[Dict]:
    return await fetchrow("SELECT * FROM maintenance_tickets WHERE id=$1", tid)


async def create_maintenance(hid: int, data: Dict) -> Dict:
    priority = data.get("priority", "normal")
    if priority not in MT_PRIORITIES:
        priority = "normal"
    row = await fetchrow(
        """INSERT INTO maintenance_tickets
           (hotel_id, room_number, title, description, priority,
            status, assigned_to, reported_by)
           VALUES ($1,$2,$3,$4,$5,'open',$6,$7) RETURNING *""",
        hid,
        (data.get("room_number") or "").strip(),
        (data.get("title") or "").strip()[:200],
        data.get("description") or "",
        priority,
        (data.get("assigned_to") or "").strip(),
        (data.get("reported_by") or "").strip(),
    )
    return dict(row) if row else {}


async def update_maintenance(tid: int, data: Dict) -> Optional[Dict]:
    """Update title/description/priority/status/assigned_to/resolution."""
    allowed = ("title", "description", "priority", "status",
               "assigned_to", "resolution")
    fields, vals, i = [], [], 1
    for k, v in data.items():
        if k not in allowed:
            continue
        if k == "priority" and v not in MT_PRIORITIES:
            continue
        if k == "status" and v not in MT_STATUSES:
            continue
        fields.append(f"{k}=${i}")
        vals.append(v)
        i += 1
    if not fields:
        return await get_maintenance(tid)
    fields.append("updated_at=NOW()")
    if data.get("status") == "resolved":
        fields.append("resolved_at=NOW()")
    vals.append(tid)
    return await fetchrow(
        f"UPDATE maintenance_tickets SET {','.join(fields)} WHERE id=${i} RETURNING *",
        *vals,
    )


async def delete_maintenance(tid: int):
    """Soft delete via status='cancelled' — preserves history."""
    await execute(
        "UPDATE maintenance_tickets SET status='cancelled', updated_at=NOW() WHERE id=$1",
        tid,
    )


# ══════════════════════════════════════════════════════════════════
# RETURNING GUEST LOOKUP (auto-fill registration)
# ══════════════════════════════════════════════════════════════════
async def lookup_returning_guest(hid: int, phone: str) -> Optional[Dict]:
    """
    If this phone has stayed at this hotel before, return the most recent
    booking summary so the registration page can auto-fill name + ID type.
    Never returns sensitive ID numbers / photo URLs — UI fills them fresh.
    """
    if not phone or len(phone) < 10:
        return None
    row = await fetchrow(
        """SELECT guest_name, id_proof_type,
                  COUNT(*)        OVER () AS total_visits,
                  MAX(checkin_date) OVER () AS last_visit
           FROM bookings
           WHERE guest_phone=$1 AND hotel_id=$2
           ORDER BY created_at DESC LIMIT 1""",
        phone, hid,
    )
    if not row:
        return None
    return {
        "found": True,
        "name": row["guest_name"] or "",
        "id_proof_type": row["id_proof_type"] or "",
        "total_visits": int(row["total_visits"] or 0),
        "last_visit": row["last_visit"].isoformat() if row.get("last_visit") else "",
    }




# ══════════════════════════════════════════════════════════════════
# REAL FOOD / RESTAURANT MODULE  (replaces menu_url placeholder)
# ══════════════════════════════════════════════════════════════════
import json as _json

FOOD_ORDER_STATUSES = ("Placed", "Preparing", "Ready", "Delivered", "Cancelled")


# ── Menu items (food) ─────────────────────────────────────────────
async def list_food_items(hid: int, available_only: bool = False) -> List[Dict]:
    """All food items for a hotel, ordered for menu display.

    `available_only=True` is what the guest-facing endpoints use; the admin
    UI passes False so unavailable items are still editable.
    """
    where = "hotel_id=$1"
    if available_only:
        where += " AND is_available=TRUE"
    return await fetch(
        f"SELECT * FROM hotel_food_items WHERE {where} "
        f"ORDER BY category, sort_order, name", hid,
    )


async def get_food_item(item_id: int, hid: int) -> Optional[Dict]:
    return await fetchrow(
        "SELECT * FROM hotel_food_items WHERE id=$1 AND hotel_id=$2",
        item_id, hid,
    )


async def create_food_item(hid: int, data: Dict) -> Dict:
    row = await fetchrow(
        """INSERT INTO hotel_food_items
           (hotel_id, category, name, description, price, image_url, type,
            is_available, is_bestseller, spice_level, serving_hours, sort_order)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *""",
        hid,
        (data.get("category") or "Other").strip()[:80],
        (data.get("name") or "").strip()[:150],
        data.get("description") or "",
        float(data.get("price", 0)),
        data.get("image_url") or "",
        (data.get("type") or "veg").strip().lower()[:20],
        bool(data.get("is_available", True)),
        bool(data.get("is_bestseller", False)),
        (data.get("spice_level") or "").strip()[:20],
        (data.get("serving_hours") or "").strip()[:50],
        int(data.get("sort_order", 0)),
    )
    return dict(row) if row else {}


async def update_food_item(item_id: int, hid: int, data: Dict) -> Optional[Dict]:
    allowed = ("category", "name", "description", "price", "image_url",
               "type", "is_available", "is_bestseller", "spice_level",
               "serving_hours", "sort_order")
    fields, vals, i = [], [], 1
    for k, v in (data or {}).items():
        if k in allowed:
            fields.append(f"{k}=${i}")
            vals.append(v)
            i += 1
    if not fields:
        return await get_food_item(item_id, hid)
    fields.append("updated_at=NOW()")
    vals += [item_id, hid]
    return await fetchrow(
        f"UPDATE hotel_food_items SET {','.join(fields)} "
        f"WHERE id=${i} AND hotel_id=${i+1} RETURNING *",
        *vals,
    )


async def delete_food_item(item_id: int, hid: int):
    """Hard delete is fine here — food items aren't referenced from order rows
    by id (we snapshot name/price into the order at the time of placement)."""
    await execute(
        "DELETE FROM hotel_food_items WHERE id=$1 AND hotel_id=$2",
        item_id, hid,
    )


async def list_food_categories(hid: int) -> List[str]:
    """Distinct categories used by this hotel's food items, ordered by usage."""
    rows = await fetch(
        """SELECT category, COUNT(*) AS n FROM hotel_food_items
           WHERE hotel_id=$1 GROUP BY category ORDER BY n DESC, category""",
        hid,
    )
    return [r["category"] for r in rows]


# ── Food orders ───────────────────────────────────────────────────
async def list_food_orders(hid: int, status: Optional[str] = None,
                            limit: int = 100) -> List[Dict]:
    if status:
        return await fetch(
            "SELECT * FROM hotel_food_orders WHERE hotel_id=$1 AND status=$2 "
            "ORDER BY id DESC LIMIT $3", hid, status, limit,
        )
    return await fetch(
        """SELECT * FROM hotel_food_orders WHERE hotel_id=$1
           ORDER BY CASE WHEN status IN ('Placed','Preparing','Ready') THEN 0 ELSE 1 END,
                    id DESC LIMIT $2""",
        hid, limit,
    )


async def get_food_order(order_id: int, hid: int) -> Optional[Dict]:
    return await fetchrow(
        "SELECT * FROM hotel_food_orders WHERE id=$1 AND hotel_id=$2",
        order_id, hid,
    )


async def create_food_order(hid: int, data: Dict) -> Dict:
    """
    Create a food order AND mirror it as a stay_charge so the existing bill /
    revenue flows naturally include food spend without extra plumbing.

    Caller passes: booking_id, room_number, guest_phone, guest_name, items
    where items is [{food_item_id, qty}]. We re-resolve each item's price from
    the DB (server-authoritative) and snapshot {name, price, qty} into items_json.
    """
    booking_id  = (data.get("booking_id") or "").strip()
    room_number = (data.get("room_number") or "").strip()
    guest_phone = (data.get("guest_phone") or "").strip()
    guest_name  = (data.get("guest_name") or "").strip()
    notes       = (data.get("notes") or "").strip()
    raw_items   = data.get("items") or []

    if not raw_items:
        raise ValueError("items is required")

    # Resolve and snapshot each item from the DB
    snapshot: list[dict] = []
    item_ids = [int(i.get("food_item_id")) for i in raw_items
                 if i.get("food_item_id") is not None]
    if not item_ids:
        raise ValueError("each item must have food_item_id")

    rows = await fetch(
        "SELECT id,name,price,is_available FROM hotel_food_items "
        "WHERE id=ANY($1::int[]) AND hotel_id=$2",
        item_ids, hid,
    )
    by_id = {int(r["id"]): r for r in rows}
    for it in raw_items:
        fid = int(it.get("food_item_id"))
        qty = int(it.get("qty", 1))
        if qty <= 0:
            continue
        row = by_id.get(fid)
        if not row:
            raise ValueError(f"food item {fid} not found")
        if not row["is_available"]:
            raise ValueError(f"'{row['name']}' is unavailable")
        snapshot.append({
            "food_item_id": fid,
            "name":         row["name"],
            "price":        float(row["price"] or 0),
            "qty":          qty,
        })

    if not snapshot:
        raise ValueError("no valid items")

    subtotal = round(sum(s["price"] * s["qty"] for s in snapshot), 2)
    tax      = 0.0   # future: pull GST per item; food rules vary by hotel
    total    = round(subtotal + tax, 2)

    # 1. Insert the food order itself
    order = await fetchrow(
        """INSERT INTO hotel_food_orders
           (hotel_id, booking_id, room_number, guest_phone, guest_name,
            items_json, subtotal, tax, total, notes, status)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,'Placed') RETURNING *""",
        hid, booking_id, room_number, guest_phone, guest_name,
        _json.dumps(snapshot), subtotal, tax, total, notes,
    )
    order_id = order["id"]

    # 2. Mirror to stay_charges so the bill picks it up. Skipped for walk-in
    #    food orders that aren't tied to a booking (rare, but supported).
    charge_id = None
    if booking_id:
        descr = ", ".join(f"{s['qty']}x {s['name']}" for s in snapshot)[:200]
        await insert_stay_charge({
            "booking_id":     booking_id,
            "service_type":   "Food",
            "description":    descr or f"Food order #{order_id}",
            "amount":         total,
            "tax":            tax,
            "total":          total,
            "payment_status": "Pending",
            "order_ref":      f"FOOD#{order_id}",
            "hotel_id":       hid,
        })
        charge_id = await fetchval(
            "SELECT id FROM stay_charges WHERE order_ref=$1 LIMIT 1",
            f"FOOD#{order_id}",
        )
        if charge_id:
            await execute(
                "UPDATE hotel_food_orders SET stay_charge_id=$1 WHERE id=$2",
                charge_id, order_id,
            )

    out = dict(order)
    out["items"] = snapshot   # return parsed for convenience
    out["stay_charge_id"] = charge_id
    return out


async def update_food_order_status(order_id: int, hid: int, status: str) -> Optional[Dict]:
    if status not in FOOD_ORDER_STATUSES:
        raise ValueError(f"invalid status '{status}' (allowed: {FOOD_ORDER_STATUSES})")
    fields = ["status=$1", "updated_at=NOW()"]
    vals: list = [status]
    if status == "Delivered":
        fields.append("delivered_at=NOW()")
    if status == "Cancelled":
        # Soft-cancel the linked charge so it disappears from the bill total
        order = await get_food_order(order_id, hid)
        if order and order.get("stay_charge_id"):
            await execute(
                "UPDATE stay_charges SET payment_status='Cancelled' "
                "WHERE id=$1 AND payment_status='Pending'",
                order["stay_charge_id"],
            )
    vals += [order_id, hid]
    return await fetchrow(
        f"UPDATE hotel_food_orders SET {','.join(fields)} "
        f"WHERE id=${len(vals)-1} AND hotel_id=${len(vals)} RETURNING *",
        *vals,
    )


async def get_active_food_orders_for_room(hid: int, room: str) -> List[Dict]:
    """Used by the bot when a guest asks about their food order status."""
    return await fetch(
        """SELECT * FROM hotel_food_orders
           WHERE hotel_id=$1 AND room_number=$2
             AND status IN ('Placed','Preparing','Ready')
           ORDER BY id DESC""",
        hid, room,
    )



# ══════════════════════════════════════════════════════════════════
# CHANNEL MANAGER (OTA aggregator: AxisRooms / STAAH / RateGain)
# One adapter integration here = MMT + Goibibo + Booking.com + Agoda
# + Expedia + 50 more, because the aggregator already speaks to all of
# them. We only have to be a clean source of inventory and a faithful
# sink for OTA reservations. Everything below is per-hotel.
# ══════════════════════════════════════════════════════════════════
async def get_channel_account(hotel_id: int) -> Optional[Dict]:
    return await fetchrow(
        "SELECT * FROM channel_accounts WHERE hotel_id=$1", hotel_id
    )


# Fields the operator can set/update via the dashboard. We deliberately
# keep this list small — secret material is only writable through the
# explicit "connect" path, not via a generic update.
_CHANNEL_ACCOUNT_BASIC = [
    "provider", "base_url", "hotel_code",
    "push_inventory_minutes", "pull_bookings_minutes",
    "inventory_horizon_days", "dry_run", "is_active",
]
_CHANNEL_ACCOUNT_SECRETS = [
    "api_key", "api_secret", "username", "password", "webhook_secret",
]


async def upsert_channel_account(hotel_id: int, data: Dict) -> Dict:
    """
    Insert or update the channel-manager account row for this hotel.
    Secrets are only overwritten when explicitly provided in `data` — so a
    dashboard "save" that only changes intervals never wipes the API key.
    """
    existing = await get_channel_account(hotel_id)
    if not existing:
        row = await fetchrow("""
            INSERT INTO channel_accounts
                (hotel_id, provider, base_url, hotel_code,
                 api_key, api_secret, username, password, webhook_secret,
                 push_inventory_minutes, pull_bookings_minutes,
                 inventory_horizon_days, dry_run, is_active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            RETURNING *""",
            hotel_id,
            (data.get("provider") or "axisrooms")[:40],
            (data.get("base_url") or "")[:300],
            (data.get("hotel_code") or "")[:80],
            (data.get("api_key") or "")[:300],
            (data.get("api_secret") or "")[:300],
            (data.get("username") or "")[:120],
            (data.get("password") or "")[:300],
            (data.get("webhook_secret") or "")[:200],
            int(data.get("push_inventory_minutes") or 30),
            int(data.get("pull_bookings_minutes") or 15),
            int(data.get("inventory_horizon_days") or 60),
            bool(data.get("dry_run", True)),
            bool(data.get("is_active", False)),
        )
        return dict(row)

    fields, vals, i = [], [], 1
    for k in _CHANNEL_ACCOUNT_BASIC:
        if k in data:
            fields.append(f"{k}=${i}")
            vals.append(data[k])
            i += 1
    for k in _CHANNEL_ACCOUNT_SECRETS:
        # Only overwrite if a non-empty value is provided.
        if k in data and (data[k] or "") != "":
            fields.append(f"{k}=${i}")
            vals.append(str(data[k])[:300])
            i += 1
    if not fields:
        return existing
    fields.append("updated_at=NOW()")
    vals.append(hotel_id)
    return await fetchrow(
        f"UPDATE channel_accounts SET {','.join(fields)} "
        f"WHERE hotel_id=${i} RETURNING *",
        *vals,
    )


async def disconnect_channel_account(hotel_id: int):
    """Mark account inactive and wipe credentials. Keeps row for audit."""
    await execute(
        """UPDATE channel_accounts
           SET is_active=FALSE, api_key='', api_secret='', password='',
               webhook_secret='', last_error='', updated_at=NOW()
           WHERE hotel_id=$1""",
        hotel_id,
    )


async def update_channel_account_status(hotel_id: int, *,
                                        last_inventory_push_at=None,
                                        last_booking_pull_at=None,
                                        last_error: Optional[str] = None):
    """Updated by the sync workers; never by a user-facing API."""
    fields, vals, i = [], [], 1
    if last_inventory_push_at is not None:
        fields.append(f"last_inventory_push_at=${i}"); vals.append(last_inventory_push_at); i += 1
    if last_booking_pull_at is not None:
        fields.append(f"last_booking_pull_at=${i}"); vals.append(last_booking_pull_at); i += 1
    if last_error is not None:
        fields.append(f"last_error=${i}"); vals.append(str(last_error)[:2000]); i += 1
    if not fields:
        return
    fields.append("updated_at=NOW()")
    vals.append(hotel_id)
    await execute(
        f"UPDATE channel_accounts SET {','.join(fields)} WHERE hotel_id=${i}",
        *vals,
    )


# ── Room-type and rate-plan mapping ─────────────────────────────────
async def list_channel_room_types(hotel_id: int) -> List[Dict]:
    return await fetch(
        "SELECT * FROM channel_room_types WHERE hotel_id=$1 ORDER BY provider_code",
        hotel_id,
    )


async def upsert_channel_room_type(hotel_id: int, data: Dict) -> Dict:
    row = await fetchrow("""
        INSERT INTO channel_room_types
            (hotel_id, room_type, provider_code, provider_label,
             total_units, base_rate, is_active)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT (hotel_id, provider_code) DO UPDATE
           SET room_type=$2, provider_label=$4, total_units=$5,
               base_rate=$6, is_active=$7, updated_at=NOW()
        RETURNING *""",
        hotel_id,
        (data.get("room_type") or "")[:80],
        (data.get("provider_code") or "")[:80],
        (data.get("provider_label") or "")[:150],
        int(data.get("total_units") or 0),
        float(data.get("base_rate") or 0),
        bool(data.get("is_active", True)),
    )
    return dict(row)


async def delete_channel_room_type(hotel_id: int, rt_id: int):
    await execute(
        "DELETE FROM channel_room_types WHERE id=$1 AND hotel_id=$2",
        rt_id, hotel_id,
    )


async def list_channel_rate_plans(hotel_id: int) -> List[Dict]:
    return await fetch(
        "SELECT * FROM channel_rate_plans WHERE hotel_id=$1 ORDER BY room_type_id, code",
        hotel_id,
    )


async def upsert_channel_rate_plan(hotel_id: int, data: Dict) -> Dict:
    rp_id = data.get("id")
    if rp_id:
        row = await fetchrow("""
            UPDATE channel_rate_plans
            SET room_type_id=$2, code=$3, name=$4, meal_plan=$5,
                rate_modifier=$6, is_default=$7, is_active=$8,
                updated_at=NOW()
            WHERE id=$1 AND hotel_id=$9 RETURNING *""",
            int(rp_id),
            int(data.get("room_type_id") or 0),
            (data.get("code") or "BAR")[:40],
            (data.get("name") or "")[:150],
            (data.get("meal_plan") or "EP")[:20],
            float(data.get("rate_modifier") or 1.0),
            bool(data.get("is_default", False)),
            bool(data.get("is_active", True)),
            hotel_id,
        )
    else:
        row = await fetchrow("""
            INSERT INTO channel_rate_plans
                (hotel_id, room_type_id, code, name, meal_plan,
                 rate_modifier, is_default, is_active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
            hotel_id,
            int(data.get("room_type_id") or 0),
            (data.get("code") or "BAR")[:40],
            (data.get("name") or "")[:150],
            (data.get("meal_plan") or "EP")[:20],
            float(data.get("rate_modifier") or 1.0),
            bool(data.get("is_default", False)),
            bool(data.get("is_active", True)),
        )
    return dict(row) if row else {}


async def delete_channel_rate_plan(hotel_id: int, rp_id: int):
    await execute(
        "DELETE FROM channel_rate_plans WHERE id=$1 AND hotel_id=$2",
        rp_id, hotel_id,
    )


# ── Inventory snapshots (what we last pushed for date X) ────────────
async def upsert_channel_inventory(hotel_id: int, room_type_id: int,
                                   stay_date, available_units: int,
                                   base_rate: float, stop_sell: bool,
                                   status: str = "pending"):
    await execute("""
        INSERT INTO channel_inventory
            (hotel_id, room_type_id, stay_date, available_units, base_rate,
             stop_sell, last_pushed_at, last_push_status)
        VALUES ($1,$2,$3,$4,$5,$6, NOW(), $7)
        ON CONFLICT (hotel_id, room_type_id, stay_date) DO UPDATE
           SET available_units=$4, base_rate=$5, stop_sell=$6,
               last_pushed_at=NOW(), last_push_status=$7, updated_at=NOW()""",
        hotel_id, room_type_id, stay_date,
        int(available_units), float(base_rate),
        bool(stop_sell), status[:20],
    )


async def list_channel_inventory(hotel_id: int, days: int = 30) -> List[Dict]:
    return await fetch(
        """SELECT ci.*, rt.provider_code, rt.room_type, rt.provider_label
           FROM channel_inventory ci
           LEFT JOIN channel_room_types rt ON rt.id=ci.room_type_id
           WHERE ci.hotel_id=$1 AND ci.stay_date >= CURRENT_DATE
             AND ci.stay_date <  CURRENT_DATE + ($2::int || ' days')::interval
           ORDER BY ci.stay_date, rt.provider_code""",
        hotel_id, max(1, min(int(days), 365)),
    )


# ── Sync log ────────────────────────────────────────────────────────
async def insert_sync_log(hotel_id: int, provider: str, operation: str, *,
                          status: str = "ok", records: int = 0,
                          duration_ms: int = 0, error: str = "",
                          payload_summary: str = ""):
    await execute("""
        INSERT INTO channel_sync_log
            (hotel_id, provider, operation, status, records,
             duration_ms, error, payload_summary)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
        hotel_id, (provider or "")[:40], (operation or "")[:40],
        (status or "ok")[:20], int(records or 0), int(duration_ms or 0),
        (error or "")[:4000], (payload_summary or "")[:4000],
    )


async def list_sync_log(hotel_id: int, limit: int = 100) -> List[Dict]:
    return await fetch(
        "SELECT * FROM channel_sync_log WHERE hotel_id=$1 "
        "ORDER BY id DESC LIMIT $2",
        hotel_id, max(1, min(int(limit), 500)),
    )


# ── Channel reservations (OTA bookings pulled from aggregator) ──────
async def upsert_channel_booking(hotel_id: int, provider: str,
                                 provider_ref: str, data: Dict) -> Dict:
    """
    Idempotent upsert keyed on (hotel_id, provider, provider_ref). Used by
    pull_bookings to absorb the same reservation multiple times safely
    (e.g. after the OTA sends a modification).
    """
    row = await fetchrow("""
        INSERT INTO channel_bookings
            (hotel_id, provider, provider_ref, ota_source, ota_booking_id,
             guest_name, guest_email, guest_phone, guest_country,
             checkin_date, checkout_date, nights, guests,
             room_type_code, rate_plan_code, room_count,
             currency, total_amount, ota_commission, payment_terms,
             status, special_requests, raw_payload)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                $14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
        ON CONFLICT (hotel_id, provider, provider_ref) DO UPDATE
           SET ota_source=EXCLUDED.ota_source,
               ota_booking_id=EXCLUDED.ota_booking_id,
               guest_name=EXCLUDED.guest_name,
               guest_email=EXCLUDED.guest_email,
               guest_phone=EXCLUDED.guest_phone,
               guest_country=EXCLUDED.guest_country,
               checkin_date=EXCLUDED.checkin_date,
               checkout_date=EXCLUDED.checkout_date,
               nights=EXCLUDED.nights,
               guests=EXCLUDED.guests,
               room_type_code=EXCLUDED.room_type_code,
               rate_plan_code=EXCLUDED.rate_plan_code,
               room_count=EXCLUDED.room_count,
               currency=EXCLUDED.currency,
               total_amount=EXCLUDED.total_amount,
               ota_commission=EXCLUDED.ota_commission,
               payment_terms=EXCLUDED.payment_terms,
               status=CASE
                   WHEN channel_bookings.status='ingested' AND EXCLUDED.status<>'cancelled'
                   THEN channel_bookings.status
                   ELSE EXCLUDED.status END,
               special_requests=EXCLUDED.special_requests,
               raw_payload=EXCLUDED.raw_payload,
               updated_at=NOW(),
               cancelled_at=CASE WHEN EXCLUDED.status='cancelled' THEN NOW() ELSE channel_bookings.cancelled_at END
        RETURNING *""",
        hotel_id, (provider or "")[:40], provider_ref[:120],
        (data.get("ota_source") or "")[:60],
        (data.get("ota_booking_id") or "")[:120],
        (data.get("guest_name") or "")[:200],
        (data.get("guest_email") or "")[:200],
        (data.get("guest_phone") or "")[:40],
        (data.get("guest_country") or "")[:80],
        data.get("checkin_date"), data.get("checkout_date"),
        int(data.get("nights") or 0), int(data.get("guests") or 1),
        (data.get("room_type_code") or "")[:80],
        (data.get("rate_plan_code") or "")[:40],
        int(data.get("room_count") or 1),
        (data.get("currency") or "INR")[:8],
        float(data.get("total_amount") or 0),
        float(data.get("ota_commission") or 0),
        (data.get("payment_terms") or "pay_at_hotel")[:40],
        (data.get("status") or "new")[:20],
        (data.get("special_requests") or "")[:2000],
        (data.get("raw_payload") or "")[:8000],
    )
    return dict(row)


async def list_channel_bookings(hotel_id: int, status: Optional[str] = None,
                                limit: int = 200) -> List[Dict]:
    if status:
        return await fetch(
            """SELECT * FROM channel_bookings
               WHERE hotel_id=$1 AND status=$2
               ORDER BY checkin_date DESC, id DESC LIMIT $3""",
            hotel_id, status, max(1, min(int(limit), 1000)),
        )
    return await fetch(
        """SELECT * FROM channel_bookings WHERE hotel_id=$1
           ORDER BY checkin_date DESC, id DESC LIMIT $2""",
        hotel_id, max(1, min(int(limit), 1000)),
    )


async def get_channel_booking(hotel_id: int, provider: str,
                              provider_ref: str) -> Optional[Dict]:
    return await fetchrow(
        "SELECT * FROM channel_bookings "
        "WHERE hotel_id=$1 AND provider=$2 AND provider_ref=$3",
        hotel_id, provider, provider_ref,
    )


async def mark_channel_booking_ingested(channel_booking_id: int,
                                        hotel_id: int,
                                        booking_id: str):
    await execute(
        """UPDATE channel_bookings
           SET status='ingested', mapped_booking_id=$3,
               ingested_at=NOW(), updated_at=NOW()
           WHERE id=$1 AND hotel_id=$2""",
        channel_booking_id, hotel_id, booking_id[:40],
    )


async def aggregate_inventory_for_dates(hotel_id: int, dates: List) -> List[Dict]:
    """
    For each (room_type, date) compute available_units = total_units - reserved.
    `reserved` = active bookings whose stay overlaps the date AND whose room
    has the matching room_type. Used by the inventory pusher.
    """
    if not dates:
        return []
    return await fetch(
        """
        WITH dates AS (
            SELECT unnest($2::date[]) AS d
        ),
        types AS (
            SELECT id, hotel_id, room_type, provider_code, total_units, base_rate
            FROM channel_room_types
            WHERE hotel_id=$1 AND is_active=TRUE
        ),
        reserved AS (
            SELECT r.room_type, d.d AS stay_date, COUNT(*) AS held
            FROM dates d
            JOIN bookings b ON b.hotel_id=$1
                            AND b.status IN ('Active','Reserved','CheckedIn','CheckedOut')
                            AND b.checkin_date  <= d.d
                            AND b.checkout_date >  d.d
            JOIN rooms r ON r.room_number=b.room_number AND r.hotel_id=$1
            GROUP BY r.room_type, d.d
        )
        SELECT t.id AS room_type_id, t.room_type, t.provider_code,
               t.total_units, t.base_rate, d.d AS stay_date,
               GREATEST(t.total_units - COALESCE(r.held,0), 0) AS available_units
        FROM types t CROSS JOIN dates d
        LEFT JOIN reserved r
               ON r.room_type=t.room_type AND r.stay_date=d.d
        ORDER BY d.d, t.provider_code
        """,
        hotel_id, dates,
    )
