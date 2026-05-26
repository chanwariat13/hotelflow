import hashlib, hmac, secrets, logging
from typing import Optional, Dict
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

ROLE_PERMISSIONS = {
    "owner":   {"can_approve_checkin":True,"can_reject_checkin":True,"can_checkout_guest":True,"can_manage_services":True,"can_manage_rooms":True,"can_view_revenue":True,"can_view_id_proofs":True,"can_broadcast":True,"can_manage_staff":True,"can_edit_hotel":True},
    "manager": {"can_approve_checkin":True,"can_reject_checkin":True,"can_checkout_guest":True,"can_manage_services":True,"can_manage_rooms":True,"can_view_revenue":True,"can_view_id_proofs":True,"can_broadcast":True,"can_manage_staff":False,"can_edit_hotel":False},
    "staff":   {"can_approve_checkin":True,"can_reject_checkin":True,"can_checkout_guest":False,"can_manage_services":False,"can_manage_rooms":False,"can_view_revenue":False,"can_view_id_proofs":False,"can_broadcast":False,"can_manage_staff":False,"can_edit_hotel":False},
}

# ── Password hashing ────────────────────────────────────────────────
# We previously used a single round of salted SHA-256, which is GPU-
# crackable at billions of guesses per second. Modern recommendations
# (OWASP / NIST) call for a memory-hard or slow KDF. Without pulling in
# argon2-cffi or bcrypt as a hard dependency, PBKDF2-HMAC-SHA256 with
# 600,000 iterations is the strongest stdlib-only option and matches the
# OWASP 2023 baseline.
#
# Format strings:
#   pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>   (current)
#   <sha256_hash_hex>:<salt_hex>                       (legacy)
# `verify_password` understands both. Successful logins via the legacy
# format trigger a transparent rehash (handled in services.database via
# the consumer of this module).
PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256",
        pw.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"{PBKDF2_ALGO}${PBKDF2_ITERATIONS}${salt}${h}"


def _verify_legacy_sha256(pw: str, stored: str) -> bool:
    """Old format `<hash>:<salt>` → `sha256(salt+pw)`."""
    if ":" not in stored:
        return False
    try:
        h, salt = stored.split(":", 1)
    except ValueError:
        return False
    expected = hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, h)


def _verify_pbkdf2(pw: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != PBKDF2_ALGO:
        return False
    try:
        iterations = int(parts[1])
    except ValueError:
        return False
    salt = parts[2]
    expected_hex = parts[3]
    actual = hashlib.pbkdf2_hmac(
        "sha256", pw.encode("utf-8"),
        salt.encode("utf-8"), iterations,
    ).hex()
    return hmac.compare_digest(expected_hex, actual)


def verify_password(pw: str, stored: str) -> bool:
    """Verify against either the new pbkdf2 format or the legacy sha256
    one. Constant-time within each branch."""
    if not stored:
        return False
    if stored.startswith(PBKDF2_ALGO + "$"):
        return _verify_pbkdf2(pw, stored)
    return _verify_legacy_sha256(pw, stored)


def needs_rehash(stored: str) -> bool:
    """True if the stored hash should be upgraded on next successful login."""
    if not stored or not stored.startswith(PBKDF2_ALGO + "$"):
        return True
    parts = stored.split("$")
    try:
        iterations = int(parts[1])
    except (IndexError, ValueError):
        return True
    return iterations < PBKDF2_ITERATIONS

def apply_role_defaults(role: str, overrides: dict = None) -> dict:
    perms = dict(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["staff"]))
    if overrides:
        for k,v in overrides.items():
            if k in perms and v is not None: perms[k] = v
    return perms

def get_token_from_request(request: Request) -> Optional[str]:
    token = request.cookies.get("hf_token")
    if not token:
        auth = request.headers.get("Authorization","")
        if auth.startswith("Bearer "): token = auth[7:]
    if not token:
        token = request.headers.get("X-HF-Token","")
    return token or None

async def get_current_user(request: Request) -> Optional[Dict]:
    from services.cache import verify_auth_token
    token = get_token_from_request(request)
    if not token: return None
    return await verify_auth_token(token)

async def require_superadmin(request: Request) -> Dict:
    user = await get_current_user(request)
    if not user or user.get("role") != "superadmin":
        raise HTTPException(401, "Superadmin login required")
    return user

async def require_hotel_access(request: Request, slug: str) -> Dict:
    user = await get_current_user(request)
    if not user: raise HTTPException(401, "Login required")
    if user.get("role") == "superadmin": return user
    if user.get("hotel_slug") != slug: raise HTTPException(403, "Access denied")
    return user

async def require_perm(request: Request, slug: str, perm: str) -> Dict:
    user = await require_hotel_access(request, slug)
    if user.get("role") == "superadmin": return user
    if not user.get(perm): raise HTTPException(403, f"No permission: {perm}")
    return user
