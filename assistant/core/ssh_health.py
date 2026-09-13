"""Live reachability for every registered SSH host (ssh_ops.py's ssh_hosts registry) --
not just Jarvis's own machines. A plain TCP connect to port 22, never a real SSH login:
the health panel answers "is this box up" for the owner's own network topology view, not
"can Jarvis actually run commands there," so no credentials are read or used here at all.
"""
import socket
import time
from concurrent.futures import ThreadPoolExecutor

SSH_PORT = 22
DEFAULT_TIMEOUT = 2.0


def check_host(name: str, cfg: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """One host's live reachability, merged with the same is_jarvis_host/purpose
    metadata ssh_ops.SSHOpsClient.describe_hosts() exposes to the LLM -- the health
    panel and the model see the same facts about what each box is."""
    start = time.monotonic()
    try:
        with socket.create_connection((cfg["host"], SSH_PORT), timeout=timeout):
            reachable = True
    except OSError:
        reachable = False
    latency_ms = round((time.monotonic() - start) * 1000) if reachable else None
    return {
        "name": name,
        "host": cfg["host"],
        "is_jarvis_host": bool(cfg.get("is_jarvis_host", False)),
        "purpose": cfg.get("purpose", ""),
        "reachable": reachable,
        "latency_ms": latency_ms,
    }


def check_all_hosts(hosts: dict, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Every registered host, checked concurrently -- sequential would be up to
    N * timeout worst-case (each unreachable host costs the full timeout), a bad look
    for a panel meant to refresh on a normal dashboard poll interval."""
    if not hosts:
        return []
    names = sorted(hosts)
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = list(pool.map(lambda n: check_host(n, hosts[n], timeout), names))
    return results
