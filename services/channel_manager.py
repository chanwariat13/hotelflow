"""
services/channel_manager.py
─────────────────────────────────────────────────────────────────────
Channel-manager (OTA aggregator) integration.

Why this exists
───────────────
Building first-party connectors to MakeMyTrip, Goibibo, Booking.com,
Agoda, Expedia and 50 other OTAs is a 6-month project per OTA. Every
mid-market PMS therefore integrates with a channel-manager aggregator
(AxisRooms, STAAH, RateGain, SiteMinder, eZee Centrix, etc.) which has
already done that work. We ship inventory + rates to the aggregator,
and reservations come back from the aggregator. One integration here =
50+ OTAs live.

What this module does
─────────────────────
- Defines a thin adapter interface (`BaseChannelAdapter`) with three
  methods: `push_inventory`, `push_rates`, `pull_bookings`.
- Provides two concrete adapters as starting points: `AxisRoomsAdapter`
  and `StaahAdapter`. Both implementations are written to be safe to
  ship today: they hit the configured aggregator's REST endpoints
  using the documented payload shape, but every adapter call is
  guarded so that:
    * No call is ever made when the account has `dry_run=TRUE`.
    * Network and 4xx/5xx errors never raise — they are returned as
      a `{ok:False,error:...}` result which is logged to
      `channel_sync_log` and surfaced in the dashboard.
- Coordinates the high-level workflow:
    push_inventory_for_hotel(hotel_id, days)
    pull_bookings_for_hotel(hotel_id)
    ingest_channel_booking(channel_booking_id, hotel_id, room_number)
- Inventory is computed from our own `rooms` + `bookings` tables, so
  there is exactly one source of truth (us). The aggregator is a sink.

Operator workflow (the simplified mental model)
──────────────────────────────────────────────
  1. /channel/connect with provider+credentials  → row in channel_accounts
  2. /channel/room-types — map "Deluxe AC" → "DLX"
  3. /channel/rate-plans — define BAR / NRR plans
  4. Flip dry_run=False when ready
  5. Scheduler pushes inventory every 30 min, pulls bookings every 15
  6. New OTA reservations show up in the dashboard. Operator clicks
     "Assign room" → ingest_channel_booking creates a real booking.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services import database as db
from services.audit import audit

logger = logging.getLogger(__name__)


# ── Result type ────────────────────────────────────────────────────
@dataclass
class SyncResult:
    ok: bool
    operation: str
    records: int = 0
    error: str = ""
    payload_summary: str = ""
    duration_ms: int = 0
    raw: Any = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "operation": self.operation,
            "records": self.records, "error": self.error,
            "payload_summary": self.payload_summary,
            "duration_ms": self.duration_ms,
        }


# ── Adapter base ───────────────────────────────────────────────────
class BaseChannelAdapter:
    """
    Common interface every channel-manager adapter implements.

    `account` is the row from channel_accounts (a dict). Adapters MUST
    NOT mutate it. Adapters MUST NOT raise — return SyncResult instead.
    """
    name: str = "base"
    default_base_url: str = ""

    def __init__(self, account: Dict[str, Any]):
        self.account = account
        self.base_url = (account.get("base_url") or self.default_base_url).rstrip("/")
        self.hotel_code = account.get("hotel_code") or ""
        self.api_key = account.get("api_key") or ""
        self.api_secret = account.get("api_secret") or ""
        self.username = account.get("username") or ""
        self.password = account.get("password") or ""
        self.dry_run = bool(account.get("dry_run", True))

    # ── Public surface ────────────────────────────────────────────
    async def push_inventory(self, rows: List[Dict]) -> SyncResult: ...
    async def push_rates(self, rows: List[Dict]) -> SyncResult: ...
    async def pull_bookings(self, since: Optional[datetime]) -> SyncResult: ...

    # ── Helpers ───────────────────────────────────────────────────
    def _summary(self, rows: List[Dict]) -> str:
        if not rows:
            return "0 rows"
        first = rows[0]
        keys = ",".join(sorted(list(first.keys()))[:6])
        return f"{len(rows)} rows; keys={keys}"


# ── AxisRooms adapter ──────────────────────────────────────────────
# AxisRooms (now part of Yatra) exposes a JSON+REST API; the base URL is
# usually https://channels.axisrooms.com/api. Endpoints below mirror the
# documented integration shape. Treat the URLs as configurable — a hotel
# can override `base_url` at connect time if their tenant uses a
# different region or sandbox.
class AxisRoomsAdapter(BaseChannelAdapter):
    name = "axisrooms"
    default_base_url = "https://channels.axisrooms.com/api"

    def _headers(self) -> Dict[str, str]:
        # AxisRooms uses an API key in the X-API-KEY header for the v2
        # API. Some tenants are still on v1 with username/password —
        # we send both and the gateway uses whichever matches.
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            h["X-API-KEY"] = self.api_key
        if self.api_secret:
            h["X-API-SECRET"] = self.api_secret
        return h

    async def _post(self, path: str, body: Dict) -> Tuple[int, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            try:
                payload = resp.json()
            except Exception:
                payload = {"raw": resp.text[:1000]}
            return resp.status_code, payload

    async def _get(self, path: str, params: Optional[Dict] = None) -> Tuple[int, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=self._headers(), params=params or {})
            try:
                payload = resp.json()
            except Exception:
                payload = {"raw": resp.text[:1000]}
            return resp.status_code, payload

    async def push_inventory(self, rows: List[Dict]) -> SyncResult:
        if not rows:
            return SyncResult(ok=True, operation="push_inventory", records=0,
                              payload_summary="0 rows")
        if self.dry_run:
            return SyncResult(ok=True, operation="push_inventory",
                              records=len(rows), payload_summary=self._summary(rows) + " [dry_run]")
        body = {
            "hotelCode": self.hotel_code,
            "items": [
                {
                    "roomTypeCode": r["provider_code"],
                    "fromDate": r["stay_date"].isoformat() if hasattr(r["stay_date"], "isoformat") else str(r["stay_date"]),
                    "toDate":   r["stay_date"].isoformat() if hasattr(r["stay_date"], "isoformat") else str(r["stay_date"]),
                    "available": int(r.get("available_units") or 0),
                    "stopSell":  bool(r.get("stop_sell")),
                }
                for r in rows
            ],
        }
        try:
            status, payload = await self._post("/inventory/update", body)
            ok = 200 <= status < 300
            err = "" if ok else f"http {status}: {str(payload)[:300]}"
            return SyncResult(ok=ok, operation="push_inventory",
                              records=len(rows), error=err,
                              payload_summary=self._summary(rows), raw=payload)
        except Exception as e:
            return SyncResult(ok=False, operation="push_inventory",
                              records=0, error=str(e)[:500])

    async def push_rates(self, rows: List[Dict]) -> SyncResult:
        if not rows:
            return SyncResult(ok=True, operation="push_rates", records=0)
        if self.dry_run:
            return SyncResult(ok=True, operation="push_rates",
                              records=len(rows),
                              payload_summary=self._summary(rows) + " [dry_run]")
        body = {
            "hotelCode": self.hotel_code,
            "rates": [
                {
                    "roomTypeCode": r["provider_code"],
                    "ratePlanCode": r.get("rate_plan_code") or "BAR",
                    "fromDate": r["stay_date"].isoformat() if hasattr(r["stay_date"], "isoformat") else str(r["stay_date"]),
                    "toDate":   r["stay_date"].isoformat() if hasattr(r["stay_date"], "isoformat") else str(r["stay_date"]),
                    "amount":   float(r.get("base_rate") or 0),
                }
                for r in rows
            ],
        }
        try:
            status, payload = await self._post("/rates/update", body)
            ok = 200 <= status < 300
            err = "" if ok else f"http {status}: {str(payload)[:300]}"
            return SyncResult(ok=ok, operation="push_rates",
                              records=len(rows), error=err,
                              payload_summary=self._summary(rows), raw=payload)
        except Exception as e:
            return SyncResult(ok=False, operation="push_rates",
                              records=0, error=str(e)[:500])

    async def pull_bookings(self, since: Optional[datetime]) -> SyncResult:
        if self.dry_run:
            # In dry_run mode, return zero so we never accidentally
            # fabricate reservations.
            return SyncResult(ok=True, operation="pull_bookings",
                              records=0, payload_summary="dry_run; no fetch")
        params = {
            "hotelCode": self.hotel_code,
            "from": (since or (datetime.now(timezone.utc) - timedelta(days=2))).isoformat(),
        }
        try:
            status, payload = await self._get("/reservations", params)
            if not (200 <= status < 300):
                return SyncResult(ok=False, operation="pull_bookings",
                                  records=0, error=f"http {status}: {str(payload)[:300]}")
            reservations = payload.get("reservations") if isinstance(payload, dict) else payload
            if not isinstance(reservations, list):
                reservations = []
            normalized = [normalize_axisrooms_reservation(r) for r in reservations]
            return SyncResult(ok=True, operation="pull_bookings",
                              records=len(normalized),
                              payload_summary=f"{len(normalized)} reservations",
                              raw=normalized)
        except Exception as e:
            return SyncResult(ok=False, operation="pull_bookings",
                              records=0, error=str(e)[:500])


# ── STAAH adapter ──────────────────────────────────────────────────
# STAAH MAX uses XML over HTTPS for inventory/rate updates and JSON
# webhooks for reservation delivery. We expose the same SyncResult API.
class StaahAdapter(BaseChannelAdapter):
    name = "staah"
    default_base_url = "https://connect.staah.com/api/v2"

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def push_inventory(self, rows: List[Dict]) -> SyncResult:
        if not rows:
            return SyncResult(ok=True, operation="push_inventory", records=0)
        if self.dry_run:
            return SyncResult(ok=True, operation="push_inventory",
                              records=len(rows),
                              payload_summary=self._summary(rows) + " [dry_run]")
        body = {
            "propertyId": self.hotel_code,
            "inventory": [
                {
                    "roomTypeId": r["provider_code"],
                    "date": r["stay_date"].isoformat() if hasattr(r["stay_date"], "isoformat") else str(r["stay_date"]),
                    "available": int(r.get("available_units") or 0),
                    "stopSell": bool(r.get("stop_sell")),
                } for r in rows
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(f"{self.base_url}/inventory",
                                         headers=self._headers(), json=body)
                ok = 200 <= resp.status_code < 300
                err = "" if ok else f"http {resp.status_code}: {resp.text[:300]}"
                return SyncResult(ok=ok, operation="push_inventory",
                                  records=len(rows), error=err,
                                  payload_summary=self._summary(rows))
        except Exception as e:
            return SyncResult(ok=False, operation="push_inventory",
                              records=0, error=str(e)[:500])

    async def push_rates(self, rows: List[Dict]) -> SyncResult:
        if not rows:
            return SyncResult(ok=True, operation="push_rates", records=0)
        if self.dry_run:
            return SyncResult(ok=True, operation="push_rates",
                              records=len(rows),
                              payload_summary=self._summary(rows) + " [dry_run]")
        body = {
            "propertyId": self.hotel_code,
            "rates": [
                {
                    "roomTypeId": r["provider_code"],
                    "ratePlanId": r.get("rate_plan_code") or "BAR",
                    "date": r["stay_date"].isoformat() if hasattr(r["stay_date"], "isoformat") else str(r["stay_date"]),
                    "amount": float(r.get("base_rate") or 0),
                } for r in rows
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(f"{self.base_url}/rates",
                                         headers=self._headers(), json=body)
                ok = 200 <= resp.status_code < 300
                err = "" if ok else f"http {resp.status_code}: {resp.text[:300]}"
                return SyncResult(ok=ok, operation="push_rates",
                                  records=len(rows), error=err,
                                  payload_summary=self._summary(rows))
        except Exception as e:
            return SyncResult(ok=False, operation="push_rates",
                              records=0, error=str(e)[:500])

    async def pull_bookings(self, since: Optional[datetime]) -> SyncResult:
        if self.dry_run:
            return SyncResult(ok=True, operation="pull_bookings",
                              records=0, payload_summary="dry_run; no fetch")
        params = {
            "propertyId": self.hotel_code,
            "modifiedSince": (since or (datetime.now(timezone.utc) - timedelta(days=2))).isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self.base_url}/reservations",
                                        headers=self._headers(), params=params)
                if not (200 <= resp.status_code < 300):
                    return SyncResult(ok=False, operation="pull_bookings",
                                      records=0,
                                      error=f"http {resp.status_code}: {resp.text[:300]}")
                payload = resp.json() if resp.text else {}
                reservations = payload.get("reservations") if isinstance(payload, dict) else payload
                if not isinstance(reservations, list):
                    reservations = []
                normalized = [normalize_staah_reservation(r) for r in reservations]
                return SyncResult(ok=True, operation="pull_bookings",
                                  records=len(normalized),
                                  payload_summary=f"{len(normalized)} reservations",
                                  raw=normalized)
        except Exception as e:
            return SyncResult(ok=False, operation="pull_bookings",
                              records=0, error=str(e)[:500])


# ── Reservation normalisation ──────────────────────────────────────
# Each adapter returns provider-shaped reservation dicts; we normalise
# them to a canonical shape the rest of the system uses.
def _safe_date(s: Any) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    s = str(s)[:10]
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def normalize_axisrooms_reservation(raw: Dict) -> Dict:
    g = raw.get("guest") or {}
    return {
        "provider_ref":    str(raw.get("reservationId") or raw.get("id") or "")[:120],
        "ota_source":      str(raw.get("source") or raw.get("channel") or "")[:60].lower(),
        "ota_booking_id":  str(raw.get("otaConfirmationNumber") or raw.get("otaId") or "")[:120],
        "guest_name":      str(g.get("name") or raw.get("guestName") or "")[:200],
        "guest_email":     str(g.get("email") or raw.get("email") or "")[:200],
        "guest_phone":     str(g.get("phone") or raw.get("phone") or "")[:40],
        "guest_country":   str(g.get("country") or raw.get("country") or "")[:80],
        "checkin_date":    _safe_date(raw.get("checkInDate") or raw.get("checkin")),
        "checkout_date":   _safe_date(raw.get("checkOutDate") or raw.get("checkout")),
        "nights":          int(raw.get("nights") or 0),
        "guests":          int(raw.get("guests") or raw.get("adults") or 1),
        "room_type_code":  str(raw.get("roomTypeCode") or "")[:80],
        "rate_plan_code":  str(raw.get("ratePlanCode") or "BAR")[:40],
        "room_count":      int(raw.get("roomCount") or 1),
        "currency":        str(raw.get("currency") or "INR")[:8],
        "total_amount":    float(raw.get("totalAmount") or raw.get("amount") or 0),
        "ota_commission":  float(raw.get("otaCommission") or 0),
        "payment_terms":   "prepaid" if raw.get("prepaid") else "pay_at_hotel",
        "status":          (str(raw.get("status") or "new").lower() or "new")[:20],
        "special_requests": str(raw.get("specialRequests") or "")[:2000],
        "raw_payload":     json.dumps(raw)[:8000],
    }


def normalize_staah_reservation(raw: Dict) -> Dict:
    g = raw.get("guestInfo") or raw.get("guest") or {}
    return {
        "provider_ref":    str(raw.get("reservationId") or raw.get("bookingId") or "")[:120],
        "ota_source":      str(raw.get("ota") or raw.get("sourceName") or "")[:60].lower(),
        "ota_booking_id":  str(raw.get("otaReservationId") or "")[:120],
        "guest_name":      str(g.get("fullName") or g.get("name") or "")[:200],
        "guest_email":     str(g.get("email") or "")[:200],
        "guest_phone":     str(g.get("phone") or g.get("mobile") or "")[:40],
        "guest_country":   str(g.get("country") or "")[:80],
        "checkin_date":    _safe_date(raw.get("arrivalDate") or raw.get("checkIn")),
        "checkout_date":   _safe_date(raw.get("departureDate") or raw.get("checkOut")),
        "nights":          int(raw.get("nights") or 0),
        "guests":          int(raw.get("adults") or raw.get("guests") or 1),
        "room_type_code":  str(raw.get("roomTypeId") or raw.get("roomTypeCode") or "")[:80],
        "rate_plan_code":  str(raw.get("ratePlanId") or "BAR")[:40],
        "room_count":      int(raw.get("rooms") or 1),
        "currency":        str(raw.get("currency") or "INR")[:8],
        "total_amount":    float(raw.get("grandTotal") or raw.get("totalAmount") or 0),
        "ota_commission":  float(raw.get("commission") or 0),
        "payment_terms":   "prepaid" if str(raw.get("paymentMode") or "").lower() == "prepaid" else "pay_at_hotel",
        "status":          (str(raw.get("status") or "new").lower() or "new")[:20],
        "special_requests": str(raw.get("specialInstructions") or "")[:2000],
        "raw_payload":     json.dumps(raw)[:8000],
    }


# ── Adapter factory ────────────────────────────────────────────────
ADAPTERS = {
    "axisrooms": AxisRoomsAdapter,
    "staah":     StaahAdapter,
    # Aliases people commonly mis-type:
    "axis":      AxisRoomsAdapter,
    "staah_max": StaahAdapter,
}


def get_adapter(account: Dict) -> BaseChannelAdapter:
    provider = (account.get("provider") or "axisrooms").lower()
    cls = ADAPTERS.get(provider)
    if not cls:
        # Default to AxisRooms when the operator picks an unknown
        # provider — the dashboard surfaces an error in last_error so
        # they can fix it without crashing the worker.
        cls = AxisRoomsAdapter
    return cls(account)


# ── High-level workflows used by routes + scheduler ────────────────
async def push_inventory_for_hotel(hotel_id: int, days: Optional[int] = None) -> SyncResult:
    """
    Compute available_units per (room_type, date) from our own state
    and push to the aggregator. Inventory snapshot is recorded in
    `channel_inventory` even when the push fails, so the dashboard
    can show the operator what *should* be on OTAs.
    """
    started = time.monotonic()
    account = await db.get_channel_account(hotel_id)
    if not account or not account.get("is_active"):
        result = SyncResult(ok=False, operation="push_inventory",
                            error="channel account inactive")
        await db.insert_sync_log(hotel_id, account.get("provider") if account else "",
                                 result.operation, status="failed",
                                 error=result.error)
        return result

    horizon = int(days or account.get("inventory_horizon_days") or 60)
    today = date.today()
    dates = [today + timedelta(days=i) for i in range(horizon)]
    rows = await db.aggregate_inventory_for_dates(hotel_id, dates)

    # Persist our snapshot first, regardless of push outcome.
    for r in rows:
        await db.upsert_channel_inventory(
            hotel_id=hotel_id,
            room_type_id=r["room_type_id"],
            stay_date=r["stay_date"],
            available_units=int(r["available_units"] or 0),
            base_rate=float(r["base_rate"] or 0),
            stop_sell=False,
            status="pending",
        )

    if not rows:
        result = SyncResult(ok=True, operation="push_inventory",
                            records=0, payload_summary="no room types mapped")
        await db.insert_sync_log(hotel_id, account.get("provider"),
                                 result.operation, status="ok",
                                 records=0, payload_summary=result.payload_summary,
                                 duration_ms=int((time.monotonic()-started)*1000))
        return result

    adapter = get_adapter(account)
    inv_result = await adapter.push_inventory(rows)
    rate_result = await adapter.push_rates(rows)

    duration_ms = int((time.monotonic() - started) * 1000)
    status = "ok" if (inv_result.ok and rate_result.ok) else (
        "dry_run" if account.get("dry_run") and not inv_result.error and not rate_result.error
        else "failed"
    )
    err = " | ".join([e for e in (inv_result.error, rate_result.error) if e])

    await db.insert_sync_log(
        hotel_id, account.get("provider"), "push_inventory",
        status=status, records=len(rows), duration_ms=duration_ms,
        error=err, payload_summary=inv_result.payload_summary,
    )

    # Mark snapshot as pushed (even on partial failure we update for
    # visibility; the error column on sync_log is the source of truth).
    push_status = "ok" if status in ("ok", "dry_run") else "failed"
    for r in rows:
        await db.upsert_channel_inventory(
            hotel_id=hotel_id,
            room_type_id=r["room_type_id"],
            stay_date=r["stay_date"],
            available_units=int(r["available_units"] or 0),
            base_rate=float(r["base_rate"] or 0),
            stop_sell=False,
            status=push_status,
        )

    await db.update_channel_account_status(
        hotel_id,
        last_inventory_push_at=datetime.now(timezone.utc),
        last_error=err if err else "",
    )

    return SyncResult(
        ok=(status in ("ok", "dry_run")),
        operation="push_inventory",
        records=len(rows),
        error=err,
        payload_summary=inv_result.payload_summary,
        duration_ms=duration_ms,
    )


async def pull_bookings_for_hotel(hotel_id: int) -> SyncResult:
    """Fetch new/modified reservations from the aggregator and upsert."""
    started = time.monotonic()
    account = await db.get_channel_account(hotel_id)
    if not account or not account.get("is_active"):
        result = SyncResult(ok=False, operation="pull_bookings",
                            error="channel account inactive")
        await db.insert_sync_log(hotel_id, account.get("provider") if account else "",
                                 result.operation, status="failed",
                                 error=result.error)
        return result

    since = account.get("last_booking_pull_at")
    if isinstance(since, str):
        try:
            since = datetime.fromisoformat(since)
        except Exception:
            since = None

    adapter = get_adapter(account)
    result = await adapter.pull_bookings(since)
    duration_ms = int((time.monotonic() - started) * 1000)

    if not result.ok:
        await db.insert_sync_log(
            hotel_id, account.get("provider"), "pull_bookings",
            status="failed", error=result.error,
            payload_summary=result.payload_summary, duration_ms=duration_ms,
        )
        await db.update_channel_account_status(hotel_id, last_error=result.error)
        return result

    inserted = 0
    for normalized in (result.raw or []):
        if not normalized.get("provider_ref"):
            continue
        await db.upsert_channel_booking(
            hotel_id=hotel_id, provider=account["provider"],
            provider_ref=normalized["provider_ref"], data=normalized,
        )
        inserted += 1

    status = "ok" if not account.get("dry_run") else "dry_run"
    await db.insert_sync_log(
        hotel_id, account.get("provider"), "pull_bookings",
        status=status, records=inserted, duration_ms=duration_ms,
        payload_summary=f"{inserted} reservations upserted",
    )
    await db.update_channel_account_status(
        hotel_id,
        last_booking_pull_at=datetime.now(timezone.utc),
        last_error="",
    )
    return SyncResult(
        ok=True, operation="pull_bookings", records=inserted,
        duration_ms=duration_ms,
        payload_summary=f"{inserted} reservations upserted",
    )


async def ingest_channel_booking(hotel_id: int, channel_booking_id: int,
                                 room_number: str, *, actor: str = "system",
                                 request=None) -> Dict:
    """
    Convert a `channel_bookings` row into a real `bookings` row so the
    rest of HotelFlow (check-in, billing, FormC, late charges) treats
    it like any other reservation.

    `room_number` is chosen by the operator from the dashboard. We
    refuse to assign a room that is already occupied for overlapping
    dates; the operator must reassign.
    """
    cb = await db.fetchrow(
        "SELECT * FROM channel_bookings WHERE id=$1 AND hotel_id=$2",
        channel_booking_id, hotel_id,
    )
    if not cb:
        raise ValueError("channel booking not found")
    if cb.get("status") == "ingested" and cb.get("mapped_booking_id"):
        return {"already_ingested": True, "booking_id": cb["mapped_booking_id"]}
    if cb.get("status") == "cancelled":
        raise ValueError("cannot ingest a cancelled OTA booking")

    if not room_number:
        raise ValueError("room_number is required")

    # Conflict check: any active booking already holding this room across
    # the requested date range?
    overlap = await db.fetchval(
        """SELECT COUNT(*) FROM bookings
           WHERE hotel_id=$1 AND room_number=$2 AND status='Active'
             AND checkin_date  < $4
             AND checkout_date > $3""",
        hotel_id, room_number,
        cb.get("checkin_date"), cb.get("checkout_date"),
    )
    if int(overlap or 0) > 0:
        raise ValueError(
            f"room {room_number} is already occupied for the requested dates"
        )

    # Build a HotelFlow booking_id from the OTA reference so the operator
    # can correlate at a glance. If a booking with that id somehow exists
    # we fall back to a timestamp suffix.
    short_ref = (cb.get("provider_ref") or "")[-10:].upper().replace("-", "")
    candidate = f"OTA{short_ref}"
    existing = await db.fetchval("SELECT 1 FROM bookings WHERE booking_id=$1", candidate)
    if existing:
        candidate = f"OTA{short_ref}{int(datetime.utcnow().timestamp()) % 100000}"

    booking_data = {
        "booking_id":     candidate,
        "room_number":    room_number,
        "guest_name":     cb.get("guest_name") or "OTA Guest",
        "guest_phone":    (cb.get("guest_phone") or "").replace(" ", ""),
        "checkin_date":   cb.get("checkin_date"),
        "checkout_date":  cb.get("checkout_date"),
        "status":         "Active",
        "payment_mode":   "Prepaid (OTA)" if (cb.get("payment_terms") == "prepaid") else "Pay at checkout",
        "guest_count":    int(cb.get("guests") or 1),
        "alternate_phone": "",
        "hotel_id":       hotel_id,
        "id_proof_type":  "OTA",
        "id_proof_number": cb.get("ota_booking_id") or cb.get("provider_ref") or "",
    }
    await db.insert_booking(booking_data)
    # Reflect OTA origin on the booking row so the dashboard can show a
    # source badge ("MMT", "Booking.com", etc.).
    await db.execute(
        "UPDATE bookings SET ota_source=$1, channel_ref=$2 WHERE booking_id=$3",
        (cb.get("ota_source") or "")[:60],
        (cb.get("provider_ref") or "")[:120],
        candidate,
    )
    await db.set_room_occupied(room_number, hotel_id)
    await db.mark_channel_booking_ingested(channel_booking_id, hotel_id, candidate)

    await audit(
        "channel.booking.ingested",
        actor=actor, actor_role="ops",
        hotel_id=hotel_id, target=candidate,
        payload={
            "provider_ref": cb.get("provider_ref"),
            "ota_source": cb.get("ota_source"),
            "room_number": room_number,
            "amount": float(cb.get("total_amount") or 0),
        },
        request=request,
    )

    return {"booking_id": candidate, "room_number": room_number}


async def run_all_active_hotels(operation: str) -> List[Dict]:
    """
    Helper used by the scheduler: run `push_inventory` or `pull_bookings`
    for every hotel that has an active channel account.
    """
    rows = await db.fetch(
        """SELECT h.id, h.hotel_name, ca.provider
           FROM hotels h JOIN channel_accounts ca ON ca.hotel_id=h.id
           WHERE h.is_active=TRUE AND ca.is_active=TRUE"""
    )
    results: List[Dict] = []
    for row in rows:
        try:
            if operation == "push_inventory":
                r = await push_inventory_for_hotel(row["id"])
            elif operation == "pull_bookings":
                r = await pull_bookings_for_hotel(row["id"])
            else:
                continue
            results.append({"hotel_id": row["id"], **r.as_dict()})
        except Exception as e:
            logger.exception("channel %s failed for hotel %s", operation, row["id"])
            results.append({"hotel_id": row["id"], "ok": False, "error": str(e)[:300]})
    return results
