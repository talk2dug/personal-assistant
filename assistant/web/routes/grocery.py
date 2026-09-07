"""Grocery: the Kroger-backed shadow cart and the recipe-to-cart propose/confirm flow,
over the web UI. (Kitchen inventory, formerly this module's pantry board, moved to
kitchen.py/kitchen_db.py as its own quantity-tracked system -- see project memory
project_recipe_manager.md, Phase 3.)

Owner-only, same rule as chat's Kroger tools (see telegram_bot.py's docstring) --
Kroger holds the owner's real account and cart. Cart-add here is a direct call, not
routed through chat's pending_actions: the page's confirm button IS the confirmation,
same precedent as routes/review.py and routes/schedule.py's writes.
"""
import asyncio
import functools
import json

from fastapi import APIRouter, HTTPException, Request

from ...core import kroger_recipe
from ..auth import require_owner

router = APIRouter(prefix="/api/grocery", tags=["grocery"])


def _kroger(request: Request):
    kroger = request.app.state.kroger
    if kroger is None:
        raise HTTPException(503, "Kroger is not configured")
    return kroger


async def _call(kroger, name: str, arguments: dict | None = None) -> dict:
    """kroger.mcp_client.call_tool blocks and, for the real Kroger client, calls
    asyncio.run() internally -- which raises if run directly on uvicorn's own event
    loop (the exact bug found and fixed for Era/phone in chat.py; routes are just as
    exposed as chat was). Route it through a thread executor the same way."""
    loop = asyncio.get_running_loop()
    call = functools.partial(kroger.mcp_client.call_tool, name, arguments or {})
    try:
        return await loop.run_in_executor(None, call)
    except Exception as e:
        raise HTTPException(502, f"Kroger tool call failed: {e}")


def _unwrap(result: dict) -> dict:
    """Real kroger-mcp tool results come back as {"is_error", "content": [json_text]} --
    same shape scheduler.py's _era_payload unwraps for Era. The synthetic
    add_recipe_to_cart tool returns a plain dict already, so pass those through."""
    if "content" not in result:
        return result
    if result.get("is_error"):
        raise HTTPException(502, "; ".join(result.get("content") or ["kroger error"]))
    content = result.get("content") or []
    if not content:
        return {}
    return json.loads(content[0])


@router.get("/cart")
async def view_cart(request: Request):
    require_owner(request)
    kroger = _kroger(request)
    return _unwrap(await _call(kroger, "view_current_cart"))


@router.post("/cart/clear")
async def clear_cart(request: Request):
    """Clears Jarvis's local shadow-cart tracking only -- it cannot touch the real
    Kroger cart (the API exposes no such permission). Use this after checking out for
    real on Kroger's own site/app, so the shadow view doesn't keep showing stale items."""
    require_owner(request)
    kroger = _kroger(request)
    return _unwrap(await _call(kroger, "clear_current_cart"))


@router.get("/stores")
async def search_stores(request: Request, zip_code: str | None = None):
    require_owner(request)
    kroger = _kroger(request)
    args = {"zip_code": zip_code} if zip_code else {}
    return _unwrap(await _call(kroger, "search_locations", args))


@router.get("/stores/preferred")
async def get_preferred_store(request: Request):
    require_owner(request)
    kroger = _kroger(request)
    return _unwrap(await _call(kroger, "get_preferred_location"))


@router.post("/stores/preferred")
async def set_preferred_store(request: Request):
    require_owner(request)
    kroger = _kroger(request)
    body = await request.json()
    location_id = body.get("location_id")
    if not location_id:
        raise HTTPException(400, "location_id is required")
    return _unwrap(await _call(kroger, "set_preferred_location", {"location_id": location_id}))


@router.post("/recipe/propose")
async def propose_recipe(request: Request):
    """Read-only: searches for products matching each ingredient and returns the
    matches for review. Adds nothing to the cart -- see kroger_recipe.py."""
    require_owner(request)
    kroger = _kroger(request)
    body = await request.json()
    ingredients = body.get("ingredients") or []
    if not ingredients:
        raise HTTPException(400, "ingredients is required")
    result = await _call(kroger, kroger_recipe.RECIPE_TOOL_NAME, {
        "dish": body.get("dish", ""), "servings": body.get("servings"), "ingredients": ingredients,
    })
    return _unwrap(result)


@router.post("/recipe/confirm")
async def confirm_recipe(request: Request):
    """The real cart write -- the owner has already seen propose_recipe's matches and
    is confirming exactly these items by submitting this form, so this executes
    directly rather than through chat's pending_actions (same precedent as every
    other page-triggered write)."""
    require_owner(request)
    kroger = _kroger(request)
    body = await request.json()
    items = body.get("items") or []
    if not items:
        raise HTTPException(400, "items is required")
    return _unwrap(await _call(kroger, "bulk_add_to_cart", {"items": items}))
