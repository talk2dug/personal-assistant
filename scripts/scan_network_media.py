"""Discover every Linux host on the LAN, find its drives, and catalogue the media.

Built to run unattended. Discovery and scanning are one command because the two halves
are useless apart: a host list without drives tells you nothing, and a drive list goes
stale the moment something gets unplugged.

    python scripts/scan_network_media.py              # discover + scan everything
    python scripts/scan_network_media.py --discover   # discover only, no scanning
    python scripts/scan_network_media.py --host 192.168.0.32

Parallel across hosts, serial within a host. Two `find` runs against two USB drives on
the same box would just make both slower — they share one USB controller — but nine
boxes scanning at once costs nothing since the work is on their disks, not this laptop.
"""
import argparse
import concurrent.futures as cf
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import paramiko  # noqa: E402

from assistant.config import load_config  # noqa: E402
from assistant.core import media_scan  # noqa: E402

_CFG = load_config()

# Credentials come from config.json (scan_ssh_password), with an env override for
# running this on a machine that shouldn't hold the password on disk. Never inline it
# here — this script is in git and the boxes it opens hold everything.
import os  # noqa: E402

PASSWORD = os.environ.get("JARVIS_SCAN_PASSWORD") or _CFG.scan_ssh_password
if not PASSWORD:
    sys.exit("No SSH password: set scan_ssh_password in config.json "
             "or JARVIS_SCAN_PASSWORD in the environment.")

LOGINS = [(u, PASSWORD) for u in _CFG.scan_ssh_users]
SUBNET = _CFG.scan_subnet
DB_PATH = _CFG.db_path

# Mounts that are never the owner's media. Boot partitions and the EFI stub show up on
# every single box and would otherwise need dismissing nine times in the review UI.
SKIP_MOUNTS = {"/boot", "/boot/efi", "/boot/firmware", "/sys/firmware/efi/efivars"}
SKIP_FSTYPES = {"efivarfs", "vfat", "squashfs", "overlay", "tmpfs", "devtmpfs"}

# A drive with almost nothing on it isn't worth a scan run, but it IS worth recording
# so the owner can see it exists and is empty rather than wondering if it was missed.
MIN_USED_BYTES = 50 * 1024 * 1024


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- discovery ----------------------------------------------------------------

def ssh_alive(ip, timeout=0.6):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, 22)) == 0
    except Exception:
        return False


def find_hosts():
    log(f"sweeping {SUBNET}.1-254 for SSH ...")
    with cf.ThreadPoolExecutor(max_workers=128) as pool:
        ips = [f"{SUBNET}.{n}" for n in range(1, 255)]
        alive = [ip for ip, ok in zip(ips, pool.map(ssh_alive, ips)) if ok]
    log(f"  {len(alive)} hosts answering on 22")
    return alive


PROBE = r"""
echo "###HOST"; hostname
echo "###MODEL"; tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo ""
echo "###OS"; . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"
echo "###DF"; df -B1 --output=source,fstype,size,used,target -x tmpfs -x devtmpfs \
  -x squashfs -x overlay 2>/dev/null | tail -n +2
echo "###LSBLK"; lsblk -b -J -o NAME,SIZE,TYPE,MOUNTPOINT,LABEL,TRAN,RM 2>/dev/null
echo "###SHARES"; grep -hE '^\[|^\s*path\s*=' /etc/samba/smb.conf 2>/dev/null
"""


def connect(ip):
    """Return (client, username) using whichever of the two logins works."""
    for user, pw in LOGINS:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(ip, username=user, password=pw, timeout=15,
                           allow_agent=False, look_for_keys=False, banner_timeout=25)
            return client, user
        except paramiko.AuthenticationException:
            client.close()
            continue
        except Exception:
            client.close()
            return None, None
    return None, None


def split_sections(text):
    out, key = {}, None
    for line in text.splitlines():
        if line.startswith("###"):
            key = line[3:]
            out[key] = []
        elif key:
            out[key].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def parse_shares(text):
    """Map mountpoint -> samba share name, so the UI can show how to reach a drive
    from Windows without a second lookup."""
    shares, current = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            current = None if name in ("global", "printers", "print$", "homes") else name
        elif current and line.lower().startswith("path"):
            path = line.split("=", 1)[1].strip().rstrip("/")
            if path:
                shares[path] = current
    return shares


def probe_host(ip):
    client, user = connect(ip)
    if client is None:
        return None
    try:
        _, out, _ = client.exec_command(PROBE, timeout=60)
        sections = split_sections(out.read().decode(errors="replace"))
    except Exception:
        return None
    finally:
        client.close()

    model = sections.get("MODEL", "").strip()
    os_name = sections.get("OS", "").strip()
    kind = "pi" if "raspberry" in model.lower() else (
        "ubuntu" if "ubuntu" in os_name.lower() else "linux")

    removable = {}
    try:
        tree = json.loads(sections.get("LSBLK", "{}") or "{}")

        def walk(nodes, parent_tran=""):
            for n in nodes:
                tran = n.get("tran") or parent_tran
                mp = n.get("mountpoint")
                if mp:
                    removable[mp] = {"tran": tran or "",
                                     "label": n.get("label") or "",
                                     "rm": bool(n.get("rm"))}
                walk(n.get("children") or [], tran)

        walk(tree.get("blockdevices", []))
    except (ValueError, TypeError):
        pass

    shares = parse_shares(sections.get("SHARES", ""))

    volumes = []
    for line in sections.get("DF", "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        source, fstype, size_s, used_s, target = parts
        if target in SKIP_MOUNTS or fstype in SKIP_FSTYPES:
            continue
        if not source.startswith("/dev/"):
            continue
        try:
            size, used = int(size_s), int(used_s)
        except ValueError:
            continue

        info = removable.get(target, {})
        enabled, reason = True, ""
        if used < MIN_USED_BYTES:
            enabled, reason = False, f"only {used / 1e6:.1f} MB used — effectively empty"
        volumes.append({
            "mountpoint": target,
            "fstype": fstype,
            "size_bytes": size,
            "used_bytes": used,
            "label": info.get("label", ""),
            "removable": info.get("tran") in ("usb",) or info.get("rm", False),
            "share_name": shares.get(target.rstrip("/"), ""),
            "enabled": enabled,
            "skip_reason": reason,
        })

    return {
        "address": ip,
        "hostname": sections.get("HOST", "").strip(),
        "os": os_name,
        "model": model,
        "kind": kind,
        "login_user": user,
        "volumes": volumes,
    }


def discover(only_host=None):
    ips = [only_host] if only_host else find_hosts()
    log(f"probing {len(ips)} hosts for drives ...")
    results = []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for res in pool.map(probe_host, ips):
            if res is None:
                continue
            results.append(res)
            host_id = media_scan.upsert_host(
                DB_PATH, res["address"], res["hostname"], res["os"],
                res["model"], res["kind"], res["login_user"])
            for v in res["volumes"]:
                media_scan.upsert_volume(
                    DB_PATH, host_id, v["mountpoint"], v["label"], v["fstype"],
                    v["size_bytes"], v["used_bytes"], v["removable"],
                    v["share_name"], v["enabled"], v["skip_reason"])
            keep = [v for v in res["volumes"] if v["enabled"]]
            log(f"  {res['address']:15s} {res['hostname'][:20]:22s} "
                f"{len(res['volumes'])} vols, {len(keep)} worth scanning")
            for v in keep:
                log(f"       → {v['mountpoint']:34s} {v['fstype']:8s} "
                    f"{v['used_bytes'] / 1e9:7.1f} GB used"
                    + (f"  [//{res['address']}/{v['share_name']}]" if v["share_name"] else ""))
    return results


# --- scanning -----------------------------------------------------------------

def scan_host_volumes(volumes):
    """Scan one host's volumes one after another."""
    out = []
    for v in volumes:
        label = f"{v['hostname'] or v['address']}:{v['mountpoint']}"
        log(f"  scanning {label} ...")
        started = time.time()

        def progress(files, total, _l=label):
            log(f"      {_l}: {files:,} files, {total / 1e9:.1f} GB so far")

        res = media_scan.scan_volume_over_ssh(DB_PATH, v, PASSWORD, progress=progress)
        secs = time.time() - started
        if res["status"] == "done":
            log(f"  ✓ {label}: {res['files']:,} media files, "
                f"{res['bytes'] / 1e9:.1f} GB in {secs:.0f}s")
        else:
            log(f"  ✗ {label}: {res['error'][:160]}")
        out.append((label, res))
    return out


def scan_all(only_host=None):
    volumes = [v for v in media_scan.list_volumes(DB_PATH, enabled_only=True)
               if not only_host or v["address"] == only_host]
    if not volumes:
        log("nothing to scan")
        return

    by_host = {}
    for v in volumes:
        by_host.setdefault(v["address"], []).append(v)

    log(f"scanning {len(volumes)} volumes across {len(by_host)} hosts "
        f"({sum(v['used_bytes'] or 0 for v in volumes) / 1e9:.0f} GB of data)")

    with cf.ThreadPoolExecutor(max_workers=len(by_host)) as pool:
        for _ in pool.map(scan_host_volumes, by_host.values()):
            pass


def windows_volume_label(root):
    """The drive's own label, e.g. 'TranferDSK'. Empty string if it has none."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(os.path.splitdrive(root)[0] + "\\"),
            buf, 261, None, None, None, None, 0)
        return buf.value.strip() if ok else ""
    except Exception:
        return ""


def scan_local(paths):
    """Catalogue drives plugged into this laptop, e.g. --local E:\\ F:\\

    Registered against a synthetic 'local' host so they sit in the same review table
    as the network drives — the decision of what to keep shouldn't depend on which
    machine a drive happened to be attached to.

    Drive letters get reused. Unplug one USB drive, plug in another, and Windows hands
    the new one the same letter — so identity here is the volume *label*, not the
    letter. When the label at a letter changes, the previous drive's row is retired
    (its mountpoint annotated, its catalogue and copy ledger untouched) and the new
    drive gets its own row. Without this, re-scanning E: would silently delete the
    record of a drive that is sitting on a shelf, fully collected.
    """
    import os

    # "Laptop" rather than the real hostname: this becomes a folder name in the
    # collection, and DESKTOP-J7QVFRM tells a human nothing. The label after it is
    # what actually distinguishes these.
    host_id = media_scan.upsert_host(
        DB_PATH, "127.0.0.1", "Laptop", "Windows", "", "local", "")
    for raw in paths:
        root = os.path.abspath(raw)
        if not os.path.isdir(root):
            log(f"  ✗ {raw}: not a directory")
            continue
        try:
            usage = os.statvfs(root)  # noqa: F841 — POSIX only, falls through on Windows
            size = used = 0
        except AttributeError:
            import shutil
            total, used_b, _ = shutil.disk_usage(root)
            size, used = total, used_b

        label = windows_volume_label(root) or os.path.splitdrive(root)[0] or root

        prior = next((v for v in media_scan.list_volumes(DB_PATH)
                      if v["host_kind"] == "local" and v["mountpoint"] == root), None)
        if prior and (prior["label"] or "") != label:
            retired = f"{root} [{prior['label'] or 'unlabelled'}, detached]"
            media_scan.rename_volume(DB_PATH, prior["id"], retired)
            log(f"  ! {root} now holds '{label}', was '{prior['label']}' — "
                f"retired the old record as {retired!r} (its files and copies are kept)")

        vol_id = media_scan.upsert_volume(
            DB_PATH, host_id, root, label,
            "local", size, used, removable=True)
        volume = next(v for v in media_scan.list_volumes(DB_PATH) if v["id"] == vol_id)

        log(f"  scanning {root} ...")
        started = time.time()
        res = media_scan.scan_local_path(
            DB_PATH, volume,
            progress=lambda f, t: log(f"      {root}: {f:,} files, {t / 1e9:.1f} GB so far"))
        if res["status"] == "done":
            log(f"  ✓ {root}: {res['files']:,} media files, {res['bytes'] / 1e9:.1f} GB "
                f"in {time.time() - started:.0f}s")
        else:
            log(f"  ✗ {root}: {res['error'][:160]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="discover only, don't scan")
    ap.add_argument("--scan", action="store_true", help="scan only, skip discovery")
    ap.add_argument("--host", help="limit to one address")
    ap.add_argument("--local", nargs="+", metavar="PATH",
                    help="catalogue drives attached to this machine (e.g. --local E:\\ F:\\)")
    args = ap.parse_args()

    media_scan.init_media_db(DB_PATH)

    if args.local:
        scan_local(args.local)
    elif not args.scan:
        discover(args.host)
    if not args.discover and not args.local:
        scan_all(args.host)

    s = media_scan.summary(DB_PATH)
    log("=" * 62)
    log(f"catalogue now holds {s['total_files']:,} files, {s['total_bytes'] / 1e9:.1f} GB")
    for k in s["by_kind"]:
        log(f"   {k['kind']:9s} {k['files']:>8,} files  {(k['bytes'] or 0) / 1e9:>8.1f} GB")


if __name__ == "__main__":
    main()
