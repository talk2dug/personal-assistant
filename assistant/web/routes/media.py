"""The media catalogue — what was found on the network, and what to do with it.

Read-mostly. The scanning itself runs as a standalone script (scripts/scan_network_media.py)
rather than inside the web process: a sweep of nine hosts takes long enough that tying it
to a request, or to the uptime of the web server, would be a mistake. These endpoints
serve what that script has already written and let the owner mark folders import/skip.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import media_scan
from ..auth import require_user

router = APIRouter(prefix="/api/media", tags=["media"])

VALID_DECISIONS = {"undecided", "import", "skip", "imported"}


def _owner_id(request: Request) -> int:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "the media catalogue is owner-only")
    return user["id"]


@router.get("/summary")
async def summary(request: Request):
    _owner_id(request)
    return media_scan.summary(request.app.state.cfg.db_path)


@router.get("/hosts")
async def hosts(request: Request):
    _owner_id(request)
    db = request.app.state.cfg.db_path
    return {
        "hosts": media_scan.list_hosts(db),
        "volumes": media_scan.list_volumes(db),
    }


@router.get("/folders")
async def folders(request: Request, min_files: int = 1, kind: str = "",
                  decision: str = "", limit: int = 500, offset: int = 0):
    _owner_id(request)
    return {
        "folders": media_scan.folder_table(
            request.app.state.cfg.db_path,
            min_files=min_files, kind=kind, decision=decision,
            limit=min(limit, 2000), offset=offset),
    }


@router.post("/folders/{folder_id}/decide")
async def decide(request: Request, folder_id: int):
    _owner_id(request)
    body = await request.json()
    decision = (body or {}).get("decision", "")
    if decision not in VALID_DECISIONS:
        raise HTTPException(400, f"decision must be one of {sorted(VALID_DECISIONS)}")
    ok = media_scan.set_decision(
        request.app.state.cfg.db_path, folder_id, decision, (body or {}).get("notes", ""))
    if not ok:
        raise HTTPException(404, "no such folder")
    return {"ok": True, "decision": decision}


@router.post("/folders/decide-bulk")
async def decide_bulk(request: Request):
    """Mark many folders at once — triaging hundreds of rows one request at a time is
    the difference between a usable review page and an abandoned one."""
    _owner_id(request)
    body = await request.json()
    decision = (body or {}).get("decision", "")
    ids = (body or {}).get("ids") or []
    if decision not in VALID_DECISIONS:
        raise HTTPException(400, f"decision must be one of {sorted(VALID_DECISIONS)}")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids must be a non-empty list")
    db = request.app.state.cfg.db_path
    changed = sum(1 for i in ids if media_scan.set_decision(db, int(i), decision))
    return {"ok": True, "changed": changed}


@router.get("/collection")
async def collection(request: Request):
    """Progress of the copy into the local collection, per source drive."""
    _owner_id(request)
    return media_scan.collection_summary(request.app.state.cfg.db_path)


@router.get("/archives")
async def archives(request: Request, limit: int = 200, unreadable: bool = False):
    """Archives ranked by how much media they hold.

    Separate from /folders because an unopened bundle is a different kind of decision:
    the folder view says "this directory has 60 STLs", while this says "this one 616MB
    zip has 7,228 SVGs in it that nothing has ever seen."
    """
    _owner_id(request)
    return {"archives": media_scan.archive_table(
        request.app.state.cfg.db_path, limit=min(limit, 1000), unreadable=unreadable)}


@router.get("/duplicates")
async def duplicates(request: Request, limit: int = 200):
    _owner_id(request)
    return {"groups": media_scan.duplicate_groups(
        request.app.state.cfg.db_path, limit=min(limit, 1000))}
