-- ═══════════════════════════════════════════════════════════════════
-- HotelFlow v2 — Complete Migration
-- Safe, idempotent. Run on existing DB anytime.
-- psql -U postgres -d your_db -f migration.sql
-- ═══════════════════════════════════════════════════════════════════

-- ── 0. Base tables (required for fresh deployments) ──────────────
-- These eight tables are the historical "core" of HotelFlow. On an
-- already-running database they exist and `CREATE TABLE IF NOT EXISTS`
-- is a no-op. On a fresh database, the section that follows
-- ("1. Add hotel_id to all existing tables") used to fail with
-- `ERROR: relation "bookings" does not exist`, leaving the deploy
-- broken. Defining them here closes that gap.
--
-- Columns are kept minimal: every column is exercised by the runtime
-- code (services/database.py / routes/*.py). Newer compliance and
-- reporting columns are added later in this file via
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS so they are also reachable
-- by long-lived deployments.

CREATE TABLE IF NOT EXISTS bookings (
    id                  SERIAL PRIMARY KEY,
    booking_id          VARCHAR(40)  UNIQUE NOT NULL,
    room_number         VARCHAR(20)  DEFAULT '',
    guest_name          VARCHAR(200) DEFAULT '',
    guest_phone         VARCHAR(40)  DEFAULT '',
    guest_email         VARCHAR(200) DEFAULT '',
    guest_count         INTEGER      DEFAULT 1,
    alternate_phone     VARCHAR(40)  DEFAULT '',
    checkin_date        TIMESTAMP,
    checkout_date       TIMESTAMP,
    status              VARCHAR(20)  DEFAULT 'Active',  -- Active/Rejected/CheckedOut/Cancelled
    payment_mode        VARCHAR(40)  DEFAULT 'Pay at checkout',
    id_proof_type       VARCHAR(40)  DEFAULT '',
    id_proof_number     VARCHAR(80)  DEFAULT '',
    id_proof_photo      TEXT         DEFAULT '',
    id_proof_photo_back TEXT         DEFAULT '',
    total_paid          NUMERIC(12,2) DEFAULT 0,
    hotel_id            INTEGER      DEFAULT 1,
    created_at          TIMESTAMP    DEFAULT NOW(),
    updated_at          TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rooms (
    id           SERIAL PRIMARY KEY,
    room_number  VARCHAR(20)  UNIQUE NOT NULL,
    room_type    VARCHAR(80)  DEFAULT 'Standard',
    floor        INTEGER      DEFAULT 1,
    room_rate    NUMERIC(10,2) DEFAULT 0,
    qr_secret    VARCHAR(60)  DEFAULT '',
    status       VARCHAR(20)  DEFAULT 'Vacant',         -- Vacant/Occupied/OutOfOrder
    hotel_id     INTEGER      DEFAULT 1,
    created_at   TIMESTAMP    DEFAULT NOW(),
    updated_at   TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stay_charges (
    id              SERIAL PRIMARY KEY,
    booking_id      VARCHAR(40)  NOT NULL,
    charge_date     DATE         DEFAULT CURRENT_DATE,
    service_type    VARCHAR(60)  DEFAULT '',
    description     TEXT         DEFAULT '',
    amount          NUMERIC(10,2) DEFAULT 0,
    tax             NUMERIC(10,2) DEFAULT 0,
    total           NUMERIC(10,2) DEFAULT 0,
    payment_status  VARCHAR(20)  DEFAULT 'Pending',     -- Pending/Paid/Refunded
    payment_method  VARCHAR(40)  DEFAULT '',
    order_ref       VARCHAR(200) DEFAULT '',
    hotel_id        INTEGER      DEFAULT 1,
    created_at      TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_logs (
    id              SERIAL PRIMARY KEY,
    booking_id      VARCHAR(40)  DEFAULT '',
    guest_phone     VARCHAR(40)  DEFAULT '',
    room_number     VARCHAR(20)  DEFAULT '',
    guest_name      VARCHAR(200) DEFAULT '',
    amount          NUMERIC(12,2) DEFAULT 0,
    payment_method  VARCHAR(40)  DEFAULT '',
    reference       VARCHAR(200) DEFAULT '',
    payment_date    TIMESTAMP    DEFAULT NOW(),
    hotel_id        INTEGER      DEFAULT 1,
    created_at      TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS services (
    id            SERIAL PRIMARY KEY,
    service_name  VARCHAR(150) NOT NULL,
    category      VARCHAR(80)  DEFAULT 'Other',
    price         NUMERIC(10,2) DEFAULT 0,
    department    VARCHAR(80)  DEFAULT 'Reception',
    description   TEXT         DEFAULT '',
    is_active     BOOLEAN      DEFAULT TRUE,
    hotel_id      INTEGER      DEFAULT 1,
    created_at    TIMESTAMP    DEFAULT NOW(),
    updated_at    TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS staff_departments (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(80)  NOT NULL,
    display_name    VARCHAR(150) DEFAULT '',
    whatsapp_number VARCHAR(40)  DEFAULT '',
    is_active       BOOLEAN      DEFAULT TRUE,
    hotel_id        INTEGER      DEFAULT 1,
    created_at      TIMESTAMP    DEFAULT NOW(),
    updated_at      TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS service_requests (
    id             SERIAL PRIMARY KEY,
    request_id     VARCHAR(40)  UNIQUE NOT NULL,
    booking_id     VARCHAR(40)  DEFAULT '',
    service_name   VARCHAR(150) DEFAULT '',
    category       VARCHAR(80)  DEFAULT '',
    department     VARCHAR(80)  DEFAULT '',
    status         VARCHAR(20)  DEFAULT 'Pending',     -- Pending/Completed/Cancelled
    priority       VARCHAR(20)  DEFAULT 'Normal',
    guest_note     TEXT         DEFAULT '',
    charge_amount  NUMERIC(10,2) DEFAULT 0,
    requested_at   TIMESTAMP    DEFAULT NOW(),
    completed_at   TIMESTAMP,
    hotel_id       INTEGER      DEFAULT 1,
    created_at     TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS additional_booking_guests (
    id                  SERIAL PRIMARY KEY,
    booking_id          VARCHAR(40)  NOT NULL,
    guest_name          VARCHAR(200) DEFAULT '',
    id_proof_type       VARCHAR(40)  DEFAULT '',
    id_proof_number     VARCHAR(80)  DEFAULT '',
    id_proof_photo      TEXT         DEFAULT '',
    id_proof_photo_back TEXT         DEFAULT '',
    hotel_id            INTEGER      DEFAULT 1,
    created_at          TIMESTAMP    DEFAULT NOW()
);

-- ── 1. Add hotel_id to all existing tables (safe) ────────────────
ALTER TABLE bookings                  ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;
ALTER TABLE rooms                     ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;
ALTER TABLE stay_charges              ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;
ALTER TABLE payment_logs              ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;
ALTER TABLE services                  ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;
ALTER TABLE staff_departments         ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;
ALTER TABLE service_requests          ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;
ALTER TABLE additional_booking_guests ADD COLUMN IF NOT EXISTS hotel_id INTEGER DEFAULT 1;

-- ── 2. Hotels table (full branding + config) ──────────────────────
CREATE TABLE IF NOT EXISTS hotels (
    id                        SERIAL PRIMARY KEY,
    hotel_name                VARCHAR(100) NOT NULL,
    slug                      VARCHAR(60)  NOT NULL UNIQUE,
    instance_name             VARCHAR(100) NOT NULL UNIQUE,

    -- Branding
    logo_url                  TEXT         DEFAULT '',
    primary_color             VARCHAR(20)  DEFAULT '#c8a84b',
    secondary_color           VARCHAR(20)  DEFAULT '#1a2942',
    background_color          VARCHAR(20)  DEFAULT '#0d1117',
    button_color              VARCHAR(20)  DEFAULT '#c8a84b',
    text_color                VARCHAR(20)  DEFAULT '#ffffff',
    font_choice               VARCHAR(50)  DEFAULT 'Outfit',

    -- Info shown on guest pages
    tagline                   VARCHAR(200) DEFAULT 'Your home away from home',
    address                   TEXT         DEFAULT '',
    city                      VARCHAR(100) DEFAULT '',
    google_maps_url           TEXT         DEFAULT '',
    hotel_email               VARCHAR(150) DEFAULT '',
    hotel_whatsapp            VARCHAR(20)  DEFAULT '',
    check_in_time             VARCHAR(20)  DEFAULT '2:00 PM',
    checkout_time_display     VARCHAR(20)  DEFAULT '11:00 AM',
    welcome_message           TEXT         DEFAULT 'Welcome! We are delighted to have you.',
    footer_text               TEXT         DEFAULT '',

    -- URLs
    google_review_url         TEXT         DEFAULT '',
    menu_url                  TEXT         DEFAULT '',

    -- Contact
    emergency_number          VARCHAR(20)  DEFAULT '',
    wifi_name                 VARCHAR(100) DEFAULT '',
    wifi_password             VARCHAR(100) DEFAULT '',

    -- Payment
    payment_mode              VARCHAR(20)  DEFAULT 'razorpay',
    razorpay_key_id           VARCHAR(100) DEFAULT '',
    razorpay_secret           VARCHAR(200) DEFAULT '',
    upi_id                    VARCHAR(100) DEFAULT '',
    upi_display_name          VARCHAR(100) DEFAULT '',

    -- Infrastructure
    gotenberg_url             TEXT         DEFAULT 'http://localhost:3000',
    cloudinary_cloud_name     VARCHAR(100) DEFAULT '',
    cloudinary_upload_preset  VARCHAR(100) DEFAULT '',

    -- Staff phones (for backward compat — bot now uses hotel_users)
    staff_phones              TEXT[]       DEFAULT '{}',
    report_phones             TEXT[]       DEFAULT '{}',

    -- Checkout & Charges
    checkout_hour             INTEGER      DEFAULT 11,
    late_charge_flat          NUMERIC(10,2) DEFAULT 500,
    late_fee_per_hour         NUMERIC(10,2) DEFAULT 0,
    max_late_fee              NUMERIC(10,2) DEFAULT 1500,
    svc_open_hour             INTEGER      DEFAULT 7,
    svc_close_hour            INTEGER      DEFAULT 23,

    -- Scheduler times (IST, per hotel)
    sched_daily_report_hour   INTEGER      DEFAULT 7,
    sched_monthly_report_hour INTEGER      DEFAULT 9,
    sched_reminder1_hour      INTEGER      DEFAULT 21,
    sched_reminder2_hour      INTEGER      DEFAULT 10,
    sched_reminder2_min       INTEGER      DEFAULT 30,
    sched_late_alert_hour     INTEGER      DEFAULT 11,
    sched_late_alert_min      INTEGER      DEFAULT 30,
    sched_auto_charge_hour    INTEGER      DEFAULT 12,
    sched_auto_charge_min     INTEGER      DEFAULT 0,

    is_active                 BOOLEAN      DEFAULT TRUE,
    created_at                TIMESTAMP    DEFAULT NOW()
);

-- ── 3. Hotel users — Owners, Managers, Staff ──────────────────────
-- This is the KEY table. Bot reads this for WhatsApp number recognition.
-- Change a number here → bot recognizes it instantly. No restart.
CREATE TABLE IF NOT EXISTS hotel_users (
    id                   SERIAL PRIMARY KEY,
    hotel_id             INTEGER      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    name                 VARCHAR(100) NOT NULL,
    whatsapp_number      VARCHAR(20)  DEFAULT '',   -- used by bot to identify staff
    role                 VARCHAR(20)  NOT NULL DEFAULT 'staff',  -- owner / manager / staff
    username             VARCHAR(60)  NOT NULL,
    password_hash        VARCHAR(300) NOT NULL DEFAULT '',

    -- Granular permissions (role sets defaults, can be customized per user)
    can_approve_checkin  BOOLEAN DEFAULT FALSE,
    can_reject_checkin   BOOLEAN DEFAULT FALSE,
    can_checkout_guest   BOOLEAN DEFAULT FALSE,
    can_manage_services  BOOLEAN DEFAULT FALSE,
    can_manage_rooms     BOOLEAN DEFAULT FALSE,
    can_view_revenue     BOOLEAN DEFAULT FALSE,
    can_view_id_proofs   BOOLEAN DEFAULT FALSE,
    can_broadcast        BOOLEAN DEFAULT FALSE,
    can_manage_staff     BOOLEAN DEFAULT FALSE,  -- only owner
    can_edit_hotel       BOOLEAN DEFAULT FALSE,  -- only owner

    is_active            BOOLEAN DEFAULT TRUE,
    created_at           TIMESTAMP DEFAULT NOW(),
    UNIQUE(hotel_id, username)
);

-- ── 4. Master admin user (you) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(60) NOT NULL UNIQUE,
    password_hash VARCHAR(300) NOT NULL,
    name          VARCHAR(100) DEFAULT 'Admin',
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMP DEFAULT NOW()
);

-- ── 5. Indexes ────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_hotels_slug        ON hotels(slug);
CREATE INDEX IF NOT EXISTS idx_hotels_instance    ON hotels(instance_name);
CREATE INDEX IF NOT EXISTS idx_hotel_users_hotel  ON hotel_users(hotel_id);
CREATE INDEX IF NOT EXISTS idx_hotel_users_wa     ON hotel_users(whatsapp_number);
CREATE INDEX IF NOT EXISTS idx_hotel_users_role   ON hotel_users(hotel_id, role);
CREATE INDEX IF NOT EXISTS idx_bookings_hotel     ON bookings(hotel_id);
CREATE INDEX IF NOT EXISTS idx_bookings_phone     ON bookings(guest_phone);
CREATE INDEX IF NOT EXISTS idx_bookings_status    ON bookings(status);
CREATE INDEX IF NOT EXISTS idx_bookings_room      ON bookings(room_number, status);
CREATE INDEX IF NOT EXISTS idx_charges_booking    ON stay_charges(booking_id);
CREATE INDEX IF NOT EXISTS idx_rooms_hotel        ON rooms(hotel_id, status);
CREATE INDEX IF NOT EXISTS idx_services_hotel     ON services(hotel_id);
CREATE INDEX IF NOT EXISTS idx_sr_status          ON service_requests(status);

-- ── 5b. Real food / restaurant module ─────────────────────────────
-- Replaces the legacy `hotels.menu_url` placeholder. Each hotel manages its
-- own room-service menu here. Food orders auto-link to stay_charges so they
-- show up on the bill and in revenue reports without extra plumbing.
CREATE TABLE IF NOT EXISTS hotel_food_items (
    id              SERIAL PRIMARY KEY,
    hotel_id        INTEGER       NOT NULL,
    category        VARCHAR(80)   DEFAULT 'Other',
    name            VARCHAR(150)  NOT NULL,
    description     TEXT          DEFAULT '',
    price           NUMERIC(10,2) NOT NULL DEFAULT 0,
    image_url       TEXT          DEFAULT '',
    type            VARCHAR(20)   DEFAULT 'veg',     -- veg / nonveg / egg
    is_available    BOOLEAN       DEFAULT TRUE,
    is_bestseller   BOOLEAN       DEFAULT FALSE,
    spice_level     VARCHAR(20)   DEFAULT '',
    serving_hours   VARCHAR(50)   DEFAULT '',
    sort_order      INTEGER       DEFAULT 0,
    created_at      TIMESTAMP     DEFAULT NOW(),
    updated_at      TIMESTAMP     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_food_items_hotel ON hotel_food_items(hotel_id, is_available);
CREATE INDEX IF NOT EXISTS idx_food_items_cat   ON hotel_food_items(hotel_id, category);

CREATE TABLE IF NOT EXISTS hotel_food_orders (
    id              SERIAL PRIMARY KEY,
    hotel_id        INTEGER       NOT NULL,
    booking_id      VARCHAR(40)   DEFAULT '',
    room_number     VARCHAR(20)   DEFAULT '',
    guest_phone     VARCHAR(20)   DEFAULT '',
    guest_name      VARCHAR(150)  DEFAULT '',
    items_json      JSONB         NOT NULL DEFAULT '[]'::jsonb,
    subtotal        NUMERIC(10,2) DEFAULT 0,
    tax             NUMERIC(10,2) DEFAULT 0,
    total           NUMERIC(10,2) DEFAULT 0,
    notes           TEXT          DEFAULT '',
    status          VARCHAR(20)   DEFAULT 'Placed',
    stay_charge_id  INTEGER,
    created_at      TIMESTAMP     DEFAULT NOW(),
    updated_at      TIMESTAMP     DEFAULT NOW(),
    delivered_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_food_orders_hotel   ON hotel_food_orders(hotel_id, status);
CREATE INDEX IF NOT EXISTS idx_food_orders_booking ON hotel_food_orders(booking_id);
CREATE INDEX IF NOT EXISTS idx_food_orders_room    ON hotel_food_orders(room_number, status);

-- ── 6. Master admin: seeded automatically by services/database.ensure_admin_seed() ──
-- The previous version of this migration shipped a hard-coded password hash for
-- the user 'admin' which the application's verify_password() couldn't actually
-- verify (the seed format was incompatible with the runtime hash format). The
-- application now seeds the admin user from the ADMIN_USERNAME / ADMIN_PASSWORD
-- env vars on first boot via ensure_admin_seed(), so this SQL no longer needs
-- to insert a row. See services/database.py for details.
-- (Intentionally no INSERT INTO admin_users here — set ADMIN_PASSWORD in env.)

-- ── 7. Seed your existing hotel ───────────────────────────────────
-- The previous version of this file shipped real WhatsApp numbers and an
-- instance name belonging to a specific deployment, leaking PII into every
-- clone of the repo. Hotels are now created at runtime via the master admin
-- dashboard (POST /api/admin/hotels) — see services/database.create_hotel().
-- If you want to seed a placeholder hotel for local development, copy the
-- example below, fill in your own values, and run it manually:
--
-- INSERT INTO hotels (hotel_name, slug, instance_name, primary_color,
--                    secondary_color, payment_mode, checkout_hour,
--                    late_charge_flat, gotenberg_url, is_active)
-- VALUES ('Example Hotel', 'example-hotel', 'example-instance',
--         '#c8a84b', '#1a2942', 'razorpay', 11, 500,
--         'http://localhost:3000', TRUE)
-- ON CONFLICT (slug) DO NOTHING;

SELECT 'Migration complete ✓' AS result;
SELECT 'IMPORTANT: Change admin password at /admin after first login!' AS warning;



-- ── 8. Night audit + KPI reports ─────────────────────────────────
-- One row per (hotel_id, audit_date). Auto-populated by the nightly
-- scheduler; can also be back-filled manually from the dashboard.
ALTER TABLE hotels   ADD COLUMN IF NOT EXISTS sched_night_audit_hour INTEGER DEFAULT 2;
ALTER TABLE hotels   ADD COLUMN IF NOT EXISTS sched_night_audit_min  INTEGER DEFAULT 0;
ALTER TABLE hotels   ADD COLUMN IF NOT EXISTS auto_post_room_rent    BOOLEAN DEFAULT TRUE;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS ota_source             VARCHAR(60) DEFAULT '';

CREATE TABLE IF NOT EXISTS night_audits (
    id                    SERIAL PRIMARY KEY,
    hotel_id              INTEGER NOT NULL,
    audit_date            DATE    NOT NULL,
    status                VARCHAR(20)   DEFAULT 'completed',
    total_rooms           INTEGER       DEFAULT 0,
    occupied_rooms        INTEGER       DEFAULT 0,
    available_rooms       INTEGER       DEFAULT 0,
    room_nights_sold      INTEGER       DEFAULT 0,
    room_revenue          NUMERIC(12,2) DEFAULT 0,
    food_revenue          NUMERIC(12,2) DEFAULT 0,
    service_revenue       NUMERIC(12,2) DEFAULT 0,
    other_revenue         NUMERIC(12,2) DEFAULT 0,
    total_revenue         NUMERIC(12,2) DEFAULT 0,
    tax_collected         NUMERIC(12,2) DEFAULT 0,
    cash_collected        NUMERIC(12,2) DEFAULT 0,
    online_collected      NUMERIC(12,2) DEFAULT 0,
    pending_revenue       NUMERIC(12,2) DEFAULT 0,
    adr                   NUMERIC(10,2) DEFAULT 0,
    revpar                NUMERIC(10,2) DEFAULT 0,
    trevpar               NUMERIC(10,2) DEFAULT 0,
    occupancy_pct         NUMERIC(6,2)  DEFAULT 0,
    checkins_count        INTEGER       DEFAULT 0,
    checkouts_count       INTEGER       DEFAULT 0,
    no_shows_count        INTEGER       DEFAULT 0,
    rent_postings_added   INTEGER       DEFAULT 0,
    rent_postings_skipped INTEGER       DEFAULT 0,
    errors                TEXT          DEFAULT '',
    notes                 TEXT          DEFAULT '',
    run_at                TIMESTAMP     DEFAULT NOW(),
    run_by                VARCHAR(100)  DEFAULT 'scheduler',
    UNIQUE(hotel_id, audit_date)
);
CREATE INDEX IF NOT EXISTS idx_night_audits_hotel_date ON night_audits(hotel_id, audit_date DESC);

-- ════════════════════════════════════════════════════════════════
-- Channel Manager (OTA aggregator) integration
-- One adapter integration here = MMT + Goibibo + Booking.com + Agoda
-- + Expedia + 50 more, because the aggregator already speaks to all
-- of them. Provider is per-hotel (axisrooms / staah / rategain / ...).
-- ════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS channel_accounts (
    id              SERIAL PRIMARY KEY,
    hotel_id        INTEGER      NOT NULL UNIQUE,
    provider        VARCHAR(40)  NOT NULL DEFAULT 'axisrooms',
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
    dry_run         BOOLEAN      DEFAULT TRUE,
    is_active       BOOLEAN      DEFAULT FALSE,
    last_inventory_push_at TIMESTAMP,
    last_booking_pull_at   TIMESTAMP,
    last_error      TEXT         DEFAULT '',
    created_at      TIMESTAMP    DEFAULT NOW(),
    updated_at      TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_channel_accounts_hotel ON channel_accounts(hotel_id);

CREATE TABLE IF NOT EXISTS channel_room_types (
    id                  SERIAL PRIMARY KEY,
    hotel_id            INTEGER      NOT NULL,
    room_type           VARCHAR(80)  NOT NULL,
    provider_code       VARCHAR(80)  NOT NULL,
    provider_label      VARCHAR(150) DEFAULT '',
    total_units         INTEGER      DEFAULT 0,
    base_rate           NUMERIC(10,2) DEFAULT 0,
    is_active           BOOLEAN      DEFAULT TRUE,
    created_at          TIMESTAMP    DEFAULT NOW(),
    updated_at          TIMESTAMP    DEFAULT NOW(),
    UNIQUE(hotel_id, provider_code)
);
CREATE INDEX IF NOT EXISTS idx_channel_room_types_hotel ON channel_room_types(hotel_id);

CREATE TABLE IF NOT EXISTS channel_rate_plans (
    id              SERIAL PRIMARY KEY,
    hotel_id        INTEGER      NOT NULL,
    room_type_id    INTEGER      NOT NULL,
    code            VARCHAR(40)  NOT NULL,
    name            VARCHAR(150) DEFAULT '',
    meal_plan       VARCHAR(20)  DEFAULT 'EP',
    rate_modifier   NUMERIC(6,3) DEFAULT 1.000,
    is_default      BOOLEAN      DEFAULT FALSE,
    is_active       BOOLEAN      DEFAULT TRUE,
    created_at      TIMESTAMP    DEFAULT NOW(),
    updated_at      TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_channel_rate_plans_hotel ON channel_rate_plans(hotel_id);
CREATE INDEX IF NOT EXISTS idx_channel_rate_plans_rt    ON channel_rate_plans(room_type_id);

CREATE TABLE IF NOT EXISTS channel_inventory (
    id              SERIAL PRIMARY KEY,
    hotel_id        INTEGER      NOT NULL,
    room_type_id    INTEGER      NOT NULL,
    stay_date       DATE         NOT NULL,
    available_units INTEGER      DEFAULT 0,
    base_rate       NUMERIC(10,2) DEFAULT 0,
    stop_sell       BOOLEAN      DEFAULT FALSE,
    last_pushed_at  TIMESTAMP,
    last_push_status VARCHAR(20) DEFAULT 'pending',
    created_at      TIMESTAMP    DEFAULT NOW(),
    updated_at      TIMESTAMP    DEFAULT NOW(),
    UNIQUE(hotel_id, room_type_id, stay_date)
);
CREATE INDEX IF NOT EXISTS idx_channel_inv_hotel_date ON channel_inventory(hotel_id, stay_date);

CREATE TABLE IF NOT EXISTS channel_sync_log (
    id              SERIAL PRIMARY KEY,
    hotel_id        INTEGER      NOT NULL,
    provider        VARCHAR(40)  DEFAULT '',
    operation       VARCHAR(40)  NOT NULL,
    status          VARCHAR(20)  DEFAULT 'ok',
    records         INTEGER      DEFAULT 0,
    duration_ms     INTEGER      DEFAULT 0,
    error           TEXT         DEFAULT '',
    payload_summary TEXT         DEFAULT '',
    created_at      TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_channel_sync_log_hotel ON channel_sync_log(hotel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS channel_bookings (
    id                  SERIAL PRIMARY KEY,
    hotel_id            INTEGER      NOT NULL,
    provider            VARCHAR(40)  DEFAULT '',
    provider_ref        VARCHAR(120) NOT NULL,
    ota_source          VARCHAR(60)  DEFAULT '',
    ota_booking_id      VARCHAR(120) DEFAULT '',
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
    payment_terms       VARCHAR(40)  DEFAULT 'pay_at_hotel',
    status              VARCHAR(20)  DEFAULT 'new',
    special_requests    TEXT         DEFAULT '',
    raw_payload         TEXT         DEFAULT '',
    mapped_booking_id   VARCHAR(40)  DEFAULT '',
    received_at         TIMESTAMP    DEFAULT NOW(),
    ingested_at         TIMESTAMP,
    cancelled_at        TIMESTAMP,
    created_at          TIMESTAMP    DEFAULT NOW(),
    updated_at          TIMESTAMP    DEFAULT NOW(),
    UNIQUE(hotel_id, provider, provider_ref)
);
CREATE INDEX IF NOT EXISTS idx_channel_bookings_hotel ON channel_bookings(hotel_id, status);
CREATE INDEX IF NOT EXISTS idx_channel_bookings_dates ON channel_bookings(hotel_id, checkin_date);
CREATE INDEX IF NOT EXISTS idx_channel_bookings_phone ON channel_bookings(guest_phone);

ALTER TABLE bookings ADD COLUMN IF NOT EXISTS ota_source  VARCHAR(60)  DEFAULT '';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS channel_ref VARCHAR(120) DEFAULT '';
