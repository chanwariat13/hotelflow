"""
services/audit.py
Append-only audit log helper. Use audit() to record every privileged action.
Never raises — audit failures must never break the user's request.
"""
import json
import logging
from typing import Any, Optional
from services.database import execute, fetch

logger = logging.getLogger(__name__)


async def audit(
    action: str,
    *,
    actor: str = "system",
    actor_role: str = "system",
    hotel_id: Optional[int] = None,
    target: str = "",
    payload: Any = None,
    request=None,
) -> None:
    """
    Record a privileged action.

    Examples:
      await audit("hotel.create", actor="admin", actor_role="superadmin",
                  hotel_id=hid, target=str(hid), payload={"name": ...}, request=req)
      await audit("payment.confirm.cash", actor=staff_phone, actor_role="staff",
                  hotel_id=hid, target=order_id, payload={"total": total})

    Failures are logged and swallowed; never break the calling request.
    """
    try:
        ip = ua = ""
        if request is not None:
            try:
                ip = request.client.host if request.client else ""
            except Exception:
                ip = ""
            try:
                ua = (request.headers.get("user-agent") or "")[:300]
            except Exception:
                ua = ""
        try:
            payload_str = json.dumps(payload, default=str)[:5000] if payload is not None else ""
        except Exception:
            payload_str = str(payload)[:5000]

        await execute(
            """INSERT INTO audit_log
               (hotel_id, actor, actor_role, action, target, payload, ip, user_agent)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            hotel_id, actor[:200], actor_role[:50], action[:100],
            str(target)[:200], payload_str, ip[:100], ua,
        )
    except Exception as e:
        logger.warning(f"audit log failed action={action} hid={hotel_id}: {e}")


async def list_audit(
    hotel_id: Optional[int] = None,
    action_prefix: Optional[str] = None,
    limit: int = 200,
) -> list:
    """Return recent audit entries (newest first), optionally filtered."""
    where = []
    args = []
    if hotel_id is not None:
        args.append(hotel_id); where.append(f"hotel_id=${len(args)}")
    if action_prefix:
        args.append(action_prefix + "%"); where.append(f"action LIKE ${len(args)}")
    args.append(max(1, min(int(limit or 200), 1000)))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return await fetch(
        f"SELECT * FROM audit_log {where_sql} ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )
