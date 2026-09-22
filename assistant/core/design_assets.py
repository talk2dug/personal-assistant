"""Artwork Jack owns, catalogued so he can make something from it later.

His own description of what this is for: *"These mostly will be for me to make things
from later. Like my pipeline in print-station. I would select something from the catalog
and make something from it then have the system create the marketing pipeline and sell
it for me."*

So this is not an archive and it is not the drive catalogue. `media_files` holds 251,976
rows built by walking his disks, and it answers "what do I have and where are the
duplicates" -- a different question, for a different job. This table holds the far
smaller set of designs he has deliberately handed to Jarvis as raw material, and its
whole reason to exist is that something downstream will pick one and build a product
from it.

WHY A ROW AND NOT JUST A FILE. Before this, artwork he emailed was written to a folder
and that was the end of it -- no record, nothing to list, nothing to pick from. A folder
is not a catalogue: you cannot ask it what has already been used, what came in last week,
or what he called something. The file is still the artefact; this is what makes it
findable.

WHAT IT DELIBERATELY DOES NOT DO. It does not create a product, a listing or a concept.
Picking an asset and turning it into something to sell is his decision and a separate
step -- see product_concepts.source, which today only ever says 'trend'.
"""
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS design_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    -- What he called it, taken from the subject line he sent it with. His words are
    -- better than anything derived from the file, which is usually IMG_1018.JPG.
    title TEXT,
    path TEXT NOT NULL,
    -- Where it came from: emailed | uploaded | imported.
    source TEXT NOT NULL DEFAULT 'emailed',
    note TEXT,
    width INTEGER,
    height INTEGER,
    bytes INTEGER,
    -- available | used | retired. 'used' is not deleted: a design he has already sold
    -- something from is exactly the one he may want again.
    status TEXT NOT NULL DEFAULT 'available',
    times_used INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_design_assets_status
    ON design_assets(status, id DESC);
"""


def init_design_assets(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _measure(path: str) -> tuple:
    """Dimensions and size, or (None, None, size) if it cannot be opened. Never raises:
    an unmeasurable image is still an asset, and refusing to catalogue it would lose the
    thing this exists to keep."""
    size = None
    try:
        size = os.path.getsize(path)
    except OSError:
        pass
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.width, img.height, size
    except Exception:
        return None, None, size


def add(db_path: str, owner_user_id: int, path: str, title: str | None = None,
        source: str = "emailed", note: str | None = None) -> int:
    """Catalogue one design. Returns its id."""
    init_design_assets(db_path)
    width, height, size = _measure(path)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO design_assets
                   (owner_user_id, title, path, source, note, width, height, bytes,
                    added_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (owner_user_id, (title or "").strip() or os.path.basename(path),
             path, source, note, width, height, size, _now()))
        conn.commit()
        return int(cur.lastrowid)


def already_have(db_path: str, owner_user_id: int, path: str) -> bool:
    with closing(_connect(db_path)) as conn:
        return conn.execute(
            "SELECT 1 FROM design_assets WHERE owner_user_id = ? AND path = ?",
            (owner_user_id, path)).fetchone() is not None


def catalogue(db_path: str, owner_user_id: int, status: str | None = "available",
              limit: int = 100) -> list[dict]:
    """What he can pick from. Newest first -- a design he sent this week is far more
    likely to be the one he is reaching for than one from months ago."""
    init_design_assets(db_path)
    sql = "SELECT * FROM design_assets WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def get(db_path: str, owner_user_id: int, asset_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM design_assets WHERE id = ? AND owner_user_id = ?",
            (asset_id, owner_user_id)).fetchone()
    return dict(row) if row else None


def mark_used(db_path: str, owner_user_id: int, asset_id: int) -> bool:
    """Record that something was made from this. Counted rather than flagged, because
    'I have sold three things off this design' is a different fact from 'I have used it',
    and the count is the one that tells him which artwork actually earns."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE design_assets
                  SET times_used = times_used + 1, status = 'used'
                WHERE id = ? AND owner_user_id = ?""", (asset_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


def set_status(db_path: str, owner_user_id: int, asset_id: int, status: str) -> bool:
    if status not in ("available", "used", "retired"):
        raise ValueError("status must be available, used or retired")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE design_assets SET status = ? WHERE id = ? AND owner_user_id = ?",
            (status, asset_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0
