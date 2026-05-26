# ═══════════════════ services/cache.py ═══════════════════
import redis.asyncio as aioredis, json, logging
from typing import Optional
from config.settings import REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASS

logger = logging.getLogger(__name__)
_redis = None

async def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                                password=REDIS_PASS or None, decode_responses=True)
    return _redis

async def close_redis():
    global _redis
    if _redis: await _redis.close(); _redis = None

async def get_session(phone: str) -> Optional[dict]:
    r = await get_redis()
    raw = await r.get(f"session:{phone}")
    if not raw or raw == "null": return None
    try: return json.loads(raw)
    except: return None

async def set_session(phone: str, session: dict, ttl: int = 86400):
    r = await get_redis()
    await r.set(f"session:{phone}", json.dumps(session), ex=ttl)

async def delete_session(phone: str):
    r = await get_redis()
    await r.delete(f"session:{phone}")

async def get_room(room: str) -> Optional[str]:
    r = await get_redis()
    val = await r.get(f"room:{room}")
    return val if val and val != "null" else None

async def set_room(room: str, phone: str, ttl: int = 86400):
    r = await get_redis()
    await r.set(f"room:{room}", phone, ex=ttl)

async def delete_room(room: str):
    r = await get_redis()
    await r.delete(f"room:{room}")

async def is_blocked(phone: str, hotel_id: int = 0) -> bool:
    """Block keys are scoped per hotel so one tenant can't block guests at
    another tenant. The legacy global `blocked:<phone>` key is consulted as
    a fallback for backward compatibility — once it expires there it is
    only the per-hotel namespace that matters."""
    r = await get_redis()
    if hotel_id:
        if await r.get(f"blocked:{hotel_id}:{phone}"):
            return True
    return bool(await r.get(f"blocked:{phone}"))

async def block_user(phone: str, hotel_id: int = 0):
    r = await get_redis()
    key = f"blocked:{hotel_id}:{phone}" if hotel_id else f"blocked:{phone}"
    await r.set(key, "1")

async def unblock_user(phone: str, hotel_id: int = 0):
    r = await get_redis()
    if hotel_id:
        await r.delete(f"blocked:{hotel_id}:{phone}")
    # Always clear the legacy key too, so an UNBLOCK from any hotel removes
    # a stale globally-blocked entry from the previous code path.
    await r.delete(f"blocked:{phone}")

async def get_pending(phone: str) -> Optional[dict]:
    r = await get_redis()
    raw = await r.get(f"pending:{phone}")
    if not raw: return None
    try: return json.loads(raw)
    except: return None

async def set_pending(phone: str, data: dict, ttl: int = 3600):
    r = await get_redis()
    await r.set(f"pending:{phone}", json.dumps(data), ex=ttl)

async def delete_pending(phone: str):
    r = await get_redis()
    await r.delete(f"pending:{phone}")

async def get_all_occupied_rooms() -> list:
    r = await get_redis()
    result = []
    async for key in r.scan_iter("room:*"):
        val = await r.get(key)
        if val and val != "null":
            result.append({"room": key.replace("room:", ""), "phone": val})
    return result

def calc_ttl(checkout_date: str) -> int:
    from datetime import datetime
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    try:
        co = ist.localize(datetime.strptime(checkout_date, "%Y-%m-%d").replace(hour=23, minute=59))
        diff = int((co - datetime.now(ist)).total_seconds())
        return max(diff, 3600)
    except: return 86400

async def create_auth_token(user_id: int, role: str, hotel_id: int = 0,
                             hotel_slug: str = "", extra: dict = None, ttl: int = 28800) -> str:
    import secrets
    token = secrets.token_urlsafe(40)
    r = await get_redis()
    data = {"user_id": user_id, "role": role, "hotel_id": hotel_id, "hotel_slug": hotel_slug}
    if extra: data.update(extra)
    await r.set(f"auth:{token}", json.dumps(data), ex=ttl)
    return token

async def verify_auth_token(token: str) -> Optional[dict]:
    if not token: return None
    r = await get_redis()
    raw = await r.get(f"auth:{token}")
    if not raw: return None
    try: return json.loads(raw)
    except: return None

async def revoke_auth_token(token: str):
    r = await get_redis()
    await r.delete(f"auth:{token}")


async def claim_event(scope: str, event_id: str, ttl: int = 7 * 24 * 3600) -> bool:
    """Idempotency guard for webhook deliveries.

    Atomically mark `event_id` as processed for the given `scope` (e.g.
    `f"razorpay:{hotel_id}"`). Returns True on the FIRST claim — caller
    must proceed with side effects. Returns False on every subsequent
    delivery — caller must short-circuit.

    Razorpay (and most other webhook providers) retry on any non-2xx /
    timeout for up to 24h. Without this guard, retries replayed
    `payment_link.paid` events through `_handle_razorpay_event`,
    double-marking charges paid, double-incrementing `total_paid`, and
    inserting duplicate `payment_log` rows. TTL defaults to 7 days, which
    is well past every reasonable retry window.
    """
    # If we don't have an id we cannot dedupe. Better to allow a single
    # processing through than to silently drop legitimate traffic.
    if not event_id:
        return True
    r = await get_redis()
    key = f"evt:{scope}:{event_id}"
    # SET key value NX EX ttl  →  returns True only if the key didn't exist.
    return bool(await r.set(key, "1", nx=True, ex=ttl))
