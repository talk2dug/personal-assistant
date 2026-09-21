"""The model catalogue — the faces the store's products are shown on.

Jack's requirement, in his words: *"it should always be the same for the different things,
like boy tshirts is the same guy, girls the same girl, hats mugs whatever."* One persona
per category, held steady across every product in it. That consistency is most of what
separates a catalogue that reads as a brand from one that reads as a pile of unrelated
print-on-demand mockups, and it costs nothing but bookkeeping — which is what this is.

The source is 338 Leonardo exports in a folder, and the useful accident is that Leonardo
names its files after the prompt that made them:

    Lucid_Origin_a_cinematic_photo_of_Headshot_portrait_young_Latina_woman_0.jpg
    Phoenix_10_A_woman_in_her_late_30s_dirty_blonde_hair_in_a_mess_2.jpg

So the folder already contains its own taxonomy. Two files that share everything but the
trailing index are two shots of the *same described person*, which is exactly the grouping
a persona needs — 275 people across 338 shots, most with two to four angles.

Files are indexed where they live rather than copied. That is three quarters of a gigabyte
on an internal drive, and a second copy would be a second thing to keep in sync for no
benefit; only small thumbnails are generated, for the dashboard.
"""
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# The generator that produced a file, stripped from the front of its name. Leonardo writes
# the model name first, and it is worth keeping: different models age differently, and
# "which of these came from Phoenix" is the question behind any future re-generation.
_GENERATORS = ("Lucid_Origin", "Phoenix_10", "Leonardo_Phoenix", "AlbedoBase_XL",
               "Leonardo_Kino_XL", "Leonardo_Anime_XL", "Leonardo_Diffusion_XL",
               "Leonardo_Lightning_XL", "Leonardo_Vision_XL", "DreamShaper_v7",
               "Absolute_Reality_v16", "PhotoReal", "Flux_Dev", "Flux_Schnell")

# Boilerplate Leonardo inserts that says nothing about who the person is.
# Alternation is longest-first on purpose: with (a|an) the regex matches the "a" of "An"
# and leaves a stray "n", so every androgynous persona was described as "n androgynous
# young person". A leftmost-alternation bug reads as bad data, not as a code fault.
_NOISE = re.compile(
    r"^(an|a|the)?[_ ]*(cinematic|photorealistic|hyperrealistic|professional|studio)?"
    r"[_ ]*(photo|portrait|image|shot|picture)?[_ ]*(of)?[_ ]*", re.I)

_GENDER_WORDS = (
    ("woman", "female"), ("women", "female"), ("female", "female"), ("girl", "female"),
    ("lady", "female"), ("man", "male"), ("men", "male"), ("male", "male"),
    ("boy", "male"), ("guy", "male"), ("androgynous", "androgynous"),
    ("nonbinary", "androgynous"), ("person", "unspecified"), ("people", "unspecified"),
)

# Ordered longest-first so "late 20s" is not matched as "20s" with the qualifier lost.
_AGE_PATTERNS = (
    (re.compile(r"\b(early|mid|late)[_ ](\d0)s\b", re.I), lambda m: f"{m.group(1).lower()} {m.group(2)}s"),
    (re.compile(r"\bin (?:his|her|their) (early|mid|late) (\d0)s\b", re.I), lambda m: f"{m.group(1).lower()} {m.group(2)}s"),
    (re.compile(r"\bage[_ ](\d{2})\b", re.I), lambda m: f"age {m.group(1)}"),
    (re.compile(r"\b(\d0)s\b"), lambda m: f"{m.group(1)}s"),
    (re.compile(r"\b(teen|teenage|young adult|middle aged|senior|elderly)\b", re.I),
     lambda m: m.group(1).lower()),
)

_ETHNICITY_WORDS = ("caucasian", "white", "black", "african", "asian", "east asian",
                    "south asian", "latina", "latino", "hispanic", "pacific islander",
                    "native american", "middle eastern", "mixed race", "mixed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Stable slug derived from the prompt text, so re-importing the same folder updates
    -- personas in place instead of duplicating all 275 of them.
    key TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    generator TEXT,
    gender TEXT,
    age_band TEXT,
    ethnicity TEXT,
    -- Which product category this face fronts. NULL means "in the pool, unassigned".
    -- Exactly one persona per category is the rule the whole table exists to enforce.
    assigned_category TEXT,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'active', 'retired')),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id INTEGER NOT NULL REFERENCES model_personas(id),
    -- Absolute source path. Indexed, never copied: 0.74 GB on an internal drive, and a
    -- duplicate would only be a second thing to keep in sync.
    path TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    thumb_path TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_images_persona ON model_images(persona_id);
CREATE INDEX IF NOT EXISTS idx_model_personas_cat ON model_personas(assigned_category);
"""


def init_model_catalog(db_path: str) -> None:
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


def parse_file_name(file_name: str) -> dict:
    """Pull a persona out of a Leonardo export's name.

    Best-effort and explicitly so: every field can come back None, because a wrong guess
    about who someone is would propagate into which products they front. The one field
    that must be right is `key`, since it is what groups shots of the same person.
    """
    stem = re.sub(r"\.(jpg|jpeg|png|webp)$", "", file_name, flags=re.I)

    generator = None
    for candidate in _GENERATORS:
        if stem.startswith(candidate):
            generator = candidate
            stem = stem[len(candidate):].lstrip("_")
            break

    # Leonardo appends _0.._3 per variation. Stripping it is what collapses four files
    # into one person; without this the catalogue would hold 338 strangers.
    stem = re.sub(r"_\d{1,2}$", "", stem)

    text = stem.replace("_", " ").strip()
    text = _NOISE.sub("", text).strip()
    readable = re.sub(r"\s+", " ", text)

    lowered = readable.lower()
    gender = next((g for word, g in _GENDER_WORDS if re.search(rf"\b{word}\b", lowered)), None)

    age_band = None
    for pattern, render in _AGE_PATTERNS:
        match = pattern.search(lowered)
        if match:
            age_band = render(match)
            break

    ethnicity = None
    for word in sorted(_ETHNICITY_WORDS, key=len, reverse=True):
        if re.search(rf"\b{word}\b", lowered):
            ethnicity = word
            break

    key = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")[:80] or "unnamed"
    return {"key": key, "description": readable or stem, "generator": generator,
            "gender": gender, "age_band": age_band, "ethnicity": ethnicity}


def import_directory(db_path: str, directory: str, *, thumb_dir: str | None = None,
                     thumb_px: int = 400) -> dict:
    """Index every image in a folder, grouping shots of the same person into one persona.

    Idempotent: re-running picks up new files and leaves existing personas (and any
    category assignments made against them) exactly as they were. That matters because the
    assignment is the expensive human-ish decision, and an import that silently reset it
    would be worse than no import.
    """
    init_model_catalog(db_path)
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"model directory not found: {directory}")

    files = sorted(f for f in os.listdir(directory)
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
    added_personas = added_images = skipped = 0

    with closing(_connect(db_path)) as conn:
        for file_name in files:
            path = os.path.join(directory, file_name)
            if conn.execute("SELECT 1 FROM model_images WHERE path = ?", (path,)).fetchone():
                skipped += 1
                continue

            parsed = parse_file_name(file_name)
            now = _now()
            row = conn.execute("SELECT id FROM model_personas WHERE key = ?",
                               (parsed["key"],)).fetchone()
            if row is None:
                cur = conn.execute(
                    """INSERT INTO model_personas (key, description, generator, gender,
                                                   age_band, ethnicity, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (parsed["key"], parsed["description"], parsed["generator"],
                     parsed["gender"], parsed["age_band"], parsed["ethnicity"], now, now))
                persona_id = cur.lastrowid
                added_personas += 1
            else:
                persona_id = row["id"]

            width = height = None
            thumb_path = None
            try:
                from PIL import Image

                with Image.open(path) as image:
                    width, height = image.size
                    if thumb_dir:
                        os.makedirs(thumb_dir, exist_ok=True)
                        thumb_path = os.path.join(thumb_dir, f"{parsed['key'][:60]}-{persona_id}.jpg")
                        if not os.path.exists(thumb_path):
                            copy = image.convert("RGB")
                            copy.thumbnail((thumb_px, thumb_px))
                            copy.save(thumb_path, "JPEG", quality=82)
            except Exception as exc:                      # noqa: BLE001
                # A single unreadable file must not abandon the other 337.
                logger.warning("could not read image %s: %s", file_name, exc)

            conn.execute(
                """INSERT INTO model_images (persona_id, path, file_name, width, height,
                                             thumb_path, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (persona_id, path, file_name, width, height, thumb_path, now))
            added_images += 1
        conn.commit()

    return {"files_seen": len(files), "images_added": added_images,
            "personas_added": added_personas, "already_known": skipped}


def list_personas(db_path: str, *, assigned_category: str | None = None,
                  status: str | None = None, limit: int = 500) -> list[dict]:
    sql = ["""SELECT p.*, COUNT(i.id) AS shots,
                     (SELECT thumb_path FROM model_images WHERE persona_id = p.id
                       AND thumb_path IS NOT NULL LIMIT 1) AS thumb
                FROM model_personas p
                LEFT JOIN model_images i ON i.persona_id = p.id
               WHERE 1=1"""]
    args: list = []
    if assigned_category is not None:
        sql.append("AND p.assigned_category = ?")
        args.append(assigned_category)
    if status:
        sql.append("AND p.status = ?")
        args.append(status)
    sql.append("GROUP BY p.id ORDER BY p.assigned_category IS NULL, p.assigned_category, shots DESC")
    sql.append(f"LIMIT {int(limit)}")
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(" ".join(sql), args)]


def persona_images(db_path: str, persona_id: int) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM model_images WHERE persona_id = ? ORDER BY file_name",
            (persona_id,))]


def assign_category(db_path: str, persona_id: int, category: str) -> dict:
    """Make this persona the face of a category, displacing whoever held it.

    The displacement is the point. One face per category is the entire rule, so assigning
    a second one has to mean *replacing* the first rather than quietly creating a category
    with two faces that then alternate between products.
    """
    category = category.strip().lower()
    if not category:
        raise ValueError("a category is required")
    now = _now()
    with closing(_connect(db_path)) as conn:
        previous = conn.execute(
            "SELECT id, description FROM model_personas WHERE assigned_category = ? AND id != ?",
            (category, persona_id)).fetchall()
        conn.execute(
            """UPDATE model_personas SET assigned_category = NULL, status = 'candidate',
                   updated_at = ? WHERE assigned_category = ? AND id != ?""",
            (now, category, persona_id))
        changed = conn.execute(
            """UPDATE model_personas SET assigned_category = ?, status = 'active',
                   updated_at = ? WHERE id = ?""", (category, now, persona_id)).rowcount
        conn.commit()
    if not changed:
        raise ValueError(f"no persona with id {persona_id}")
    return {"persona_id": persona_id, "category": category,
            "replaced": [dict(r) for r in previous]}


def category_face(db_path: str, category: str) -> dict | None:
    """Who fronts this category right now, with their shots -- what a mockup job asks."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM model_personas WHERE assigned_category = ? AND status = 'active'",
            (category.strip().lower(),)).fetchone()
        if row is None:
            return None
        persona = dict(row)
        persona["images"] = [dict(r) for r in conn.execute(
            "SELECT * FROM model_images WHERE persona_id = ? ORDER BY file_name",
            (persona["id"],))]
        return persona


def catalog_summary(db_path: str) -> dict:
    with closing(_connect(db_path)) as conn:
        personas = conn.execute("SELECT COUNT(*) FROM model_personas").fetchone()[0]
        images = conn.execute("SELECT COUNT(*) FROM model_images").fetchone()[0]
        assigned = conn.execute(
            "SELECT assigned_category, COUNT(*) n FROM model_personas "
            "WHERE assigned_category IS NOT NULL GROUP BY assigned_category").fetchall()
    return {"personas": personas, "images": images,
            "categories": {r["assigned_category"]: r["n"] for r in assigned}}
