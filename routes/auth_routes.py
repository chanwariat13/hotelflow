"""
routes/auth_routes.py — Login/logout for all roles
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from services import database as db
from services.cache import create_auth_token, revoke_auth_token
from services.auth import get_token_from_request

router = APIRouter(prefix="/auth")

# 8 hours
TOKEN_TTL = 28800


# Cookie attributes shared by all auth responses.
# IMPORTANT: set these on the JSONResponse we return, NOT on a separately
# injected Response — otherwise FastAPI silently drops the cookie because
# the returned response replaces the injected one.
def _set_auth_cookie(resp: JSONResponse, token: str):
    resp.set_cookie(
        key="hf_token",
        value=token,
        max_age=TOKEN_TTL,
        httponly=True,
        samesite="lax",
        secure=False,  # set True if you only ever serve over HTTPS
        path="/",
    )


@router.post("/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    user = await db.verify_admin_login(body.get("username", ""), body.get("password", ""))
    if not user:
        return JSONResponse({"success": False, "error": "Invalid credentials"}, status_code=401)
    token = await create_auth_token(user["id"], "superadmin", 0, "", {}, TOKEN_TTL)
    resp = JSONResponse({
        "success": True,
        "token": token,
        "name": user["name"],
        "role": "superadmin",
    })
    _set_auth_cookie(resp, token)
    return resp


@router.post("/hotel/{slug}/login")
async def hotel_login(slug: str, request: Request):
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"success": False, "error": "Hotel not found"}, status_code=404)
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")
    # Optional role from the new login dropdown. When supplied, we verify the
    # user actually has that role so a staff account can't accidentally pick
    # "Owner" on the form. When absent (older clients), we fall back to the
    # original username+password check.
    expected_role = (body.get("role") or "").strip().lower()

    user = await db.verify_hotel_user_login(hotel["id"], username, password)
    if not user:
        return JSONResponse({"success": False, "error": "Invalid credentials"}, status_code=401)

    if expected_role and (user.get("role") or "").lower() != expected_role:
        # Don't leak which field was wrong — keep the message generic-ish but
        # actionable so the operator picks the right role next time.
        return JSONResponse(
            {"success": False, "error": f"This account is not a {expected_role}. Pick the correct role."},
            status_code=401,
        )

    # Include all permissions in the token payload.
    extra = {k: v for k, v in user.items() if k.startswith("can_")}
    extra["name"] = user["name"]
    extra["user_role"] = user["role"]

    token = await create_auth_token(user["id"], user["role"], hotel["id"], slug, extra, TOKEN_TTL)
    resp = JSONResponse({
        "success": True,
        "token": token,
        "name": user["name"],
        "role": user["role"],
        "slug": slug,
        "permissions": extra,
    })
    _set_auth_cookie(resp, token)
    return resp


@router.post("/logout")
async def logout(request: Request):
    token = get_token_from_request(request)
    if token:
        await revoke_auth_token(token)
    resp = JSONResponse({"success": True})
    resp.delete_cookie("hf_token", path="/")
    return resp


@router.get("/me")
async def me(request: Request):
    from services.auth import get_current_user
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=401)
    return JSONResponse({"authenticated": True, **user})
