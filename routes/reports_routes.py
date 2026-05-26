"""
routes/reports_routes.py — KPI dashboards + night-audit API.

Mounts under /api/hotel/{slug}/reports/* and /api/admin/reports/*.

Per-hotel (require can_view_revenue, except `/run` which requires
can_edit_hotel because it writes auto-posted charges):

    POST  /api/hotel/{slug}/reports/night-audit/run         manual run
    GET   /api/hotel/{slug}/reports/night-audits            list past audits
    GET   /api/hotel/{slug}/reports/night-audits/{date}     single date
    GET   /api/hotel/{slug}/reports/kpi                     time-series
    GET   /api/hotel/{slug}/reports/segments                revenue by type
    GET   /api/hotel/{slug}/reports/source-mix              direct vs OTA

Superadmin:

    GET   /api/admin/reports/night-audits                   cross-hotel snapshot
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services import database as db
from services import night_audit as na
from services.auth import require_perm, require_superadmin

logger = logging.getLogger(__name__)


# ─── Per-hotel ─────────────────────────────────────────────────────
hotel_router = APIRouter(prefix="/api/hotel")


async def _hotel(slug: str, request: Request, perm: str = "can_view_revenue"):
    user = await require_perm(request, slug, perm)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    return hotel, user


def _parse_date(s: Optional[str], fallback: date) -> date:
    if not s:
        return fallback
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        raise HTTPException(400, f"invalid date '{s}', want YYYY-MM-DD")


def _default_window(qp) -> tuple[date, date]:
    """Defaults to the last 30 days inclusive."""
    today = date.today()
    to_d = _parse_date(qp.get("to"),   today)
    fr_d = _parse_date(qp.get("from"), to_d - timedelta(days=29))
    if fr_d > to_d:
        raise HTTPException(400, "'from' must be <= 'to'")
    if (to_d - fr_d).days > 365:
        raise HTTPException(400, "range cannot exceed 365 days")
    return fr_d, to_d


@hotel_router.post("/{slug}/reports/night-audit/run")
async def manual_run_night_audit(slug: str, request: Request):
    """Manual trigger. Owners use this to re-run a missed/back-fill day."""
    hotel, user = await _hotel(slug, request, perm="can_edit_hotel")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    audit_date = _parse_date(body.get("date"), date.today() - timedelta(days=1))
    actor = str(user.get("username") or user.get("name") or "owner")
    result = await na.run_night_audit(
        hotel["id"], audit_date=audit_date, actor=actor,
        send_whatsapp=bool(body.get("send_whatsapp", False)),  # default off for manual runs
        request=request,
    )
    return JSONResponse({"success": True, "audit": result})


@hotel_router.get("/{slug}/reports/night-audits")
async def list_audits(slug: str, request: Request):
    hotel, _ = await _hotel(slug, request)
    fr, to = _default_window(request.query_params)
    rows = await na.list_night_audits(hotel["id"], from_date=fr, to_date=to)
    return JSONResponse({"from": fr.isoformat(), "to": to.isoformat(),
                         "audits": rows})


@hotel_router.get("/{slug}/reports/night-audits/{audit_date}")
async def get_audit(slug: str, audit_date: str, request: Request):
    hotel, _ = await _hotel(slug, request)
    d = _parse_date(audit_date, None)  # type: ignore[arg-type]
    if not d:
        raise HTTPException(400, "audit_date required (YYYY-MM-DD)")
    persisted = await db.fetchrow(
        "SELECT * FROM night_audits WHERE hotel_id=$1 AND audit_date=$2",
        hotel["id"], d,
    )
    # Always return a payload — fall back to live compute for dates we
    # haven't audited yet, so the dashboard can render a 404-less detail
    # page.
    live = await na.compute_metrics(hotel["id"], d)
    return JSONResponse({
        "audit": dict(persisted) if persisted else None,
        "live":  live,
    })


@hotel_router.get("/{slug}/reports/kpi")
async def kpi_series(slug: str, request: Request):
    """ADR / RevPAR / TRevPAR / occupancy time-series for charts."""
    hotel, _ = await _hotel(slug, request)
    fr, to = _default_window(request.query_params)
    series = await na.get_kpi_series(hotel["id"], fr, to)

    # Roll-up so dashboards can show 'last 30 days' headline KPIs without
    # a second round-trip.
    n = len(series) or 1
    avg = lambda k: round(sum(float(d[k] or 0) for d in series) / n, 2)
    sum_ = lambda k: round(sum(float(d[k] or 0) for d in series), 2)
    return JSONResponse({
        "from": fr.isoformat(), "to": to.isoformat(),
        "series": series,
        "rollup": {
            "avg_occupancy_pct": avg("occupancy_pct"),
            "avg_adr":           avg("adr"),
            "avg_revpar":        avg("revpar"),
            "avg_trevpar":       avg("trevpar"),
            "sum_room_revenue":  sum_("room_revenue"),
            "sum_total_revenue": sum_("total_revenue"),
        },
    })


@hotel_router.get("/{slug}/reports/segments")
async def revenue_segments(slug: str, request: Request):
    hotel, _ = await _hotel(slug, request)
    fr, to = _default_window(request.query_params)
    return JSONResponse(await na.get_revenue_segments(hotel["id"], fr, to))


@hotel_router.get("/{slug}/reports/source-mix")
async def source_mix(slug: str, request: Request):
    hotel, _ = await _hotel(slug, request)
    fr, to = _default_window(request.query_params)
    return JSONResponse(await na.get_source_mix(hotel["id"], fr, to))


# ─── Superadmin ────────────────────────────────────────────────────
admin_router = APIRouter(prefix="/api/admin")


@admin_router.get("/reports/night-audits")
async def admin_overview(request: Request):
    """Cross-hotel snapshot for the master admin (yesterday's audit row
    per hotel + computed-on-the-fly fallback)."""
    await require_superadmin(request)
    yesterday = date.today() - timedelta(days=1)
    hotels = await db.fetch("SELECT id, hotel_name, slug FROM hotels WHERE is_active=TRUE ORDER BY id")
    out = []
    for h in hotels:
        row = await db.fetchrow(
            "SELECT * FROM night_audits WHERE hotel_id=$1 AND audit_date=$2",
            h["id"], yesterday,
        )
        if row:
            out.append({"hotel_id": h["id"], "hotel_name": h["hotel_name"],
                         "slug": h["slug"], "source": "audit", **dict(row)})
        else:
            metrics = await na.compute_metrics(h["id"], yesterday)
            out.append({"hotel_id": h["id"], "hotel_name": h["hotel_name"],
                         "slug": h["slug"], "source": "live", **metrics})
    return JSONResponse({"date": yesterday.isoformat(), "hotels": out})


def get_routers():
    return [hotel_router, admin_router]
