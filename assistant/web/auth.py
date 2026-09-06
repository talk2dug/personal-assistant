"""Session-based auth: login/logout routes plus the dependency functions every other
route module uses to check who's asking. One password per configured user (config.json's
`web_password`), session cookie after login — no real accounts system, just two people.
"""
from fastapi import APIRouter, HTTPException, Request

from ..core import db

router = APIRouter(prefix="/api", tags=["auth"])


def require_user(request: Request) -> dict:
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "not logged in")
    cfg = request.app.state.cfg
    user = db.get_user_by_id(cfg.db_path, user_id)
    if user is None:
        raise HTTPException(401, "not logged in")
    return user


def require_owner(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    return user


@router.post("/login")
async def login(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip().lower()
    password = body.get("password") or ""

    cfg = request.app.state.cfg
    user_cfg = next((u for u in cfg.users if u.display_name.lower() == name), None)
    if user_cfg is None or not user_cfg.web_password or password != user_cfg.web_password:
        raise HTTPException(401, "invalid credentials")

    db_user = db.get_user_by_chat_id(cfg.db_path, user_cfg.telegram_chat_id)
    if db_user is None:
        raise HTTPException(401, "invalid credentials")

    request.session["user_id"] = db_user["id"]
    return {"ok": True, "display_name": db_user["display_name"], "role": db_user["role"]}


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = require_user(request)
    return {"display_name": user["display_name"], "role": user["role"]}
