"""Drive inventory: every folder on every drive, browsable, and tagged with where it belongs.

Read-mostly, like /api/media. The scan itself is scripts/inventory_drives.py -- walking
a few terabytes across the network takes far longer than any request should. These
endpoints serve what it wrote to inventory.db and record Jack's category tags.

Owner-only: this is a list of every file on every machine in the house.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import drive_inventory as inv
from ...core import storage_plan
from ..auth import require_owner

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


def _db(request: Request) -> str:
    require_owner(request)
    path = inv.default_db_path(request.app.state.cfg.db_path)
    inv.init_db(path)
    return path


@router.get("/overview")
async def overview(request: Request):
    return inv.overview(_db(request))


@router.get("/folders/{dir_id}")
async def folder(request: Request, dir_id: int):
    try:
        return inv.folder(_db(request), dir_id)
    except LookupError:
        raise HTTPException(404, "no such folder -- the drive may have been rescanned")


@router.get("/search")
async def search(request: Request, q: str = ""):
    return {"results": inv.search(_db(request), q)}


@router.post("/folders/{dir_id}/tag")
async def tag(request: Request, dir_id: int):
    """{"category": "<key>"} tags the folder and everything under it; {"category": null}
    clears the tag so the suggestion shows through again."""
    db = _db(request)
    body = await request.json() or {}
    try:
        return inv.set_tag(db, dir_id, body.get("category"), body.get("note"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError:
        raise HTTPException(404, "no such folder")


# ------------------------------------------------------------------ move plan
# The consolidation plan (core/storage_plan.py): proposals only. Approving a group here
# marks it for the mover; nothing on any drive changes from these endpoints.

@router.get("/plan")
async def plan(request: Request):
    return storage_plan.summary(_db(request))


@router.post("/plan/rebuild")
async def plan_rebuild(request: Request):
    """Recompute from the current inventory, keeping approvals and exclusions."""
    db = _db(request)
    return storage_plan.build(db, protected=storage_plan.default_protected(request.app.state.cfg.db_path))


@router.get("/plan/units")
async def plan_units(request: Request, section: str, bucket: str, source_volume_id: int):
    return {"units": storage_plan.units(_db(request), section, bucket, source_volume_id)}


@router.post("/plan/status")
async def plan_status(request: Request):
    """{"status": "approved"|"excluded"|"proposed", "ids": [...]} or {..., "group":
    {"section", "bucket", "source_volume_id"}}."""
    db = _db(request)
    body = await request.json() or {}
    try:
        n = storage_plan.set_status(db, body.get("status"), ids=body.get("ids"), group=body.get("group"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "changed": n}
