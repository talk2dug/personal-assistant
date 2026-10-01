"""The consolidation plan: where every folder that matters should end up, what is already
there, and what can go -- computed from inventory.db, approved by Jack, executed later.

Jack's decisions (2026-09-27/28) this encodes:
  * Side hustle lives on jarvisbox D:, merged BY TYPE under D:\\Side Hustle\\ (Design
    Bundles, SVG & Cut Files, Vector Art, Layered Art, Images, 3D Models, Fonts,
    Documents, Video, Other) -- not filed by which old drive it came from.
  * Personal media on E:\\Personal\\<Photos|Home Video|Music|Documents>, projects on
    E:\\Projects\\.
  * Nothing is deleted outright: a removed copy is quarantined for 30 days.
  * H:\\APP Date and H:\\virtualMachines are held until he decides.

How "merge by type" is read, because the literal version would wreck things:
  * The unit is a FOLDER, not a file. A folder of SVGs goes to SVG & Cut Files under its
    own name; a generically named folder ("SVG", "PNG", "Commercial Use") takes its
    parent's name too ("Halloween Bundle - SVG") so a bundle stays recognisable.
  * Projects and personal media move as whole trees. Splitting a code project, or a
    GoPro card dump, by file type would break it.
  * Two folders that map to the same destination merge; a file whose exact content is
    already kept elsewhere is not copied again.

Dedupe order matters: units already on the destination drive are planned first, so the
copy that is already on D: is the one kept and nothing is copied just to replace it.

This module only reads the inventory and writes proposals. It never touches a drive.
"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from . import drive_inventory as inv

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('files', 'tree', 'junk')),
    section TEXT NOT NULL,
    bucket TEXT NOT NULL,
    source_volume_id INTEGER NOT NULL,
    source_uid TEXT NOT NULL,
    source_path TEXT NOT NULL,
    dest_uid TEXT,
    dest_path TEXT,
    files INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    new_files INTEGER NOT NULL DEFAULT 0,
    new_bytes INTEGER NOT NULL DEFAULT 0,
    dup_files INTEGER NOT NULL DEFAULT 0,
    dup_bytes INTEGER NOT NULL DEFAULT 0,
    same_drive INTEGER NOT NULL DEFAULT 0,
    excludes TEXT,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'approved', 'excluded', 'running', 'done', 'failed')),
    note TEXT,
    planned_at TEXT NOT NULL,
    UNIQUE (source_uid, source_path, kind)
);
CREATE INDEX IF NOT EXISTS idx_plan_units_group ON plan_units(section, bucket, source_volume_id);

CREATE TABLE IF NOT EXISTS plan_decisions (
    source_uid TEXT NOT NULL,
    source_path TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (source_uid, source_path)
);
"""

SIDE_HUSTLE = "Side Hustle"
PERSONAL = "Personal"
PROJECTS = "Projects"
JUNK = "Junk"
SIDE = "side_hustle"   # the inventory category key

# (host, mount) of each destination. Resolved to a volume uid at plan time, so a drive
# letter change is caught rather than silently planning into the wrong disk.
DESTINATIONS = {SIDE_HUSTLE: ("jarvisbox", "D:\\"), PERSONAL: ("jarvisbox", "E:\\"),
                PROJECTS: ("jarvisbox", "E:\\")}
DEST_ROOT = {SIDE_HUSTLE: "Side Hustle", PERSONAL: "Personal", PROJECTS: "Projects"}

# Folders Jack is still deciding about: never planned, listed under "needs a decision".
# APP Date was released 2026-09-29: Jack chose to rescue it to E:\Personal\Home Video.
HOLDS = {("jarvisbox", "H:\\", "virtualMachines")}
CLOUD_LABELS = {"google drive", "onedrive", "dropbox", "icloud drive"}

SIDE_BUCKETS = [
    ("Design Bundles", "zip rar 7z"),
    ("SVG & Cut Files", "svg dxf studio3 studio gsp scut5 scut4 fcm sbp pes dst jef"),
    ("Vector Art", "ai eps cdr"),
    ("Layered Art", "psd psb afdesign afphoto procreate xcf kra clip indd"),
    ("Images", "png jpg jpeg webp gif tif tiff bmp heic avif"),
    ("3D Models", "stl 3mf obj step stp f3d f3z blend gcode ply fbx skp scad glb max"),
    ("Fonts", "ttf otf woff woff2"),
    ("Documents", "pdf doc docx xls xlsx ppt pptx txt rtf csv odt"),
    ("Video", "mp4 mov avi mkv webm m4v wmv"),
]
EXT_BUCKET = {e: b for b, exts in SIDE_BUCKETS for e in exts.split()}
OTHER = "Other"
PERSONAL_BUCKET = {"photos": "Photos", "home_video": "Home Video", "music": "Music", "documents": "Documents"}

# Folder names that say nothing on their own once lifted out of their bundle.
GENERIC = re.compile(
    r"^(svg|svgs|png|pngs|jpg|jpeg|pdf|eps|ai|dxf|psd|cdr|studio3?|cut ?files?|files?|images?|"
    r"pictures?|vectors?|layered|print|printables?|sublimation|commercial( use)?|personal( use)?|"
    r"license|licen[cs]e|extras?|bonus|preview|previews|mockups?|misc|new folder( \(\d+\))?|"
    r"\d+|[a-z]|_+|originals?|final|finals|export|exports|source|sources|web|hi-?res|low-?res)$", re.I)
JUNK_NAMES = {"$recycle.bin", ".spotlight-v100", ".fseventsd", ".trashes", ".trash-1000"}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: str) -> None:
    inv.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        if "excludes" not in {r[1] for r in conn.execute("PRAGMA table_info(plan_units)")}:
            conn.execute("ALTER TABLE plan_units ADD COLUMN excludes TEXT")
        conn.commit()


def resolve_nesting(units: list[dict]) -> list[dict]:
    """No two units may claim the same file. A tree unit (a project, a personal folder)
    moves everything beneath it, so anything else planned inside one is resolved here:

      * inside a Projects tree -> dropped. A project moves intact; carving its folders
        out by type or category would break it (found in the first dry run: H:\arduino
        held 9 home-video and 39 nested project units).
      * inside a Personal tree -> kept, and the tree skips it (`excludes`): a side-hustle
        folder found inside an old laptop's Documents still goes to D: by type.
    """
    by_vol: dict[str, list] = {}
    for u in units:
        by_vol.setdefault(u["source_uid"], []).append(u)
    keep = []
    for vol_units in by_vol.values():
        vol_units.sort(key=lambda u: (u["source_path"].count("/"), u["source_path"]))
        project_roots = []
        survivors = []
        for u in vol_units:
            if any(u["source_path"].startswith(r + "/") for r in project_roots):
                continue
            if u["kind"] == "tree" and (u["section"] == PROJECTS or u.get("bucket") == CODE_BUCKET):
                project_roots.append(u["source_path"])
            survivors.append(u)
        for u in survivors:
            if u["kind"] != "tree" or u["section"] == PROJECTS or u.get("bucket") == CODE_BUCKET:
                continue
            inner = [x for x in survivors if x is not u and x["source_path"].startswith(u["source_path"] + "/")]
            if inner:
                u["excludes"] = json.dumps([{"path": x["source_path"], "kind": x["kind"]} for x in inner])
                # Only the outermost carve-outs: one nested inside another is already counted.
                outer = [x for x in inner if not any(
                    y is not x and y["kind"] == "tree" and x["source_path"].startswith(y["source_path"] + "/")
                    for y in inner)]
                carved = sum(x["bytes"] for x in outer)
                u["bytes"] = max(0, u["bytes"] - carved)
                u["new_bytes"] = max(0, u["new_bytes"] - carved)
        keep.extend(survivors)
    return keep


def safe_name(name: str) -> str:
    """A folder name that is legal on Windows (D: and E: are NTFS)."""
    s = _ILLEGAL.sub("_", name).strip().rstrip(".")
    return s[:120] or "_"


def display_name(names: list[str]) -> str:
    """names: a folder's path components, outermost first. Lift generic names by
    prefixing up to two meaningful ancestors: [..., 'Halloween Bundle', 'SVG'] ->
    'Halloween Bundle - SVG'."""
    parts = [names[-1]] if names else ["(drive root)"]
    i = len(names) - 2
    while GENERIC.match(parts[0].split(" - ")[0]) and i >= 0 and len(parts) < 3:
        parts.insert(0, names[i])
        i -= 1
    return safe_name(" - ".join(parts))


def _bucket_for(files: list[tuple]) -> str:
    """Side-hustle bucket by bytes over the folder's own files."""
    by = {}
    for _name, ext, size, _sha in files:
        b = EXT_BUCKET.get(ext, OTHER)
        by[b] = by.get(b, 0) + size
    return max(by, key=by.get) if by else OTHER


# A folder that directly holds one of these is the root of a code project and moves whole.
PROJECT_CHILD_DIRS = {".git", ".svn", "node_modules", ".venv", "venv", ".vscode", ".idea", ".pio"}
CODE_BUCKET = "Software & Code"


def live_paths(jarvis_db: str | None, obsidian_config: str | None = None) -> set[tuple[str, str]]:
    """(drive, top-level folder) pairs something RUNNING reads from: any absolute path in
    Jarvis's own database, and Obsidian's vaults. Never planned.

    Found the hard way (2026-09-28): the first real run moved a note out of the live
    Obsidian vault (D:/Obsidian/Jarvis) and would next have scattered D:/Jarvis Generated
    (512 design_assets rows, mail photos, review images) and D:/AI Models (338
    model_images rows) by file type. The old import queue's own log tables
    (media_copies, scan_volumes) are history, not live use, and are ignored.
    """
    out = set()

    def add(p: str):
        m = re.match(r"^([A-Za-z]:)[\\/]+([^\\/]+)", p or "")
        if m:
            out.add((m.group(1).upper() + "\\", m.group(2)))
        m = re.match(r"^(/mnt/[^/]+|/media/[^/]+/[^/]+)/([^/]+)", p or "")
        if m:
            out.add((m.group(1), m.group(2)))

    if jarvis_db:
        with closing(sqlite3.connect(jarvis_db)) as c:
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                if t in ("media_copies", "scan_volumes", "scan_hosts", "media_files", "media_folders",
                         "media_archives", "scan_runs"):
                    continue
                for col in [r[1] for r in c.execute(f"PRAGMA table_info('{t}')") if (r[2] or "").upper() in ("TEXT", "")]:
                    try:
                        for (val,) in c.execute(
                                f"""SELECT DISTINCT "{col}" FROM '{t}' WHERE "{col}" GLOB '[A-Za-z]:*'
                                    OR "{col}" LIKE '/mnt/%' OR "{col}" LIKE '/media/%' LIMIT 5000"""):
                            add(val)
                    except sqlite3.Error:
                        continue
    if obsidian_config:
        try:
            import json as _json
            with open(obsidian_config, encoding="utf-8") as f:
                for vault in _json.load(f).get("vaults", {}).values():
                    add(vault.get("path", ""))
        except (OSError, ValueError):
            pass
    return out


def _code_roots(dirs: dict) -> list[str]:
    """Outermost folders that are code projects by content: they hold project files or a
    .git/node_modules/.vscode-style folder."""
    kids = {}
    for x in dirs.values():
        kids.setdefault(x["parent_id"], []).append(x)
    marked = sorted({x["path"] for x in dirs.values()
                     if x["parent_id"] is not None and (x["project_marker"] or any(
                         k["name"].lower() in PROJECT_CHILD_DIRS for k in kids.get(x["id"], [])))}, key=len)
    outer = []
    for p in marked:
        if not any(p.startswith(o + "/") for o in outer):
            outer.append(p)
    return outer


_COPY_SUFFIX = re.compile(r"(\s*\(\d+\)|-main|-master)+$", re.I)


def _project_key(name: str) -> str:
    """'CustomVinylRetail-main (1)' and 'CustomVinylRetail' are the same project."""
    return _COPY_SUFFIX.sub("", name.strip()).lower()


def default_protected(jarvis_db: str) -> set[tuple[str, str]]:
    """live_paths() for this machine: Jarvis's database plus the local Obsidian config."""
    import os
    # Jack's own profile explicitly as well: run as SYSTEM (the H: rescue task), %APPDATA%
    # is the system profile and the live vault silently dropped off the protected list.
    out = set()
    for cfg_path in (os.path.expandvars(r"%APPDATA%\obsidian\obsidian.json"),
                     r"C:\Users\swayze\AppData\Roaming\obsidian\obsidian.json"):
        out |= live_paths(jarvis_db, cfg_path)
    return out


def build(db_path: str, protected: set[tuple[str, str]] | None = None) -> dict:
    """Recompute the proposed plan. Approvals and exclusions already given are kept for
    any unit that still exists (matched on source drive + path). `protected` is
    live_paths(): (drive, top-level folder) pairs that are never planned."""
    protected = protected or set()
    init_db(db_path)
    with closing(inv.connect(db_path)) as conn:
        vols = {r["id"]: dict(r) for r in conn.execute(
            """SELECT v.id, v.uid, v.mount, v.label, v.is_system, v.online, v.current_scan_id, h.name AS host
                 FROM inv_volumes v JOIN inv_hosts h ON h.id = v.host_id WHERE v.current_scan_id IS NOT NULL""")}
        dest_uid = {}
        for section, (host, mount) in DESTINATIONS.items():
            match = [v for v in vols.values() if v["host"] == host and v["mount"] == mount]
            if not match:
                raise LookupError(f"destination {host} {mount} for {section} isn't in the inventory")
            dest_uid[section] = match[0]["uid"]
        eligible = {vid: v for vid, v in vols.items()
                    if not v["is_system"] and v["online"] and (v["label"] or "").lower() not in CLOUD_LABELS}

        # Interrupted ('running') and 'failed' units were approved: a rebuild must keep them
        # approved so the mover retries them, not drop them back to "proposed" (it did, on
        # 2026-09-29, un-approving the ~28 folders left from the interrupted run).
        prior = {(r["source_uid"], r["source_path"], r["kind"]):
                 ("approved" if r["status"] in ("running", "failed") else r["status"]) for r in conn.execute(
            "SELECT source_uid, source_path, kind, status FROM plan_units "
            "WHERE status IN ('approved','excluded','done','running','failed')")}

        # Pass 1: code project names anywhere on the network. The old collector copied
        # only the media out of projects into D:\Collected Art, so a copy there has no
        # package.json to recognise it by; knowing the name from the full copy elsewhere
        # stops its icons being filed under Images (found on the first real run).
        code_names = set()
        for vid in eligible:
            vdirs = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, parent_id, path, name, project_marker FROM inv_dirs WHERE scan_id = ?",
                (vols[vid]["current_scan_id"],))}
            for root in _code_roots(vdirs):
                key = _project_key(root.rsplit("/", 1)[-1])
                if len(key) >= 6 and not GENERIC.match(key) and key not in ("projects", "arduino"):
                    code_names.add(key)

        units, needs = [], []
        flagged_copies = set()
        side_seen: set[str] = set()     # content already kept on D: (or planned to be)
        # Destination drive first: its copies are the ones kept.
        order = sorted(eligible, key=lambda vid: (vols[vid]["uid"] != dest_uid[SIDE_HUSTLE], vid))
        for vid in order:
            v = vols[vid]
            dirs = {r["id"]: dict(r) for r in conn.execute(
                """SELECT id, parent_id, path, name, collapsed, category, total_files, total_bytes, direct_files,
                          project_marker
                     FROM inv_dirs WHERE scan_id = ?""", (v["current_scan_id"],))}
            hashes = {r["rel_path"]: r["sha256"] for r in conn.execute(
                "SELECT rel_path, sha256 FROM inv_hashes WHERE volume_uid = ?", (v["uid"],))}
            files_by_dir: dict[int, list] = {}
            for f in conn.execute(
                    """SELECT f.dir_id, f.name, f.ext, f.size_bytes FROM inv_files f WHERE f.scan_id = ?""",
                    (v["current_scan_id"],)):
                d = dirs.get(f["dir_id"])
                rel = f["name"] if not d or not d["path"] else f"{d['path']}/{f['name']}"
                files_by_dir.setdefault(f["dir_id"], []).append((f["name"], f["ext"], f["size_bytes"], hashes.get(rel)))

            def held(path):
                return any(v["host"] == h and v["mount"] == m and (path == p or path.startswith(p + "/"))
                           for h, m, p in HOLDS)

            def parent_cat(d):
                p = dirs.get(d["parent_id"])
                return p["category"] if p and p["parent_id"] is not None else None

            # Code project roots, found by content rather than by category: a folder that
            # holds project files or a .git/node_modules/.vscode. The outermost one wins.
            outer_roots = _code_roots(dirs)
            protected_here = {folder for drive, folder in protected if drive == v["mount"]}

            for d in sorted(dirs.values(), key=lambda x: x["path"]):
                path, cat = d["path"], d["category"]
                top = path.split("/", 1)[0]
                if path and top in protected_here:
                    if path == top:
                        needs.append((v, path, d["total_bytes"], "in use by Jarvis or Obsidian: never moved"))
                    continue
                if path in outer_roots and cat in (SIDE, "projects") + tuple(PERSONAL_BUCKET):
                    section = SIDE_HUSTLE if cat == SIDE else PROJECTS
                    name = safe_name(path.split("/")[-1])
                    dest = (f"{DEST_ROOT[SIDE_HUSTLE]}/{CODE_BUCKET}/{name}" if section == SIDE_HUSTLE
                            else f"{DEST_ROOT[PROJECTS]}/{name}")
                    units.append(_unit("tree", section, CODE_BUCKET if section == SIDE_HUSTLE else "Projects",
                                       v, path, dest_uid[section], dest, d["total_files"], d["total_bytes"],
                                       d["total_files"], d["total_bytes"], 0, 0))
                    continue
                if any(path.startswith(r + "/") for r in outer_roots):
                    continue            # inside a code project: it moves with the project
                if d["collapsed"]:
                    if d["name"].lower() in JUNK_NAMES and d["total_bytes"] > 0:
                        units.append(_unit("junk", JUNK, "Junk", v, path, None, None,
                                           d["total_files"], d["total_bytes"], 0, 0, d["total_files"], d["total_bytes"]))
                    continue
                if held(path):
                    if any(path == p for _, _, p in HOLDS):
                        needs.append((v, path, d["total_bytes"], "held: you're deciding"))
                    continue
                if cat == SIDE and d["direct_files"]:
                    segs = path.split("/")
                    hit = next((i for i, seg in enumerate(segs) if _project_key(seg) in code_names), None)
                    if hit is not None:
                        copy_root = "/".join(segs[:hit + 1])
                        if copy_root not in flagged_copies:
                            flagged_copies.add(copy_root)
                            copy_dir = next((x for x in dirs.values() if x["path"] == copy_root), None)
                            needs.append((v, copy_root, copy_dir["total_bytes"] if copy_dir else 0,
                                          "part of a code project kept whole elsewhere: not split by type"))
                        continue
                    files = files_by_dir.get(d["id"], [])
                    bucket = _bucket_for(files)
                    names = path.split("/") if path else [v["label"] or v["mount"]]
                    dest = f"{DEST_ROOT[SIDE_HUSTLE]}/{bucket}/{display_name(names)}"
                    new_f = new_b = dup_f = dup_b = 0
                    for _n, _e, size, sha in files:
                        if sha and sha in side_seen:
                            dup_f += 1
                            dup_b += size
                        else:
                            new_f += 1
                            new_b += size
                            if sha:
                                side_seen.add(sha)
                    units.append(_unit("files", SIDE_HUSTLE, bucket, v, path, dest_uid[SIDE_HUSTLE], dest,
                                       len(files), sum(f[2] for f in files), new_f, new_b, dup_f, dup_b))
                elif cat in PERSONAL_BUCKET or cat == "projects":
                    if parent_cat(d) == cat:
                        continue                      # the whole tree moves with its top folder
                    section = PROJECTS if cat == "projects" else PERSONAL
                    bucket = "Projects" if cat == "projects" else PERSONAL_BUCKET[cat]
                    name = safe_name(path.split("/")[-1] if path else (v["label"] or "drive"))
                    dest = (f"{DEST_ROOT[PROJECTS]}/{name}" if cat == "projects"
                            else f"{DEST_ROOT[PERSONAL]}/{bucket}/{name}")
                    units.append(_unit("tree", section, bucket, v, path, dest_uid[section], dest,
                                       d["total_files"], d["total_bytes"], d["total_files"], d["total_bytes"], 0, 0))
                elif cat in ("unsorted", "software") and parent_cat(d) != cat and path and d["total_bytes"] >= 100 << 20:
                    needs.append((v, path, d["total_bytes"], f"{cat.replace('_', ' ')}: not moved until you tag it"))

        units = resolve_nesting(units)
        # Whole-folder moves never share a destination: merging two versions of the same
        # project (three CrimeCameraClients, two rallyComputers, two Vinyl Stuffs on the
        # first real plan) would interleave their files. Design folders DO merge -- that
        # is the point of merging by type -- so this is trees only.
        taken = set()
        for u in sorted(units, key=lambda u: -u["bytes"]):   # the biggest keeps the plain name
            if u["kind"] != "tree":
                continue
            base, n = u["dest_path"], 2
            while (u["dest_uid"], u["dest_path"].lower()) in taken:
                u["dest_path"] = f"{base} ({n})"
                n += 1
            taken.add((u["dest_uid"], u["dest_path"].lower()))
        for u in units:
            u.setdefault("excludes", None)
            u["same_drive"] = int(u["dest_uid"] == u["source_uid"])
            u["status"] = prior.get((u["source_uid"], u["source_path"], u["kind"]), "proposed")
        now = _now()
        conn.execute("DELETE FROM plan_units WHERE status NOT IN ('done', 'running')")
        conn.executemany(
            """INSERT OR REPLACE INTO plan_units (kind, section, bucket, source_volume_id, source_uid, source_path,
                   dest_uid, dest_path, files, bytes, new_files, new_bytes, dup_files, dup_bytes, same_drive,
                   excludes, status, planned_at)
               VALUES (:kind, :section, :bucket, :source_volume_id, :source_uid, :source_path, :dest_uid,
                   :dest_path, :files, :bytes, :new_files, :new_bytes, :dup_files, :dup_bytes, :same_drive,
                   :excludes, :status, :planned_at)""",
            [{**u, "planned_at": now} for u in units])
        conn.execute("DELETE FROM plan_decisions")
        conn.executemany("INSERT OR REPLACE INTO plan_decisions (source_uid, source_path, reason) VALUES (?, ?, ?)",
                         [(v["uid"], p, reason) for v, p, _b, reason in needs])
        conn.commit()
    return summary(db_path)



def _unit(kind, section, bucket, v, path, dest_uid, dest_path, files, bytes_, new_f, new_b, dup_f, dup_b) -> dict:
    return {"kind": kind, "section": section, "bucket": bucket, "source_volume_id": v["id"],
            "source_uid": v["uid"], "source_path": path, "dest_uid": dest_uid, "dest_path": dest_path,
            "files": files, "bytes": bytes_, "new_files": new_f, "new_bytes": new_b,
            "dup_files": dup_f, "dup_bytes": dup_b}


def summary(db_path: str) -> dict:
    init_db(db_path)
    with closing(inv.connect(db_path)) as conn:
        groups = [dict(r) for r in conn.execute(
            """SELECT u.section, u.bucket, u.source_volume_id, h.name AS host, v.mount, v.label,
                      COUNT(*) AS units, SUM(u.files) AS files, SUM(u.bytes) AS bytes,
                      SUM(u.new_bytes) AS new_bytes, SUM(u.dup_bytes) AS dup_bytes, MAX(u.same_drive) AS same_drive,
                      SUM(u.status = 'approved') AS approved, SUM(u.status = 'excluded') AS excluded,
                      SUM(u.status = 'done') AS done, SUM(u.status = 'failed') AS failed
                 FROM plan_units u JOIN inv_volumes v ON v.id = u.source_volume_id JOIN inv_hosts h ON h.id = v.host_id
                GROUP BY u.section, u.bucket, u.source_volume_id
                ORDER BY CASE u.section WHEN 'Side Hustle' THEN 0 WHEN 'Personal' THEN 1 WHEN 'Projects' THEN 2 ELSE 3 END,
                         u.bucket, bytes DESC""")]
        needs = [dict(r) for r in conn.execute(
            """SELECT p.source_path, p.reason, v.mount, v.label, h.name AS host,
                      (SELECT d.total_bytes FROM inv_dirs d WHERE d.scan_id = v.current_scan_id AND d.path = p.source_path) AS bytes
                 FROM plan_decisions p JOIN inv_volumes v ON v.uid = p.source_uid JOIN inv_hosts h ON h.id = v.host_id
                ORDER BY bytes DESC""")]
        freed = [dict(r) for r in conn.execute(
            """SELECT h.name AS host, v.mount, v.label, v.size_bytes, v.used_bytes,
                      SUM(CASE WHEN u.same_drive = 0 THEN u.bytes ELSE u.dup_bytes END) AS freed
                 FROM plan_units u JOIN inv_volumes v ON v.id = u.source_volume_id JOIN inv_hosts h ON h.id = v.host_id
                WHERE u.status != 'excluded' GROUP BY u.source_volume_id ORDER BY freed DESC""")]
        planned = conn.execute("SELECT MAX(planned_at) FROM plan_units").fetchone()[0]
    tot = lambda k: sum(g[k] or 0 for g in groups if g["section"] != JUNK)  # noqa: E731
    return {"planned_at": planned, "groups": groups, "needs_decision": needs, "freed_by_drive": freed,
            "totals": {"units": sum(g["units"] for g in groups), "copy_bytes": sum((g["new_bytes"] or 0)
                       for g in groups if not g["same_drive"] and g["section"] != JUNK),
                       "dup_bytes": tot("dup_bytes"), "bytes": tot("bytes"),
                       "junk_bytes": sum(g["bytes"] or 0 for g in groups if g["section"] == JUNK)}}


def units(db_path: str, section: str, bucket: str, source_volume_id: int, limit: int = 500) -> list[dict]:
    with closing(inv.connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, kind, source_path, dest_path, files, bytes, new_bytes, dup_bytes, status
                 FROM plan_units WHERE section = ? AND bucket = ? AND source_volume_id = ?
                ORDER BY bytes DESC LIMIT ?""", (section, bucket, source_volume_id, limit))]


def set_status(db_path: str, status: str, ids: list[int] | None = None,
               group: dict | None = None) -> int:
    """Approve / exclude / reset units, by id or a whole (section, bucket, drive) group.
    Units already done or running are never touched."""
    if status not in ("approved", "excluded", "proposed"):
        raise ValueError("status must be approved, excluded or proposed")
    with closing(inv.connect(db_path)) as conn:
        if ids:
            q = f"UPDATE plan_units SET status = ? WHERE id IN ({','.join('?' * len(ids))}) AND status NOT IN ('done','running')"
            n = conn.execute(q, [status, *ids]).rowcount
        elif group:
            n = conn.execute(
                """UPDATE plan_units SET status = ? WHERE section = ? AND bucket = ? AND source_volume_id = ?
                     AND status NOT IN ('done','running')""",
                (status, group["section"], group["bucket"], int(group["source_volume_id"]))).rowcount
        else:
            raise ValueError("give ids or a group")
        conn.commit()
    return n
