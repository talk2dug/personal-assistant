"""Kitchen: the owner's own recipe catalog, kitchen inventory/purchase intake, and
shopping list over the web UI.

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


@router.get("/inventory")
async def list_inventory(request: Request, status: str | None = None):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return kitchen_db.list_inventory(cfg.db_path, user["id"], status)


@router.post("/inventory")
async def upsert_inventory(request: Request):
    """Manual add-or-adjust for one item -- the same primitive record_purchase/
    update_inventory_quantity use from chat, exposed for the Kitchen page's own form."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    item = (body.get("item") or "").strip()
    if not item:
        raise HTTPException(400, "item is required")
    result = kitchen_db.upsert_inventory_item(
        cfg.db_path, user["id"], item,
        quantity_delta=body.get("quantity_delta"), quantity_set=body.get("quantity"),
        unit=body.get("unit"), low_threshold=body.get("low_threshold"), notes=body.get("notes"),
        reason=body.get("reason", "manual_adjust"),
    )
    return result


@router.delete("/inventory/{item_id}")
async def remove_inventory(item_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    ok = kitchen_db.delete_inventory_item(cfg.db_path, user["id"], item_id)
    if not ok:
        raise HTTPException(404, "inventory item not found")
    return {"ok": True}


@router.post("/inventory/from-photo")
async def inventory_from_photo(request: Request, photo: UploadFile, item: str | None = None):
    """Reads a photographed item (a recount, most often) via vision and hands back an
    UNSAVED draft -- same review-before-commit contract as recipes/from-photo. `item`,
    when the owner is re-photographing something already tracked, tells the model the
    name up front so it only has to estimate quantity."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    bridge = request.app.state.bridge
    if bridge is None:
        raise HTTPException(503, "the GPU bridge is not configured")

    image_bytes = await photo.read()
    if not image_bytes:
        raise HTTPException(400, "empty photo")

    out_dir = pathlib.Path(cfg.generated_media_path) / "kitchen_photos"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = pathlib.Path(photo.filename or "").suffix or ".jpg"
    saved_path = out_dir / f"inventory_{user['id']}_{int(time.time() * 1000)}{ext}"
    saved_path.write_bytes(image_bytes)

    loop = asyncio.get_running_loop()
    call = functools.partial(kitchen_vision.analyze_inventory_photo, bridge, image_bytes, item)
    draft = await loop.run_in_executor(None, call)
    draft["photo_path"] = str(saved_path)
    return draft


@router.post("/purchases")
async def record_purchases(request: Request):
    """Commits a confirmed batch of purchased items -- shared by manual web entry and a
    confirmed (edited) list of receipt-scan draft items. Unlike inventory/from-photo and
    purchases/from-receipt, this one writes immediately: by the time it's called, the
    owner has already reviewed the list."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    items = body.get("items") or []
    reason = body.get("reason", "purchase_manual")
    updated = []
    for entry in items:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        result = kitchen_db.upsert_inventory_item(
            cfg.db_path, user["id"], name,
            quantity_delta=entry.get("quantity", 0), unit=entry.get("unit"), reason=reason,
        )
        updated.append(result)
    return {"ok": True, "updated": updated}


@router.post("/purchases/from-receipt")
async def purchases_from_receipt(request: Request, photo: UploadFile):
    """Reads a photographed receipt via vision and hands back an UNSAVED draft list of line
    items for the owner to check off/edit -- POST /purchases (above) is what actually
    commits them, same review-before-commit contract as every other AI-derived draft here."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    bridge = request.app.state.bridge
    if bridge is None:
        raise HTTPException(503, "the GPU bridge is not configured")

    image_bytes = await photo.read()
    if not image_bytes:
        raise HTTPException(400, "empty photo")

    out_dir = pathlib.Path(cfg.generated_media_path) / "kitchen_photos"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = pathlib.Path(photo.filename or "").suffix or ".jpg"
    saved_path = out_dir / f"receipt_{user['id']}_{int(time.time() * 1000)}{ext}"
    saved_path.write_bytes(image_bytes)

    loop = asyncio.get_running_loop()
    call = functools.partial(kitchen_vision.analyze_receipt_photo, bridge, image_bytes)
    draft = await loop.run_in_executor(None, call)
    draft["photo_path"] = str(saved_path)
    return draft


@router.get("/shopping-list")
async def list_shopping_list(request: Request, status: str | None = "pending"):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return kitchen_db.list_shopping_list(cfg.db_path, user["id"], status)


@router.post("/shopping-list")
async def add_to_shopping_list(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    item = (body.get("item") or "").strip()
    if not item:
        raise HTTPException(400, "item is required")
    return kitchen_db.add_to_shopping_list(cfg.db_path, user["id"], item, body.get("quantity_hint"))


@router.delete("/shopping-list/{item}")
async def remove_from_shopping_list(item: str, request: Request):
    """`item` is the item's own text, not a numeric id -- shopping_list_items has no
    single stable id the frontend already knows the way inventory rows do (a repeated
    add/auto-flag can produce several historical rows for the same name over time), and
    the underlying kitchen_db functions already resolve by normalized name."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    ok = kitchen_db.remove_from_shopping_list(cfg.db_path, user["id"], item)
    if not ok:
        raise HTTPException(404, "item not found (pending) on the shopping list")
    return {"ok": True}


@router.post("/shopping-list/{item}/purchased")
async def mark_shopping_list_item_purchased(item: str, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    ok = kitchen_db.mark_shopping_list_item_purchased(cfg.db_path, user["id"], item)
    if not ok:
        raise HTTPException(404, "item not found (pending) on the shopping list")
    return {"ok": True}
