"""Confirm duplicates by content: SHA-256 every file that shares its exact size with another
file somewhere in the inventory, on the machine that holds it.

    python scripts/inventory_hash.py              # every data drive
    python scripts/inventory_hash.py --host jarvisbox
    python scripts/inventory_hash.py --summary    # just print what's confirmed so far

Read-only on every drive. Run scripts/inventory_drives.py first; this works from its
file list. Each machine hashes its own files (a Pi reading its own USB SSD beats pulling
the bytes over the network), and sends back one line per file. Resumable: a file already
hashed at the same size and mtime is skipped, so an interrupted run just picks up again.
"""
import argparse
import concurrent.futures as cf
import hashlib
import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inventory_drives import DB, host_list, human, log, ssh_connect  # noqa: E402

from assistant.core import drive_inventory as inv  # noqa: E402

BATCH = 500
# Drive labels that are cloud mirrors, never read for hashing (see main()).
CLOUD_LABELS = {"google drive", "onedrive", "dropbox", "icloud drive"}

# Runs ON the remote box: reads one path per line from stdin, prints "sha256<TAB>path".
REMOTE_HASHER = r"""
import hashlib, sys
for line in sys.stdin:
    p = line.rstrip("\n")
    if not p:
        continue
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        print(h.hexdigest() + "\t" + p, flush=True)
    except OSError as e:
        print("ERR\t" + p, flush=True)
"""


def local_hashes(root: str, cands: list[dict]):
    for c in cands:
        path = os.path.join(root, c["rel_path"].replace("/", os.sep))
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            yield c, h.hexdigest()
        except OSError:
            yield c, None


def remote_hashes(client, mount: str, cands: list[dict], sudo: bool):
    by_path = {f"{mount.rstrip('/')}/{c['rel_path']}": c for c in cands}
    chan = client.get_transport().open_session()
    chan.exec_command(f"{'sudo -n ' if sudo else ''}python3 -c {shlex.quote(REMOTE_HASHER)}")
    chan.sendall(("\n".join(by_path) + "\n").encode("utf-8", "surrogateescape"))
    chan.shutdown_write()
    buf = b""
    while True:
        data = chan.recv(1 << 16)
        if not data:
            break
        buf += data
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            sha, _, path = ln.decode("utf-8", "replace").partition("\t")
            c = by_path.get(path)
            if c is not None:
                yield c, (None if sha == "ERR" else sha)


def hash_host(h: dict, volumes: list[dict]) -> None:
    client = sudo = None
    try:
        if not h.get("local"):
            client = ssh_connect(h)
            sudo = client.exec_command("sudo -n true 2>/dev/null && echo yes")[1].read().decode().strip() == "yes"
        for v in volumes:
            cands = inv.hash_candidates(DB, v["id"])
            if not cands:
                log(f"[{h['name']}] {v['mount']}: nothing new to hash")
                continue
            total = sum(c["size_bytes"] for c in cands)
            log(f"[{h['name']}] {v['mount']}: hashing {len(cands):,} files, {human(total)}")
            started, done, done_bytes, errors, batch = time.time(), 0, 0, 0, []
            source = (local_hashes(v["mount"], cands) if h.get("local")
                      else remote_hashes(client, v["mount"], cands, sudo))
            for c, sha in source:
                done += 1
                done_bytes += c["size_bytes"]
                if sha is None:
                    errors += 1
                    continue
                batch.append((c["rel_path"], c["size_bytes"], c["mtime"], sha))
                if len(batch) >= BATCH:
                    inv.record_hashes(DB, v["uid"], batch)
                    batch = []
                if done % 5000 == 0:
                    log(f"[{h['name']}] {v['mount']}: {done:,}/{len(cands):,} files, {human(done_bytes)}")
            inv.record_hashes(DB, v["uid"], batch)
            log(f"[{h['name']}] {v['mount']}: done, {done - errors:,} hashed, {errors} unreadable, "
                f"{human(done_bytes)} in {time.time() - started:.0f}s")
    except Exception as e:
        log(f"[{h['name']}] hashing FAILED: {e}")
    finally:
        if client is not None:
            client.close()


def print_summary() -> None:
    s = inv.duplicate_summary(DB)
    log(f"hashed {s['hashed_files']:,} files ({human(s['hashed_bytes'])}); confirmed duplicates: "
        f"{s['groups']:,} sets, {s['extra_files']:,} extra copies, {human(s['extra_bytes'])} reclaimable")
    for d in s["per_drive"]:
        log(f"   {d['host']} {d['mount']} ({d['label']}): {d['files']:,} files in duplicate sets, {human(d['bytes'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    inv.init_db(DB)
    if args.summary:
        print_summary()
        return

    with inv.connect(DB) as conn:
        vols = [dict(r) for r in conn.execute(
            """SELECT v.id, v.uid, v.mount, v.label, h.name AS host FROM inv_volumes v JOIN inv_hosts h ON h.id = v.host_id
                WHERE v.is_system = 0 AND v.online = 1 AND v.current_scan_id IS NOT NULL""")]
    hosts = {h["name"]: h for h in host_list()}
    work = {}
    for v in vols:
        # Cloud-synced drives (Google Drive for desktop streams files on demand): reading
        # them to hash would download every file. The cloud already holds that copy.
        if (v.get("label") or "").lower() in CLOUD_LABELS:
            log(f"[{v['host']}] {v['mount']} is a cloud drive ({v['label']}); not hashing")
            continue
        if v["host"] in hosts and (not args.host or v["host"] in args.host):
            work.setdefault(v["host"], []).append(v)
    log(f"hashing duplicate candidates on {sum(len(x) for x in work.values())} drives across {len(work)} hosts")
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda name: hash_host(hosts[name], work[name]), work))
    print_summary()


if __name__ == "__main__":
    main()
