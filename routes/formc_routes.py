"""
routes/formc_routes.py — FormC / FRRO foreign-guest reporting endpoints.

Workflow expected by hotel staff:
  1. Guest checks in. If `is_foreign_guest=true`, the booking gets
     formc_status='Pending' automatically.
  2. Staff fills in passport/visa/arrival fields (if not captured at check-in).
  3. Staff opens /api/formc/{slug}/pending → sees all pending foreign guests.
  4. Staff downloads the bulk-upload CSV (one row per booking) and uploads it
     to https://indianfrro.gov.in.
  5. Once the portal returns a reference number, staff calls
     POST /api/formc/{slug}/booking/{booking_id}/mark_filed with that
     reference and the booking flips to 'Filed'.

We deliberately do NOT push to the FRRO portal automatically — there is no
documented public API. This service produces the upload artefacts and tracks
the filing status for legal-defensibility.
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from services import database as db
from services import formc

router = APIRouter(prefix="/api/formc", tags=["formc"])


async def _get_hotel_or_404(slug: str) -> dict:
    h = await db.get_hotel_by_slug(slug)
    if not h:
        raise HTTPException(404, f"Unknown hotel '{slug}'")
    return h


@router.get("/{slug}/summary")
async def formc_summary(slug: str):
    """Quick stats for the dashboard tile."""
    h = await _get_hotel_or_404(slug)
    pending = await db.count_formc_pending(h["id"])
    last24 = await db.fetch("""
        SELECT booking_id, guest_name, nationality, checkin_date, formc_status
        FROM bookings
        WHERE hotel_id=$1 AND is_foreign_guest=TRUE
          AND created_at >= NOW() - INTERVAL '7 days'
        ORDER BY checkin_date DESC LIMIT 20
    """, h["id"])
    overdue = sum(1 for b in last24 if formc.is_overdue(b))
    return JSONResponse({
        "hotel_id":      h["id"],
        "pending":       pending,
        "overdue_recent": overdue,
        "recent":        [
            {**b, "checkin_date": str(b.get("checkin_date") or "")}
            for b in last24
        ],
    })


@router.get("/{slug}/pending")
async def list_pending(slug: str, limit: int = Query(100, ge=1, le=500),
                       offset: int = Query(0, ge=0)):
    """List foreign-guest bookings that still need to be filed."""
    h = await _get_hotel_or_404(slug)
    rows = await db.list_formc_bookings(h["id"], status="Pending",
                                         limit=limit, offset=offset)
    out: List[dict] = []
    for b in rows:
        deadline = formc.filing_deadline(b)
        errs = formc.validate_foreign_guest(b)
        out.append({
            "booking_id":     b["booking_id"],
            "guest_name":     b["guest_name"],
            "room_number":    b["room_number"],
            "nationality":    b.get("nationality", ""),
            "passport_no":    b.get("passport_no", ""),
            "visa_no":        b.get("visa_no", ""),
            "checkin_date":   str(b.get("checkin_date") or ""),
            "deadline":       deadline.isoformat() if deadline else None,
            "is_overdue":     formc.is_overdue(b),
            "validation_errors": errs,
            "ready_to_file":  not errs,
        })
    return JSONResponse({"hotel_id": h["id"], "items": out, "count": len(out)})


@router.get("/{slug}/all")
async def list_all(slug: str,
                   status: Optional[str] = Query(None, pattern="^(Pending|Filed|Failed|NotRequired)$"),
                   limit: int = Query(200, ge=1, le=1000),
                   offset: int = Query(0, ge=0)):
    h = await _get_hotel_or_404(slug)
    rows = await db.list_formc_bookings(h["id"], status=status,
                                         limit=limit, offset=offset)
    return JSONResponse({"hotel_id": h["id"], "items": rows, "count": len(rows)})


@router.get("/{slug}/booking/{booking_id}/xml")
async def export_xml(slug: str, booking_id: str):
    h = await _get_hotel_or_404(slug)
    b = await db.get_booking_by_id(booking_id)
    if not b or b.get("hotel_id") != h["id"]:
        raise HTTPException(404, "Booking not found for this hotel")
    if not b.get("is_foreign_guest"):
        raise HTTPException(400, "Booking is not flagged as a foreign guest")
    xml = formc.build_xml(b, h)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="formC-{booking_id}.xml"'},
    )


@router.get("/{slug}/booking/{booking_id}/csv")
async def export_single_csv(slug: str, booking_id: str):
    h = await _get_hotel_or_404(slug)
    b = await db.get_booking_by_id(booking_id)
    if not b or b.get("hotel_id") != h["id"]:
        raise HTTPException(404, "Booking not found for this hotel")
    if not b.get("is_foreign_guest"):
        raise HTTPException(400, "Booking is not flagged as a foreign guest")
    csv_text = formc.build_csv([b], h)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="formC-{booking_id}.csv"'},
    )


@router.get("/{slug}/bulk_csv")
async def export_bulk_csv(slug: str,
                          status: str = Query("Pending", pattern="^(Pending|Filed|Failed)$"),
                          since_days: int = Query(7, ge=1, le=90)):
    """
    Bulk CSV download of all foreign-guest bookings matching status, useful
    when staff wants to upload a batch to indianfrro.gov.in at end of shift.
    """
    h = await _get_hotel_or_404(slug)
    rows = await db.fetch("""
        SELECT * FROM bookings
        WHERE hotel_id=$1 AND is_foreign_guest=TRUE AND formc_status=$2
          AND created_at >= NOW() - ($3 || ' days')::INTERVAL
        ORDER BY checkin_date ASC
    """, h["id"], status, str(since_days))
    if not rows:
        raise HTTPException(404, "No foreign-guest bookings match the filter")
    csv_text = formc.build_csv(rows, h)
    fname = f"formC-bulk-{slug}-{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


class ForeignFieldsBody(BaseModel):
    is_foreign_guest:        Optional[bool]   = None
    nationality:             Optional[str]    = None
    sex:                     Optional[str]    = None
    date_of_birth:           Optional[str]    = None
    passport_no:             Optional[str]    = None
    passport_place_of_issue: Optional[str]    = None
    passport_issue_date:     Optional[str]    = None
    passport_expiry_date:    Optional[str]    = None
    visa_no:                 Optional[str]    = None
    visa_type:               Optional[str]    = None
    visa_issue_place:        Optional[str]    = None
    visa_issue_date:         Optional[str]    = None
    visa_expiry_date:        Optional[str]    = None
    arrival_in_india_date:   Optional[str]    = None
    arrival_in_india_port:   Optional[str]    = None
    last_country_visited:    Optional[str]    = None
    next_destination:        Optional[str]    = None
    purpose_of_visit:        Optional[str]    = None
    formc_remarks:           Optional[str]    = None


@router.put("/{slug}/booking/{booking_id}")
async def update_foreign(slug: str, booking_id: str, body: ForeignFieldsBody, request: Request):
    h = await _get_hotel_or_404(slug)
    existing = await db.get_booking_by_id(booking_id)
    if not existing or existing.get("hotel_id") != h["id"]:
        raise HTTPException(404, "Booking not found for this hotel")
    payload = {k: v for k, v in body.dict().items() if v is not None}
    if not payload:
        return JSONResponse({"ok": True, "booking_id": booking_id, "updated": 0})
    row = await db.update_booking_foreign_fields(booking_id, payload)
    await db.log_formc_event(
        hotel_id=h["id"], booking_id=booking_id,
        action="updated",
        filed_by=str(request.headers.get("X-Operator", "")),
        notes=f"updated keys: {','.join(payload.keys())}",
    )
    return JSONResponse({"ok": True, "booking": row})


class MarkFiledBody(BaseModel):
    reference: str
    filed_by:  str = ""
    notes:     str = ""


@router.post("/{slug}/booking/{booking_id}/mark_filed")
async def mark_filed(slug: str, booking_id: str, body: MarkFiledBody):
    h = await _get_hotel_or_404(slug)
    existing = await db.get_booking_by_id(booking_id)
    if not existing or existing.get("hotel_id") != h["id"]:
        raise HTTPException(404, "Booking not found for this hotel")
    if not existing.get("is_foreign_guest"):
        raise HTTPException(400, "Booking is not a foreign guest — nothing to file")
    if not body.reference.strip():
        raise HTTPException(400, "FRRO reference number is required")
    row = await db.mark_formc_filed(
        booking_id=booking_id,
        reference=body.reference.strip(),
        filed_by=body.filed_by.strip() or "operator",
        hotel_id=h["id"],
        payload=body.notes,
    )
    return JSONResponse({"ok": True, "booking": row})


@router.get("/{slug}/booking/{booking_id}/history")
async def filing_history(slug: str, booking_id: str):
    h = await _get_hotel_or_404(slug)
    rows = await db.fetch("""
        SELECT * FROM formc_filings
        WHERE hotel_id=$1 AND booking_id=$2
        ORDER BY created_at DESC""", h["id"], booking_id)
    return JSONResponse({"booking_id": booking_id, "events": rows})
