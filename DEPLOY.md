# 🏨 HotelFlow v2 — Coolify Deployment Guide

## Your Server Details
- VPS IP: 187.77.184.114
- PostgreSQL DB: Test-DB
- Redis: default (db 0)

---

## Step 1 — Run DB Migration First

Connect to your PostgreSQL and run the migration:

```bash
psql -h 187.77.184.114 -U postgres -d Test-DB -f migration.sql
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

Create a `Dockerfile` in the project root:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "main.py"]
```

---

## Step 3 — Set Environment Variables in Coolify

In your Coolify app → **Environment Variables**, add these:

```
DB_HOST=187.77.184.114
DB_PORT=5432
DB_NAME=Test-DB
DB_USER=postgres
DB_PASS=PHGfaOJVQIzp9CV6g0zL3EYmrbtxtf9g0p90AeyvuJLaDmU3EFifC2hvXTbqSB26

REDIS_HOST=187.77.184.114
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASS=WmLLwM6kBnaWdnPfqfPhdJR940QQRLxUo0jNBS0zolMGFOlD6JMwSkNUmVp9LXCN

EVOLUTION_API_URL=http://187.77.184.114:8080
EVOLUTION_API_KEY=your_evolution_api_key_here

HOST=0.0.0.0
PORT=8000
BASE_URL=https://your-domain.coolify.io

GOTENBERG_URL=http://187.77.184.114:3000

SECRET_KEY=generate_64_random_chars_here
```

> **Generate SECRET_KEY:**
> ```bash
> python3 -c "import secrets; print(secrets.token_hex(32))"
> ```

---

## Step 4 — Point Evolution API Webhooks

For each hotel's WhatsApp instance in Evolution API:

```
Webhook URL: https://your-app.coolify.io/webhook/whatsapp
```

---

## Step 5 — First Login

| URL | Credentials |
|-----|-------------|
| `https://your-app/login` | Username: `admin` Password: `admin123` |

⚠️ **Change password immediately** after first login at `/admin` → Change Password.

---

## All URLs After Deploy

| What | URL |
|------|-----|
| Health Check | `https://your-app/` |
| **Your Master Admin** | `https://your-app/admin` |
| **Hotel Owner Login** | `https://your-app/login` |
| Hotel Dashboard | `https://your-app/hotel/{slug}` |
| Guest Registration | `https://your-app/register/{slug}` |
| Guest Menu/Services | `https://your-app/menu/{slug}` |
| Guest Bill View | `https://your-app/bill/{slug}` |
| WhatsApp Webhook | `https://your-app/webhook/whatsapp` |

---

## Adding a New Hotel Client (30 seconds)

1. Go to `https://your-app/admin`
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
pg_dump -h 187.77.184.114 -U postgres Test-DB > backup_$(date +%Y%m%d).sql
```
