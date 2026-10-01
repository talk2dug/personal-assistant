"""A full inventory of every folder on every drive in the house, for deciding what goes where.

This replaces media_scan.py's view of the world for *organising*. That module only ever
looked at media (by extension, above 8KB) and rolled files up into a flat list of
32,000 leaf folders with no totals -- fine for "copy the artwork", useless for "this old
laptop drive: what's on it, and where should each part live". Jack's words: it was
"really hard for me to determine what to import and what not to import."

What this does differently, and why:

**Every file, every folder, full depth.** Documents, code, installers and junk are half
of what's on a recovered drive; an inventory that can't see them can't help clean it up.

**Totals roll up the tree.** Each folder carries its own files AND everything beneath it
(files, bytes, a per-type byte breakdown), so the page can be browsed like WinDirStat:
start at the drive root, see which child is 80% of it, go in.

**Drives are identified by filesystem UUID / volume serial, not mount point.** media_scan
keyed on "E:\\", and the same physical drive is F: now. A drive that moved letters must
keep its history and its tags.

**Bulk noise is collapsed, not dropped.** node_modules, .git, AppData, a live OS's
/usr and C:\\Windows are counted (their size matters -- "that Windows folder is 38GB of
junk from the old laptop") but get no per-file rows and no subfolders, which keeps the
database to what a person would ever browse.

**Categories are suggested, then owned by Jack.** Each folder gets a suggested category
with a written reason. Tagging a folder applies to everything beneath it unless a
subfolder has its own tag. Tags are keyed by (drive UUID, path), so a rescan never loses
them.

Lives in its own SQLite file (inventory.db beside jarvis.db): a few million file rows
don't belong in the live app database or its backups.
"""
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS inv_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    address TEXT,
    os TEXT,
    reachable INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS inv_volumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    host_id INTEGER NOT NULL REFERENCES inv_hosts(id),
    mount TEXT NOT NULL,
    label TEXT,
    fstype TEXT,
    size_bytes INTEGER,
    used_bytes INTEGER,
    is_system INTEGER NOT NULL DEFAULT 0,
    online INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT,
    current_scan_id INTEGER,
    excluded INTEGER NOT NULL DEFAULT 0,
    exclude_reason TEXT
);

CREATE TABLE IF NOT EXISTS inv_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id INTEGER NOT NULL REFERENCES inv_volumes(id),
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    files INTEGER NOT NULL DEFAULT 0,
    dirs INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS inv_dirs (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL,
    volume_id INTEGER NOT NULL,
    parent_id INTEGER,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    depth INTEGER NOT NULL,
    collapsed TEXT,
    direct_files INTEGER NOT NULL DEFAULT 0,
    direct_bytes INTEGER NOT NULL DEFAULT 0,
    total_files INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    total_dirs INTEGER NOT NULL DEFAULT 0,
    oldest_mtime REAL,
    newest_mtime REAL,
    type_bytes TEXT,
    top_exts TEXT,
    camera_files INTEGER NOT NULL DEFAULT 0,
    project_marker INTEGER NOT NULL DEFAULT 0,
    suggested TEXT,
    suggested_reason TEXT,
    category TEXT,
    category_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_dirs_parent ON inv_dirs(parent_id);
CREATE INDEX IF NOT EXISTS idx_inv_dirs_path ON inv_dirs(scan_id, path);
CREATE INDEX IF NOT EXISTS idx_inv_dirs_name ON inv_dirs(name);

CREATE TABLE IF NOT EXISTS inv_files (
    scan_id INTEGER NOT NULL,
    dir_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    ext TEXT,
    ftype TEXT,
    size_bytes INTEGER NOT NULL,
    mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_inv_files_dir ON inv_files(dir_id);
CREATE INDEX IF NOT EXISTS idx_inv_files_scan ON inv_files(scan_id);

-- Content hashes, keyed like tags (drive uid + path) so a rescan doesn't throw them
-- away. size+mtime are kept so a file that changed since hashing is re-hashed.
CREATE TABLE IF NOT EXISTS inv_hashes (
    volume_uid TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL,
    sha256 TEXT NOT NULL,
    hashed_at TEXT NOT NULL,
    PRIMARY KEY (volume_uid, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_inv_hashes_sha ON inv_hashes(sha256);

CREATE TABLE IF NOT EXISTS inv_tags (
    volume_uid TEXT NOT NULL,
    path TEXT NOT NULL,
    category TEXT NOT NULL,
    note TEXT,
    set_at TEXT NOT NULL,
    PRIMARY KEY (volume_uid, path)
);
"""

# --------------------------------------------------------------------- categories

CATEGORIES = [
    ("side_hustle", "Side hustle"),
    ("photos", "Photos"),
    ("home_video", "Home video"),
    ("music", "Music"),
    ("projects", "Projects"),
    ("documents", "Documents"),
    ("software", "Software"),
    ("system_junk", "System & junk"),
    ("unsorted", "Unsorted"),
]
CATEGORY_KEYS = {k for k, _ in CATEGORIES}

# ---------------------------------------------------------------------- file types
# Grouped by what the file IS, for the breakdown bar. Categories are where it BELONGS;
# the two are related but not the same (a PNG can be a product mockup or a holiday snap).
FILE_TYPES = {
    "image": "jpg jpeg png gif webp bmp tif tiff heic heif avif raw cr2 nef arw dng",
    "video": "mp4 mov avi mkv webm m4v mpg mpeg wmv 3gp mts m2ts vob flv",
    "audio": "mp3 wav aac flac m4a ogg wma aiff alac opus mid midi",
    "design": ("psd psb xcf ai eps svg cdr afdesign afphoto procreate clip kra indd dxf "
               "studio3 studio gsp scut5 scut4 fcm sbp ttf otf woff woff2 pes dst jef"),
    "model3d": "stl 3mf obj step stp f3d f3z blend gcode ply fbx skp scad",
    "document": ("pdf doc docx xls xlsx ppt pptx odt ods odp rtf txt md csv tsv pages "
                 "numbers key epub mobi xps one msg eml vcf ics"),
    "code": ("py js ts jsx tsx c h cpp hpp cs java go rs rb php html htm css scss json yaml "
             "yml toml ini cfg xml sh bat ps1 ipynb sql lua ino kt swift vue r m"),
    "archive": "zip rar 7z tar gz tgz bz2 xz",
    "software": "exe msi dmg iso apk deb rpm img appimage pkg cab msix vhd vhdx vmdk ova",
    "system": "dll sys drv ocx mui log tmp dat bak lnk db ldb dmp etl evtx pf manifest cat so o a lib pyc",
}
EXT_TYPE = {ext: t for t, exts in FILE_TYPES.items() for ext in exts.split()}
TYPE_ORDER = list(FILE_TYPES) + ["other"]

_CAMERA_RE = re.compile(
    r"^(img|dsc|dscn|dscf|pxl|vid|mov|mvi|gopr|gh\d|gx\d|dji|sam|wp|p\d{7}|\d{8}[_-]\d{6}|"
    r"\d{4}-\d{2}-\d{2} \d{2}\.\d{2})", re.I)
_PROJECT_FILES = {
    "package.json", "requirements.txt", "pyproject.toml", "setup.py", "cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "cmakelists.txt", "makefile", "platformio.ini", "docker-compose.yml",
    "dockerfile", ".gitignore", "tsconfig.json", "vite.config.js",
}

# Folders counted but not itemised. Matched on the folder's own name, anywhere.
COLLAPSE_NAMES = {
    "node_modules": "system_junk", ".git": "projects", ".svn": "projects", "__pycache__": "system_junk",
    ".venv": "system_junk", "venv": "system_junk", "site-packages": "system_junk",
    "dist-packages": "system_junk", ".cache": "system_junk", ".npm": "system_junk",
    ".gradle": "system_junk", ".pio": "system_junk", ".idea": "system_junk", ".vs": "system_junk",
    "$recycle.bin": "system_junk", "system volume information": "system_junk",
    ".trash-1000": "system_junk", ".trashes": "system_junk", ".spotlight-v100": "system_junk",
    ".fseventsd": "system_junk", "lost+found": "system_junk", "appdata": "system_junk",
    "frigate": "system_junk", "windows.old": "system_junk",
}
# On the drive a machine is RUNNING from, these top-level trees are the OS itself.
# On a recovered drive they are NOT collapsed: a dead laptop's Windows folder is exactly
# the junk Jack wants to see and delete, so it's itemised and suggested as System & junk.
SYSTEM_ROOT_COLLAPSE = {
    "windows": "system_junk", "program files": "software", "program files (x86)": "software",
    "programdata": "system_junk", "usr": "system_junk", "lib": "system_junk", "lib64": "system_junk",
    "bin": "system_junk", "sbin": "system_junk", "etc": "system_junk", "var": "system_junk",
    "snap": "software", "boot": "system_junk", "opt": "software", "recovery": "system_junk",
    "perflogs": "system_junk",
}
# Never walked at all: virtual filesystems with no bytes on disk.
LINUX_PRUNE = ["proc", "sys", "dev", "run", "tmp"]

# ------------------------------------------------------------------ name markers
# First match wins. side_hustle and system_junk are "sticky": everything beneath inherits
# them regardless of subfolder names, so "Etsy Shop/Photos" stays side hustle and
# "Windows/Media" doesn't become Music.
_MARKERS = [
    ("system_junk", re.compile(
        r"^(windows|windows\.old|program files.*|programdata|\$recycle\.bin|recovery|perflogs|"
        r"msocache|config\.msi|drivers|temp|tmp|cache|caches|thumbnails|\.thumbnails|"
        r"system32|syswow64|winsxs|intel|amd|nvidia|esd|\$windows\.~bt|\$winreagent)$", re.I)),
    ("side_hustle", re.compile(
        # design[_ ]assets / podfriendly: the NAS's Share/Saved SHit/Design_Assets tree is
        # all bought design bundles, and without these its "Christmas"/"Birthday" bundles
        # were read as personal photos (6 folders filed under Personal/Photos, 2026-09-29).
        r"(etsy|shopify|printify|printful|blue ?ridge|brcc|custom ?co|cricut|silhouette|"
        r"design[_ -]?assets|podfriendly|"
        r"decal|sticker|vinyl|sublimat|mockup|tumbler|t-?shirt|svg|cut ?files?|design bundle|"
        r"creative ?fabrica|designbundles|so fontsy|collected art|print ?station|laser|"
        r"glowforge|lightburn|products?|listings?|merch|pod\b|dtf|htv|patterns?|templates?|"
        r"logos?|clip ?art|graphics|fonts|leonardo|midjourney|dall-?e|stable ?diffusion|comfy ?ui|"
        r"\bai[ _-]?(art|models?|generated|images?)\b|generated)", re.I)),
    ("photos", re.compile(
        r"^(dcim|camera( roll)?|pictures|my pictures|photos?|google photos|icloud photos|"
        r"iphone|android|takeout|screenshots|wallpapers?|100apple|\d{3}apple|\d{3}_?(canon|nikon)?)$|"
        r"(^|\b)(photos|pictures|vacation|wedding|birthday|christmas)(\b|$)", re.I)),
    ("home_video", re.compile(
        r"^(videos?|my videos|movies|home movies|gopro|dji|camcorder|tv shows?|films?)$", re.I)),
    ("music", re.compile(r"^(music|my music|itunes|itunes media|audio|songs|playlists|albums|podcasts)$", re.I)),
    ("software", re.compile(
        r"^(installers?|setups?|software|programs|apps|drivers|isos?|firmware|downloads?/software)$",
        re.I)),
    ("projects", re.compile(
        r"^(projects?|repos?|source|src|code|dev|github|gitlab|arduino|platformio|fusion 360|"
        r"cad|kicad|esphome|home ?assistant|raspberry ?pi|3d ?prints?|thingiverse|printables)$", re.I)),
    ("documents", re.compile(
        r"^(documents|my documents|docs|paperwork|taxes|tax|receipts|bills|bank(ing)?|insurance|"
        r"medical|records|resumes?|scans|scanned|statements|invoices|contracts|legal|school)$|"
        r"(^|\b)(tax(es)? ?\d{4}|w-?2|1099)(\b|$)", re.I)),
]
STICKY = {"side_hustle", "system_junk"}

# A type holding at least this share of a folder's bytes decides it by content.
DOMINANT_SHARE = 0.6
TYPE_TO_CATEGORY = {
    "video": "home_video", "audio": "music", "design": "side_hustle", "document": "documents",
    "code": "projects", "model3d": "projects", "software": "software", "system": "system_junk",
}


# Folder ids per scan. No drive holds anywhere near ten million folders.
ID_BLOCK = 10_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_db_path(jarvis_db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(jarvis_db_path)), "inventory.db")


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        have = {r[1] for r in conn.execute("PRAGMA table_info(inv_dirs)")}
        for col in ("camera_files", "project_marker"):
            if col not in have:
                conn.execute(f"ALTER TABLE inv_dirs ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def file_type(ext: str) -> str:
    return EXT_TYPE.get(ext, "other")


def split_ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1:].lower() if 0 < dot < len(name) - 1 else ""


# ======================================================================= aggregation

class _Dir:
    __slots__ = ("path", "name", "depth", "collapsed", "direct_files", "direct_bytes",
                 "total_files", "total_bytes", "total_dirs", "oldest", "newest",
                 "type_bytes", "exts", "camera", "project_marker", "id", "parent",
                 "suggested", "reason")

    def __init__(self, path, name, depth, parent):
        self.path, self.name, self.depth, self.parent = path, name, depth, parent
        self.collapsed = None
        self.direct_files = self.direct_bytes = 0
        self.total_files = self.total_bytes = self.total_dirs = 0
        self.oldest = self.newest = None
        self.type_bytes = {}
        self.exts = {}
        self.camera = 0
        self.project_marker = False
        self.id = None


class Aggregator:
    """Builds the folder tree for ONE volume from a stream of entries.

    Feed it (kind, size, mtime, relpath) tuples -- kind 'f' or 'd', relpath relative to
    the volume root with '/' separators. Files are yielded back in batches for insertion
    (so a 400k-file drive never sits in memory twice); folders are held until finish()
    because their totals depend on everything beneath them.
    """

    def __init__(self, is_system_volume: bool = False):
        self.is_system = is_system_volume
        self.root = _Dir("", "", 0, None)
        self.dirs = {"": self.root}
        self._collapse_of = {"": None}
        self.files = 0
        self.bytes = 0

    def _dir(self, path: str) -> _Dir:
        d = self.dirs.get(path)
        if d is not None:
            return d
        slash = path.rfind("/")
        parent_path, name = (path[:slash], path[slash + 1:]) if slash >= 0 else ("", path)
        parent = self._dir(parent_path)
        d = _Dir(path, name, parent.depth + 1, parent)
        low = name.lower()
        if low in COLLAPSE_NAMES:
            d.collapsed = COLLAPSE_NAMES[low]
        elif self.is_system and parent is self.root and low in SYSTEM_ROOT_COLLAPSE:
            d.collapsed = SYSTEM_ROOT_COLLAPSE[low]
        self.dirs[path] = d
        return d

    def _owner(self, path: str) -> _Dir:
        """The folder an entry is recorded against: itself, or the outermost collapsed
        folder above it (whose contents are counted but not itemised).

        _collapse_of maps every path seen to the collapsed folder that owns it, or None
        when it is itemised normally."""
        if path in self._collapse_of:
            owner = self._collapse_of[path]
            return self.dirs[owner if owner is not None else path]
        slash = path.rfind("/")
        parent_path = path[:slash] if slash >= 0 else ""
        self._owner(parent_path)
        above = self._collapse_of[parent_path]
        if above is not None:
            self._collapse_of[path] = above
            return self.dirs[above]
        d = self._dir(path)
        self._collapse_of[path] = path if d.collapsed else None
        return d

    def add(self, kind: str, size: int, mtime: float | None, relpath: str):
        """Returns a file row tuple (dir, name, ext, ftype, size, mtime) to store, or None."""
        relpath = relpath.strip("/")
        if kind == "d":
            if relpath:
                self._owner(relpath)
            return None
        if not relpath:
            return None
        slash = relpath.rfind("/")
        dir_path, name = (relpath[:slash], relpath[slash + 1:]) if slash >= 0 else ("", relpath)
        d = self._owner(dir_path)
        ext = split_ext(name)
        ftype = file_type(ext)
        d.direct_files += 1
        d.direct_bytes += size
        d.type_bytes[ftype] = d.type_bytes.get(ftype, 0) + size
        if ext:
            e = d.exts.get(ext)
            if e is None:
                d.exts[ext] = [1, size]
            else:
                e[0] += 1
                e[1] += size
        if mtime:
            if d.oldest is None or mtime < d.oldest:
                d.oldest = mtime
            if d.newest is None or mtime > d.newest:
                d.newest = mtime
        self.files += 1
        self.bytes += size
        if d.collapsed:
            return None
        if ftype in ("image", "video") and _CAMERA_RE.match(name):
            d.camera += 1
        if name.lower() in _PROJECT_FILES:
            d.project_marker = True
        return (d, name, ext, ftype, size, mtime)

    def finish(self) -> list[_Dir]:
        """Roll totals up the tree; returns folders parents-first."""
        ordered = sorted(self.dirs.values(), key=lambda d: d.depth)
        for d in ordered:
            d.total_files, d.total_bytes = d.direct_files, d.direct_bytes
        for d in reversed(ordered):
            p = d.parent
            if p is None:
                continue
            p.total_files += d.total_files
            p.total_bytes += d.total_bytes
            p.total_dirs += d.total_dirs + 1
            for t, b in d.type_bytes.items():
                p.type_bytes[t] = p.type_bytes.get(t, 0) + b
            for ext, (n, b) in d.exts.items():
                e = p.exts.get(ext)
                if e is None:
                    p.exts[ext] = [n, b]
                else:
                    e[0] += n
                    e[1] += b
            p.camera += d.camera
            if d.oldest is not None and (p.oldest is None or d.oldest < p.oldest):
                p.oldest = d.oldest
            if d.newest is not None and (p.newest is None or d.newest > p.newest):
                p.newest = d.newest
        return ordered


# ======================================================================= classifier

def _marker(name: str) -> str | None:
    for cat, rx in _MARKERS:
        if rx.search(name):
            return cat
    return None


def suggest(d, parent_cat: str | None, parent_sticky: bool) -> tuple[str, str, bool]:
    """(category, reason, sticky) for one folder, given what its parent resolved to.

    Order of evidence: a sticky ancestor, a collapsed-folder type, the folder's own name,
    project files inside it, what its bytes are mostly made of, and finally its parent.
    """
    if parent_sticky and parent_cat:
        return parent_cat, "inside a folder already sorted as this", True
    if d.collapsed:
        cat = parent_cat if d.collapsed == "projects" and parent_cat == "projects" else d.collapsed
        return cat, f"'{d.name}' is counted but not itemised (tool/OS data)", cat in STICKY
    if d.name:
        m = _marker(d.name)
        if m:
            return m, f"folder name '{d.name}'", m in STICKY
    if d.project_marker:
        return "projects", "contains project files (package.json, requirements.txt, ...)", False
    if d.name.startswith(".") and len(d.name) > 1:
        return "software", f"hidden app folder '{d.name}'", False

    total = d.total_bytes
    if total > 0 and d.type_bytes:
        top_type, top_bytes = max(d.type_bytes.items(), key=lambda kv: kv[1])
        share = top_bytes / total
        if share >= DOMINANT_SHARE:
            pct = round(share * 100)
            if top_type == "image":
                camera_share = d.camera / max(1, d.total_files)
                if camera_share >= 0.3:
                    return "photos", f"{pct}% images, mostly camera-named", False
                if parent_cat and parent_cat != "unsorted":
                    return parent_cat, f"{pct}% images, inside a {parent_cat.replace('_', ' ')} folder", False
                # Artwork, icons and AI renders are all "images" too. Guessing Photos for
                # them was wrong more often than right on the first real drives.
                return "unsorted", f"{pct}% images, but not camera photos: artwork or photos?", False
            if top_type == "video" and d.camera == 0 and parent_cat and parent_cat != "unsorted":
                return parent_cat, f"{pct}% video, inside a {parent_cat.replace('_', ' ')} folder", False
            cat = TYPE_TO_CATEGORY.get(top_type)
            if cat:
                return cat, f"{pct}% {top_type} files by size", cat in STICKY and top_type == "system"
    if parent_cat:
        return parent_cat, "same as the folder it's in", False
    return "unsorted", "nothing to go on yet", False


def classify(dirs_parents_first) -> None:
    """Sets d.suggested / d.reason on every folder of a finished Aggregator."""
    resolved = {}
    for d in dirs_parents_first:
        if d.parent is None:
            d.suggested, d.reason, sticky = "unsorted", "drive root", False
            if d.project_marker:
                d.suggested, d.reason = "projects", "contains project files"
        else:
            pc, ps = resolved[id(d.parent)]
            d.suggested, d.reason, sticky = suggest(d, None if pc == "unsorted" else pc, ps)
        resolved[id(d)] = (d.suggested, sticky)


def reclassify(db_path: str, volume_id: int | None = None) -> int:
    """Re-run the suggestion rules over what's already scanned, then re-apply tags.
    For when the rules improve: no drive needs walking again."""
    n = 0
    with closing(connect(db_path)) as conn:
        q = "SELECT id, current_scan_id FROM inv_volumes WHERE current_scan_id IS NOT NULL"
        vols = [tuple(r) for r in (conn.execute(q + " AND id = ?", (volume_id,)) if volume_id
                                   else conn.execute(q))]
        for _, scan_id in vols:
            nodes, ordered = {}, []
            for r in conn.execute(
                    """SELECT id, parent_id, name, depth, collapsed, total_files, total_bytes, type_bytes,
                              camera_files, project_marker FROM inv_dirs WHERE scan_id = ? ORDER BY depth""",
                    (scan_id,)):
                d = _Dir(None, r["name"], r["depth"], nodes.get(r["parent_id"]))
                d.id, d.collapsed = r["id"], r["collapsed"]
                d.total_files, d.total_bytes = r["total_files"], r["total_bytes"]
                d.type_bytes = json.loads(r["type_bytes"] or "{}")
                d.camera, d.project_marker = r["camera_files"], bool(r["project_marker"])
                nodes[r["id"]] = d
                ordered.append(d)
            classify(ordered)
            conn.executemany("UPDATE inv_dirs SET suggested = ?, suggested_reason = ? WHERE id = ?",
                             [(d.suggested, d.reason, d.id) for d in ordered])
            conn.commit()
            n += len(ordered)
    for vid, _ in vols:
        apply_tags(db_path, vid)
    return n


# ====================================================================== persistence

def upsert_host(db_path: str, name: str, address: str, os_name: str, reachable: bool = True,
                error: str | None = None) -> int:
    with closing(connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO inv_hosts (name, address, os, reachable, last_seen_at, error)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET address=excluded.address, os=excluded.os,
                   reachable=excluded.reachable, error=excluded.error,
                   last_seen_at=CASE WHEN excluded.reachable THEN excluded.last_seen_at ELSE last_seen_at END""",
            (name, address, os_name, int(reachable), _now(), error))
        conn.commit()
        return conn.execute("SELECT id FROM inv_hosts WHERE name = ?", (name,)).fetchone()[0]


def upsert_volume(db_path: str, host_id: int, uid: str, mount: str, label: str = "", fstype: str = "",
                  size_bytes: int | None = None, used_bytes: int | None = None, is_system: bool = False) -> int:
    """Keyed on uid: a drive that moved from E: to F:, or from one Pi to another, is the
    same drive and keeps its scans and tags."""
    with closing(connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO inv_volumes (uid, host_id, mount, label, fstype, size_bytes, used_bytes,
                                        is_system, online, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(uid) DO UPDATE SET host_id=excluded.host_id, mount=excluded.mount,
                   label=excluded.label, fstype=excluded.fstype, size_bytes=excluded.size_bytes,
                   used_bytes=excluded.used_bytes, is_system=excluded.is_system, online=1,
                   last_seen_at=excluded.last_seen_at""",
            (uid, host_id, mount, label, fstype, size_bytes, used_bytes, int(is_system), _now()))
        conn.commit()
        return conn.execute("SELECT id FROM inv_volumes WHERE uid = ?", (uid,)).fetchone()[0]


def merge_host(db_path: str, name: str, into: str) -> None:
    """A registry alias turned out to be another host's machine: move anything recorded
    under the alias to the real host and drop the alias."""
    with closing(connect(db_path)) as conn:
        dup = conn.execute("SELECT id FROM inv_hosts WHERE name = ?", (name,)).fetchone()
        real = conn.execute("SELECT id FROM inv_hosts WHERE name = ?", (into,)).fetchone()
        if dup is None or real is None or dup["id"] == real["id"]:
            return
        conn.execute("UPDATE inv_volumes SET host_id = ? WHERE host_id = ?", (real["id"], dup["id"]))
        conn.execute("DELETE FROM inv_hosts WHERE id = ?", (dup["id"],))
        conn.commit()


def mark_host_volumes_offline(db_path: str, host_id: int, seen_uids: set[str]) -> None:
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT id, uid FROM inv_volumes WHERE host_id = ?", (host_id,)).fetchall()
        for r in rows:
            if r["uid"] not in seen_uids:
                conn.execute("UPDATE inv_volumes SET online = 0 WHERE id = ?", (r["id"],))
        conn.commit()


def start_scan(db_path: str, volume_id: int) -> int:
    with closing(connect(db_path)) as conn:
        cur = conn.execute("INSERT INTO inv_scans (volume_id, started_at) VALUES (?, ?)", (volume_id, _now()))
        conn.commit()
        return cur.lastrowid


def fail_scan(db_path: str, scan_id: int, error: str) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute("DELETE FROM inv_files WHERE scan_id = ?", (scan_id,))
        conn.execute("DELETE FROM inv_dirs WHERE scan_id = ?", (scan_id,))
        conn.execute("UPDATE inv_scans SET status='failed', error=?, finished_at=? WHERE id=?",
                     (error[:2000], _now(), scan_id))
        conn.commit()


class ScanWriter:
    """Streams one volume's scan into the DB. File rows go in as they arrive; folders go
    in at finish(). The previous scan's rows are only removed once the new one is
    complete, so a scan that dies halfway leaves the old picture intact."""

    BATCH = 5000

    def __init__(self, db_path: str, volume_id: int, scan_id: int, is_system: bool = False):
        self.db_path, self.volume_id, self.scan_id = db_path, volume_id, scan_id
        self.agg = Aggregator(is_system)
        self.conn = connect(db_path)
        # Each scan owns its own id block, so hosts scanned in parallel never collide and
        # file rows can reference a folder id before the folder row itself is written.
        self._next_id = scan_id * ID_BLOCK
        self._pending = []
        self.errors = 0

    def _id_for(self, d: _Dir) -> int:
        if d.id is None:
            d.id = self._next_id
            self._next_id += 1
        return d.id

    def add(self, kind: str, size: int, mtime: float | None, relpath: str) -> None:
        row = self.agg.add(kind, size, mtime, relpath)
        if row is not None:
            d, name, ext, ftype, size, mtime = row
            self._pending.append((self.scan_id, self._id_for(d), name, ext, ftype, size, mtime))
            if len(self._pending) >= self.BATCH:
                self._flush()

    def _flush(self) -> None:
        if self._pending:
            self.conn.executemany(
                "INSERT INTO inv_files (scan_id, dir_id, name, ext, ftype, size_bytes, mtime) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", self._pending)
            self.conn.commit()
            self._pending = []

    def finish(self) -> dict:
        self._flush()
        dirs = self.agg.finish()
        classify(dirs)
        rows = []
        for d in dirs:
            top = sorted(d.exts.items(), key=lambda kv: -kv[1][1])[:6]
            rows.append((
                self._id_for(d), self.scan_id, self.volume_id,
                self._id_for(d.parent) if d.parent is not None else None,
                d.path, d.name, d.depth, d.collapsed, d.direct_files, d.direct_bytes,
                d.total_files, d.total_bytes, d.total_dirs, d.oldest, d.newest,
                json.dumps(d.type_bytes, separators=(",", ":")),
                json.dumps([[e, n, b] for e, (n, b) in top], separators=(",", ":")),
                d.camera, int(d.project_marker), d.suggested, d.reason,
            ))
        self.conn.executemany(
            """INSERT INTO inv_dirs (id, scan_id, volume_id, parent_id, path, name, depth, collapsed,
                   direct_files, direct_bytes, total_files, total_bytes, total_dirs, oldest_mtime,
                   newest_mtime, type_bytes, top_exts, camera_files, project_marker,
                   suggested, suggested_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        old = [r[0] for r in self.conn.execute(
            "SELECT id FROM inv_scans WHERE volume_id = ? AND id != ?", (self.volume_id, self.scan_id))]
        for sid in old:
            self.conn.execute("DELETE FROM inv_files WHERE scan_id = ?", (sid,))
            self.conn.execute("DELETE FROM inv_dirs WHERE scan_id = ?", (sid,))
        self.conn.execute(
            "UPDATE inv_scans SET status='done', files=?, dirs=?, bytes=?, errors=?, finished_at=? WHERE id=?",
            (self.agg.files, len(dirs), self.agg.bytes, self.errors, _now(), self.scan_id))
        self.conn.execute("UPDATE inv_volumes SET current_scan_id = ? WHERE id = ?", (self.scan_id, self.volume_id))
        self.conn.commit()
        self.conn.close()
        apply_tags(self.db_path, self.volume_id)
        return {"files": self.agg.files, "dirs": len(dirs), "bytes": self.agg.bytes}

    def abort(self, error: str) -> None:
        try:
            self.conn.close()
        finally:
            fail_scan(self.db_path, self.scan_id, error)


# ============================================================================ tags

def apply_tags(db_path: str, volume_id: int, under_path: str | None = None) -> int:
    """Resolve every folder's effective category: its own tag, else the nearest tagged
    ancestor's, else its suggestion. Recomputes one subtree when under_path is given."""
    with closing(connect(db_path)) as conn:
        vol = conn.execute("SELECT uid, current_scan_id FROM inv_volumes WHERE id = ?", (volume_id,)).fetchone()
        if vol is None or vol["current_scan_id"] is None:
            return 0
        tags = {r["path"]: r["category"] for r in conn.execute(
            "SELECT path, category FROM inv_tags WHERE volume_uid = ?", (vol["uid"],))}
        params = [vol["current_scan_id"]]
        where = "scan_id = ?"
        if under_path:
            where += " AND (path = ? OR path LIKE ? ESCAPE '\\')"
            params += [under_path, _like_prefix(under_path)]
        rows = conn.execute(f"SELECT id, parent_id, path, suggested FROM inv_dirs WHERE {where} ORDER BY depth",
                            params).fetchall()
        inherited = {}
        if under_path and rows:
            # The subtree inherits whatever tag sits nearest above it.
            parts = under_path.split("/")
            ancestors = ["/".join(parts[:i]) for i in range(len(parts) - 1, -1, -1)]
            inherited[rows[0]["parent_id"]] = next((tags[a] for a in ancestors if a in tags), None)
        updates = []
        for r in rows:
            above = inherited.get(r["parent_id"])
            if r["path"] in tags:
                cat, src, down = tags[r["path"]], "tag", tags[r["path"]]
            elif above:
                cat, src, down = above, "inherited_tag", above
            else:
                cat, src, down = r["suggested"], "suggested", None
            inherited[r["id"]] = down
            updates.append((cat, src, r["id"]))
        conn.executemany("UPDATE inv_dirs SET category = ?, category_source = ? WHERE id = ?", updates)
        conn.commit()
        return len(updates)


def _like_prefix(path: str) -> str:
    esc = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{esc}/%"


def set_tag(db_path: str, dir_id: int, category: str | None, note: str | None = None) -> dict:
    """Tag a folder (and, by inheritance, everything under it). category=None clears the
    tag and lets the suggestion show through again."""
    if category is not None and category not in CATEGORY_KEYS:
        raise ValueError(f"unknown category {category!r}")
    with closing(connect(db_path)) as conn:
        d = conn.execute(
            """SELECT d.path, d.volume_id, v.uid FROM inv_dirs d JOIN inv_volumes v ON v.id = d.volume_id
               WHERE d.id = ?""", (dir_id,)).fetchone()
        if d is None:
            raise LookupError("no such folder")
        if category is None:
            conn.execute("DELETE FROM inv_tags WHERE volume_uid = ? AND path = ?", (d["uid"], d["path"]))
        else:
            conn.execute(
                """INSERT INTO inv_tags (volume_uid, path, category, note, set_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(volume_uid, path) DO UPDATE SET category=excluded.category,
                       note=excluded.note, set_at=excluded.set_at""",
                (d["uid"], d["path"], category, note, _now()))
        conn.commit()
    changed = apply_tags(db_path, d["volume_id"], under_path=d["path"] or None)
    return {"ok": True, "changed": changed}


# ====================================================================== duplicates
#
# A duplicate can only ever be a file of exactly the same size, so only files whose size
# appears more than once are read at all -- a fraction of the data. Hashes are SHA-256 of
# the whole file: this decides what gets deleted, so no partial-hash shortcuts.

DUP_MIN_BYTES = 1 << 20   # below 1MB the space isn't worth the reads


def hash_candidates(db_path: str, volume_id: int, min_bytes: int = DUP_MIN_BYTES) -> list[dict]:
    """Files on one (non-OS) drive that share their exact size with some other file in
    the inventory and have no current hash. [{rel_path, size_bytes, mtime}]"""
    with closing(connect(db_path)) as conn:
        v = conn.execute("SELECT uid, current_scan_id FROM inv_volumes WHERE id = ?", (volume_id,)).fetchone()
        if v is None or v["current_scan_id"] is None:
            return []
        rows = conn.execute(
            """WITH shared AS (
                   SELECT f.size_bytes FROM inv_files f
                     JOIN inv_volumes vv ON vv.current_scan_id = f.scan_id AND vv.is_system = 0
                    WHERE f.size_bytes >= ? GROUP BY f.size_bytes HAVING COUNT(*) > 1)
               SELECT CASE WHEN d.path = '' THEN f.name ELSE d.path || '/' || f.name END AS rel_path,
                      f.size_bytes, f.mtime
                 FROM inv_files f JOIN inv_dirs d ON d.id = f.dir_id
                WHERE f.scan_id = ? AND f.size_bytes IN (SELECT size_bytes FROM shared)""",
            (min_bytes, v["current_scan_id"])).fetchall()
        have = {(r["rel_path"], r["size_bytes"], r["mtime"]) for r in conn.execute(
            "SELECT rel_path, size_bytes, mtime FROM inv_hashes WHERE volume_uid = ?", (v["uid"],))}
    return [dict(r) for r in rows if (r["rel_path"], r["size_bytes"], r["mtime"]) not in have]


def record_hashes(db_path: str, volume_uid: str, rows: list[tuple]) -> None:
    """rows: (rel_path, size_bytes, mtime, sha256)."""
    now = _now()
    with closing(connect(db_path)) as conn:
        conn.executemany(
            """INSERT INTO inv_hashes (volume_uid, rel_path, size_bytes, mtime, sha256, hashed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(volume_uid, rel_path) DO UPDATE SET size_bytes=excluded.size_bytes,
                   mtime=excluded.mtime, sha256=excluded.sha256, hashed_at=excluded.hashed_at""",
            [(volume_uid, p, s, m, h, now) for p, s, m, h in rows])
        conn.commit()


def duplicate_summary(db_path: str) -> dict:
    """Confirmed (same SHA-256) duplicates across every drive: how much space the extra
    copies take, and where those extra copies sit."""
    with closing(connect(db_path)) as conn:
        r = conn.execute(
            """SELECT COUNT(*) AS groups, COALESCE(SUM(n - 1), 0) AS extra_files,
                      COALESCE(SUM((n - 1) * size_bytes), 0) AS extra_bytes
                 FROM (SELECT sha256, MAX(size_bytes) AS size_bytes, COUNT(*) AS n
                         FROM inv_hashes GROUP BY sha256 HAVING COUNT(*) > 1)""").fetchone()
        per_drive = [dict(x) for x in conn.execute(
            """SELECT h.volume_uid, v.mount, v.label, ho.name AS host, COUNT(*) AS files,
                      SUM(h.size_bytes) AS bytes
                 FROM inv_hashes h JOIN inv_volumes v ON v.uid = h.volume_uid
                 JOIN inv_hosts ho ON ho.id = v.host_id
                WHERE h.sha256 IN (SELECT sha256 FROM inv_hashes GROUP BY sha256 HAVING COUNT(*) > 1)
                GROUP BY h.volume_uid ORDER BY bytes DESC""")]
        hashed = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM inv_hashes").fetchone()
    return {**dict(r), "per_drive": per_drive, "hashed_files": hashed[0], "hashed_bytes": hashed[1]}


# ========================================================================= queries

def _dir_out(r) -> dict:
    return {
        "id": r["id"], "path": r["path"], "name": r["name"] or "(drive root)", "depth": r["depth"],
        "collapsed": r["collapsed"], "direct_files": r["direct_files"], "direct_bytes": r["direct_bytes"],
        "total_files": r["total_files"], "total_bytes": r["total_bytes"], "total_dirs": r["total_dirs"],
        "oldest": r["oldest_mtime"], "newest": r["newest_mtime"],
        "type_bytes": json.loads(r["type_bytes"] or "{}"), "top_exts": json.loads(r["top_exts"] or "[]"),
        "suggested": r["suggested"], "suggested_reason": r["suggested_reason"],
        "category": r["category"] or r["suggested"], "category_source": r["category_source"] or "suggested",
    }


def overview(db_path: str) -> dict:
    with closing(connect(db_path)) as conn:
        hosts = [dict(r) for r in conn.execute("SELECT * FROM inv_hosts ORDER BY name")]
        vols = []
        for v in conn.execute(
                """SELECT v.*, h.name AS host, s.status AS scan_status, s.finished_at AS scanned_at,
                          s.files AS scan_files, s.bytes AS scan_bytes, s.error AS scan_error
                     FROM inv_volumes v JOIN inv_hosts h ON h.id = v.host_id
                     LEFT JOIN inv_scans s ON s.id = (SELECT MAX(id) FROM inv_scans WHERE volume_id = v.id)
                    ORDER BY h.name, v.mount"""):
            item = dict(v)
            item["root_id"] = None
            item["by_category"] = {}
            if v["current_scan_id"]:
                root = conn.execute("SELECT id FROM inv_dirs WHERE scan_id = ? AND parent_id IS NULL",
                                    (v["current_scan_id"],)).fetchone()
                item["root_id"] = root["id"] if root else None
                item["by_category"] = {r["category"] or "unsorted": r["b"] for r in conn.execute(
                    "SELECT category, SUM(direct_bytes) AS b FROM inv_dirs WHERE scan_id = ? "
                    "GROUP BY category HAVING SUM(direct_bytes) > 0", (v["current_scan_id"],))}
            vols.append(item)
        tagged = conn.execute("SELECT COUNT(*) FROM inv_tags").fetchone()[0]
    totals = {}
    for v in vols:
        for c, b in v["by_category"].items():
            totals[c] = totals.get(c, 0) + (b or 0)
    return {"hosts": hosts, "volumes": vols, "category_totals": totals, "tags": tagged,
            "categories": [{"key": k, "label": lbl} for k, lbl in CATEGORIES], "types": TYPE_ORDER}


def folder(db_path: str, dir_id: int, file_limit: int = 200) -> dict:
    with closing(connect(db_path)) as conn:
        r = conn.execute("SELECT * FROM inv_dirs WHERE id = ?", (dir_id,)).fetchone()
        if r is None:
            raise LookupError("no such folder")
        crumbs = []
        p = r
        while p is not None:
            crumbs.append({"id": p["id"], "name": p["name"] or "(drive root)"})
            p = conn.execute("SELECT id, name, parent_id FROM inv_dirs WHERE id = ?",
                             (p["parent_id"],)).fetchone() if p["parent_id"] else None
        children = [_dir_out(c) for c in conn.execute(
            "SELECT * FROM inv_dirs WHERE parent_id = ? ORDER BY total_bytes DESC", (dir_id,))]
        files = [dict(f) for f in conn.execute(
            "SELECT name, ext, ftype, size_bytes, mtime FROM inv_files WHERE dir_id = ? "
            "ORDER BY size_bytes DESC LIMIT ?", (dir_id, file_limit))]
        tag = conn.execute(
            """SELECT t.category, t.note FROM inv_tags t JOIN inv_volumes v ON v.uid = t.volume_uid
               WHERE v.id = ? AND t.path = ?""", (r["volume_id"], r["path"])).fetchone()
        vol = conn.execute(
            "SELECT v.id, v.mount, v.label, h.name AS host FROM inv_volumes v JOIN inv_hosts h ON h.id = v.host_id "
            "WHERE v.id = ?", (r["volume_id"],)).fetchone()
    out = _dir_out(r)
    out.update({"breadcrumb": list(reversed(crumbs)), "children": children, "files": files,
                "files_shown": len(files), "own_tag": dict(tag) if tag else None, "volume": dict(vol)})
    return out


def search(db_path: str, q: str, limit: int = 100) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT d.*, v.label AS vlabel, v.mount AS vmount, h.name AS host
                 FROM inv_dirs d JOIN inv_volumes v ON v.id = d.volume_id AND v.current_scan_id = d.scan_id
                 JOIN inv_hosts h ON h.id = v.host_id
                WHERE d.name LIKE ? ORDER BY d.total_bytes DESC LIMIT ?""", (f"%{q}%", limit)).fetchall()
    out = []
    for r in rows:
        item = _dir_out(r)
        item.update({"host": r["host"], "volume_label": r["vlabel"], "volume_mount": r["vmount"]})
        out.append(item)
    return out
