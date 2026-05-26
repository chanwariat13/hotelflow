-- ═══════════════════════════════════════════════════════════════════
-- HotelFlow v2 — Complete Migration
-- Safe, idempotent. Run on existing DB anytime.
-- psql -U postgres -d your_db -f migration.sql
-- ═══════════════════════════════════════════════════════════════════

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

-- ── 6. Seed master admin (change password after first login!) ─────
-- Default: admin / admin123  ← CHANGE THIS IMMEDIATELY
-- Hash format expected by services/auth.py:verify_password():
--   sha256(salt || password)  stored as  "<hex_hash>:<salt>"
-- Below: salt = 'defaultsalt', password = 'admin123'
INSERT INTO admin_users (username, password_hash, name)
VALUES (
    'admin',
    '86908ee61ea114e1728cf0613c977cb70a5f2a73b56e4db9990a4fab8561ae92:defaultsalt',
    'Super Admin'
) ON CONFLICT (username) DO NOTHING;

-- ── 7. Seed your existing hotel (safe, skips if exists) ───────────
INSERT INTO hotels (hotel_name, slug, instance_name, primary_color, secondary_color,
    emergency_number, wifi_name, wifi_password, payment_mode,
    staff_phones, report_phones, checkout_hour, late_charge_flat, gotenberg_url, is_active)
VALUES ('Grand Stay Hotel', 'grand-stay', 'Propertybaajar',
    '#c8a84b', '#1a2942', '917340226277', 'HotelWifi', 'wifi@123',
    'razorpay', ARRAY['917340226277','917413049091'],
    ARRAY['917340226277','917413049091'], 11, 500, 'http://localhost:3000', TRUE)
ON CONFLICT (slug) DO NOTHING;

SELECT 'Migration complete ✓' AS result;
SELECT 'IMPORTANT: Change admin password at /admin after first login!' AS warning;
