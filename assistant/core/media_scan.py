"""Discovery and cataloguing of media scattered across the house network.

The problem this solves: years of artwork, designs, cut files and footage live on USB
drives hanging off half a dozen Raspberry Pis and Ubuntu boxes, plus external drives
that get plugged into the laptop. Nobody knows what's where, what's duplicated, or what
still matters. Before anything can be imported and organised, there has to be an
inventory.

Design decisions worth knowing:

**SSH, not SMB.** Most of these volumes are shared over Samba, but walking an SMB mount
from Windows means a network round trip per stat() — hours for a 185GB drive. Running
`find` on the box itself and streaming back one line per file is the same information in
minutes, and it works on the hosts that don't export shares at all.

**Metadata now, hashes later.** The first pass records path/size/mtime/kind only. Content
hashing 440GB over the network would take all night and most of it would be wasted —
duplicates can't differ in size, so hashing is only ever needed *within* a group of
same-size files. `hash_candidates()` does that second pass on demand.

**Folder rollups are the deliverable.** The decision the owner actually makes is "bring
this folder over / skip it", not "bring this file over". Per-file rows exist to compute
the rollup and to find duplicates; the review table is `media_folders`.

**Resumable.** An overnight run across nine hosts will have something time out. Each
volume is its own scan_run row with its own status, so a failure isolates to one volume
and a re-run skips what already completed.
"""
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL UNIQUE,
    hostname TEXT,
    os TEXT,
    model TEXT,
    kind TEXT NOT NULL DEFAULT 'linux' CHECK (kind IN ('pi', 'ubuntu', 'linux', 'local')),
    login_user TEXT,
    reachable INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_volumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES scan_hosts(id),
    mountpoint TEXT NOT NULL,
    label TEXT,
    fstype TEXT,
    size_bytes INTEGER,
    used_bytes INTEGER,
    removable INTEGER NOT NULL DEFAULT 0,
    share_name TEXT,
    -- Skipped volumes stay in the table so the UI can show what was deliberately
    -- ignored (empty drives, boot partitions) rather than silently omitting them.
    enabled INTEGER NOT NULL DEFAULT 1,
    skip_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(host_id, mountpoint)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id INTEGER NOT NULL REFERENCES scan_volumes(id),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
    files_found INTEGER NOT NULL DEFAULT 0,
    bytes_found INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id INTEGER NOT NULL REFERENCES scan_volumes(id),
    dir_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime TEXT,
    content_hash TEXT,
    UNIQUE(volume_id, dir_path, filename)
);

CREATE INDEX IF NOT EXISTS idx_media_files_vol_dir ON media_files(volume_id, dir_path);
CREATE INDEX IF NOT EXISTS idx_media_files_size ON media_files(size_bytes);
CREATE INDEX IF NOT EXISTS idx_media_files_kind ON media_files(kind);
CREATE INDEX IF NOT EXISTS idx_media_files_hash ON media_files(content_hash);

-- The review table. One row per directory that directly contains media, with a
-- decision the owner sets from the UI.
CREATE TABLE IF NOT EXISTS media_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id INTEGER NOT NULL REFERENCES scan_volumes(id),
    dir_path TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    kind_counts TEXT,
    top_exts TEXT,
    oldest_mtime TEXT,
    newest_mtime TEXT,
    decision TEXT NOT NULL DEFAULT 'undecided'
        CHECK (decision IN ('undecided', 'import', 'skip', 'imported')),
    notes TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(volume_id, dir_path)
);

CREATE INDEX IF NOT EXISTS idx_media_folders_decision ON media_folders(decision);

-- Every file pulled into the local collection, and where it came from.
--
-- Provenance is the point: once 200k files from eleven drives are merged into one
-- tree, "which drive was this on" is unanswerable without a record, and that question
-- gets asked the moment something looks wrong. status='duplicate' rows carry no bytes
-- of their own — they point at the copy that won, so a later dedupe report can say
-- what was skipped and from where.
CREATE TABLE IF NOT EXISTS media_copies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES media_files(id),
    volume_id INTEGER NOT NULL REFERENCES scan_volumes(id),
    source_path TEXT NOT NULL,
    dest_path TEXT,
    content_hash TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'copied'
        CHECK (status IN ('copied', 'duplicate', 'skipped', 'failed')),
    duplicate_of TEXT,
    error TEXT,
    copied_at TEXT NOT NULL,
    UNIQUE(volume_id, source_path)
);

CREATE INDEX IF NOT EXISTS idx_media_copies_hash ON media_copies(content_hash);
CREATE INDEX IF NOT EXISTS idx_media_copies_status ON media_copies(status);

-- What's inside an archive, learned from its index rather than by extracting it.
-- A zip's central directory lists every member with its uncompressed size, so this
-- costs one seek per file instead of 130GB of disk writes.
CREATE TABLE IF NOT EXISTS media_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES media_files(id),
    volume_id INTEGER NOT NULL REFERENCES scan_volumes(id),
    inner_files INTEGER NOT NULL DEFAULT 0,
    inner_media INTEGER NOT NULL DEFAULT 0,
    inner_bytes INTEGER NOT NULL DEFAULT 0,
    kind_counts TEXT,
    top_exts TEXT,
    sample_paths TEXT,
    error TEXT,
    scanned_at TEXT NOT NULL,
    UNIQUE(file_id)
);
"""

# --- what counts as media -----------------------------------------------------
#
# Grouped by what the owner would *do* with the file, not by MIME type. "design" and
# "cut" are separate from "image" because a layered PSD and a flattened JPEG have very
# different value when rebuilding a product catalogue, and .studio3 files are the
# training data the contour trainer needs — they're the reason this scan exists as much
# as the artwork is.
KINDS = {
    "image": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic", "avif"],
    "design": ["psd", "psb", "xcf", "cdr", "afdesign", "afphoto", "procreate", "clip", "kra"],
    "vector": ["svg", "ai", "eps", "dxf", "cdr"],
    "cut": ["studio3", "studio", "gsp", "scut5", "scut4", "fcm", "sbp"],
    "model3d": ["stl", "3mf", "obj", "step", "stp", "f3d", "blend", "gcode", "ply"],
    "video": ["mp4", "mov", "avi", "mkv", "webm", "m4v", "mpg", "mpeg", "wmv"],
    "audio": ["mp3", "wav", "aac", "flac", "m4a", "ogg"],
    "font": ["ttf", "otf", "woff", "woff2"],
    "doc": ["pdf"],
    # Downloaded design and STL bundles routinely sit unopened for years. On the first
    # external drive scanned, archives were 137GB of 181GB — treating them as
    # uninteresting would have missed three quarters of the drive.
    "archive": ["zip", "rar", "7z", "tar", "gz", "tgz"],
}

EXT_KIND = {ext: kind for kind, exts in KINDS.items() for ext in exts}
# .cdr and .eps appear in two groups above; first-wins is fine, both are vector-ish.
ALL_EXTS = sorted(EXT_KIND)

# Directories that are never the owner's artwork. Skipping them at the `find` level is
# far cheaper than filtering millions of rows afterwards — a single node_modules tree
# can hold more files than every drive's real content combined.
PRUNE_DIRS = [
    # Frigate is the NVR. Its clips/recordings trees were 149k files and 75GB across
    # two hosts — 63% of the first full sweep by file count — and none of it is art.
    "Frigate", "frigate",
    "node_modules", ".git", ".svn", ".cache", "__pycache__", ".venv", "venv",
    "$RECYCLE.BIN", "System Volume Information", ".Trash-1000", ".Trashes",
    "lost+found", "AppData", "Windows", "Program Files", "Program Files (x86)",
    ".npm", ".local/share/Trash", "site-packages", "dist-packages",
]

# Thumbnails, sprite sheets and favicons are media by extension and noise by intent.
MIN_FILE_BYTES = 8 * 1024

# Files at or below this are hashed in memory during collection so a duplicate is
# never written to disk at all. Sized to cover the vast majority of artwork files
# while never buffering a multi-gigabyte bundle.
BUFFER_MAX_BYTES = 64 * 1024 * 1024


def init_media_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- hosts and volumes --------------------------------------------------------

def upsert_host(db_path: str, address: str, hostname: str = "", os_name: str = "",
                model: str = "", kind: str = "linux", login_user: str = "",
                reachable: bool = True) -> int:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO scan_hosts
                   (address, hostname, os, model, kind, login_user, reachable,
                    last_seen_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                   hostname = excluded.hostname,
                   os = excluded.os,
                   model = excluded.model,
                   kind = excluded.kind,
                   login_user = excluded.login_user,
                   reachable = excluded.reachable,
                   last_seen_at = excluded.last_seen_at""",
            (address, hostname, os_name, model, kind, login_user,
             1 if reachable else 0, _now(), _now()),
        )
        conn.commit()
        return conn.execute("SELECT id FROM scan_hosts WHERE address = ?",
                            (address,)).fetchone()["id"]


def upsert_volume(db_path: str, host_id: int, mountpoint: str, label: str = "",
                  fstype: str = "", size_bytes: int = 0, used_bytes: int = 0,
                  removable: bool = False, share_name: str = "",
                  enabled: bool = True, skip_reason: str = "") -> int:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO scan_volumes
                   (host_id, mountpoint, label, fstype, size_bytes, used_bytes,
                    removable, share_name, enabled, skip_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(host_id, mountpoint) DO UPDATE SET
                   label = excluded.label,
                   fstype = excluded.fstype,
                   size_bytes = excluded.size_bytes,
                   used_bytes = excluded.used_bytes,
                   removable = excluded.removable,
                   share_name = excluded.share_name,
                   enabled = excluded.enabled,
                   skip_reason = excluded.skip_reason""",
            (host_id, mountpoint, label, fstype, size_bytes, used_bytes,
             1 if removable else 0, share_name, 1 if enabled else 0, skip_reason, _now()),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM scan_volumes WHERE host_id = ? AND mountpoint = ?",
            (host_id, mountpoint)).fetchone()["id"]


def rename_volume(db_path: str, volume_id: int, new_mountpoint: str) -> bool:
    """Retire a volume record by moving it aside, keeping its catalogue and copies.

    Used when a removable drive's letter is reassigned to different hardware: the old
    drive's history stays queryable instead of being overwritten by whatever got
    plugged in next.
    """
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("UPDATE scan_volumes SET mountpoint = ? WHERE id = ?",
                           (new_mountpoint, volume_id))
        conn.commit()
        return cur.rowcount > 0


def list_hosts(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scan_hosts ORDER BY address")]


def list_volumes(db_path: str, enabled_only: bool = False) -> list[dict]:
    sql = """SELECT v.*, h.address, h.hostname, h.kind AS host_kind, h.login_user,
                    (SELECT status FROM scan_runs r WHERE r.volume_id = v.id
                      ORDER BY r.id DESC LIMIT 1) AS last_status,
                    (SELECT files_found FROM scan_runs r WHERE r.volume_id = v.id
                      ORDER BY r.id DESC LIMIT 1) AS last_files,
                    (SELECT finished_at FROM scan_runs r WHERE r.volume_id = v.id
                      ORDER BY r.id DESC LIMIT 1) AS last_finished
               FROM scan_volumes v JOIN scan_hosts h ON h.id = v.host_id"""
    if enabled_only:
        sql += " WHERE v.enabled = 1"
    sql += " ORDER BY h.address, v.mountpoint"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql)]


# --- the find command ---------------------------------------------------------

# Scanning a root filesystem is worth doing — artwork turns up in home directories and
# stray /opt project folders — but /usr/share alone holds tens of thousands of icons and
# theme PNGs that are all >8KB and all noise. Pruned by absolute path, so a folder the
# owner happens to call "share" on a data drive is unaffected.
SYSTEM_PRUNE_PATHS = [
    "/usr", "/var", "/proc", "/sys", "/dev", "/run", "/snap", "/boot",
    "/lib", "/lib32", "/lib64", "/sbin", "/bin", "/etc",
]


def build_find_command(mountpoint: str) -> str:
    """Build the remote `find` that streams one line per media file.

    Output is NUL-free, tab-separated: size, mtime epoch, full path. Paths can contain
    literally anything except NUL and newline, so tabs are the only safe separator here
    and the parser splits on the first two tabs only.
    """
    prunes = [f"-name {_shq(d)}" for d in PRUNE_DIRS]
    if mountpoint == "/":
        prunes += [f"-path {_shq(p)}" for p in SYSTEM_PRUNE_PATHS]
    prune_expr = " -o ".join(prunes)
    exts = " -o ".join(f"-iname '*.{e}'" for e in ALL_EXTS)
    # find's stderr is discarded because unreadable subdirectories are normal on drives
    # with mixed ownership and would otherwise drown the real output. That leaves no way
    # to tell "found nothing" from "path doesn't exist", since both surface only as a
    # non-zero exit — hence the explicit -d test and a distinct exit code for the second.
    return (
        f"test -d {_shq(mountpoint)} || exit 9; "
        f"find {_shq(mountpoint)} -xdev "
        f"\\( \\( {prune_expr} \\) -prune \\) -o "
        f"\\( -type f -size +{MIN_FILE_BYTES // 1024}k \\( {exts} \\) "
        f"-printf '%s\\t%T@\\t%p\\n' \\) 2>/dev/null"
    )


def _shq(s: str) -> str:
    """Single-quote for POSIX sh. Mount points here include 'Vinyl Thing' and
    'Untitled 2' — unquoted they would silently scan the wrong path."""
    return "'" + s.replace("'", "'\\''") + "'"


def parse_find_line(line: str, mountpoint: str):
    """Turn one `find -printf` line into a row dict, or None if unusable."""
    parts = line.rstrip("\n").split("\t", 2)
    if len(parts) != 3:
        return None
    size_s, mtime_s, full = parts
    try:
        size = int(size_s)
        mtime = datetime.fromtimestamp(float(mtime_s), timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None

    slash = full.rfind("/")
    if slash < 0:
        return None
    dir_path, filename = full[:slash], full[slash + 1:]

    # Store paths relative to the mount so the same drive re-plugged elsewhere still
    # matches, and so the UI can show short paths.
    if dir_path.startswith(mountpoint):
        dir_path = dir_path[len(mountpoint):].lstrip("/")

    dot = filename.rfind(".")
    ext = filename[dot + 1:].lower() if dot > 0 else ""
    kind = EXT_KIND.get(ext)
    if kind is None:
        return None

    return {
        "dir_path": dir_path or ".",
        "filename": filename,
        "ext": ext,
        "kind": kind,
        "size_bytes": size,
        "mtime": mtime,
    }


# --- scanning -----------------------------------------------------------------

def start_run(db_path: str, volume_id: int) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO scan_runs (volume_id, status, started_at) VALUES (?, 'running', ?)",
            (volume_id, _now()))
        conn.commit()
        return cur.lastrowid


def finish_run(db_path: str, run_id: int, status: str, files: int = 0,
               size: int = 0, error: str = "") -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE scan_runs SET status = ?, files_found = ?, bytes_found = ?,
                   error = ?, finished_at = ? WHERE id = ?""",
            (status, files, size, error[:2000], _now(), run_id))
        conn.commit()


def insert_files(db_path: str, volume_id: int, rows: list[dict]) -> None:
    if not rows:
        return
    with closing(_connect(db_path)) as conn:
        conn.executemany(
            """INSERT INTO media_files
                   (volume_id, dir_path, filename, ext, kind, size_bytes, mtime)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(volume_id, dir_path, filename) DO UPDATE SET
                   size_bytes = excluded.size_bytes,
                   mtime = excluded.mtime""",
            [(volume_id, r["dir_path"], r["filename"], r["ext"], r["kind"],
              r["size_bytes"], r["mtime"]) for r in rows])
        conn.commit()


def clear_volume(db_path: str, volume_id: int) -> None:
    """Drop a volume's previous results so a re-scan doesn't leave files that have
    since been deleted from the drive sitting in the catalogue forever."""
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM media_files WHERE volume_id = ?", (volume_id,))
        conn.execute("DELETE FROM media_folders WHERE volume_id = ?", (volume_id,))
        conn.commit()


def scan_volume_over_ssh(db_path: str, volume: dict, password: str,
                         batch_size: int = 2000, progress=None) -> dict:
    """Run the find on the host and stream results into the catalogue.

    Returns {files, bytes, status, error}. Never raises for a remote-side problem —
    one unreachable drive at 3am must not take down the other eight.
    """
    import paramiko

    volume_id = volume["id"]
    run_id = start_run(db_path, volume_id)
    clear_volume(db_path, volume_id)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    batch, files, total = [], 0, 0
    try:
        client.connect(volume["address"], username=volume["login_user"],
                       password=password, timeout=20,
                       allow_agent=False, look_for_keys=False, banner_timeout=30)
        cmd = build_find_command(volume["mountpoint"])
        _, stdout, stderr = client.exec_command(cmd, timeout=None)

        for raw in stdout:
            row = parse_find_line(raw, volume["mountpoint"])
            if row is None:
                continue
            batch.append(row)
            files += 1
            total += row["size_bytes"]
            if len(batch) >= batch_size:
                insert_files(db_path, volume_id, batch)
                batch = []
                if progress:
                    progress(files, total)

        insert_files(db_path, volume_id, batch)
        exit_status = stdout.channel.recv_exit_status()
        err = stderr.read().decode(errors="replace").strip()

        # Exit 9 is our own marker for "the mount point isn't there" — a real failure.
        # Any other non-zero is find complaining about directories it couldn't read,
        # which is routine and not a reason to discard a good result (or to call a
        # genuinely media-free host a failure).
        if exit_status == 9:
            msg = err or f"{volume['mountpoint']} is not mounted on {volume['address']}"
            finish_run(db_path, run_id, "failed", 0, 0, msg)
            return {"files": 0, "bytes": 0, "status": "failed", "error": msg}

        rebuild_folders(db_path, volume_id)
        finish_run(db_path, run_id, "done", files, total)
        return {"files": files, "bytes": total, "status": "done", "error": ""}

    except Exception as e:
        insert_files(db_path, volume_id, batch)
        msg = f"{type(e).__name__}: {e}"
        finish_run(db_path, run_id, "failed", files, total, msg)
        return {"files": files, "bytes": total, "status": "failed", "error": msg}
    finally:
        client.close()


def scan_local_path(db_path: str, volume: dict, batch_size: int = 2000,
                    progress=None) -> dict:
    """Same as scan_volume_over_ssh but for a drive plugged into this machine.

    Used for the external drives the owner plugs into the laptop. os.scandir rather
    than a shell `find` — Windows has no GNU find, and scandir already returns the
    stat data as part of the directory read, so it's one syscall per entry.
    """
    import os

    volume_id = volume["id"]
    run_id = start_run(db_path, volume_id)
    clear_volume(db_path, volume_id)

    root = volume["mountpoint"]
    prune = {d.lower() for d in PRUNE_DIRS}
    batch, files, total = [], 0, 0
    try:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except (PermissionError, OSError):
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() not in prune:
                            stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat()
                    if st.st_size < MIN_FILE_BYTES:
                        continue
                    dot = entry.name.rfind(".")
                    ext = entry.name[dot + 1:].lower() if dot > 0 else ""
                    kind = EXT_KIND.get(ext)
                    if kind is None:
                        continue
                    dir_path = os.path.dirname(entry.path)
                    if dir_path.startswith(root):
                        dir_path = dir_path[len(root):].strip("\\/")
                    batch.append({
                        "dir_path": dir_path.replace("\\", "/") or ".",
                        "filename": entry.name,
                        "ext": ext,
                        "kind": kind,
                        "size_bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(
                            st.st_mtime, timezone.utc).isoformat(),
                    })
                    files += 1
                    total += st.st_size
                    if len(batch) >= batch_size:
                        insert_files(db_path, volume_id, batch)
                        batch = []
                        if progress:
                            progress(files, total)
                except (OSError, ValueError):
                    continue

        insert_files(db_path, volume_id, batch)
        rebuild_folders(db_path, volume_id)
        finish_run(db_path, run_id, "done", files, total)
        return {"files": files, "bytes": total, "status": "done", "error": ""}
    except Exception as e:
        insert_files(db_path, volume_id, batch)
        msg = f"{type(e).__name__}: {e}"
        finish_run(db_path, run_id, "failed", files, total, msg)
        return {"files": files, "bytes": total, "status": "failed", "error": msg}


# --- rollups ------------------------------------------------------------------

def rebuild_folders(db_path: str, volume_id: int) -> int:
    """Recompute the folder review table for one volume.

    Existing decisions are preserved across a re-scan — the owner shouldn't have to
    re-triage a drive because it got scanned again.
    """
    with closing(_connect(db_path)) as conn:
        prior = {r["dir_path"]: (r["decision"], r["notes"]) for r in conn.execute(
            "SELECT dir_path, decision, notes FROM media_folders WHERE volume_id = ?",
            (volume_id,))}

        rows = conn.execute(
            """SELECT dir_path,
                      COUNT(*)          AS file_count,
                      SUM(size_bytes)   AS total_bytes,
                      MIN(mtime)        AS oldest_mtime,
                      MAX(mtime)        AS newest_mtime
                 FROM media_files WHERE volume_id = ?
                GROUP BY dir_path""", (volume_id,)).fetchall()

        kinds = {}
        for r in conn.execute(
                """SELECT dir_path, kind, COUNT(*) AS n FROM media_files
                    WHERE volume_id = ? GROUP BY dir_path, kind""", (volume_id,)):
            kinds.setdefault(r["dir_path"], {})[r["kind"]] = r["n"]

        exts = {}
        for r in conn.execute(
                """SELECT dir_path, ext, COUNT(*) AS n FROM media_files
                    WHERE volume_id = ? GROUP BY dir_path, ext
                    ORDER BY dir_path, n DESC""", (volume_id,)):
            bucket = exts.setdefault(r["dir_path"], [])
            if len(bucket) < 5:
                bucket.append(f"{r['ext']}:{r['n']}")

        import json
        conn.execute("DELETE FROM media_folders WHERE volume_id = ?", (volume_id,))
        conn.executemany(
            """INSERT INTO media_folders
                   (volume_id, dir_path, file_count, total_bytes, kind_counts,
                    top_exts, oldest_mtime, newest_mtime, decision, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(volume_id, r["dir_path"], r["file_count"], r["total_bytes"] or 0,
              json.dumps(kinds.get(r["dir_path"], {})),
              ", ".join(exts.get(r["dir_path"], [])),
              r["oldest_mtime"], r["newest_mtime"],
              prior.get(r["dir_path"], ("undecided", None))[0],
              prior.get(r["dir_path"], ("undecided", None))[1],
              _now()) for r in rows])
        conn.commit()
        return len(rows)


def folder_table(db_path: str, min_files: int = 1, kind: str = "",
                 decision: str = "", limit: int = 500, offset: int = 0) -> list[dict]:
    """The review table: folders worth deciding on, biggest first."""
    sql = """SELECT f.*, v.mountpoint, v.label, h.address, h.hostname
               FROM media_folders f
               JOIN scan_volumes v ON v.id = f.volume_id
               JOIN scan_hosts h ON h.id = v.host_id
              WHERE f.file_count >= ?"""
    params: list = [min_files]
    if decision:
        sql += " AND f.decision = ?"
        params.append(decision)
    if kind:
        # kind_counts is a JSON object; a substring test is enough to filter on
        # "has any of this kind" without needing the JSON1 extension.
        sql += " AND f.kind_counts LIKE ?"
        params.append(f'%"{kind}"%')
    sql += " ORDER BY f.total_bytes DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def set_decision(db_path: str, folder_id: int, decision: str, notes: str = "") -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE media_folders SET decision = ?, notes = ?, updated_at = ? WHERE id = ?",
            (decision, notes or None, _now(), folder_id))
        conn.commit()
        return cur.rowcount > 0


def summary(db_path: str) -> dict:
    """Headline numbers for the top of the review page."""
    with closing(_connect(db_path)) as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes FROM media_files"
        ).fetchone()
        by_kind = [dict(r) for r in conn.execute(
            """SELECT kind, COUNT(*) AS files, SUM(size_bytes) AS bytes
                 FROM media_files GROUP BY kind ORDER BY bytes DESC""")]
        by_volume = [dict(r) for r in conn.execute(
            """SELECT v.id, v.mountpoint, v.label, h.hostname, h.address,
                      COUNT(m.id) AS files, COALESCE(SUM(m.size_bytes), 0) AS bytes
                 FROM scan_volumes v
                 JOIN scan_hosts h ON h.id = v.host_id
                 LEFT JOIN media_files m ON m.volume_id = v.id
                WHERE v.enabled = 1
                GROUP BY v.id ORDER BY bytes DESC""")]
        decisions = [dict(r) for r in conn.execute(
            """SELECT decision, COUNT(*) AS folders, SUM(total_bytes) AS bytes
                 FROM media_folders GROUP BY decision""")]
        runs = [dict(r) for r in conn.execute(
            """SELECT r.*, v.mountpoint, h.hostname FROM scan_runs r
                 JOIN scan_volumes v ON v.id = r.volume_id
                 JOIN scan_hosts h ON h.id = v.host_id
                ORDER BY r.id DESC LIMIT 20""")]
        return {
            "total_files": totals["files"],
            "total_bytes": totals["bytes"],
            "by_kind": by_kind,
            "by_volume": by_volume,
            "decisions": decisions,
            "recent_runs": runs,
        }


# --- collecting into one local tree ------------------------------------------

def long_path(path: str) -> str:
    r"""Make a Windows path immune to the 260-character MAX_PATH limit.

    Real failures from the first collection run, all the same shape:
        D:\Collected Art\Laptop-E\Downloads\Design-Assets\Tshirt Designs\
        450 SVG Design Mega Bundle Vol 4\The weather outside is frightful,
        but the wine is so delightful-01.png

    Design bundles nest deeply and name files after the whole slogan, so this is
    routine here, not an edge case. The \\?\ prefix opts a path out of MAX_PATH, but
    only if it's absolute and backslash-separated with no . or .. components — hence
    the normpath/abspath before prefixing. No-op off Windows.
    """
    import os

    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(os.path.normpath(path))
    if full.startswith("\\\\"):  # UNC share -> \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + full.lstrip("\\")
    return "\\\\?\\" + full


def volume_slug(volume: dict) -> str:
    """A stable, filesystem-safe folder name identifying where files came from."""
    host = volume.get("hostname") or volume.get("address") or "unknown"
    label = volume.get("label") or volume.get("mountpoint") or ""
    label = label.strip("/\\").replace(":", "").replace("/", "-").replace("\\", "-")
    raw = f"{host}-{label}" if label else host
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-") or "unknown"


def collect_volume(db_path: str, volume: dict, dest_root: str, kinds=None,
                   dedupe: bool = True, progress=None, ssh_password: str = "") -> dict:
    """Copy a volume's catalogued files into dest_root, preserving structure.

    Layout is `<dest_root>/<volume slug>/<original relative path>`. Flattening would
    collide immediately — there are four folders called "planters" across these drives
    — and would throw away the folder names, which are most of what identifies a design.

    Files are hashed with SHA-256 *while* being copied. The bytes have to be read
    anyway, so the hash is free, and it makes cross-drive dedupe exact rather than a
    guess from name and size. With `dedupe`, a file whose hash was already collected is
    recorded as a duplicate and not written twice.

    Resumable: a destination that already exists at the right size is left alone.
    """
    import hashlib
    import os

    volume_id = volume["id"]
    remote = volume.get("host_kind") != "local"
    sftp = client = None

    with closing(_connect(db_path)) as conn:
        sql = """SELECT id, dir_path, filename, size_bytes, kind FROM media_files
                  WHERE volume_id = ?"""
        params: list = [volume_id]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += list(kinds)
        sql += " ORDER BY dir_path, filename"
        rows = [dict(r) for r in conn.execute(sql, params)]
        done = {r["source_path"] for r in conn.execute(
            """SELECT source_path FROM media_copies
                WHERE volume_id = ? AND status IN ('copied', 'duplicate')""", (volume_id,))}
        seen_hashes = {r["content_hash"]: r["dest_path"] for r in conn.execute(
            "SELECT content_hash, dest_path FROM media_copies WHERE status = 'copied'")
            if r["content_hash"]}

    target_root = os.path.join(dest_root, volume_slug(volume))
    mount = volume["mountpoint"]
    stats = {"copied": 0, "duplicate": 0, "skipped": 0, "failed": 0,
             "bytes": 0, "read_bytes": 0}
    batch = []
    last_flush = time.monotonic()

    try:
        if remote:
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(volume["address"], username=volume["login_user"],
                           password=ssh_password, timeout=20,
                           allow_agent=False, look_for_keys=False, banner_timeout=30)
            sftp = client.open_sftp()

        for i, row in enumerate(rows, 1):
            rel = os.path.join(row["dir_path"].replace("/", os.sep), row["filename"]) \
                if row["dir_path"] != "." else row["filename"]
            if remote:
                src = f"{mount.rstrip('/')}/{row['dir_path']}/{row['filename']}" \
                    if row["dir_path"] != "." else f"{mount.rstrip('/')}/{row['filename']}"
            else:
                src = os.path.join(mount, rel)

            if src in done:
                stats["skipped"] += 1
                continue

            dest = os.path.join(target_root, rel)
            # Every filesystem call below goes through the long-path form; the plain
            # `dest` is what gets recorded, since that's the path a human will type.
            wdest = long_path(dest)
            rec = {"file_id": row["id"], "src": src, "dest": dest, "hash": None,
                   "size": row["size_bytes"], "status": "copied",
                   "dup_of": None, "error": ""}
            try:
                # Already there at the right size — a resumed run must not re-read
                # gigabytes to prove what the filesystem already tells us.
                if os.path.exists(wdest) and os.path.getsize(wdest) == row["size_bytes"]:
                    rec["status"] = "skipped"
                    stats["skipped"] += 1
                elif dedupe and row["size_bytes"] <= BUFFER_MAX_BYTES:
                    # Small file, dedupe on: hash from memory and only touch the disk
                    # if it turns out to be new. On a drive that is 80% duplicates of
                    # one already collected, writing every file to a .part just to
                    # delete it doubles the write load for nothing.
                    opener = sftp.open(src, "rb") if remote else open(long_path(src), "rb")
                    with opener as fin:
                        if remote:
                            fin.prefetch()
                        data = fin.read()
                    rec["hash"] = hashlib.sha256(data).hexdigest()
                    rec["size"] = len(data)
                    stats["read_bytes"] += len(data)

                    if rec["hash"] in seen_hashes:
                        rec["status"] = "duplicate"
                        rec["dup_of"] = seen_hashes[rec["hash"]]
                        rec["dest"] = None
                        stats["duplicate"] += 1
                    else:
                        os.makedirs(os.path.dirname(wdest), exist_ok=True)
                        tmp = wdest + ".part"
                        with open(tmp, "wb") as fout:
                            fout.write(data)
                        os.replace(tmp, wdest)
                        seen_hashes[rec["hash"]] = dest
                        stats["copied"] += 1
                        stats["bytes"] += len(data)
                    del data
                else:
                    # Large file (or dedupe off): stream it, since buffering a
                    # multi-gigabyte bundle in memory is the worse trade.
                    os.makedirs(os.path.dirname(wdest), exist_ok=True)
                    tmp = wdest + ".part"
                    h = hashlib.sha256()
                    total = 0
                    opener = sftp.open(src, "rb") if remote else open(long_path(src), "rb")
                    with opener as fin, open(tmp, "wb") as fout:
                        if remote:
                            fin.prefetch()
                        while True:
                            chunk = fin.read(1024 * 1024)
                            if not chunk:
                                break
                            h.update(chunk)
                            fout.write(chunk)
                            total += len(chunk)
                    rec["hash"] = h.hexdigest()
                    rec["size"] = total
                    stats["read_bytes"] += total

                    if dedupe and rec["hash"] in seen_hashes:
                        os.remove(tmp)
                        rec["status"] = "duplicate"
                        rec["dup_of"] = seen_hashes[rec["hash"]]
                        rec["dest"] = None
                        stats["duplicate"] += 1
                    else:
                        os.replace(tmp, wdest)
                        seen_hashes[rec["hash"]] = dest
                        stats["copied"] += 1
                        stats["bytes"] += total
            except Exception as e:
                rec["status"] = "failed"
                rec["error"] = f"{type(e).__name__}: {e}"[:400]
                stats["failed"] += 1
                try:
                    if os.path.exists(wdest + ".part"):
                        os.remove(wdest + ".part")
                except OSError:
                    pass

            batch.append((rec["file_id"], volume_id, rec["src"], rec["dest"], rec["hash"],
                          rec["size"], rec["status"], rec["dup_of"], rec["error"], _now()))

            # Flush on count *or* elapsed time. A count-only trigger goes quiet for
            # ten minutes on a run whose first 200 files are multi-gigabyte bundles,
            # which reads as a hang from the UI and from anyone watching the log.
            if (len(batch) >= 200 or i == len(rows)
                    or time.monotonic() - last_flush >= 15):
                _write_copies(db_path, batch)
                batch = []
                last_flush = time.monotonic()
                if progress:
                    progress(i, len(rows), stats)
    finally:
        _write_copies(db_path, batch)
        if sftp:
            sftp.close()
        if client:
            client.close()

    return stats


def _write_copies(db_path: str, batch: list) -> None:
    if not batch:
        return
    with closing(_connect(db_path)) as conn:
        conn.executemany(
            """INSERT INTO media_copies
                   (file_id, volume_id, source_path, dest_path, content_hash,
                    size_bytes, status, duplicate_of, error, copied_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(volume_id, source_path) DO UPDATE SET
                   dest_path = excluded.dest_path,
                   content_hash = excluded.content_hash,
                   size_bytes = excluded.size_bytes,
                   status = excluded.status,
                   duplicate_of = excluded.duplicate_of,
                   error = excluded.error,
                   copied_at = excluded.copied_at""", batch)
        conn.commit()


def collection_summary(db_path: str) -> dict:
    with closing(_connect(db_path)) as conn:
        by_status = [dict(r) for r in conn.execute(
            """SELECT status, COUNT(*) files, COALESCE(SUM(size_bytes),0) bytes
                 FROM media_copies GROUP BY status""")]
        by_volume = [dict(r) for r in conn.execute(
            """SELECT h.hostname, h.address, v.mountpoint, v.label,
                      SUM(c.status='copied') copied, SUM(c.status='duplicate') duplicate,
                      SUM(c.status='failed') failed,
                      COALESCE(SUM(CASE WHEN c.status='copied' THEN c.size_bytes END),0) bytes
                 FROM media_copies c
                 JOIN scan_volumes v ON v.id = c.volume_id
                 JOIN scan_hosts h ON h.id = v.host_id
                GROUP BY c.volume_id ORDER BY bytes DESC""")]
        return {"by_status": by_status, "by_volume": by_volume}


def peek_local_archives(db_path: str, volume_id: int, progress=None) -> dict:
    """Read the index of every archive on a local volume and record what's inside.

    Only reads the central directory — no extraction, no temp files. A 500MB zip is
    answered by a couple of seeks. Archives that are encrypted, truncated or in a format
    the stdlib can't open record the error rather than vanishing, so a folder full of
    unreadable RARs still shows up as something needing a decision.
    """
    import json
    import zipfile

    with closing(_connect(db_path)) as conn:
        vol = conn.execute("SELECT mountpoint FROM scan_volumes WHERE id = ?",
                           (volume_id,)).fetchone()
        if vol is None:
            return {"scanned": 0, "failed": 0}
        root = vol["mountpoint"]
        rows = [dict(r) for r in conn.execute(
            """SELECT id, dir_path, filename FROM media_files
                WHERE volume_id = ? AND kind = 'archive'
                  AND id NOT IN (SELECT file_id FROM media_archives)""", (volume_id,))]

    import os
    scanned = failed = 0
    batch = []
    for i, row in enumerate(rows, 1):
        full = os.path.join(root, row["dir_path"].replace("/", os.sep), row["filename"])
        rec = {"file_id": row["id"], "inner_files": 0, "inner_media": 0,
               "inner_bytes": 0, "kinds": {}, "exts": {}, "samples": [], "error": ""}
        try:
            if not full.lower().endswith(".zip"):
                # rar/7z need external tooling; record them as unread rather than
                # silently reporting an empty archive.
                raise NotImplementedError("not a zip — needs external extractor")
            with zipfile.ZipFile(full) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    rec["inner_files"] += 1
                    rec["inner_bytes"] += info.file_size
                    name = info.filename
                    dot = name.rfind(".")
                    ext = name[dot + 1:].lower() if dot > 0 else ""
                    kind = EXT_KIND.get(ext)
                    if kind and kind != "archive":
                        rec["inner_media"] += 1
                        rec["kinds"][kind] = rec["kinds"].get(kind, 0) + 1
                        rec["exts"][ext] = rec["exts"].get(ext, 0) + 1
                        if len(rec["samples"]) < 5:
                            rec["samples"].append(name)
            scanned += 1
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            failed += 1

        top = sorted(rec["exts"].items(), key=lambda kv: -kv[1])[:5]
        batch.append((rec["file_id"], volume_id, rec["inner_files"], rec["inner_media"],
                      rec["inner_bytes"], json.dumps(rec["kinds"]),
                      ", ".join(f"{e}:{n}" for e, n in top),
                      " | ".join(rec["samples"]), rec["error"], _now()))

        if len(batch) >= 100 or i == len(rows):
            with closing(_connect(db_path)) as conn:
                conn.executemany(
                    """INSERT INTO media_archives
                           (file_id, volume_id, inner_files, inner_media, inner_bytes,
                            kind_counts, top_exts, sample_paths, error, scanned_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(file_id) DO UPDATE SET
                           inner_files = excluded.inner_files,
                           inner_media = excluded.inner_media,
                           inner_bytes = excluded.inner_bytes,
                           kind_counts = excluded.kind_counts,
                           top_exts = excluded.top_exts,
                           sample_paths = excluded.sample_paths,
                           error = excluded.error,
                           scanned_at = excluded.scanned_at""", batch)
                conn.commit()
            batch = []
            if progress:
                progress(i, len(rows))

    return {"scanned": scanned, "failed": failed, "total": len(rows)}


def archive_table(db_path: str, limit: int = 200, unreadable: bool = False) -> list[dict]:
    """Archives ranked by how much media they contain, for the review page."""
    sql = """SELECT a.*, f.dir_path, f.filename, f.size_bytes,
                    v.mountpoint, v.label, h.hostname, h.address
               FROM media_archives a
               JOIN media_files f ON f.id = a.file_id
               JOIN scan_volumes v ON v.id = a.volume_id
               JOIN scan_hosts h ON h.id = v.host_id
              WHERE a.error """ + ("!= ''" if unreadable else "= ''") + """
              ORDER BY a.inner_media DESC, f.size_bytes DESC LIMIT ?"""
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, (limit,))]


def duplicate_groups(db_path: str, limit: int = 200) -> list[dict]:
    """Files that appear more than once by (size, filename).

    Not proof of duplication — but on drives named 'TransferDisk' and 'NewVolume' the
    same file genuinely is sitting in four places, and this finds it for the cost of a
    GROUP BY instead of reading 440GB. Confirm with hash_candidates() before deleting
    anything.
    """
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT filename, size_bytes, COUNT(*) AS copies,
                      SUM(size_bytes) - size_bytes AS wasted_bytes,
                      GROUP_CONCAT(DISTINCT volume_id) AS volume_ids
                 FROM media_files
                GROUP BY filename, size_bytes
               HAVING copies > 1
                ORDER BY wasted_bytes DESC LIMIT ?""", (limit,))]
