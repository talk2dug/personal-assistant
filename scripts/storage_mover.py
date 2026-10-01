"""Run the approved parts of the consolidation plan (Media & drives -> Move plan).

    python scripts/storage_mover.py --dry-run          # what would happen, nothing touched
    python scripts/storage_mover.py --limit 5          # just the next five units
    python scripts/storage_mover.py --unit 1234        # one unit
    python scripts/storage_mover.py --same-drive-only  # just the renames within a drive
    python scripts/storage_mover.py                    # everything approved

Resumable: stop it at any point and run it again. See core/storage_mover.py for the
safety rules (verify before quarantine, never overwrite, quarantine instead of delete).
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inventory_drives import CFG, DB, host_list, human, log, ssh_connect  # noqa: E402

from assistant.core import drive_inventory as inv  # noqa: E402
from assistant.core import storage_mover as sm  # noqa: E402


def build_sources(units: list[dict]) -> tuple[dict, dict]:
    """volume uid -> source reader, for every drive the units touch; plus local mounts of
    the destinations. One SSH session per remote machine."""
    with inv.connect(DB) as c:
        vols = {r["uid"]: dict(r) for r in c.execute(
            "SELECT v.uid, v.mount, h.name AS host FROM inv_volumes v JOIN inv_hosts h ON h.id = v.host_id")}
    hosts = {h["name"]: h for h in host_list()}
    clients, sources = {}, {}
    for uid in {u["source_uid"] for u in units}:
        v = vols[uid]
        if v["host"] == "jarvisbox":
            sources[uid] = sm.LocalSource(v["mount"])
            continue
        if v["host"] not in clients:
            try:
                client = ssh_connect(hosts[v["host"]])
                sudo = client.exec_command("sudo -n true 2>/dev/null && echo yes")[1].read().decode().strip() == "yes"
                clients[v["host"]] = (client, sudo)
            except Exception as e:
                log(f"[{v['host']}] unreachable, its units wait: {e}")
                clients[v["host"]] = None
        if clients[v["host"]]:
            client, sudo = clients[v["host"]]
            spec = hosts[v["host"]]
            # Password-sudo where passwordless isn't set up (ubuntuserver003), and a fresh
            # session if a long run's connection drops.
            sources[uid] = sm.SSHSource(client, v["mount"], sudo,
                                        password=None if sudo else CFG.scan_ssh_password,
                                        reconnect=lambda spec=spec: ssh_connect(spec))
    dests = {u["dest_uid"]: vols[u["dest_uid"]]["mount"] for u in units if u["dest_uid"]}
    return sources, dests


_LOCK = None


def single_instance() -> bool:
    """Only one mover at a time. Two runs copying the same folder write the same .partial
    file (2026-09-29: an SSH-launched run that looked dead was still going when a second
    was started as SYSTEM). An OS file lock is released automatically if the process dies,
    so there is no stale-lock cleanup to get wrong."""
    global _LOCK
    path = Path(DB).with_name("storage_mover.lock")
    _LOCK = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def main():
    if not single_instance():
        log("mover: another mover is already running; not starting a second one")
        sys.exit(2)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--unit", type=int, action="append")
    ap.add_argument("--copy-only", action="append", default=[], metavar="HOST:MOUNT",
                    help="never write to this drive: copy off it, leave originals (repeatable), "
                         "e.g. jarvisbox:H:\\")
    ap.add_argument("--only", action="append", default=[], metavar="HOST:MOUNT",
                    help="run only this drive's units (repeatable)")
    ap.add_argument("--skip", action="append", default=[], metavar="HOST:MOUNT",
                    help="leave this drive's units for another run (repeatable)")
    ap.add_argument("--first", action="append", default=[], metavar="HOST:MOUNT",
                    help="run this drive's units before everything else (repeatable)")
    ap.add_argument("--same-drive-only", action="store_true",
                    help="only reorganise within a drive (renames); no copying or quarantining")
    args = ap.parse_args()

    sm.init_db(DB)
    units = sm.approved_units(DB)
    if args.unit:
        units = [u for u in units if u["id"] in args.unit]
    if args.same_drive_only:
        units = [u for u in units if u["same_drive"] and u["kind"] != "junk"]
    def pick(spec):
        host, _, mount = spec.partition(":")
        return {u["source_uid"] for u in sm.approved_units(DB) if u["source_host"] == host and u["source_mount"] == mount}
    copy_only = set().union(*[pick(x) for x in args.copy_only]) if args.copy_only else set()
    first = set().union(*[pick(x) for x in args.first]) if args.first else set()
    if first:
        units.sort(key=lambda u: u["source_uid"] not in first)   # stable: keeps the safe order within
    if args.only:
        only = set().union(*[pick(x) for x in args.only])
        units = [u for u in units if u["source_uid"] in only]
    if args.skip:
        skip = set().union(*[pick(x) for x in args.skip])
        units = [u for u in units if u["source_uid"] not in skip]
    if args.limit:
        units = units[:args.limit]
    if not units:
        log("mover: nothing approved to do")
        return
    sources, dests = build_sources(units)
    from assistant.core import storage_plan
    protected = storage_plan.default_protected(CFG.db_path)
    log(f"protected (never moved): {sorted(protected)}")
    if copy_only:
        log(f"copy-only (originals left untouched): {sorted(args.copy_only)}")
    mover = sm.Mover(DB, sources, dests, log=log, dry_run=args.dry_run, protected=protected,
                     copy_only=copy_only)
    total = sum(u["bytes"] for u in units)
    log(f"mover{' (DRY RUN)' if args.dry_run else ''}: {len(units):,} units, {human(total)}")
    started, done_bytes = time.time(), 0
    tally = {"copied": 0, "relocated": 0, "duplicate": 0, "failed": 0}
    for i, u in enumerate(units, 1):
        try:
            s = mover.run_unit(u)
        except Exception as e:
            log(f"[{i}/{len(units)}] SKIPPED {u['source_host']} {u['source_mount']} {u['source_path']}: {e}")
            continue
        for k in tally:
            tally[k] += s.get(k, 0)
        done_bytes += u["bytes"]
        if s["failed"] or u["bytes"] > 1 << 30 or i % 200 == 0 or args.dry_run and i <= 20:
            log(f"[{i}/{len(units)}] {u['source_host']} {u['source_mount']}{u['source_path']} -> {u['dest_path']}: "
                f"{s['copied']} copied, {s['relocated']} relocated, {s['duplicate']} dup/junk, {s['failed']} failed "
                f"({human(done_bytes)} of {human(total)}, {time.time() - started:.0f}s)")
    if not args.dry_run:
        # Originals left behind by an earlier interrupted run (copied + verified, never
        # quarantined): finish them for every unit, not just the ones run this time.
        with inv.connect(DB) as c:
            leftover = [dict(r) for r in c.execute(
                """SELECT DISTINCT u.* FROM move_log l JOIN plan_units u ON u.id = l.unit_id
                    WHERE l.action = 'copied' AND NOT EXISTS (SELECT 1 FROM move_log q WHERE q.unit_id = l.unit_id
                          AND q.source_rel = l.source_rel AND q.action = 'quarantined')""")]
        for u in leftover:
            if u["source_uid"] in sources:
                mover._finish_interrupted_quarantines(u, sources[u["source_uid"]])
        if leftover:
            log(f"finished quarantining originals for {len(leftover)} previously interrupted unit(s)")
    log(f"mover finished: {tally}")


if __name__ == "__main__":
    main()
