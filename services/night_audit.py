"""
services/night_audit.py
─────────────────────────────────────────────────────────────────────
Nightly close + KPI report for a hotel.

What "night audit" means in this codebase
─────────────────────────────────────────
Hotels above the ~10-room mark cannot run on guesswork. Every night
they need a single batch that:

1. Posts the room-rent charge for every in-house guest's stay-night
   that hasn't been posted yet (this is the #1 thing front-desk staff
   forget when they're busy).
2. Computes the day's KPIs:
       Occupancy %  = occupied_rooms / total_rooms
       ADR          = room_revenue   / occupied_rooms
       RevPAR       = room_revenue   / total_rooms
       TRevPAR      = total_revenue  / total_rooms
3. Reconciles cash + online collected against pending.
4. Persists one row per (hotel_id, audit_date) so the dashboard can
   plot trends and the owner can prove revenue numbers to the bank.
5. WhatsApps the snapshot to owners + managers.

The job is idempotent — running it twice for the same date does NOT
double-post charges, because we look up `service_type='Room Rent'` rows
keyed on (booking_id, charge_date) before inserting.

Public surface
──────────────
    compute_metrics(hotel_id, audit_date)         -> dict   (no writes)
    auto_post_room_rent(hotel_id, audit_date)     -> dict
    run_night_audit(hotel_id, audit_date, actor)  -> dict   (full pipeline)
    list_night_audits(hotel_id, from_, to_)       -> list[dict]
    get_kpi_series(hotel_id, from_, to_)          -> list[dict]
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from services import database as db
from services.audit import audit

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────
def _yesterday() -> date:
    """IST yesterday — but we don't pin to a tz here because the
    hotels.sched_night_audit_hour cron is already IST-anchored. Caller
    can override audit_date for back-fills."""
    return date.today() - timedelta(days=1)


def _f(v) -> float:
    try:
        return round(float(v or 0), 2)
    except Exception:
        return 0.0


def _safe_div(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 2) if denominator else 0.0


# ─── Compute (read-only) ──────────────────────────────────────────
async def compute_metrics(hotel_id: int, audit_date: date) -> Dict[str, Any]:
    """
    Pure compute: pull occupancy / movement / revenue numbers for the
    given business day. No writes. Safe to call from the dashboard.
    """
    # Total rooms — from rooms table (current; assumes inventory is stable
    # day to day, which holds for our customers).
    total_rooms = int(await db.fetchval(
        "SELECT COUNT(*) FROM rooms WHERE hotel_id=$1", hotel_id) or 0)

    # Occupied rooms on the audit date = active stays whose interval
    # straddles that date (checkin_date <= audit_date < checkout_date).
    occupied_rooms = int(await db.fetchval(
        """SELECT COUNT(DISTINCT room_number) FROM bookings
           WHERE hotel_id=$1
             AND status IN ('Active','CheckedOut','Reserved','CheckedIn')
             AND checkin_date  <= $2
             AND checkout_date >  $2""",
        hotel_id, audit_date) or 0)

    room_nights_sold = int(await db.fetchval(
        """SELECT COUNT(*) FROM bookings
           WHERE hotel_id=$1
             AND status IN ('Active','CheckedOut','Reserved','CheckedIn')
             AND checkin_date  <= $2
             AND checkout_date >  $2""",
        hotel_id, audit_date) or 0)

    # Movement — guests who checked in or out on the audit date.
    checkins_count = int(await db.fetchval(
        """SELECT COUNT(*) FROM bookings
           WHERE hotel_id=$1 AND checkin_date::date=$2""",
        hotel_id, audit_date) or 0)

    checkouts_count = int(await db.fetchval(
        """SELECT COUNT(*) FROM bookings
           WHERE hotel_id=$1 AND status='CheckedOut'
             AND updated_at::date=$2""",
        hotel_id, audit_date) or 0)

    # Revenue split for the audit date — by service_type.
    rev_row = await db.fetchrow(
        """SELECT
             COALESCE(SUM(sc.total)                                                   ,0) AS total_revenue,
             COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type='Room Rent')         ,0) AS room_revenue,
             COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type='Food')              ,0) AS food_revenue,
             COALESCE(SUM(sc.total) FILTER(WHERE sc.service_type NOT IN ('Room Rent','Food')),0) AS service_revenue,
             COALESCE(SUM(sc.tax)                                                     ,0) AS tax_collected,
             COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Pending')         ,0) AS pending_revenue,
             COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Paid' AND sc.payment_method ILIKE 'cash'),0) AS cash_collected,
             COALESCE(SUM(sc.total) FILTER(WHERE sc.payment_status='Paid' AND sc.payment_method NOT ILIKE 'cash'),0) AS online_collected
           FROM stay_charges sc
           JOIN bookings    b ON b.booking_id = sc.booking_id
           WHERE b.hotel_id = $1 AND sc.charge_date::date = $2""",
        hotel_id, audit_date) or {}

    room_revenue    = _f(rev_row.get("room_revenue"))
    food_revenue    = _f(rev_row.get("food_revenue"))
    service_revenue = _f(rev_row.get("service_revenue"))
    other_revenue   = 0.0
    total_revenue   = _f(rev_row.get("total_revenue"))
    tax_collected   = _f(rev_row.get("tax_collected"))
    cash_collected  = _f(rev_row.get("cash_collected"))
    online_collected = _f(rev_row.get("online_collected"))
    pending_revenue = _f(rev_row.get("pending_revenue"))

    available_rooms = max(total_rooms - occupied_rooms, 0)
    occupancy_pct   = _safe_div(occupied_rooms * 100.0, total_rooms)
    adr             = _safe_div(room_revenue, occupied_rooms)
    revpar          = _safe_div(room_revenue, total_rooms)
    trevpar         = _safe_div(total_revenue, total_rooms)

    return {
        "audit_date":          audit_date.isoformat(),
        "total_rooms":         total_rooms,
        "occupied_rooms":      occupied_rooms,
        "available_rooms":     available_rooms,
        "room_nights_sold":    room_nights_sold,
        "occupancy_pct":       occupancy_pct,
        "adr":                 adr,
        "revpar":              revpar,
        "trevpar":             trevpar,
        "room_revenue":        room_revenue,
        "food_revenue":        food_revenue,
        "service_revenue":     service_revenue,
        "other_revenue":       other_revenue,
        "total_revenue":       total_revenue,
        "tax_collected":       tax_collected,
        "cash_collected":      cash_collected,
        "online_collected":    online_collected,
        "pending_revenue":     pending_revenue,
        "checkins_count":      checkins_count,
        "checkouts_count":     checkouts_count,
        "no_shows_count":      0,   # bookings table has no 'NoShow' status today
    }


# ─── Auto-post room rent ──────────────────────────────────────────
async def auto_post_room_rent(hotel_id: int, audit_date: date) -> Dict[str, int]:
    """
    For every in-house booking on `audit_date`, ensure exactly one
    Room Rent charge exists with charge_date = audit_date. Idempotent.

    Returns: {"added": N, "skipped": M}
    """
    rows = await db.fetch(
        """SELECT b.booking_id, b.room_number,
                  COALESCE(r.room_rate,0) AS room_rate,
                  COALESCE(h.default_gst_rate, 12.00) AS gst_rate,
                  COALESCE(h.state_code,'') AS hotel_state,
                  COALESCE(b.guest_state_code,'') AS guest_state
           FROM bookings b
           JOIN hotels h ON h.id = b.hotel_id
           LEFT JOIN rooms r ON r.room_number = b.room_number AND r.hotel_id = b.hotel_id
           WHERE b.hotel_id = $1
             AND b.status IN ('Active','Reserved','CheckedIn')
             AND b.checkin_date  <= $2
             AND b.checkout_date >  $2""",
        hotel_id, audit_date)

    added = skipped = 0
    for r in rows:
        bid  = r["booking_id"]
        rate = float(r.get("room_rate") or 0)
        if rate <= 0:
            # Don't fabricate a charge with rate=0 — log and skip.
            skipped += 1
            continue

        # Idempotency: one Room Rent row per (booking, charge_date).
        exists = await db.fetchval(
            """SELECT 1 FROM stay_charges
               WHERE booking_id=$1 AND service_type='Room Rent' AND charge_date::date=$2
               LIMIT 1""", bid, audit_date)
        if exists:
            skipped += 1
            continue

        gst_rate     = float(r.get("gst_rate") or 0)
        hotel_state  = (r.get("hotel_state") or "").strip()
        guest_state  = (r.get("guest_state") or "").strip()
        is_inter     = bool(hotel_state and guest_state and hotel_state != guest_state)

        await db.insert_stay_charge({
            "booking_id":   bid,
            "charge_date":  audit_date,
            "service_type": "Room Rent",
            "description":  f"Room rent for {audit_date.isoformat()}",
            "amount":       rate,
            "tax_rate":     gst_rate,
            "is_inter_state": is_inter,
            "payment_status": "Pending",
            "hotel_id":     hotel_id,
            "order_ref":    f"NA-{audit_date.isoformat()}-{bid}",
        })
        added += 1

    return {"added": added, "skipped": skipped}


# ─── Persist + send ───────────────────────────────────────────────
async def _persist_audit(hotel_id: int, metrics: Dict[str, Any], *,
                          status: str, run_by: str, postings: Dict[str, int],
                          errors: str = "") -> Dict[str, Any]:
    row = await db.fetchrow("""
        INSERT INTO night_audits
            (hotel_id, audit_date, status,
             total_rooms, occupied_rooms, available_rooms, room_nights_sold,
             room_revenue, food_revenue, service_revenue, other_revenue,
             total_revenue, tax_collected, cash_collected, online_collected, pending_revenue,
             adr, revpar, trevpar, occupancy_pct,
             checkins_count, checkouts_count, no_shows_count,
             rent_postings_added, rent_postings_skipped, errors, run_by)
        VALUES ($1,$2,$3,
                $4,$5,$6,$7,
                $8,$9,$10,$11,
                $12,$13,$14,$15,$16,
                $17,$18,$19,$20,
                $21,$22,$23,
                $24,$25,$26,$27)
        ON CONFLICT (hotel_id, audit_date) DO UPDATE SET
            status                = EXCLUDED.status,
            total_rooms           = EXCLUDED.total_rooms,
            occupied_rooms        = EXCLUDED.occupied_rooms,
            available_rooms       = EXCLUDED.available_rooms,
            room_nights_sold      = EXCLUDED.room_nights_sold,
            room_revenue          = EXCLUDED.room_revenue,
            food_revenue          = EXCLUDED.food_revenue,
            service_revenue       = EXCLUDED.service_revenue,
            other_revenue         = EXCLUDED.other_revenue,
            total_revenue         = EXCLUDED.total_revenue,
            tax_collected         = EXCLUDED.tax_collected,
            cash_collected        = EXCLUDED.cash_collected,
            online_collected      = EXCLUDED.online_collected,
            pending_revenue       = EXCLUDED.pending_revenue,
            adr                   = EXCLUDED.adr,
            revpar                = EXCLUDED.revpar,
            trevpar               = EXCLUDED.trevpar,
            occupancy_pct         = EXCLUDED.occupancy_pct,
            checkins_count        = EXCLUDED.checkins_count,
            checkouts_count       = EXCLUDED.checkouts_count,
            no_shows_count        = EXCLUDED.no_shows_count,
            rent_postings_added   = night_audits.rent_postings_added + EXCLUDED.rent_postings_added,
            rent_postings_skipped = night_audits.rent_postings_skipped + EXCLUDED.rent_postings_skipped,
            errors                = EXCLUDED.errors,
            run_at                = NOW(),
            run_by                = EXCLUDED.run_by
        RETURNING *
    """,
        hotel_id, date.fromisoformat(metrics["audit_date"]), status,
        metrics["total_rooms"], metrics["occupied_rooms"],
        metrics["available_rooms"], metrics["room_nights_sold"],
        metrics["room_revenue"], metrics["food_revenue"],
        metrics["service_revenue"], metrics["other_revenue"],
        metrics["total_revenue"], metrics["tax_collected"],
        metrics["cash_collected"], metrics["online_collected"],
        metrics["pending_revenue"],
        metrics["adr"], metrics["revpar"], metrics["trevpar"],
        metrics["occupancy_pct"],
        metrics["checkins_count"], metrics["checkouts_count"],
        metrics["no_shows_count"],
        int(postings.get("added", 0)), int(postings.get("skipped", 0)),
        (errors or "")[:4000], (run_by or "scheduler")[:100],
    )
    return dict(row) if row else {}


async def _send_report_to_owners(hotel: Dict, metrics: Dict, postings: Dict):
    """WhatsApp snapshot to owners + managers. Failure never blocks the
    rest of the night audit — that's the whole point of nightly closing."""
    try:
        from services.whatsapp import send_to_phones
        phones = await db.get_staff_phones(hotel["id"], ["owner", "manager"])
        if not phones:
            return
        msg = (
            f"🌙 *NIGHT AUDIT — {hotel['hotel_name']}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 *{metrics['audit_date']}*\n\n"
            f"🏨 Occupancy: *{metrics['occupied_rooms']}/{metrics['total_rooms']}*"
            f"  ({metrics['occupancy_pct']}%)\n"
            f"📥 Check-ins: {metrics['checkins_count']}  "
            f"📤 Check-outs: {metrics['checkouts_count']}\n\n"
            f"💰 *KPI*\n"
            f"   ADR     ₹{metrics['adr']:.0f}\n"
            f"   RevPAR  ₹{metrics['revpar']:.0f}\n"
            f"   TRevPAR ₹{metrics['trevpar']:.0f}\n\n"
            f"📊 *REVENUE*\n"
            f"   Rooms    ₹{metrics['room_revenue']:.0f}\n"
            f"   Food     ₹{metrics['food_revenue']:.0f}\n"
            f"   Services ₹{metrics['service_revenue']:.0f}\n"
            f"   ━━━━━━━━━━━━━\n"
            f"   *Total   ₹{metrics['total_revenue']:.0f}*\n\n"
            f"💵 Cash:   ₹{metrics['cash_collected']:.0f}\n"
            f"🌐 Online: ₹{metrics['online_collected']:.0f}\n"
            f"⏳ Pending: ₹{metrics['pending_revenue']:.0f}\n\n"
            f"🧾 Auto-posted: *{postings.get('added',0)}* room-rent rows "
            f"({postings.get('skipped',0)} skipped)\n"
            f"━━━━━━━━━━━━━━━━━━\n_Night audit · HotelFlow_"
        )
        await send_to_phones(hotel["instance_name"], phones, msg)
    except Exception:
        logger.exception("night_audit: WhatsApp report failed (non-fatal)")


async def run_night_audit(hotel_id: int, audit_date: Optional[date] = None,
                          *, actor: str = "scheduler",
                          send_whatsapp: bool = True,
                          request=None) -> Dict[str, Any]:
    """
    Full pipeline. Safe to call repeatedly. Returns the persisted row.
    """
    audit_date = audit_date or _yesterday()
    hotel = await db.get_hotel_by_id(hotel_id)
    if not hotel:
        raise ValueError(f"hotel {hotel_id} not found")

    errors_log = []
    postings = {"added": 0, "skipped": 0}

    # 1. Auto-post rent BEFORE computing metrics, so the day's room
    # revenue picks up freshly-posted charges.
    if hotel.get("auto_post_room_rent", True):
        try:
            postings = await auto_post_room_rent(hotel_id, audit_date)
        except Exception as e:
            logger.exception("auto_post_room_rent failed for hotel %s", hotel_id)
            errors_log.append(f"auto_post_room_rent: {e}"[:500])

    # 2. Compute metrics
    metrics = await compute_metrics(hotel_id, audit_date)

    # 3. Persist
    status = "failed" if errors_log else "completed"
    row = await _persist_audit(
        hotel_id, metrics,
        status=status, run_by=actor, postings=postings,
        errors="\n".join(errors_log),
    )

    # 4. WhatsApp report (best-effort)
    if send_whatsapp and not errors_log:
        await _send_report_to_owners(hotel, metrics, postings)

    # 5. Audit log
    try:
        await audit(
            "night_audit.run",
            actor=actor, actor_role="system" if actor == "scheduler" else "owner",
            hotel_id=hotel_id, target=str(audit_date),
            payload={
                "metrics": metrics, "postings": postings,
                "status": status, "errors": errors_log,
            },
            request=request,
        )
    except Exception:
        pass

    return row or metrics


# ─── List + KPI series for the dashboard ──────────────────────────
async def list_night_audits(hotel_id: int,
                             from_date: Optional[date] = None,
                             to_date: Optional[date] = None,
                             limit: int = 365) -> List[Dict]:
    if from_date and to_date:
        return await db.fetch(
            """SELECT * FROM night_audits
               WHERE hotel_id=$1 AND audit_date BETWEEN $2 AND $3
               ORDER BY audit_date DESC LIMIT $4""",
            hotel_id, from_date, to_date, max(1, min(int(limit), 1000)))
    return await db.fetch(
        """SELECT * FROM night_audits WHERE hotel_id=$1
           ORDER BY audit_date DESC LIMIT $2""",
        hotel_id, max(1, min(int(limit), 1000)))


async def get_kpi_series(hotel_id: int,
                          from_date: date, to_date: date) -> List[Dict]:
    """
    Time-series for charts. If a date in the range has no night_audits
    row (audit hasn't run yet, or first deploy), we compute it on the fly
    so the chart never has gaps.
    """
    persisted = {
        r["audit_date"].isoformat(): r
        for r in await db.fetch(
            """SELECT * FROM night_audits
               WHERE hotel_id=$1 AND audit_date BETWEEN $2 AND $3
               ORDER BY audit_date""", hotel_id, from_date, to_date)
    }
    out: List[Dict] = []
    cur = from_date
    while cur <= to_date:
        key = cur.isoformat()
        if key in persisted:
            r = persisted[key]
            out.append({
                "date":           key,
                "occupancy_pct":  float(r["occupancy_pct"] or 0),
                "adr":            float(r["adr"] or 0),
                "revpar":         float(r["revpar"] or 0),
                "trevpar":        float(r["trevpar"] or 0),
                "room_revenue":   float(r["room_revenue"] or 0),
                "total_revenue":  float(r["total_revenue"] or 0),
                "occupied_rooms": int(r["occupied_rooms"] or 0),
                "total_rooms":    int(r["total_rooms"] or 0),
                "source":         "audit",
            })
        else:
            m = await compute_metrics(hotel_id, cur)
            out.append({
                "date":           m["audit_date"],
                "occupancy_pct":  m["occupancy_pct"],
                "adr":            m["adr"],
                "revpar":         m["revpar"],
                "trevpar":        m["trevpar"],
                "room_revenue":   m["room_revenue"],
                "total_revenue":  m["total_revenue"],
                "occupied_rooms": m["occupied_rooms"],
                "total_rooms":    m["total_rooms"],
                "source":         "live",
            })
        cur += timedelta(days=1)
    return out


async def get_revenue_segments(hotel_id: int,
                                from_date: date, to_date: date) -> Dict:
    """Revenue breakdown by service_type — feeds the pie chart."""
    rows = await db.fetch(
        """SELECT
             COALESCE(service_type,'Unknown') AS service_type,
             SUM(total) AS amount,
             COUNT(*)   AS rows
           FROM stay_charges sc
           JOIN bookings b ON b.booking_id = sc.booking_id
           WHERE b.hotel_id=$1
             AND sc.charge_date::date BETWEEN $2 AND $3
           GROUP BY service_type
           ORDER BY amount DESC""",
        hotel_id, from_date, to_date)
    total = sum(float(r["amount"] or 0) for r in rows) or 0.0
    return {
        "from": from_date.isoformat(),
        "to":   to_date.isoformat(),
        "total": round(total, 2),
        "segments": [
            {
                "service_type": r["service_type"],
                "amount":       round(float(r["amount"] or 0), 2),
                "rows":         int(r["rows"] or 0),
                "percent":      round((float(r["amount"] or 0) / total * 100), 2) if total else 0.0,
            }
            for r in rows
        ],
    }


async def get_source_mix(hotel_id: int,
                          from_date: date, to_date: date) -> Dict:
    """
    Direct vs OTA bookings split. Uses bookings.ota_source — populated
    by the channel-manager pull (or left empty for direct/walk-in).
    """
    rows = await db.fetch(
        """SELECT
             CASE WHEN COALESCE(NULLIF(ota_source,''),'') = '' THEN 'direct'
                  ELSE LOWER(ota_source) END AS source,
             COUNT(*)                       AS bookings,
             COALESCE(SUM(EXTRACT(DAY FROM (checkout_date - checkin_date)))::INT,0) AS room_nights
           FROM bookings
           WHERE hotel_id=$1
             AND created_at::date BETWEEN $2 AND $3
           GROUP BY 1
           ORDER BY bookings DESC""",
        hotel_id, from_date, to_date)
    total = sum(int(r["bookings"] or 0) for r in rows) or 0
    return {
        "from": from_date.isoformat(),
        "to":   to_date.isoformat(),
        "total_bookings": total,
        "sources": [
            {
                "source":      r["source"] or "direct",
                "bookings":    int(r["bookings"] or 0),
                "room_nights": int(r["room_nights"] or 0),
                "percent":     round((int(r["bookings"] or 0) / total * 100), 2) if total else 0.0,
            }
            for r in rows
        ],
    }


async def run_for_all_hotels() -> List[Dict]:
    """Scheduler entry — walk every active hotel and run the audit."""
    rows = await db.fetch("SELECT id, hotel_name FROM hotels WHERE is_active=TRUE")
    out: List[Dict] = []
    for h in rows:
        try:
            r = await run_night_audit(h["id"], audit_date=_yesterday(),
                                       actor="scheduler")
            out.append({"hotel_id": h["id"], "ok": True,
                         "audit_date": str(r.get("audit_date"))})
        except Exception as e:
            logger.exception("night_audit failed for hotel %s", h["id"])
            out.append({"hotel_id": h["id"], "ok": False,
                         "error": str(e)[:300]})
    return out
