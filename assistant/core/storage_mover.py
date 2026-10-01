"""Executes approved units of the consolidation plan (storage_plan.py).

Safety rules -- every one of these exists because the job is moving the only copy of
years of work:
  * Never overwrite. A same-named file with different content at the destination makes
    the incoming file "name (2).ext".
  * A copy is written as "<name>.partial" and only renamed into place once its SHA-256
    matches the source's. A mismatch deletes the partial and fails the file.
  * The source is quarantined, never deleted: moved to <its drive>/_to_delete/<date>/<its
    original path>. On the same filesystem that's a rename -- instant, and reversible
    for 30 days (storage_quarantine sweeps it later).
  * A file whose exact content is already kept at the destination is not copied again;
    its source copy is quarantined. "Kept" is rebuilt from the move log, so a resumed run
    makes the same decisions.
  * Before each unit, the destination must have room for it plus a margin.
  * Every file action is logged (move_log). A re-run skips verified work.

The destination is always this machine (jarvisbox D: / E:). Sources are this machine's
own drives (read directly) or another machine's (read with `cat` over SSH, which is
integrity-checked in transit, then re-read here to verify what landed on disk).
"""
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from . import drive_inventory as inv
from .media_scan import long_path
from . import storage_plan

QUARANTINE_DIR = "_to_delete"
READ_STALL_SECONDS = 120   # no bytes for this long on a remote read = a dead session
FREE_MARGIN = 5 << 30
CHUNK = 1 << 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS move_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    source_uid TEXT NOT NULL,
    source_rel TEXT NOT NULL,
    dest_uid TEXT,
    dest_rel TEXT,
    sha256 TEXT,
    bytes INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL CHECK (action IN ('copied', 'relocated', 'duplicate', 'quarantined', 'failed')),
    quarantine_rel TEXT,
    error TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_move_log_unit ON move_log(unit_id);
CREATE INDEX IF NOT EXISTS idx_move_log_src ON move_log(source_uid, source_rel);
CREATE INDEX IF NOT EXISTS idx_move_log_sha ON move_log(dest_uid, sha256);
"""


class MoveError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def init_db(db_path: str) -> None:
    storage_plan.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def split_name(name: str) -> tuple[str, str]:
    base, dot, ext = name.rpartition(".")
    return (base, "." + ext) if dot and base else (name, "")


# ============================================================ source access
# Two implementations of the same small interface, so the move logic below never cares
# whether a source drive is attached here or to a Pi across the house.

class LocalSource:
    def __init__(self, mount: str):
        self.mount = mount

    def path(self, rel: str) -> str:
        # long_path: design bundles nest deep and name files after whole slogans, so the
        # 260-char MAX_PATH is routine here (2 of 78,000 failed on it the first run).
        return long_path(os.path.join(self.mount, rel.replace("/", os.sep))) if rel else self.mount

    def _require_drive(self) -> None:
        """os.walk/scandir on a drive that has dropped off the system return nothing, which
        read as "empty folder" and marked units done with nothing moved: H: (a 2010 WD
        Green on USB) disconnected mid-run on 2026-09-28 and four big folders -- GoPro,
        arduino, 101MEDIA, 102PHOTO -- were marked done untouched."""
        if not os.path.exists(self.mount):
            raise MoveError(f"drive {self.mount} is not present")

    def list_files(self, rel_dir: str) -> list[tuple[str, int]]:
        self._require_drive()
        p = self.path(rel_dir)
        try:
            with os.scandir(p) as it:
                return [(e.name, e.stat(follow_symlinks=False).st_size) for e in it
                        if e.is_file(follow_symlinks=False)]
        except FileNotFoundError:
            return []

    def walk_files(self, rel_dir: str) -> list[tuple[str, int]]:
        """(rel path under rel_dir, size) for every file in the tree, collapsed noise included."""
        self._require_drive()
        root = self.path(rel_dir)
        out = []

        def fail(err):
            raise MoveError(f"can't read {err.filename}: {err.strerror}")

        for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
            dirnames[:] = [d for d in dirnames if d != QUARANTINE_DIR]
            for f in filenames:
                full = os.path.join(dirpath, f)
                if os.path.islink(full):
                    continue
                out.append((os.path.relpath(full, root).replace(os.sep, "/"), os.path.getsize(full)))
        return out

    def open_read(self, rel: str):
        return open(self.path(rel), "rb")

    def exists(self, rel: str) -> bool:
        return os.path.exists(self.path(rel))

    def quarantine(self, rel: str) -> str:
        q = f"{QUARANTINE_DIR}/{today()}/{rel}"
        dst = self.path(q)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(self.path(rel), dst)
        return q

    def rename(self, rel: str, new_rel: str) -> None:
        dst = self.path(new_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(self.path(rel), dst)

    def prune_empty_dirs(self, rel_dir: str, stop_at: str = "") -> None:
        """rmdir the folder and then its parents while they are empty, never the root."""
        rel = rel_dir
        while rel and rel != stop_at:
            try:
                os.rmdir(self.path(rel))
            except OSError:
                return
            rel = rel.rsplit("/", 1)[0] if "/" in rel else ""


class SSHSource:
    """A drive on another machine, reached over SSH.

    Three lessons from the first full run (2026-09-28/29), each of which is handled here:
      * Admin rights must cover the WHOLE command. `sudo mkdir ... && mv ...` gave only the
        mkdir root; the mv was refused on root-owned mounts. Everything runs inside one
        `sudo sh -c '...'`.
      * Some boxes need a password for sudo (ubuntuserver003). It is fed on stdin
        (`sudo -S`), never put on the command line.
      * Sessions drop on long runs ("SSH session not active" skipped ~2,650 folders).
        A dead transport is reconnected once before failing.
    A listing that fails is an error, never an empty folder: treating it as empty would
    mark a unit done with nothing moved.
    """

    def __init__(self, client, mount: str, sudo: bool, password: str | None = None, reconnect=None):
        self.client, self.mount = client, mount.rstrip("/")
        self.sudo, self.password, self.reconnect = sudo, password, reconnect

    def path(self, rel: str) -> str:
        return f"{self.mount}/{rel}" if rel else self.mount

    def _transport(self):
        t = self.client.get_transport() if self.client else None
        if (t is None or not t.is_active()) and self.reconnect:
            self.client = self.reconnect()
            t = self.client.get_transport()
        if t is None or not t.is_active():
            raise MoveError("SSH session to the source machine is down")
        return t

    def _wrap(self, cmd: str) -> tuple[str, bool]:
        """(command to send, whether the password must be written to stdin first)."""
        if self.sudo:
            return f"sudo -n sh -c {shlex.quote(cmd)}", False
        if self.password:
            return f"sudo -S -p '' sh -c {shlex.quote(cmd)}", True
        return cmd, False

    def _open(self, cmd: str):
        full, needs_pw = self._wrap(cmd)
        chan = self._transport().open_session()
        chan.exec_command(full)
        if needs_pw:
            chan.sendall((self.password + "\n").encode())
        return chan

    def _run(self, cmd: str, timeout: int = 300) -> str:
        chan = self._open(cmd)
        chan.settimeout(timeout)
        out, err = b"", b""
        while True:
            if chan.recv_ready():
                out += chan.recv(1 << 16)
            elif chan.recv_stderr_ready():
                err += chan.recv_stderr(1 << 16)
            elif chan.exit_status_ready():
                while chan.recv_ready():
                    out += chan.recv(1 << 16)
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(1 << 16)
                break
            else:
                time.sleep(0.01)
        status = chan.recv_exit_status()
        chan.close()
        if status != 0:
            raise MoveError(f"remote command failed ({status}): {err.decode('utf-8', 'replace')[:300]}")
        return out.decode("utf-8", "replace")

    def _find(self, rel_dir: str, depth: str) -> list[tuple[str, int]]:
        p = shlex.quote(self.path(rel_dir))
        if not self.exists(rel_dir):
            return []            # already moved / gone: genuinely nothing left
        # The folder is confirmed present above, so a non-zero exit from find now means some
        # ENTRIES couldn't be read, not that the listing is empty: TransferDisk's exFAT lists
        # three Rally_Server files that don't exist (filesystem damage), and treating that as
        # fatal stranded the whole folder. Keep what's readable; the unreadable ones are
        # reported and simply never get a "copied" record.
        out = self._run(f"find {p} {depth} -type f -not -path '*/{QUARANTINE_DIR}/*' -printf '%s\\t%P\\n' "
                        f"2>/tmp/storage_mover_find.err; n=$(wc -l </tmp/storage_mover_find.err); "
                        f"[ \"$n\" -gt 0 ] && echo \"#UNREADABLE $n\"; exit 0")
        rows = []
        for ln in out.splitlines():
            if ln.startswith("#UNREADABLE "):
                self.unreadable = getattr(self, "unreadable", 0) + int(ln.split()[1])
                continue
        for ln in out.splitlines():
            size, _, rel = ln.partition("\t")
            if rel:
                rows.append((rel, int(size)))
        return rows

    def list_files(self, rel_dir: str) -> list[tuple[str, int]]:
        return self._find(rel_dir, "-maxdepth 1")

    def walk_files(self, rel_dir: str) -> list[tuple[str, int]]:
        return self._find(rel_dir, "")

    def open_read(self, rel: str):
        chan = self._open(f"cat -- {shlex.quote(self.path(rel))}")
        # A read that stops arriving must fail, not wait forever: a NAS session that died
        # without closing froze the whole 2026-09-29 run for five hours on one file.
        chan.settimeout(READ_STALL_SECONDS)
        return _ChannelReader(chan)

    def exists(self, rel: str) -> bool:
        try:
            self._run(f"test -e {shlex.quote(self.path(rel))}")
            return True
        except MoveError as e:
            if "SSH session" in str(e):
                raise
            return False

    def quarantine(self, rel: str) -> str:
        q = f"{QUARANTINE_DIR}/{today()}/{rel}"
        dst = self.path(q)
        self._run(f"mkdir -p {shlex.quote(dst.rsplit('/', 1)[0])} && mv -n -- "
                  f"{shlex.quote(self.path(rel))} {shlex.quote(dst)} && test ! -e {shlex.quote(self.path(rel))}")
        return q

    def prune_empty_dirs(self, rel_dir: str, stop_at: str = "") -> None:
        rel = rel_dir
        while rel and rel != stop_at:
            try:
                self._run(f"rmdir -- {shlex.quote(self.path(rel))}")
            except MoveError:
                return
            rel = rel.rsplit("/", 1)[0] if "/" in rel else ""


class _ChannelReader:
    def __init__(self, chan):
        self.chan = chan

    def read(self, n: int) -> bytes:
        try:
            data = self.chan.recv(n)
        except OSError as e:          # socket.timeout included
            raise MoveError(f"remote read stalled: {e}")
        if not data:
            status = self.chan.recv_exit_status()
            if status != 0:
                raise MoveError(f"remote read failed (exit {status})")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.chan.close()


# ================================================================== core moves

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_verified(src, rel: str, dest_path: str, expected_sha: str | None) -> str:
    """Stream src -> dest_path.partial while hashing, re-read what landed, and only then
    rename into place. Returns the verified SHA-256. Raises MoveError on any mismatch
    (the partial is removed)."""
    partial = dest_path + ".partial"
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    h = hashlib.sha256()
    try:
        with src.open_read(rel) as r, open(partial, "wb") as w:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                w.write(chunk)
        streamed = h.hexdigest()
        if expected_sha and streamed != expected_sha:
            raise MoveError(f"source changed since it was fingerprinted ({rel})")
        if sha256_file(partial) != streamed:
            raise MoveError(f"copy on disk doesn't match what was read ({rel})")
        os.replace(partial, dest_path)
        return streamed
    except BaseException:
        if os.path.exists(partial):
            os.remove(partial)
        raise


def free_name(dest_dir: str, name: str) -> str:
    """name, or 'name (2).ext', 'name (3).ext'... whichever doesn't exist yet."""
    if not os.path.exists(os.path.join(dest_dir, name)):
        return name
    base, ext = split_name(name)
    n = 2
    while os.path.exists(os.path.join(dest_dir, f"{base} ({n}){ext}")):
        n += 1
    return f"{base} ({n}){ext}"


class Mover:
    def __init__(self, db_path: str, sources: dict, dest_mounts: dict[str, str], log=print,
                 dry_run: bool = False, protected: set[tuple[str, str]] | None = None,
                 copy_only: set[str] | None = None):
        """sources: volume uid -> LocalSource/SSHSource. dest_mounts: volume uid -> local mount.
        protected: storage_plan.live_paths() -- refused here too, even if a stale plan has them."""
        init_db(db_path)
        self.db, self.sources, self.dest_mounts, self.log, self.dry = db_path, sources, dest_mounts, log, dry_run
        self.protected = protected or set()
        # Volume uids never written to: copied off, originals left exactly as they are.
        # For a failing drive, where a quarantine rename is itself a risky write.
        self.copy_only = copy_only or set()
        self.kept: dict[str, set] = {}
        with closing(inv.connect(db_path)) as c:
            for r in c.execute("SELECT dest_uid, sha256 FROM move_log WHERE action IN ('copied','relocated') "
                               "AND sha256 IS NOT NULL"):
                self.kept.setdefault(r["dest_uid"], set()).add(r["sha256"])
            self.hashes = {}
            for r in c.execute("SELECT volume_uid, rel_path, sha256 FROM inv_hashes"):
                self.hashes[(r["volume_uid"], r["rel_path"])] = r["sha256"]

    # -- bookkeeping ---------------------------------------------------------
    def _log(self, unit, src_rel, action, dest_rel=None, sha=None, size=0, q=None, error=None):
        if self.dry:
            return
        with closing(inv.connect(self.db)) as c:
            c.execute("""INSERT INTO move_log (unit_id, source_uid, source_rel, dest_uid, dest_rel, sha256, bytes,
                             action, quarantine_rel, error, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (unit["id"], unit["source_uid"], src_rel, unit["dest_uid"], dest_rel, sha, size, action,
                       q, error, _now()))
            c.commit()

    def _status(self, unit, status, note=None):
        if self.dry:
            return
        with closing(inv.connect(self.db)) as c:
            c.execute("UPDATE plan_units SET status = ?, note = ? WHERE id = ?", (status, note, unit["id"]))
            c.commit()

    def _done_sources(self, unit) -> set:
        with closing(inv.connect(self.db)) as c:
            return {r[0] for r in c.execute(
                "SELECT source_rel FROM move_log WHERE unit_id = ? AND action IN ('copied','relocated','duplicate')",
                (unit["id"],))}

    def _finish_interrupted_quarantines(self, unit, src) -> None:
        """A run stopped between 'copied' (verified) and 'quarantined' leaves the original
        in place; a resumed run skips it as done. Finish those here."""
        if self.dry or unit["source_uid"] in self.copy_only:
            return
        with closing(inv.connect(self.db)) as c:
            pending = [r[0] for r in c.execute(
                """SELECT source_rel FROM move_log l WHERE unit_id = ? AND action = 'copied'
                     AND NOT EXISTS (SELECT 1 FROM move_log q WHERE q.unit_id = l.unit_id
                                     AND q.source_rel = l.source_rel AND q.action = 'quarantined')""",
                (unit["id"],))]
        for rel in pending:
            if src.exists(rel):
                q = src.quarantine(rel)
                self._log(unit, rel, "quarantined", q=q)

    # -- one file ------------------------------------------------------------
    def _place(self, unit, src, src_rel: str, dest_dir_rel: str, size: int) -> dict:
        """Move one file into dest_dir_rel. Returns {'action', 'bytes'}."""
        dest_mount = self.dest_mounts[unit["dest_uid"]]
        kept = self.kept.setdefault(unit["dest_uid"], set())
        name = src_rel.rsplit("/", 1)[-1]
        known = self.hashes.get((unit["source_uid"], src_rel))
        dest_dir = long_path(os.path.join(dest_mount, dest_dir_rel.replace("/", os.sep)))
        # Dedupe merges design folders. A whole-folder move (a project, a camera dump) is
        # kept exactly as it was: the same icon legitimately sits in two places inside a
        # code project, and pulling one out breaks it (31 files from Vinyl Stuff, first run).
        dedupe = unit["kind"] == "files"

        if dedupe and known and known in kept:
            q = None if self.dry or unit["source_uid"] in self.copy_only else src.quarantine(src_rel)
            self._log(unit, src_rel, "duplicate", sha=known, size=size, q=q)
            return {"action": "duplicate", "bytes": size}

        same_drive = unit["source_uid"] == unit["dest_uid"]
        existing = os.path.join(dest_dir, name)
        if dedupe and os.path.exists(existing) and not same_drive:
            if os.path.getsize(existing) == size and known and sha256_file(existing) == known:
                q = None if self.dry or unit["source_uid"] in self.copy_only else src.quarantine(src_rel)
                kept.add(known)
                self._log(unit, src_rel, "duplicate", sha=known, size=size, q=q)
                return {"action": "duplicate", "bytes": size}
        final = free_name(dest_dir, name) if not self.dry else name
        dest_rel = f"{dest_dir_rel}/{final}"
        if self.dry:
            return {"action": "relocated" if same_drive else "copied", "bytes": size}

        if same_drive:
            if os.path.abspath(src.path(src_rel)) == os.path.abspath(os.path.join(dest_dir, final)):
                self._log(unit, src_rel, "relocated", dest_rel, known, size)
            else:
                src.rename(src_rel, dest_rel)
                self._log(unit, src_rel, "relocated", dest_rel, known, size)
            if known:
                kept.add(known)
            return {"action": "relocated", "bytes": size}

        sha = copy_verified(src, src_rel, os.path.join(dest_dir, final), known)
        copy_only = unit["source_uid"] in self.copy_only
        if dedupe and sha in kept:   # unhashed source turned out identical to something already placed
            os.remove(os.path.join(dest_dir, final))
            q = None if copy_only else src.quarantine(src_rel)
            self._log(unit, src_rel, "duplicate", sha=sha, size=size, q=q)
            return {"action": "duplicate", "bytes": size}
        kept.add(sha)
        self._log(unit, src_rel, "copied", dest_rel, sha, size)
        if not copy_only:
            q = src.quarantine(src_rel)
            self._log(unit, src_rel, "quarantined", dest_rel, sha, size, q=q)
        return {"action": "copied", "bytes": size}

    # -- one unit ------------------------------------------------------------
    def run_unit(self, unit: dict) -> dict:
        src = self.sources.get(unit["source_uid"])
        if src is None:
            raise MoveError("source drive isn't reachable right now")
        top = unit["source_path"].split("/", 1)[0]
        if any(getattr(src, "mount", None) == drive and top == folder for drive, folder in self.protected):
            raise MoveError(f"{top} is in use by Jarvis or Obsidian; refusing to move it")
        stats = {"copied": 0, "relocated": 0, "duplicate": 0, "failed": 0, "bytes": 0}
        if unit["kind"] == "junk":
            if unit["source_uid"] in self.copy_only:
                raise MoveError("copy-only drive: nothing is quarantined on it")
            return self._junk(unit, src, stats)

        if unit["source_uid"] != unit["dest_uid"]:
            free = shutil.disk_usage(self.dest_mounts[unit["dest_uid"]]).free
            if free < unit["new_bytes"] + FREE_MARGIN:
                raise MoveError(f"not enough room on the destination ({free >> 30} GB free)")

        self._status(unit, "running")
        done = self._done_sources(unit)
        self._finish_interrupted_quarantines(unit, src)
        if unit["kind"] == "files":
            listing = [(f"{unit['source_path']}/{n}" if unit["source_path"] else n, s, unit["dest_path"])
                       for n, s in src.list_files(unit["source_path"])]
        else:  # tree: the whole folder, structure intact, including noise folders
            base = unit["source_path"]
            ex = json.loads(unit.get("excludes") or "[]")
            skip_trees = [e["path"] + "/" for e in ex if e["kind"] != "files"]
            skip_dirs = {e["path"] for e in ex if e["kind"] == "files"}
            listing = []
            for rel, s in src.walk_files(base):
                sub = rel.rsplit("/", 1)[0] if "/" in rel else ""
                full = f"{base}/{rel}" if base else rel
                full_dir = full.rsplit("/", 1)[0] if "/" in full else ""
                if full_dir in skip_dirs or any(full.startswith(t) for t in skip_trees):
                    continue      # planned as its own unit (storage_plan.resolve_nesting)
                listing.append((f"{base}/{rel}" if base else rel, s,
                                f"{unit['dest_path']}/{sub}" if sub else unit["dest_path"]))
        for src_rel, size, dest_dir in listing:
            if src_rel in done or src_rel.endswith(".partial"):
                continue
            try:
                r = self._place(unit, src, src_rel, dest_dir, size)
                stats[r["action"]] += 1
                stats["bytes"] += r["bytes"]
            except Exception as e:  # one bad file must not strand the rest of the unit
                stats["failed"] += 1
                self._log(unit, src_rel, "failed", size=size, error=str(e)[:500])
                self.log(f"   FAILED {src_rel}: {e}")
        if not self.dry and not stats["failed"] and unit["source_uid"] not in self.copy_only:
            src.prune_empty_dirs(unit["source_path"])
        self._status(unit, "failed" if stats["failed"] else "done",
                     f"{stats['failed']} files failed" if stats["failed"] else None)
        return stats

    def _junk(self, unit, src, stats):
        """Quarantine a junk folder's contents (recycle bins, macOS indexes)."""
        self._status(unit, "running")
        for rel, size in src.walk_files(unit["source_path"]):
            full = f"{unit['source_path']}/{rel}"
            try:
                q = None if self.dry else src.quarantine(full)
                self._log(unit, full, "quarantined", size=size, q=q)
                stats["duplicate"] += 1
                stats["bytes"] += size
            except Exception as e:
                stats["failed"] += 1
                self._log(unit, full, "failed", size=size, error=str(e)[:500])
        self._status(unit, "failed" if stats["failed"] else "done")
        return stats


def approved_units(db_path: str, limit: int | None = None) -> list[dict]:
    """Approved (or interrupted) units in the safe order: reorganise the destination drive
    first (renames; it also establishes which copies are kept), then local drives, then
    remote ones, smallest first so progress shows early; junk last."""
    with closing(inv.connect(db_path)) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT u.*, v.mount AS source_mount, h.name AS source_host
                 FROM plan_units u JOIN inv_volumes v ON v.id = u.source_volume_id JOIN inv_hosts h ON h.id = v.host_id
                WHERE u.status IN ('approved', 'running')""")]
    rank = lambda u: (u["kind"] == "junk", not u["same_drive"], u["source_host"] != "jarvisbox",  # noqa: E731
                      u["section"] != "Side Hustle", u["bytes"])
    rows.sort(key=rank)
    return rows[:limit] if limit else rows
