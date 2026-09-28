"""Live health for the owner's own network infrastructure -- every SSH host registered
in config.json's ssh_hosts, not just the ones running Jarvis's own code. Companion to
list_ssh_hosts (the chat tool dev-team employees use, business_tools.py), which already
returns the same is_jarvis_host/purpose metadata but never checks whether a host is
actually up right now.

Owner-only, matching the sensitivity of seeing the real host/credential registry exists
at all -- same gate as Credit/Crypto/Personal.
"""
from fastapi import APIRouter, Request

from ...core import network_overview, ssh_health
from ..auth import require_owner

router = APIRouter(prefix="/api/infra", tags=["infra"])


@router.get("/ssh-hosts")
async def ssh_hosts_status(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    return {"hosts": ssh_health.check_all_hosts(cfg.ssh_hosts or {})}


@router.get("/network")
async def network(request: Request):
    """The Network section: recorded host checks, Omada devices/clients and recent ops
    plans, joined. Reads jarvis.db only -- never probes -- so it stays fast when a box is
    down, which is exactly when someone opens it."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    return network_overview.build(cfg.db_path, cfg, user["id"])
