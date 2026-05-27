"""
services/cloudinary_signing.py — Short-lived signed proxy URLs for ID-proof photos.

Why this exists
───────────────
ID-proof photos (Aadhaar, passport, driving licence) are uploaded to Cloudinary
via an unsigned upload preset and the resulting `https://res.cloudinary.com/...`
URLs were stored verbatim in `bookings.id_proof_photo` /
`bookings.id_proof_photo_back` (and the same on `additional_booking_guests`).

That had two problems:

  1. Once a Cloudinary URL leaks (logs, screenshots, browser history, support
     emails, an `<img src>` in saved HTML) the in-app `🔒 Hidden` masking
     applied by `routes/hotel_routes.hotel_booking_detail` is bypassed —
     the URL is anonymously fetchable for as long as the asset exists. KYC
     data leaking is a real exposure under DPDP Act.

  2. The `can_view_id_proofs` permission was only checked at *render* time
     (when the dashboard endpoint built its response). After a staff member
     loaded the page, any URL in their browser stayed valid forever even
     after their permission was revoked.

Approach (no Cloudinary preset switch, no DB migration)
───────────────────────────────────────────────────────
We do NOT serve the Cloudinary URL to the dashboard. Instead, every endpoint
that previously emitted `id_proof_photo` now emits a relative URL of the form

    /api/hotel/{slug}/id-proof/{token}

The token is a short-lived (10-minute) HMAC-signed pointer to the DB row that
holds the actual URL. When the browser fetches it:

  * `routes/hotel_routes.fetch_id_proof` re-runs `require_perm("can_view_id_proofs")`,
    so a staff member who lost the permission five minutes ago can no longer
    fetch the photo — even with a fresh page loaded a moment ago.
  * The token's `hotel_id` is re-checked against the route's `slug`, blocking
    cross-tenant access even if a token leaks between hotels.
  * The endpoint resolves the row → grabs the stored URL → 302-redirects to
    Cloudinary. The actual `res.cloudinary.com` URL never reaches the browser
    via our JSON API.

Token format
────────────
    base64url(payload) + "." + base64url(sig)

where  payload = "<kind>|<row_id>|<which>|<exp_unix>"
       sig     = HMAC-SHA256(SECRET_KEY, payload)[:16]
       kind    = "b" (bookings) | "a" (additional_booking_guests)
       which   = "f" (front)    | "b" (back)

Truncating the HMAC to 128 bits is fine for a 10-minute auth token — it's still
2^128 brute-force work, and the signing key is the application SECRET_KEY which
also protects every JWT in the system.

Rotating SECRET_KEY invalidates all outstanding tokens immediately, which is
the correct behavior (it already invalidates sessions).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Optional

from config.settings import SECRET_KEY

logger = logging.getLogger(__name__)

# 10 minutes is long enough that a tab left open still works, short enough
# that a leaked URL is uninteresting by the time it surfaces in a log.
ID_PROOF_TTL_SECONDS = 600

# Sentinel set by `hotel_booking_detail` when the caller lacks
# `can_view_id_proofs`. We pass it through unchanged so the dashboard still
# renders the lock icon instead of a broken image.
_HIDDEN_SENTINEL = "🔒 Hidden"

# Allow-list for the redirect target in `fetch_id_proof`. Cloudinary's
# `secure_url` is always served from this host. If a hotel ever moves to a
# custom CNAME, extend this list — but never accept arbitrary URLs (would
# turn the proxy endpoint into an open redirect).
_CLOUDINARY_HOSTS = (
    "https://res.cloudinary.com/",
    # Older accounts occasionally serve from `cloudinary-a.akamaihd.net`,
    # but the upload code uses `secure_url` which is always the host above.
    # Extend here only if telemetry shows other hosts in production.
)


# ── token codec ────────────────────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # urlsafe_b64decode demands correct padding; restore it.
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _hmac(payload: bytes) -> bytes:
    if not SECRET_KEY:
        # Fail closed. The application's main.py already refuses to boot
        # without SECRET_KEY in production, but in case this is called
        # from a test or a misconfigured dev box, surface it loudly rather
        # than emitting tokens an attacker can forge with the empty key.
        raise RuntimeError(
            "SECRET_KEY is not configured; cannot sign ID-proof tokens. "
            "Set SECRET_KEY in the environment."
        )
    return hmac.new(SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).digest()[:16]


def make_id_proof_token(
    kind: str,         # "b" (bookings) or "a" (additional_booking_guests)
    row_id: int,
    which: str,        # "f" (front) or "b" (back)
    ttl: int = ID_PROOF_TTL_SECONDS,
) -> str:
    """Generate a signed, short-lived pointer to one ID-proof photo.

    The returned string is opaque to the frontend — it just round-trips it
    back to `/api/hotel/{slug}/id-proof/{token}` when rendering the image.
    """
    if kind not in ("b", "a"):
        raise ValueError(f"invalid kind: {kind!r}")
    if which not in ("f", "b"):
        raise ValueError(f"invalid which: {which!r}")
    if row_id <= 0:
        raise ValueError(f"invalid row_id: {row_id!r}")

    exp = int(time.time()) + int(ttl)
    payload = f"{kind}|{int(row_id)}|{which}|{exp}".encode("utf-8")
    sig = _hmac(payload)
    return _b64url_encode(payload) + "." + _b64url_encode(sig)


def verify_id_proof_token(token: str) -> Optional[dict]:
    """Validate `token` and return its decoded payload, or None on any failure.

    Failures we treat identically (None return, no detail leaked):
      * malformed shape
      * bad base64
      * payload doesn't have 4 fields / bad kind / bad which / bad row_id
      * signature mismatch
      * expired

    The constant-time comparison on the signature avoids timing oracles
    that would let an attacker incrementally brute-force the HMAC.
    """
    if not token or not SECRET_KEY:
        return None
    if "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except Exception:
        return None

    expected_sig = _hmac(payload)
    if not hmac.compare_digest(sig, expected_sig):
        return None

    try:
        kind, row_id_s, which, exp_s = payload.decode("utf-8").split("|")
        row_id = int(row_id_s)
        exp = int(exp_s)
    except Exception:
        return None

    if kind not in ("b", "a") or which not in ("f", "b") or row_id <= 0:
        return None
    if exp < int(time.time()):
        return None

    return {"kind": kind, "row_id": row_id, "which": which, "exp": exp}


def is_cloudinary_url(url: str) -> bool:
    """Allow-list check used by the proxy before issuing a 302.

    Without this the proxy endpoint would be an open redirect — a leaked
    token could be used to point staff browsers at attacker-controlled URLs.
    """
    if not url:
        return False
    return any(url.startswith(prefix) for prefix in _CLOUDINARY_HOSTS)


# ── response-shaping helpers ───────────────────────────────────────────────
#
# These mutate the dict in place because the surrounding endpoint code
# (hotel_booking_detail, search_guest, all_bookings) returns the same dict
# straight to JSONResponse. Returning a copy would just double the
# bookkeeping for callers without buying anything.

def _wrap_one(d: dict, slug: str, kind: str, col: str, which: str) -> None:
    """Replace `d[col]` with a signed proxy URL, if and only if it's a
    real Cloudinary URL. Empty strings, the `🔒 Hidden` sentinel, and any
    non-Cloudinary URL we don't recognize are passed through unchanged.

    Passing through `🔒 Hidden` preserves the dashboard's existing lock-icon
    rendering for staff without `can_view_id_proofs`.

    Passing through unknown values means a hotel that one day uses a
    different storage backend doesn't suddenly render dead links — the
    backend can be migrated without changing this helper.
    """
    val = d.get(col)
    if not isinstance(val, str) or not val:
        return
    if val == _HIDDEN_SENTINEL:
        return
    if not is_cloudinary_url(val):
        # Don't sign URLs we can't safely redirect to later.
        return

    row_id = d.get("id")
    if not isinstance(row_id, int) or row_id <= 0:
        # The row didn't carry its primary key — we can't build a token
        # that the proxy can resolve. Blank the URL rather than leak the
        # raw Cloudinary one.
        logger.warning(
            "wrap_id_proof: missing row id for kind=%s col=%s slug=%s; "
            "blanking URL to avoid raw-Cloudinary leak",
            kind, col, slug,
        )
        d[col] = ""
        return

    try:
        token = make_id_proof_token(kind, row_id, which)
    except RuntimeError:
        # SECRET_KEY missing — fail closed by blanking, never by leaking.
        logger.error("wrap_id_proof: SECRET_KEY missing; blanking %s", col)
        d[col] = ""
        return

    d[col] = f"/api/hotel/{slug}/id-proof/{token}"


def wrap_booking_id_proofs(booking: dict, slug: str) -> None:
    """Sign the front/back URLs on a `bookings` row in-place.

    Safe to call after the `🔒 Hidden` masking — the helper preserves the
    sentinel.
    """
    _wrap_one(booking, slug, "b", "id_proof_photo", "f")
    _wrap_one(booking, slug, "b", "id_proof_photo_back", "b")


def wrap_additional_guest_id_proofs(ag: dict, slug: str) -> None:
    """Sign the front/back URLs on an `additional_booking_guests` row in-place."""
    _wrap_one(ag, slug, "a", "id_proof_photo", "f")
    _wrap_one(ag, slug, "a", "id_proof_photo_back", "b")


def wrap_guest_lookup_id_proofs(guest: dict, slug: str) -> None:
    """Sign URLs on the `lookup_guest_by_phone` / `lookup_guest_by_id` result.

    The query returns columns from `bookings` (the most-recent booking for
    that guest), so the row id is a `bookings.id`. Caller must ensure the
    underlying SQL selects `b.id AS id`; otherwise `_wrap_one` will blank
    the URL rather than emit an unsigned Cloudinary one.
    """
    _wrap_one(guest, slug, "b", "id_proof_photo", "f")
    _wrap_one(guest, slug, "b", "id_proof_photo_back", "b")
