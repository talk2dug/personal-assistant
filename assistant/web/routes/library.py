"""The Design Library — one browse surface over two backends.

'design' is design_assets.py (the curated catalogue Jack emails/Leonardo-imports into);
every other category is library_assets.py's shared table. The frontend never has to know
that split -- every route here takes `category` and dispatches to whichever module owns
it, so the browser UI is one component regardless of which table answers.

'local_import' is NOT handled here at all -- that stays media_scan.py's existing
/api/media/* routes untouched. The Design Library page calls those directly for that one
category rather than this router re-wrapping them, so there is exactly one place that
owns drive-scan data.
"""
import json

from fastapi import APIRouter, HTTPException, Request

from ...core import design_assets, library_assets
from ..auth import require_owner

router = APIRouter(prefix="/api/library", tags=["library"])

VALID_STATUS = {"available", "used", "retired"}
ALL_CATEGORIES = ("design",) + library_assets.CATEGORIES


def _catalogue(db_path: str, owner_id: int, category: str | None, status: str | None,
              search: str | None, limit: int, offset: int) -> list[dict]:
    if category == "design":
        # design_assets.catalogue predates search/offset -- filtered in Python rather
        # than teaching that module pagination for a table that's a few hundred rows.
        rows = design_assets.catalogue(db_path, owner_id, status=status, limit=limit + offset)
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in (r.get("title") or "").lower()]
        return [{**r, "category": "design"} for r in rows[offset:offset + limit]]
    if category and category not in library_assets.CATEGORIES:
        raise HTTPException(400, f"category must be one of {ALL_CATEGORIES}")
    rows = library_assets.catalogue(db_path, owner_id, category=category, status=status,
                                    search=search, limit=limit, offset=offset)
    if category is None:
        # No category filter: designs belong in the "everything" view too.
        design_rows = design_assets.catalogue(db_path, owner_id, status=status, limit=limit)
        rows = sorted(
            rows + [{**r, "category": "design"} for r in design_rows],
            key=lambda r: r["id"], reverse=True)[:limit]
    return rows


@router.get("/assets")
async def assets(request: Request, category: str | None = None, status: str | None = None,
                 search: str | None = None, limit: int = 100, offset: int = 0):
    owner = require_owner(request)
    if status and status not in VALID_STATUS:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUS)}")
    return {"assets": _catalogue(request.app.state.cfg.db_path, owner["id"], category,
                                  status, search, min(limit, 500), offset)}


@router.get("/categories")
async def categories(request: Request):
    """Counts per category, for the rail. Designs comes from design_assets separately
    since it isn't part of library_assets' shared table."""
    owner = require_owner(request)
    db = request.app.state.cfg.db_path
    counts = library_assets.category_counts(db, owner["id"])
    counts["design"] = len(design_assets.catalogue(db, owner["id"], status=None, limit=100000))
    return {"counts": counts}


@router.post("/assets")
async def add_asset(request: Request):
    """Manual add -- what an upload becomes once the file is on disk. Designs still go
    through leonardo.py/photo_intake.py's own add() for their own reasons (dedup by
    external_id); this is for the other eight categories, added by hand or by a future
    scanner."""
    owner = require_owner(request)
    body = await request.json()
    category = body.get("category")
    path = (body.get("path") or "").strip()
    if category not in library_assets.CATEGORIES:
        raise HTTPException(400, f"category must be one of {library_assets.CATEGORIES}")
    if not path:
        raise HTTPException(400, "path is required")
    asset_id = library_assets.add(
        request.app.state.cfg.db_path, owner["id"], category, path,
        title=body.get("title"), source=body.get("source", "uploaded"),
        note=body.get("note"), metadata=body.get("metadata"))
    return {"ok": True, "id": asset_id}


def _set_status(db_path: str, owner_id: int, category: str, asset_id: int, status: str) -> bool:
    if category == "design":
        return design_assets.set_status(db_path, owner_id, asset_id, status)
    return library_assets.set_status(db_path, owner_id, asset_id, status)


@router.post("/assets/{asset_id}/decide")
async def decide(asset_id: int, request: Request):
    owner = require_owner(request)
    body = await request.json()
    category = body.get("category")
    status = body.get("status", "")
    if status not in VALID_STATUS:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUS)}")
    try:
        ok = _set_status(request.app.state.cfg.db_path, owner["id"], category, asset_id, status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "no such asset")
    return {"ok": True, "status": status}


@router.post("/assets/decide-bulk")
async def decide_bulk(request: Request):
    owner = require_owner(request)
    body = await request.json()
    category = body.get("category")
    status = body.get("status", "")
    ids = body.get("ids") or []
    if status not in VALID_STATUS:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUS)}")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids must be a non-empty list")
    db = request.app.state.cfg.db_path
    if category == "design":
        changed = sum(1 for i in ids if design_assets.set_status(db, owner["id"], int(i), status))
    else:
        changed = library_assets.set_status_bulk(db, owner["id"], [int(i) for i in ids], status)
    return {"ok": True, "changed": changed}


@router.post("/mockups/generate")
async def generate_mockup(request: Request):
    """Generate one mockup image from an existing design and file it into the library.

    This is deliberately the plain generate_media path (simrig's GPU bridge, a text
    prompt in, an image out) -- NOT the Gemini apparel-compositing pipeline described in
    docs/mockup-system.md (the flatten trick, occlusion rules, zone placement onto a real
    photo of a person). That pipeline needs a real human-model photo library that doesn't
    exist yet and is its own follow-up project; wiring a plain prompt-to-image call here
    keeps this route honest about what it actually does rather than faking the harder
    compositing work with a generic render.
    """
    owner = require_owner(request)
    bridge = request.app.state.bridge
    if bridge is None:
        raise HTTPException(503, "the GPU bridge is not configured")
    body = await request.json()
    db = request.app.state.cfg.db_path
    design_id = body.get("design_asset_id")
    if not design_id:
        raise HTTPException(400, "design_asset_id is required")
    design = design_assets.get(db, owner["id"], int(design_id))
    if design is None:
        raise HTTPException(404, "no such design asset")

    prompt = body.get("prompt") or (
        f"Product mockup photo featuring the design '{design.get('title') or 'artwork'}', "
        "clean professional product photography, studio lighting."
    )
    job = bridge.run_sync("art_director", "image_generation", prompt,
                          options={k: body[k] for k in ("negative", "width", "height") if body.get(k)},
                          timeout=300)
    if job.get("status") != "done":
        return {"ok": False, "status": job.get("status"), "error": job.get("error")}
    files = json.loads(job["result"]).get("files", [])
    if not files:
        return {"ok": False, "status": "done", "error": "no file returned"}
    asset_id = library_assets.add(
        db, owner["id"], "mockup", files[0], source="generated",
        title=f"Mockup — {design.get('title') or 'design ' + str(design_id)}",
        metadata={"design_asset_id": design_id, "prompt": prompt})
    return {"ok": True, "id": asset_id, "path": files[0]}
