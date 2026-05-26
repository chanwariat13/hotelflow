# 🏨 HotelFlow v2 — Coolify Deployment Guide

> ⚠️ **Security:** never commit real DB / Redis / Evolution / Razorpay secrets
> to this file or to the repository. Use the placeholders below and inject the
> real values via Coolify environment variables (or whatever secret store you
> use). If you previously committed real secrets, rotate them immediately.

## Your Server Details
- VPS IP:        `<your-vps-ip>`
- PostgreSQL DB: `<your-db-name>`
- Redis:         default (db 0)

---

## Step 1 — Run DB Migration First

Connect to your PostgreSQL and run the migration:

```bash
psql -h <your-vps-ip> -U postgres -d <your-db-name> -f migration.sql
```

Or paste the migration.sql content directly into your DB client.

**This is safe to run on your existing DB** — it only adds new columns and tables.

---

## Step 2 — Deploy on Coolify

### Option A: Deploy via GitHub (Recommended)

1. Push this project to a GitHub repo (private)
2. In Coolify → **New Resource** → **Application**
3. Select **GitHub** → choose your repo
4. Build Pack: **Nixpacks** or **Dockerfile**
5. Set **Start Command**:
   ```
   python main.py
   ```
6. Set **Port**: `8000`

### Option B: Deploy via Docker (Alternative)

The repo already ships a hardened `Dockerfile` and `.dockerignore` at the
project root. Just point Coolify (or any other Docker host) at it.

---

## Step 3 — Set Environment Variables in Coolify

In your Coolify app → **Environment Variables**, add these. Use **runtime**
variables for secrets (uncheck "Is Build Variable?") so they are never baked
into the Docker image.

```
DB_HOST=<your-vps-ip>
DB_PORT=5432
DB_NAME=<your-db-name>
DB_USER=postgres
DB_PASS=<your-db-pass>

REDIS_HOST=<your-vps-ip>
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASS=<your-redis-pass>

EVOLUTION_API_URL=http://<your-vps-ip>:8080
EVOLUTION_API_KEY=<your-evolution-api-key>

HOST=0.0.0.0
PORT=8000
BASE_URL=https://your-domain.example.com

GOTENBERG_URL=http://<your-vps-ip>:3000

# Master admin login. ADMIN_PASSWORD_RESET=1 forces a one-time reset on boot;
# unset it after logging in.
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<a-strong-passphrase>

# CORS: comma-separated list of frontends allowed to call the API. Use the
# real domain(s) in production — '*' is only safe for local dev.
CORS_ORIGINS=https://your-domain.example.com

# Cookie hardening. Set to 1 when serving over HTTPS (recommended in prod).
COOKIE_SECURE=1

SECRET_KEY=<generate-64-random-chars>
```

> **Generate SECRET_KEY / ADMIN_PASSWORD:**
> ```bash
> python3 -c "import secrets; print(secrets.token_hex(32))"
> ```

> **If you ever leak any of the secrets above (e.g. by committing them):**
> 1. Rotate the credential at the source (DB, Redis, Razorpay, Evolution).
> 2. Update the env var in Coolify and redeploy.
> 3. Purge the old value from git history (`git filter-repo` or BFG) and
>    force-push, then notify any collaborators with cached clones.

---

## Step 4 — Point Evolution API Webhooks

For each hotel's WhatsApp instance in Evolution API:

```
Webhook URL: https://your-domain.example.com/webhook/whatsapp
```

---

## Step 5 — First Login

| URL                                | Credentials                                          |
|------------------------------------|------------------------------------------------------|
| `https://your-domain.example.com/login` | Username: `admin`  Password: whatever you set in `ADMIN_PASSWORD` |

⚠️ **Change password immediately** after first login at `/admin` → Change Password.

---

## All URLs After Deploy

| What                      | URL                                                 |
|---------------------------|-----------------------------------------------------|
| Health Check              | `https://your-domain.example.com/`                  |
| **Master Admin**          | `https://your-domain.example.com/admin`             |
| **Hotel Owner Login**     | `https://your-domain.example.com/login`             |
| Hotel Dashboard           | `https://your-domain.example.com/hotel/{slug}`      |
| Guest Registration        | `https://your-domain.example.com/register/{slug}`   |
| Guest Menu/Services       | `https://your-domain.example.com/menu/{slug}`       |
| Guest Bill View           | `https://your-domain.example.com/bill/{slug}`       |
| WhatsApp Webhook          | `https://your-domain.example.com/webhook/whatsapp`  |

---

## Adding a New Hotel Client (30 seconds)

1. Go to `https://your-domain.example.com/admin`
2. Click **➕ Add New Hotel**
3. Fill all 8 sections
4. Click **Create Hotel Client**
5. Add their WhatsApp Evolution API instance
6. Point webhook → Done ✅

**Zero code change. Zero redeploy. Ever.**

---

## Changing Staff WhatsApp Number (Instant)

1. Go to hotel dashboard → Staff & Users
2. Click **Edit** next to the user
3. Change WhatsApp Number
4. Click **Save**

Bot recognizes new number **immediately**. No restart needed.

---

## Updating Room Price (Instant)

1. Hotel Dashboard → Services & Prices
2. Scroll to Room Rates
3. Enter new rate → Click **Update**

Done. Next booking uses new rate.

---

## Updating Service Price (Instant)

1. Hotel Dashboard → Services & Prices
2. Click **Edit** on any service
3. Change price → **Save**

Guest menu updates immediately.

---

## Logs (Coolify)

In Coolify → your app → **Logs** tab → live logs.

---

## Backup

Backup your PostgreSQL regularly:

```bash
pg_dump -h <your-vps-ip> -U postgres <your-db-name> > backup_$(date +%Y%m%d).sql
```
