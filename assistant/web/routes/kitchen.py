"""Kitchen: the owner's own recipe catalog (and, in later phases, kitchen inventory,
purchase intake, and the shopping list) over the web UI.

Owner-only, same rule as every other personal-data route (grocery.py, schedule.py).
"""
import asyncio
import functools
import mimetypes
import pathlib
import time

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ...core import kitchen_db, kitchen_vision
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


@router.post("/recipes/from-photo")
async def recipe_from_photo(request: Request, photo: UploadFile):
    """Reads a photographed recipe via the GPU bridge's vision model and hands back an
    UNSAVED draft for the owner to review/edit -- vision extraction from a handwritten
    card or a cluttered page is genuinely lossy, so this never writes to the recipe
    catalog itself. POST /recipes (already built) is what actually saves it, once the
    owner has looked the draft over."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    bridge = request.app.state.bridge
    if bridge is None:
        raise HTTPException(503, "the GPU bridge is not configured")

    image_bytes = await photo.read()
    if not image_bytes:
        raise HTTPException(400, "empty photo")

    # Saved before analysis (not after) so a photo that fails to parse still isn't lost --
    # the owner can retry, edit around a bad read, or re-upload without redoing the shot.
    out_dir = pathlib.Path(cfg.generated_media_path) / "kitchen_photos"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = pathlib.Path(photo.filename or "").suffix or ".jpg"
    saved_path = out_dir / f"recipe_{user['id']}_{int(time.time() * 1000)}{ext}"
    saved_path.write_bytes(image_bytes)

    loop = asyncio.get_running_loop()
    call = functools.partial(kitchen_vision.analyze_recipe_photo, bridge, image_bytes)
    draft = await loop.run_in_executor(None, call)
    draft["photo_path"] = str(saved_path)
    return draft


@router.get("/recipes/{recipe_id}/photo")
async def recipe_photo(recipe_id: int, request: Request):
    """Same guarded-path-then-FileResponse pattern as routes/review.py's media endpoint --
    the path comes from a database row, so it's resolved and checked to be inside
    generated_media_path before serving, rather than trusted outright."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    recipe = kitchen_db.get_recipe(cfg.db_path, user["id"], recipe_id)
    if recipe is None or not recipe.get("photo_path"):
        raise HTTPException(404, "no photo for that recipe")

    root = pathlib.Path(cfg.generated_media_path).resolve()
    path = pathlib.Path(recipe["photo_path"]).resolve()
    if not path.is_file():
        raise HTTPException(404, "the file is no longer on disk")
    if root not in path.parents:
        raise HTTPException(403, "that file is outside the generated media directory")

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)
