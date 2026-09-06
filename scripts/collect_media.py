"""Copy catalogued media into one local collection, with provenance and dedupe.

    python scripts/collect_media.py --volume 12            # one volume by id
    python scripts/collect_media.py --local                # every locally-attached drive
    python scripts/collect_media.py --host 192.168.0.43    # one network host
    python scripts/collect_media.py --all --kinds image vector cut design model3d archive

Destination defaults to `D:\\Collected Art`, laid out as
`<dest>/<host>-<drive label>/<original path>` — the original folder structure is kept
because on these drives the folder names carry most of the meaning ("300+ Stunning Vase
Designs", "Podfriendly - Bundles"), and four different drives have a folder called
"planters" that would collide the moment anything was flattened.

Every file is SHA-256'd while it's copied — the bytes are read either way, so exact
cross-drive dedupe costs nothing. Re-running is safe: anything already collected, or
already sitting at the destination at the right size, is left alone.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from assistant.config import load_config  # noqa: E402
from assistant.core import media_scan  # noqa: E402

CFG = load_config()
DB_PATH = CFG.db_path
DEFAULT_DEST = r"D:\Collected Art"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def human(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--volume", type=int, action="append", help="volume id (repeatable)")
    ap.add_argument("--host", action="append", help="limit to a host address")
    ap.add_argument("--local", action="store_true", help="all locally-attached drives")
    ap.add_argument("--all", action="store_true", help="every volume holding media")
    ap.add_argument("--kinds", nargs="+", metavar="KIND",
                    help=f"limit to these kinds (default: all). one of {sorted(media_scan.KINDS)}")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="write every copy even if an identical file was already collected")
    ap.add_argument("--dry-run", action="store_true", help="report what would be copied")
    args = ap.parse_args()

    media_scan.init_media_db(DB_PATH)
    volumes = media_scan.list_volumes(DB_PATH)

    chosen = []
    for v in volumes:
        if args.volume and v["id"] in args.volume:
            chosen.append(v)
        elif args.host and v["address"] in args.host:
            chosen.append(v)
        elif args.local and v["host_kind"] == "local":
            chosen.append(v)
        elif args.all:
            chosen.append(v)
    if not chosen:
        sys.exit("No volumes selected. Use --volume / --host / --local / --all "
                 "(see the Media page or scan_network_media.py --discover for ids).")

    # A volume with no catalogued files is almost always a mistake in the selection,
    # not an instruction to copy nothing — say so rather than silently doing nothing.
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    pending = []
    for v in chosen:
        q = "SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM media_files WHERE volume_id=?"
        p = [v["id"]]
        if args.kinds:
            q += f" AND kind IN ({','.join('?' * len(args.kinds))})"
            p += args.kinds
        n, b = conn.execute(q, p).fetchone()
        if n:
            pending.append((v, n, b))
        else:
            log(f"  (skipping {v['hostname'] or v['address']}:{v['mountpoint']} — nothing catalogued)")
    conn.close()

    if not pending:
        sys.exit("Nothing to copy.")

    total_files = sum(n for _, n, _ in pending)
    total_bytes = sum(b for _, _, b in pending)
    log(f"destination: {args.dest}")
    log(f"{len(pending)} volumes, {total_files:,} files, {human(total_bytes)} before dedupe")
    for v, n, b in pending:
        log(f"   {(v['hostname'] or v['address'])[:18]:19s} {v['mountpoint'][:28]:29s} "
            f"{n:>7,} files  {human(b):>9s}  -> {media_scan.volume_slug(v)}")

    if args.dry_run:
        log("dry run — nothing copied")
        return

    grand = {"copied": 0, "duplicate": 0, "skipped": 0, "failed": 0, "bytes": 0}
    for v, n, _ in pending:
        name = f"{v['hostname'] or v['address']}:{v['mountpoint']}"
        log(f"collecting {name} ...")
        started = time.time()

        def progress(i, total, st, _n=name, _s=started):
            elapsed = max(time.time() - _s, 1)
            # Rate is measured on bytes *read*, not bytes kept. On a drive that is
            # mostly duplicates the two differ by 5x, and reporting only what was
            # written makes a fast run look like a stalled one.
            rate = st.get("read_bytes", st["bytes"]) / elapsed
            eta = (total - i) / max(i / elapsed, 0.001)
            log(f"    {_n}: {i:,}/{total:,} · {st['copied']:,} copied "
                f"({human(st['bytes'])}) · {st['duplicate']:,} dup · {st['failed']:,} failed "
                f"· {rate / 1e6:.0f} MB/s read · eta {eta / 60:.0f}m")

        st = media_scan.collect_volume(
            DB_PATH, v, args.dest, kinds=args.kinds,
            dedupe=not args.no_dedupe, progress=progress,
            ssh_password=CFG.scan_ssh_password or "")
        for k in grand:
            grand[k] += st[k]
        log(f"  ✓ {name}: {st['copied']:,} copied ({human(st['bytes'])}), "
            f"{st['duplicate']:,} duplicates skipped, {st['failed']:,} failed, "
            f"in {time.time() - started:.0f}s")

    log("=" * 62)
    log(f"collected {grand['copied']:,} files ({human(grand['bytes'])}) into {args.dest}")
    log(f"  {grand['duplicate']:,} exact duplicates skipped, "
        f"{grand['skipped']:,} already present, {grand['failed']:,} failed")


if __name__ == "__main__":
    main()
