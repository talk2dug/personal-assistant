"""Drive inventory: every folder on every drive, browsable, and tagged with where it belongs.

Read-mostly, like /api/media. The scan itself is scripts/inventory_drives.py -- walking
a few terabytes across the network takes far longer than any request should. These
endpoints serve what it wrote to inventory.db and record Jack's category tags.

Owner-only: this is a list of every file on every machine in the house.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import drive_inventory as inv
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
