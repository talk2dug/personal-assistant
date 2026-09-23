"""The rest of the print-business asset shelf -- everything the Design Library browses
that isn't a Design (design_assets.py) or a raw drive scan (media_scan.py).

Eight categories share one table rather than eight, because they share the same shape:
something with a title, a file on disk, a status (available/used/retired), and a handful
of category-specific fields that don't want a column each. `metadata` carries those --
duration/resolution for footage, triangle_count for an STL, room_type for a room, and so
on -- as a JSON blob, the same way KindBar.counts already stores a JSON blob in
media_folders. A category with genuinely universal columns (real width/height/bytes) gets
real columns; everything narrower goes in metadata rather than adding six mostly-NULL
columns to the shared table.

WHAT THIS DELIBERATELY DOES NOT DO. It does not scan drives (that's media_scan.py) and it
does not generate anything (mockup generation stays on the existing generate_media/
gpu_bridge path -- see library.py's /mockups/generate route). This is the catalogue layer
only: a row here means "Jarvis knows this asset exists," nothing about how it got made.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

CATEGORIES = (
    "decal_icon", "cut_file", "footage", "mockup", "background", "human_model",
    "room", "stl_model",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    title TEXT,
    path TEXT NOT NULL,
    -- Where it came from: uploaded | generated | imported.
    source TEXT NOT NULL DEFAULT 'uploaded',
    note TEXT,
    width INTEGER,
    height INTEGER,
    bytes INTEGER,
    -- Category-specific fields that don't earn a column of their own -- duration/
    -- resolution for footage, triangle_count for an stl_model, room_type for a room,
    -- style/gender/pose for a human_model, the source design/background/model ids for a
    -- mockup. A JSON object, never a JSON array -- always keyed fields, read by name.
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'available',
    times_used INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_library_assets_category
    ON library_assets(owner_user_id, category, status, id DESC);
"""


def init_library_assets(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except (TypeError, ValueError):
        d["metadata"] = {}
    return d


def add(db_path: str, owner_user_id: int, category: str, path: str,
        title: str | None = None, source: str = "uploaded", note: str | None = None,
        width: int | None = None, height: int | None = None, bytes_: int | None = None,
        metadata: dict | None = None) -> int:
    """Catalogue one asset. Returns its id."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    init_library_assets(db_path)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO library_assets
                   (owner_user_id, category, title, path, source, note, width, height,
                    bytes, metadata, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (owner_user_id, category, (title or "").strip() or None, path, source, note,
             width, height, bytes_, json.dumps(metadata or {}), _now()))
        conn.commit()
        return int(cur.lastrowid)


def catalogue(db_path: str, owner_user_id: int, category: str | None = None,
              status: str | None = None, search: str | None = None,
              limit: int = 200, offset: int = 0) -> list[dict]:
    """The shelf, or one category of it. Newest first, same reasoning as
    design_assets.catalogue: what he added most recently is what he's most likely
    reaching for."""
    init_library_assets(db_path)
    sql = "SELECT * FROM library_assets WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if category:
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        sql += " AND category = ?"
        params.append(category)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if search:
        sql += " AND title LIKE ?"
        params.append(f"%{search}%")
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with closing(_connect(db_path)) as conn:
        return [_row(r) for r in conn.execute(sql, params)]


def category_counts(db_path: str, owner_user_id: int) -> dict:
    """How many assets sit in each category, for the rail. Every known category is
    present even at zero -- an empty shelf is still a real shelf, and a category
    missing from the rail entirely reads as "doesn't exist" rather than "empty"."""
    init_library_assets(db_path)
    counts = {c: 0 for c in CATEGORIES}
    with closing(_connect(db_path)) as conn:
        for row in conn.execute(
                "SELECT category, COUNT(*) AS n FROM library_assets"
                " WHERE owner_user_id = ? AND status != 'retired' GROUP BY category",
                (owner_user_id,)):
            counts[row["category"]] = row["n"]
    return counts


def get(db_path: str, owner_user_id: int, asset_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM library_assets WHERE id = ? AND owner_user_id = ?",
            (asset_id, owner_user_id)).fetchone()
    return _row(row) if row else None


def mark_used(db_path: str, owner_user_id: int, asset_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE library_assets
                  SET times_used = times_used + 1, status = 'used'
                WHERE id = ? AND owner_user_id = ?""", (asset_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


def set_status(db_path: str, owner_user_id: int, asset_id: int, status: str) -> bool:
    if status not in ("available", "used", "retired"):
        raise ValueError("status must be available, used or retired")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE library_assets SET status = ? WHERE id = ? AND owner_user_id = ?",
            (status, asset_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


def set_status_bulk(db_path: str, owner_user_id: int, asset_ids: list[int], status: str) -> int:
    if status not in ("available", "used", "retired"):
        raise ValueError("status must be available, used or retired")
    changed = 0
    with closing(_connect(db_path)) as conn:
        for asset_id in asset_ids:
            cur = conn.execute(
                "UPDATE library_assets SET status = ? WHERE id = ? AND owner_user_id = ?",
                (status, asset_id, owner_user_id))
            changed += cur.rowcount
        conn.commit()
    return changed
