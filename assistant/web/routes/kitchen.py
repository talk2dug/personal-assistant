"""Kitchen: the owner's own recipe catalog (and, in later phases, kitchen inventory,
purchase intake, and the shopping list) over the web UI.

Owner-only, same rule as every other personal-data route (grocery.py, schedule.py).
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import kitchen_db
from ..auth import require_owner

router = APIRouter(prefix="/api/kitchen", tags=["kitchen"])


@router.get("/recipes")
async def list_recipes(request: Request, query: str | None = None):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return kitchen_db.list_recipes(cfg.db_path, user["id"], query)


@router.post("/recipes")
async def create_recipe(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    title = (body.get("title") or "").strip()
    ingredients = body.get("ingredients") or []
    steps = body.get("steps") or []
    if not title:
        raise HTTPException(400, "title is required")
    if not ingredients:
        raise HTTPException(400, "ingredients is required")
    if not steps:
        raise HTTPException(400, "steps is required")
    recipe_id = kitchen_db.create_recipe(
        cfg.db_path, user["id"], title, ingredients, steps,
        servings=body.get("servings"), source=body.get("source", "manual"),
        photo_path=body.get("photo_path"), notes=body.get("notes"),
    )
    return {"ok": True, "recipe_id": recipe_id}


@router.get("/recipes/{recipe_id}")
async def get_recipe(recipe_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    recipe = kitchen_db.get_recipe(cfg.db_path, user["id"], recipe_id)
    if recipe is None:
        raise HTTPException(404, "recipe not found")
    return recipe


@router.put("/recipes/{recipe_id}")
async def update_recipe(recipe_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    ok = kitchen_db.update_recipe(
        cfg.db_path, user["id"], recipe_id,
        title=body.get("title"), servings=body.get("servings"),
        ingredients=body.get("ingredients"), steps=body.get("steps"), notes=body.get("notes"),
    )
    if not ok:
        raise HTTPException(404, "recipe not found")
    return {"ok": True}


@router.delete("/recipes/{recipe_id}")
async def delete_recipe(recipe_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    ok = kitchen_db.delete_recipe(cfg.db_path, user["id"], recipe_id)
    if not ok:
        raise HTTPException(404, "recipe not found")
    return {"ok": True}
