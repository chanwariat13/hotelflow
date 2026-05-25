"""
routes/auth_routes.py — Login/logout for all roles
"""
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from services import database as db
from services.cache import create_auth_token, revoke_auth_token
from services.auth import get_token_from_request

router = APIRouter(prefix="/auth")

@router.post("/admin/login")
async def admin_login(request: Request, response: Response):
    body = await request.json()
    user = await db.verify_admin_login(body.get("username",""), body.get("password",""))
    if not user:
        return JSONResponse({"success": False, "error": "Invalid credentials"}, 401)
    token = await create_auth_token(user["id"], "superadmin", 0, "", {}, 28800)
    response.set_cookie("hf_token", token, max_age=28800, httponly=True, samesite="lax")
    return JSONResponse({"success": True, "token": token, "name": user["name"], "role": "superadmin"})

@router.post("/hotel/{slug}/login")
async def hotel_login(slug: str, request: Request, response: Response):
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"success": False, "error": "Hotel not found"}, 404)
    body = await request.json()
    user = await db.verify_hotel_user_login(hotel["id"], body.get("username",""), body.get("password",""))
    if not user:
        return JSONResponse({"success": False, "error": "Invalid credentials"}, 401)
    # Include all permissions in token
    extra = {k: v for k, v in user.items() if k.startswith("can_")}
    extra["name"] = user["name"]
    extra["user_role"] = user["role"]
    token = await create_auth_token(user["id"], user["role"], hotel["id"], slug, extra, 28800)
    response.set_cookie("hf_token", token, max_age=28800, httponly=True, samesite="lax")
    return JSONResponse({"success": True, "token": token, "name": user["name"],
                         "role": user["role"], "slug": slug, "permissions": extra})

@router.post("/logout")
async def logout(request: Request, response: Response):
    token = get_token_from_request(request)
    if token: await revoke_auth_token(token)
    response.delete_cookie("hf_token")
    return JSONResponse({"success": True})

@router.get("/me")
async def me(request: Request):
    from services.auth import get_current_user
    user = await get_current_user(request)
    if not user: return JSONResponse({"authenticated": False}, 401)
    return JSONResponse({"authenticated": True, **user})
