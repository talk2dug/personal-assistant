"""Inventory every folder on every drive on every machine into inventory.db.

    python scripts/inventory_drives.py                 # every host, every drive
    python scripts/inventory_drives.py --list          # discover drives only, no scanning
    python scripts/inventory_drives.py --host simrig   # one host (repeatable)
    python scripts/inventory_drives.py --skip-fresh 24 # skip drives scanned in the last 24h
    python scripts/inventory_drives.py --reclassify   # re-run category rules, no rescan

Where the machines come from: config.json's ssh_hosts (the same registry the sys-admin
agent uses), this box's own drives, and any older address media_scan found on
2026-09-04 that isn't in that registry (a few Pis that only take the scan password).

How each is walked:
  * this box (Windows)  -- os.scandir, directly. Fastest by far.
  * Linux over SSH      -- `find -printf` ON the box, one line per entry streamed back.
                           Walking an SMB share from here would be a round trip per stat.
  * Windows over SSH    -- a PowerShell walker doing the same (simrig).

Parallel across hosts, serial within a host (two drives on one USB controller only slow
each other down). Each drive's scan replaces its previous one only when it completes.
"""
import argparse
import base64
import concurrent.futures as cf
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import paramiko  # noqa: E402

from assistant.config import load_config  # noqa: E402
from assistant.core import drive_inventory as inv  # noqa: E402

CFG = load_config()
DB = inv.default_db_path(CFG.db_path)
LOG_PATH = Path(CFG.db_path).resolve().parent / "logs" / "inventory.log"
_log_lock = threading.Lock()

# Hosts not worth walking: an appliance OS whose "disk" is a container.
SKIP_HOSTS = {"homeassistant"}
MIN_VOLUME_BYTES = 1 << 30
LINUX_SKIP_FSTYPES = {"tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs", "nfs", "nfs4", "cifs",
                      "smbfs", "fuse.sshfs", "proc", "sysfs", "autofs", "ramfs", "zram"}


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    with _log_lock:
        print(line, flush=True)
        LOG_PATH.parent.mkdir(exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


# ============================================================================ hosts

def host_list() -> list[dict]:
    hosts = [{"name": "jarvisbox", "address": "127.0.0.1", "local": True}]
    known_addresses = set()
    for name, spec in sorted((CFG.ssh_hosts or {}).items()):
        known_addresses.add(spec.get("host"))
        if name == "jarvisbox" or name in SKIP_HOSTS:
            continue
        hosts.append({"name": name, "address": spec["host"], "user": spec.get("user"),
                      "key_path": spec.get("key_path"), "password": spec.get("password")})
    # Older boxes media_scan reached with the shared scan password.
    try:
        with sqlite3.connect(CFG.db_path) as conn:
            legacy = conn.execute("SELECT address, hostname FROM scan_hosts WHERE kind != 'local'").fetchall()
    except sqlite3.Error:
        legacy = []
    known_names = {h["name"].lower() for h in hosts}
    for address, hostname in legacy:
        name = (hostname or address).lower()
        # A registry host that has since changed IP shows up here under its old address;
        # the registry entry is the live one.
        if address not in known_addresses and name not in known_names:
            hosts.append({"name": name, "address": address, "legacy": True})
    return hosts


# Two registry entries can be the same machine (pi5nas002 by mDNS name, pinas002 by IP).
# The first to report a real hostname claims it; the other stands down rather than
# scanning the same drives twice at once.
_claimed: dict[str, str] = {}
_claim_lock = threading.Lock()


_uid_owner: dict[str, str] = {}


def scope_uids(hostname: str, vols: list[dict]) -> None:
    """Make each volume's uid unique to one physical drive.

    Filesystem UUIDs are NOT unique in this house: Pis flashed from one image share the
    same rootfs UUID (jarvisaudio1/2, both HackRF nodes and both NASes did), so keying on
    it alone merged six machines' OS drives into three and each scan overwrote the other.
    A machine's own OS drive never moves between boxes, so it is keyed to the machine.
    Data drives keep the bare UUID so one that moves between hosts keeps its tags; if two
    hosts report the same one in a run, the later is scoped to its host too.
    """
    for v in vols:
        if v["is_system"]:
            v["uid"] = f"{hostname.lower()}:{v['uid']}"
            continue
        with _claim_lock:
            owner = _uid_owner.setdefault(v["uid"], hostname.lower())
        if owner != hostname.lower():
            v["uid"] = f"{hostname.lower()}:{v['uid']}"


def claim(hostname: str, name: str) -> str | None:
    """Returns the host name that already claimed this machine, or None if we got it."""
    key = hostname.strip().lower()
    with _claim_lock:
        owner = _claimed.get(key)
        if owner is None:
            _claimed[key] = name
        return owner


def ssh_connect(h: dict) -> paramiko.SSHClient:
    attempts = []
    if h.get("user"):
        kw = {"username": h["user"]}
        if h.get("key_path"):
            kw["key_filename"] = str(Path(h["key_path"]).expanduser())
        if h.get("password"):
            kw["password"] = h["password"]
        attempts.append(kw)
    if CFG.scan_ssh_password:
        attempts += [{"username": u, "password": CFG.scan_ssh_password} for u in CFG.scan_ssh_users]
    last = None
    for kw in attempts:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(h["address"], timeout=12, banner_timeout=20, auth_timeout=20,
                      allow_agent=False, look_for_keys=False, **kw)
            return c
        except Exception as e:
            last = e
            c.close()
    raise RuntimeError(f"could not log in: {last}")


def run(client, cmd: str, timeout: int = 60) -> str:
    _, out, err = client.exec_command(cmd, timeout=timeout)
    return out.read().decode("utf-8", "replace")


def stream(client, cmd: str):
    """Yield stdout lines as they arrive; count stderr lines (permission denied etc.)."""
    chan = client.get_transport().open_session()
    chan.exec_command(cmd)
    buf, errs = b"", [0]
    while True:
        got = False
        if chan.recv_ready():
            buf += chan.recv(1 << 20)
            got = True
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                yield ln.decode("utf-8", "replace").rstrip("\r")
        if chan.recv_stderr_ready():
            errs[0] += chan.recv_stderr(1 << 16).count(b"\n")
            got = True
        if not got:
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            time.sleep(0.02)
    if buf:
        yield buf.decode("utf-8", "replace").rstrip("\r")


def ps_encoded(script: str) -> str:
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(
        script.encode("utf-16-le")).decode()


# ========================================================================= discovery

LINUX_DISCOVER = r"""
hostname
echo '###'
df -B1 --output=source,fstype,size,used,target 2>/dev/null | tail -n +2
echo '###'
for t in $(df --output=target 2>/dev/null | tail -n +2); do
  printf '%s\t%s\t%s\n' "$t" "$(findmnt -n -o UUID --target "$t" 2>/dev/null | head -1)" \
    "$(findmnt -n -o LABEL --target "$t" 2>/dev/null | head -1)"
done
"""

WIN_DISCOVER = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:COMPUTERNAME
Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -in 2,3 -and $_.Size } |
  Select-Object DeviceID, VolumeName, VolumeSerialNumber, FileSystem, Size, FreeSpace |
  ConvertTo-Json -Compress
$env:SystemDrive
"""


def discover_linux(client) -> tuple[str, list[dict]]:
    out = run(client, LINUX_DISCOVER)
    parts = out.split("###")
    hostname = parts[0].strip()
    ids = {}
    for ln in parts[2].strip().splitlines() if len(parts) > 2 else []:
        t, uuid, label = (ln.split("\t") + ["", ""])[:3]
        ids[t] = (uuid.strip(), label.strip())
    vols = []
    for ln in parts[1].strip().splitlines() if len(parts) > 1 else []:
        cols = ln.split(None, 4)
        if len(cols) < 5:
            continue
        source, fstype, size, used, target = cols
        if fstype in LINUX_SKIP_FSTYPES or target.startswith(("/boot", "/snap", "/var/lib/docker", "/run", "/sys", "/proc", "/dev")):
            continue
        if fstype.startswith("fuse.") and fstype != "fuseblk":
            continue
        size, used = int(size), int(used)
        if size < MIN_VOLUME_BYTES:
            continue
        uuid, label = ids.get(target, ("", ""))
        vols.append({"mount": target, "fstype": fstype, "size": size, "used": used, "label": label,
                     "uid": f"uuid:{uuid}" if uuid else f"{hostname}:{target}", "is_system": target == "/"})
    # The same device mounted twice (bind mounts) would be scanned twice.
    seen, unique = set(), []
    for v in sorted(vols, key=lambda v: len(v["mount"])):
        if v["uid"] not in seen:
            seen.add(v["uid"])
            unique.append(v)
    return hostname, unique


def _win_volumes(payload: str, system_drive: str, hostname: str) -> list[dict]:
    data = json.loads(payload) if payload.strip() else []
    if isinstance(data, dict):
        data = [data]
    vols = []
    for d in data:
        size = int(d.get("Size") or 0)
        if size < MIN_VOLUME_BYTES:
            continue
        serial = d.get("VolumeSerialNumber") or ""
        letter = d["DeviceID"]
        vols.append({"mount": letter + "\\", "fstype": d.get("FileSystem") or "", "size": size,
                     "used": size - int(d.get("FreeSpace") or 0), "label": d.get("VolumeName") or "",
                     "uid": f"winserial:{serial}" if serial else f"{hostname}:{letter}",
                     "is_system": letter.upper() == system_drive.upper()})
    return vols


def discover_windows_local() -> tuple[str, list[dict]]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", WIN_DISCOVER],
                         capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[0].strip(), _win_volumes(lines[1] if len(lines) > 2 else "[]", lines[-1].strip(), lines[0].strip())


def discover_windows_ssh(client) -> tuple[str, list[dict]]:
    out = run(client, ps_encoded(WIN_DISCOVER), timeout=90)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[0].strip(), _win_volumes(lines[1] if len(lines) > 2 else "[]", lines[-1].strip(), lines[0].strip())


# ============================================================================ walkers

def walk_local(root: str):
    """(kind, size, mtime, relpath) for everything under a local Windows drive."""
    root = os.path.abspath(root)
    stack = [root]
    errors = 0
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            errors += 1
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                        continue
                    rel = os.path.relpath(e.path, root).replace("\\", "/")
                    if e.is_dir(follow_symlinks=False):
                        yield ("d", 0, None, rel)
                        stack.append(e.path)
                    else:
                        st = e.stat(follow_symlinks=False)
                        yield ("f", st.st_size, st.st_mtime, rel)
                except OSError:
                    errors += 1


def linux_find_command(mount: str, sudo: bool = False) -> str:
    """Read-only `find`. Under passwordless sudo where the box allows it, so folders the
    login user can't read aren't silently missing from the inventory."""
    prune = ""
    if mount == "/":
        prune = "\\( " + " -o ".join(f"-path /{p}" for p in inv.LINUX_PRUNE) + " \\) -prune -o "
    return (f"{'sudo -n ' if sudo else ''}find {shlex.quote(mount)} -xdev {prune}\\( -type f -o -type d \\) "
            f"-printf '%y\\t%s\\t%T@\\t%P\\n' 2>/dev/null")


WIN_WALK = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$out = [Console]::Out
$root = '__ROOT__'
$epoch = [DateTime]::new(1970,1,1,0,0,0,[DateTimeKind]::Utc)
$stack = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
$stack.Push([IO.DirectoryInfo]::new($root))
$rl = $root.Length
while ($stack.Count) {
  $d = $stack.Pop()
  try { $items = $d.EnumerateFileSystemInfos() } catch { [Console]::Error.WriteLine("E"); continue }
  try {
    foreach ($i in $items) {
      if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
      $rel = $i.FullName.Substring($rl).Replace('\','/')
      if ($i -is [IO.DirectoryInfo]) { $out.WriteLine("d`t0`t`t$rel"); $stack.Push($i) }
      else { $out.WriteLine("f`t$($i.Length)`t$([long]($i.LastWriteTimeUtc - $epoch).TotalSeconds)`t$rel") }
    }
  } catch { [Console]::Error.WriteLine("E") }
}
"""


def parse_lines(lines):
    for ln in lines:
        parts = ln.split("\t", 3)
        if len(parts) != 4:
            continue
        kind, size, mtime, rel = parts
        if kind not in ("f", "d") or not rel:
            continue
        try:
            yield kind, int(size or 0), float(mtime) if mtime else None, rel
        except ValueError:
            continue


# ============================================================================ scanning

def fresh(volume_id: int, hours: float) -> bool:
    if not hours:
        return False
    with inv.connect(DB) as conn:
        r = conn.execute("SELECT finished_at FROM inv_scans WHERE volume_id=? AND status='done' "
                         "ORDER BY id DESC LIMIT 1", (volume_id,)).fetchone()
    if not r:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(r[0])
    return age.total_seconds() < hours * 3600


def scan_volume(host_name: str, host_id: int, v: dict, entries_fn, skip_fresh: float) -> None:
    vol_id = inv.upsert_volume(DB, host_id, v["uid"], v["mount"], v["label"], v["fstype"],
                               v["size"], v["used"], v["is_system"])
    if fresh(vol_id, skip_fresh):
        log(f"[{host_name}] {v['mount']} scanned recently, skipping")
        return
    log(f"[{host_name}] {v['mount']} ({v['label'] or 'no label'}, {human(v['used'])} used) scanning...")
    scan_id = inv.start_scan(DB, vol_id)
    writer = inv.ScanWriter(DB, vol_id, scan_id, is_system=v["is_system"])
    started = time.time()
    try:
        n = 0
        for entry in entries_fn():
            writer.add(*entry)
            n += 1
            if n % 250_000 == 0:
                log(f"[{host_name}] {v['mount']} ... {n:,} entries, {human(writer.agg.bytes)}")
        res = writer.finish()
        log(f"[{host_name}] {v['mount']} done: {res['files']:,} files, {res['dirs']:,} folders, "
            f"{human(res['bytes'])} in {time.time() - started:.0f}s")
    except Exception as e:
        writer.abort(str(e))
        log(f"[{host_name}] {v['mount']} FAILED: {e}")


def scan_host(h: dict, args) -> None:
    name = h["name"]
    try:
        if h.get("local"):
            hostname, vols = discover_windows_local()
            host_id = inv.upsert_host(DB, name, h["address"], "windows")
            walker = lambda v: (lambda: walk_local(v["mount"]))  # noqa: E731
            client = None
        else:
            client = ssh_connect(h)
            uname = run(client, "uname -s 2>/dev/null", timeout=20).strip()
            if uname.lower() == "linux":
                hostname, vols = discover_linux(client)
                os_name = "linux"
                sudo = run(client, "sudo -n true 2>/dev/null && echo yes", timeout=20).strip() == "yes"
                walker = lambda v: (lambda: parse_lines(stream(  # noqa: E731
                    client, linux_find_command(v["mount"], sudo))))
            else:
                hostname, vols = discover_windows_ssh(client)
                os_name = "windows"
                walker = lambda v: (lambda: parse_lines(stream(  # noqa: E731
                    client, ps_encoded(WIN_WALK.replace("__ROOT__", v["mount"].replace("'", "''"))))))
            host_id = inv.upsert_host(DB, name, h["address"], os_name)
    except Exception as e:
        inv.upsert_host(DB, name, h["address"], "", reachable=False, error=str(e)[:500])
        log(f"[{name}] unreachable: {e}")
        return

    owner = claim(hostname, name)
    if owner is not None:
        log(f"[{name}] is the same machine as {owner} ({hostname}); skipping")
        inv.merge_host(DB, name, into=owner)
        if client is not None:
            client.close()
        return

    scope_uids(hostname, vols)
    inv.mark_host_volumes_offline(DB, host_id, {v["uid"] for v in vols})
    log(f"[{name}] {hostname}: " + (", ".join(f"{v['mount']} {human(v['used'])}" for v in vols) or "no data drives"))
    if args.list:
        for v in vols:
            inv.upsert_volume(DB, host_id, v["uid"], v["mount"], v["label"], v["fstype"],
                              v["size"], v["used"], v["is_system"])
        return
    try:
        for v in sorted(vols, key=lambda v: v["used"]):
            scan_volume(name, host_id, v, walker(v), args.skip_fresh)
    finally:
        if client is not None:
            client.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", help="host name from ssh_hosts (repeatable)")
    ap.add_argument("--list", action="store_true", help="discover drives only")
    ap.add_argument("--skip-fresh", type=float, default=0, help="skip drives scanned within N hours")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--reclassify", action="store_true",
                    help="re-run the category suggestions over existing scans, no walking")
    args = ap.parse_args()

    inv.init_db(DB)
    if args.reclassify:
        log(f"reclassified {inv.reclassify(DB):,} folders")
        return
    hosts = host_list()
    if args.host:
        want = {h.lower() for h in args.host}
        hosts = [h for h in hosts if h["name"].lower() in want]
    log(f"inventory -> {DB}: {len(hosts)} hosts: {', '.join(h['name'] for h in hosts)}")
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda h: scan_host(h, args), hosts))
    log("inventory finished")


if __name__ == "__main__":
    main()
