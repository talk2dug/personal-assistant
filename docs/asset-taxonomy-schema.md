# Legacy Media Asset Catalog — tagging taxonomy & schema

Status: proposed design, not yet implemented. Written for whoever builds the tagging
layer on top of the existing media scan/collect pipeline (`assistant/core/media_scan.py`,
`scripts/scan_network_media.py`, `scripts/collect_media.py`, `web/src/pages/Media.jsx`).

## 1. Scope and how this fits what already exists

Jarvis already does **discovery and triage**: `media_scan.py` walks every drive, records
every file into `media_files`, rolls files up into `media_folders`, and the Media page lets
the owner mark a folder `import` / `skip`. That machinery classifies by **file kind** — the
fixed set `image, design, vector, cut, model3d, video, audio, font, doc, archive` seen in
`collect_media.py`'s `KINDS` and `Media.jsx`'s `KIND_ORDER`. That's a filetype bucket, not a
catalog. It answers "is this worth collecting," not "what is this design, and what can I sell
it on."

This document defines the **second layer**: once a file is imported (kind in `image`,
`vector`, `design`, or `cut` — the actual artwork kinds; `model3d`/`video`/`audio`/`font`/`doc`
are out of scope here), it becomes a **design asset** and gets tagged across six facets:

1. Subject matter — what it's *of*
2. Art style — how it's rendered
3. Color palette — what colors it uses and how
4. Format — the technical shape of the file itself
5. Source medium — what the file was originally built to produce
6. Product fit — what it can be sold as *today*, in this shop's catalog

(5) and (6) are deliberately separate. A vinyl-decal-native line-art file (source medium =
`vinyl-decal`) is very likely *also* a fit for wood-burn and screen-print (product fit
includes `wood-burn`, `shirt-screenprint`) because it's monochrome and vector. Source medium
is provenance; product fit is a recommendation, partly rule-derived (§6), partly curated.

This reuses the two-tier tagging pattern already identified in `docs/print-station-port.md`
(Tier 2): local vision model (LLaVA/Ollama) for the bulk sweep, cloud vision (Gemini/Claude)
only for low-confidence or high-value items, same caching-to-avoid-re-billing habit.

## 2. Design principles

- **Controlled vocabulary, not free text**, for the five categorical facets. Free text is
  where legacy catalogs rot — "dog", "dogs", "doggo", "puppy-lover" all mean the same shelf.
  Every facet is a lookup table (`slug`, `label`, optional `parent_id`), editable without a
  code deploy, and asset→tag is a many-to-many junction.
- **One overflow escape hatch.** A `asset_keywords` free-text table exists for things a fixed
  vocabulary shouldn't try to enumerate — band names, movie titles, customer names on a
  personalized order. Search hits both the controlled tags and the keywords.
- **Every tag carries provenance and confidence.** `source` (`human`, `vision-ai-local`,
  `vision-ai-cloud`, `ocr`, `rule-engine`) and `confidence` (0–1, null for human) sit on every
  junction row. Low-confidence AI tags are exactly what a review queue (Jarvis already has
  one — `Review.jsx`) should surface before they're trusted for a storefront listing.
- **Multi-valued everywhere it's true.** A design can have two subjects ("dog" + "camping"),
  two styles ("distressed" + "hand-lettered"), several dominant colors, and several product
  fits. Only *format* fields are single-valued, because a file has exactly one native format.
- **Hierarchy where the domain has it, not everywhere.** Subject matter and product fit are
  the two facets people actually think in trees for ("Animals > Dogs", "Apparel > DTF
Shirt"). Style, color, and medium are flat controlled lists — forcing a hierarchy onto them
  would be false precision.

## 3. Entity-relationship overview

```
media_files (existing)
     │ 1:1 (on import)
     ▼
design_assets ────────────────────────────────────────────────────────┐
     │                                                           │
     ├─ asset_subject_tags   ─► subject_matter (self-referencing) │
     ├─ asset_style_tags     ─► art_style                         │
     ├─ asset_color_tags     ─► named_color                       │
     ├─ asset_medium_tags    ─► source_medium                     │
     ├─ asset_product_fit    ─► product_fit (self-referencing)    │
     └─ asset_keywords (free text, no vocab table)                │
                                                                    │
     format fields (file_format, is_vector, print_ready, ────────────┘
     resolution_dpi, pixel_w/h, orientation, color_mode_id)
     live directly on design_assets — one file, one format, no junction needed.
     color_mode_id → color_mode (small controlled list: line-art / single-color /
     two-color-spot / multi-color-spot / full-color-raster / grayscale)
```

## 4. Schema (SQLite, matching `media_scan.py` conventions)

```sql
-- ============================================================
-- Vocabulary tables. Seed data in section 5. All editable at
-- runtime; nothing here should require a code change to extend.
-- ============================================================

CREATE TABLE IF NOT EXISTS subject_matter (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    parent_id   INTEGER REFERENCES subject_matter(id),
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS art_style (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS named_color (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,     -- e.g. 'forest-green'
    label       TEXT NOT NULL,
    hex_hint    TEXT,                     -- representative hex, for swatch UI only
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS color_mode (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,     -- 'line-art', 'single-color', 'two-color-spot',
    label       TEXT NOT NULL,            -- 'multi-color-spot', 'full-color-raster', 'grayscale'
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS source_medium (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    category    TEXT NOT NULL,            -- 'apparel' | 'vinyl' | 'wood-laser' | 'sublimation' | 'embroidery' | 'other'
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS product_fit (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    parent_id   INTEGER REFERENCES product_fit(id),
    active      INTEGER NOT NULL DEFAULT 1
);

-- ============================================================
-- The asset itself: one row per catalogued design, 1:1 with an
-- imported media_files row. size/path/kind/sha256 stay on
-- media_files; this table only holds catalog-specific fields.
-- ============================================================

CREATE TABLE IF NOT EXISTS design_assets (
    id                INTEGER PRIMARY KEY,
    media_file_id     INTEGER NOT NULL UNIQUE REFERENCES media_files(id),
    title             TEXT,               -- human-assigned or derived from filename/folder
    notes             TEXT,

    -- Format facet (single-valued, so plain columns not a junction)
    file_format       TEXT,               -- 'svg','ai','eps','pdf','png','jpg','psd','dxf', ...
    is_vector         INTEGER,            -- 0/1/NULL(unknown)
    print_ready       INTEGER,            -- 0/1/NULL
    print_ready_note  TEXT,               -- why not, e.g. 'raster only, needs vectorizing'
    resolution_dpi    INTEGER,            -- raster only, NULL for vector
    pixel_width       INTEGER,
    pixel_height      INTEGER,
    orientation       TEXT,               -- 'square','portrait','landscape'
    color_mode_id     INTEGER REFERENCES color_mode(id),

    mature_content    INTEGER NOT NULL DEFAULT 0,   -- filter flag, independent of subject
    tag_status        TEXT NOT NULL DEFAULT 'untagged',
                      -- 'untagged' | 'ai-tagged' | 'needs-review' | 'reviewed'
    tagged_at         TEXT,
    reviewed_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_design_assets_status ON design_assets(tag_status);

-- ============================================================
-- Junctions. Every one carries provenance + confidence so a
-- review queue can filter on "AI said so, nobody's checked it."
-- ============================================================

CREATE TABLE IF NOT EXISTS asset_subject_tags (
    asset_id    INTEGER NOT NULL REFERENCES design_assets(id),
    subject_id  INTEGER NOT NULL REFERENCES subject_matter(id),
    confidence  REAL,                     -- NULL for human-entered
    source      TEXT NOT NULL,            -- 'human'|'vision-ai-local'|'vision-ai-cloud'|'ocr'|'rule-engine'
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_id, subject_id)
);

CREATE TABLE IF NOT EXISTS asset_style_tags (
    asset_id    INTEGER NOT NULL REFERENCES design_assets(id),
    style_id    INTEGER NOT NULL REFERENCES art_style(id),
    confidence  REAL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_id, style_id)
);

CREATE TABLE IF NOT EXISTS asset_color_tags (
    asset_id       INTEGER NOT NULL REFERENCES design_assets(id),
    color_id       INTEGER NOT NULL REFERENCES named_color(id),
    dominance_rank INTEGER NOT NULL DEFAULT 1,  -- 1 = most dominant
    hex_sample     TEXT,                        -- actual extracted hex, vocab is the nearest name
    source         TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_id, color_id, dominance_rank)
);

CREATE TABLE IF NOT EXISTS asset_medium_tags (
    asset_id    INTEGER NOT NULL REFERENCES design_assets(id),
    medium_id   INTEGER NOT NULL REFERENCES source_medium(id),
    is_primary  INTEGER NOT NULL DEFAULT 0,   -- the medium the file was actually built for
    confidence  REAL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_id, medium_id)
);

CREATE TABLE IF NOT EXISTS asset_product_fit (
    asset_id    INTEGER NOT NULL REFERENCES design_assets(id),
    fit_id      INTEGER NOT NULL REFERENCES product_fit(id),
    basis       TEXT NOT NULL,             -- 'rule'|'manual'|'ai'
    confidence  REAL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_id, fit_id)
);

CREATE TABLE IF NOT EXISTS asset_keywords (
    asset_id    INTEGER NOT NULL REFERENCES design_assets(id),
    keyword     TEXT NOT NULL,             -- free text: band names, IP names, customer terms
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_asset_keywords_kw ON asset_keywords(keyword);
```

All six junctions + `asset_keywords` are additive: re-running a tagging pass just inserts
rows (or upserts on the primary key), never deletes silently — consistent with the
"resumable, never destroy an existing record" habit already in `media_scan.py`.

## 5. Seed vocabularies

These are starting sets, not fixed forever — the tables are meant to be edited. They
consolidate the theme lists already used by the two legacy vision classifiers described in
`docs/print-station-port.md` (`design-categorizer.js`'s theme list and `catalog-classifier.js`'s
~30 categories) into one canonical vocabulary, so it doesn't matter which pipeline (local
LLaVA or cloud Gemini/Claude) produced the tag.

**subject_matter** (top level shown; each has room for children, e.g. `animals > dogs`,
`automotive > motorcycles`):
Animals & Pets, Outdoors & Adventure (camping/hiking/hunting/fishing), Automotive & Moto,
Patriotic & Military, Faith & Inspirational, Humor & Sarcasm, Pop Culture & Fandom
(anime/gaming/movies-tv/music-band-merch), Holiday & Seasonal, Family & Relationships
(mom-life/dad-life/wedding/baby), Occupations & Trades (nursing/teaching/farming/first-responder),
Sports & Fitness, Southern/Rustic/Country Living, Nature & Florals, Typography & Quotes
(text-only, no illustrative subject), Regional/Local Pride, Custom/Personalization
(monograms, name-drops — not a "subject" so much as a marker that this asset is a template),
Abstract & Decorative (pattern-only, no representational subject).

**art_style**: line-art, silhouette, vector-flat, distressed/grunge, watercolor,
hand-lettered/script, retro/vintage, cartoon/mascot, realistic/photographic, 3D-rendered,
mandala/geometric, minimalist, sketch/engraved-line (the look laser engraving produces),
pop-art, western/rustic-type, halftone.

**color_mode** (drives both format and the product-fit rule engine, §6):
line-art (no fill, stroke only), single-color, two-color-spot, multi-color-spot (3–6 flat
colors, still not photographic), full-color-raster (gradients/photographic/CMYK), grayscale.

**named_color**: a standard ~24-color naming table (red, crimson, orange, burnt-orange, gold,
yellow, olive, forest-green, mint, teal, navy, royal-blue, sky-blue, purple, lavender, magenta,
pink, brown, tan, black, charcoal, gray, white, silver) — deliberately the same style of
hex→name mapping already built in `nano-banana-service.js`'s `hexToColorName` (per
`docs/mockup-system.md`), so the recolor pipeline and the catalog use the same names.

**source_medium** (`category` in parens):
vinyl-decal (vinyl), sticker-diecut (vinyl), shirt-dtf (apparel), shirt-screenprint (apparel),
shirt-htv (apparel, cut vinyl heat-transfer), embroidery-digitized (embroidery),
wood-burn-laser (wood-laser), laser-engrave-acrylic (wood-laser), laser-engrave-glass
(wood-laser), metal-sublimation (sublimation), tumbler-sublimation (sublimation),
sign-yard-coroplast (vinyl), ornament (wood-laser or sublimation, tag both if ambiguous),
generic-artwork (other — no identifiable original production path, mostly legacy scans/AI art
with no known destination yet).

**product_fit** (top-level groups, children are the sellable SKUs today):
Apparel (dtf-shirt, screenprint-shirt, htv-shirt, hoodie, hat), Vinyl Decal (car-decal,
laptop-decal, sticker-sheet), Wood & Laser (wood-sign, wood-burn-ornament,
engraved-tumbler-insert), Metal Print (wall-metal-print), Drinkware (sublimation-tumbler,
koozie), Signage (yard-sign). Keep this list matched to whatever the storefront actually
sells this quarter — unlike the other five facets, this one should churn as the product line
does.

## 6. Product-fit rule engine (the `basis = 'rule'` rows)

Before any AI or human touches an asset, format + color_mode alone can populate a first pass
of `asset_product_fit`. This is cheap, deterministic, and exactly the kind of rule the
mockup/recolor pipeline in `docs/mockup-system.md` already leans on ("start from white/light
garments", "the flatten trick" for line art):

| color_mode | is_vector | print_ready | → suggested product_fit |
|---|---|---|---|
| line-art / single-color | true | true | vinyl-decal, wood-burn-ornament, wood-sign, laser-engrave, htv-shirt, screenprint-shirt |
| two-color-spot / multi-color-spot | true | true | screenprint-shirt, sticker-sheet, htv-shirt |
| full-color-raster | — | — (resolution_dpi ≥ 150) | dtf-shirt, sublimation-tumbler, wall-metal-print |
| full-color-raster | — | resolution_dpi < 150 or NULL | none suggested — flag `needs-review`: too low-res to print, candidate for the vectorizer/upscale path in `gemini-vectorizer.js` |

These rows get `basis='rule'`, `source='rule-engine'`, `confidence=1.0` for the format match
itself, but they are *suggestions to confirm*, not publish-ready claims — a design can be
technically compatible with a medium and still be a bad fit content-wise (a hunting-themed
line-art might be rule-eligible for a onesie; nobody wants that). Rule-derived fits should
land in `needs-review` status alongside AI tags, not skip review.

## 7. Tagging pipeline contract

The vision/OCR pass (ported from `design-categorizer.js` / `catalog-classifier.js` per Tier 2
of `docs/print-station-port.md`) should emit one JSON object per asset, independent of
whether it ran on local LLaVA or cloud Gemini/Claude:

```json
{
  "media_file_id": 41822,
  "model": "llava-local" ,           // or "gemini-vision", "claude-vision"
  "subjects":  [{"slug": "animals-dogs", "confidence": 0.91}],
  "styles":    [{"slug": "distressed-grunge", "confidence": 0.74}],
  "colors":    [{"hex": "#2f4f4f", "confidence": 0.95},
                {"hex": "#e8e8e8", "confidence": 0.60}],
  "color_mode":  {"slug": "two-color-spot", "confidence": 0.80},
  "ocr_text":  "CAMP LIFE",           // from catalog-ocr-generator.js equivalent; feeds asset_keywords
  "mature_content": false
}
```

Ingestion logic (not the AI call itself):
1. Insert/replace `design_assets` format fields directly (deterministic from the file).
2. Nearest-match each returned hex to `named_color` (same hue-approximation fallback table
   already built as `hexToColorName`); insert into `asset_color_tags` ranked by dominance.
3. Insert subject/style tags with `source` set to the model that produced them.
4. Run the §6 rule engine to seed `asset_product_fit` with `basis='rule'`.
5. Any AI-sourced tag with `confidence < 0.65` (threshold configurable), or any asset with
   `mature_content: true`, sets `design_assets.tag_status = 'needs-review'` and should surface
   in Jarvis's existing Review queue rather than auto-publish.
6. Everything else lands as `tag_status = 'ai-tagged'` — usable for internal browsing
   immediately, promoted to `reviewed` only when a human confirms (bulk-confirm, same pattern
   as the `import`/`skip` bulk actions already in `Media.jsx`).

Two-tier cost control, same as the existing recommendation: run local LLaVA on every asset
first; only escalate to Gemini/Claude vision for assets where LLaVA confidence is low across
the board, or where OCR is needed and Tesseract's output looks garbled.

## 8. Surfacing (API + UI sketch)

Following the existing `api.js` / `Media.jsx` pattern, a `Catalog` view can sit alongside the
current `Media` page once assets exist:

- `GET /api/catalog/assets?subject=&style=&medium=&fit=&color=&mature=false` — filtered browse,
  same query-param style as `mediaFolders`.
- `GET /api/catalog/vocab/:facet` — serves the six controlled lists for filter dropdowns and
  for the tag-editing UI; editing a vocab row (rename/deactivate) doesn't touch tag rows.
- `POST /api/catalog/assets/:id/tags` — human add/remove/confirm tags; always `source='human'`,
  `confidence=NULL`, and bumps `tag_status` toward `reviewed`.
- This is also the data source `mockup-pro`'s catalog browser (`docs/mockup-system.md`, "the
  piece most tied to the old server") should be repointed at once it's rebuilt — it currently
  fetches from API routes and `/library/...` URLs that no longer exist; this schema is the
  replacement for the lost `catalog.db` it depended on.

## 9. Rollout order

1. Create the tables above (additive migration, no impact on `media_scan.py`'s existing schema).
2. Seed the six vocab tables from §5.
3. Backfill `design_assets` + format columns for every `media_files` row already marked
   `decision='import'` — format detection is deterministic, no AI needed, do this first.
4. Run the §6 rule engine over every backfilled asset — free, immediate product-fit coverage.
5. Local LLaVA sweep for subject/style/color on the same set; cloud escalation only for
   low-confidence or flagged-mature results.
6. Wire `needs-review` assets into the existing Review queue.
7. Build the `Catalog` browse page / API once there's enough tagged data to make it useful.

## 10. Open questions for the owner

- **Product line confirmation**: the `product_fit` seed list (§5) is a guess built from what
  `docs/mockup-system.md` and `docs/print-station-port.md` describe (apparel, metal prints,
  vinyl decals/stickers). Wood-burn is named in the assignment but I found no evidence in the
  repo that it's an active product line vs. aspirational — confirm before it drives real
  storefront categories.
- **Mature content policy**: `mature_content` is a boolean flag with no defined downstream
  behavior yet (hide from catalog entirely? hide from public browse but keep in internal
  search?). Needs a decision before the review queue can act on it.
- **Confidence threshold (0.65 in §7)** is a placeholder — should be tuned once there's a
  batch of real LLaVA output to eyeball against human judgment.
