import hashlib, hmac, secrets, logging
from typing import Optional, Dict
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

ROLE_PERMISSIONS = {
    "owner":   {"can_approve_checkin":True,"can_reject_checkin":True,"can_checkout_guest":True,"can_manage_services":True,"can_manage_rooms":True,"can_view_revenue":True,"can_view_id_proofs":True,"can_broadcast":True,"can_manage_staff":True,"can_edit_hotel":True},
    "manager": {"can_approve_checkin":True,"can_reject_checkin":True,"can_checkout_guest":True,"can_manage_services":True,"can_manage_rooms":True,"can_view_revenue":True,"can_view_id_proofs":True,"can_broadcast":True,"can_manage_staff":False,"can_edit_hotel":False},
    "staff":   {"can_approve_checkin":True,"can_reject_checkin":True,"can_checkout_guest":False,"can_manage_services":False,"can_manage_rooms":False,"can_view_revenue":False,"can_view_id_proofs":False,"can_broadcast":False,"can_manage_staff":False,"can_edit_hotel":False},
}

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt+pw).encode()).hexdigest()
    return f"{h}:{salt}"

def verify_password(pw: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    h, salt = stored.split(":", 1)
    # Constant-time compare to avoid timing-side-channel leaks on the admin /
    # owner login forms. Combined with the `==` previously used here, an
    # attacker could otherwise narrow the hash byte-by-byte.
    return hmac.compare_digest(
        hashlib.sha256((salt + pw).encode()).hexdigest(),
        h,
    )

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
