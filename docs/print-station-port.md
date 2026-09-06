# Print Station → Jarvis: media & AI porting inventory

Survey of `D:\Vinyl Stuff` (repo `talk2dug/CustomVinylRetail`) for everything that makes
media or uses AI. Physical hardware (slicer, printer control, kiosk, plate packing, STL
tooling) deliberately excluded — later phase.

Scale of what's there: **54 files in `server/`, 40 in `server/modules/`, 8 in `server/lib/`**,
plus two separate Remotion video projects. Most of it is real, working code — not scaffolding.

---

## The headline

Jarvis already has the *engines* (GPU bridge, ComfyUI image + video, agent roster, approval
queue). What Print Station has that Jarvis does **not** is the *craft layer* — the accumulated
knowledge of how to turn a design into a sellable product:

1. **Cut-file generation** — the actual print-station core, and Jarvis has nothing like it
2. **Templated branded video** — Jarvis can generate raw video; it can't make a branded reel
3. **Vision cataloging** — needed for the artwork-catalog agent you asked for next
4. **Platform-specific copywriting** — Etsy's 13 tags, eBay's 80 chars, etc.

---

## Tier 1 — port these first (highest value, no equivalent in Jarvis)

### `sticker-sheet-generator.js` — 153KB, ~4,400 lines
The single most valuable file in the repo. Print-ready sheets + cut-ready SVG.

- 300 DPI PNG composites, letter sheets (`SHEET_CONFIG`, 2550×3300px)
- **Bezier-preserving** path pipeline throughout — `offsetSvgPathBezier`, `scaleSvgPath`,
  `rotateSvgPath`, `translateSvgPath` all keep curves instead of flattening to line segments.
  This is the difference between a smooth cut and a faceted one.
- `extractColorContours` — per-color contours for multi-color vinyl (the comment literally says
  "the proper way!")
- Rotation-aware nesting (`packDesignsWithRotation`, `NestingEngine`) with two modes: MANUAL
  (pack for material efficiency) and BY-ORDER (keep each customer's order grouped)
- Registration marks computed in mm→px at sheet DPI for the cutter
- Product labels: 3"×1.5" PNG, packed onto sheets

Dependencies: `sharp`, `paper` (paper.js), `potrace`. All npm, all portable. **No server needed.**

> ⚠️ `nesting-engine.js` is **0 bytes** — empty file, but it's imported at the top of the sticker
> generator. Either the nesting engine was never committed or it got truncated. The exports list
> re-exports `NestingEngine` and `createNestingEngine` from it, so packing will throw on import.
> This needs rebuilding or recovering.

### `gemini-vectorizer.js` — 43KB
Replaces potrace tracing with vision AI. The insight in its header is worth quoting:

> Gemini understands what the design IS, not just pixel boundaries, so it handles gradients,
> shadows, glow effects, and complex edges properly.

Three strategies, in quality order:
1. `direct-svg` — Gemini returns SVG path data as text (best quality)
2. `clean-trace` — Gemini makes a clean mask, potrace traces that (most reliable)
3. `color-separate` — per-color masks for multi-color vinyl

Strategy 2 is the interesting one: use the AI for what it's good at (understanding the image)
and the deterministic tool for what *it's* good at (precise tracing). That pattern ports to
any vision model, including local ones.

### `lib/contour-trainer.js` — 15KB
Learns **your** contour style from paired Silhouette Studio3 files (image + your manual cut
path) and feeds learned potrace parameters back into generation. Trains separate `sticker` and
`decal` profiles.

> ⚠️ **The code survived; the training data did not.** It read from a `studio3_catalog` table on
> the lost server, and there are **zero `.studio3` files** anywhere in the local copy, and no
> saved style profile. If your Silhouette files exist on another drive, the artwork-scan agent
> should collect them — retraining is then just running the trainer. Otherwise this capability
> is code-complete but untrained.

### Remotion video templating — `remotion/` + `tiktok-studio/`
Two React-based video projects that render real branded reels. Seven compositions, all
1080×1920 portrait:

| Composition | Size | Purpose |
|---|---|---|
| `MetalPrintStory` | 20KB | art + process + mockups, the flagship |
| `MockupReel` | 19KB | apparel drops |
| `CatalogShowcase` | 10KB | catalog browse |
| `FootageReel` | 9KB | real footage cuts |
| `TikTokPromo` | 6KB | short promo |
| `StickerReveal` | 4KB | sticker reveal |
| `ProductShowcase` | 3KB | simple product |

Durations are **computed from content** (`durationInFrames: computeDuration(mockupImages.length)`)
rather than fixed — add three mockups, the reel gets longer automatically.

`remotion/render-api.js` renders programmatically via `execFile`; `modules/remotion-renderer.js`
wraps it with a **queue and a single-render lock** (`isRendering`, `renderQueue`, 10-min timeout)
so concurrent requests don't fight over CPU. That queue pattern should merge with Jarvis's
existing GPU job queue rather than run separately.

`tiktok-studio/` is the newer, cleaner one: typed `TikTokSchema.ts` with `SceneClip`,
`CaptionOverlay`, `TextOverlay`, `BrandWatermark` components.

---

## Tier 2 — vision & cataloging (unblocks the artwork-catalog agent)

These are exactly what's needed for "locate all artwork on all my drives, identify, catalog."

| Module | What it does |
|---|---|
| `modules/design-categorizer.js` (18K) | Gemini vision → theme, mood, dominant colors, marketing copy. Has a fixed theme list (`outdoor-adventure`, `moto-garage`, ...). Caches to JSON to avoid re-billing. |
| `lib/catalog-classifier.js` (6K) | Same job via **LLaVA on Ollama** — the free/local path. ~30 predefined categories (anime, automotive, band-merch, camping, dog-lover, ...). |
| `human-model-analyzer.js` (19K) | Gemini Vision primary, **Claude Vision fallback** → gender, ethnicity, apparel type, facing direction. This is how model photos get auto-tagged so the pipeline can match a model to a design. |
| `catalog-ocr-generator.js` (11K) | Tesseract OCR for text-based designs and band logos — catches what vision models paraphrase. |
| `catalog-metadata-generator.js` (7K) | Batch AI descriptions by category with progress reporting. |

**Take the two-tier pattern:** LLaVA/local for the bulk sweep across drives, Gemini/Claude only
for items that matter or that the local model is unsure about. With thousands of files, that's
the difference between free and expensive. The caching-to-JSON habit is throughout — keep it.

---

## Tier 3 — copywriting (all Ollama-based, drops straight into Jarvis)

Notable because **these already run on local Ollama**, which Jarvis is already wired for.

- **`modules/reel-copywriter.js`** — hooks, per-item captions, explanations for reels. Explicitly
  "no cloud APIs." Two templates: `apparel`, `metal-print`. Brand strings are hardcoded to
  `BlueRidge Custom Co` / `Asheville, NC` — **needs updating for Richmond.**
- **`modules/listing-optimizer.js`** — per-platform title/description/tags with the real limits
  encoded: Shopify 255/5000, Etsy 140/2000/**13 tags**, eBay 80/4000, Amazon 200/2000/5 bullets,
  TikTok 100/1000. Those limits are tedious to rediscover.
- **`modules/etsy-listing-generator.js`** — Etsy-specific, built for copy-paste because *"API
  access was denied."* Worth knowing before you try the Etsy API again.
- **`modules/seo-engine.js`** — JSON-LD (Product/Organization/LocalBusiness), sitemap, robots.txt,
  OG tags.
- **`leonardo-prompt-generator.js`** — despite the name, it's the **prompt generator for Gemini**,
  running on Ollama with a failover chain (GPU bridge → server Ollama → raw keywords). Has tuned
  templates, e.g. vehicles for metal sublimation: three-quarter front view, full car in frame,
  pure black background for contrast.

---

## Tier 4 — orchestration (the "autonomous" layer)

- **`modules/apparel-pipeline.js`** (79K) — categorize designs → match models → generate mockups
  → publish to Shopify → create TikTok reels → Telegram notify. `processNewCollection(category)`.
- **`modules/metal-print-pipeline.js`** (17K) — same shape, room mockups instead of models, with
  Shopify size variants.
- **`modules/curated-campaign.js`** (18K) — limited-run drops on S&S Activewear blanks with
  scarcity/urgency copy.

These are the blueprint for what your agents should *do*, but they're the most server-coupled
code in the repo (Shopify, `/mnt/` paths, the design DB). **Port the sequence, not the file.**
Jarvis's agent + review-queue model already replaces the Telegram-notify step.

---

## Tier 5 — publishing connectors

- `lib/facebook-post-scheduler.js` (74K) — scheduled posting, mockup generation, AI captions
- `modules/fb-group-poster.js` — groups reject ads, so copy is rewritten value-first by Ollama
  and delivered to you via Telegram deep-link to paste manually. Pragmatic workaround for
  missing group permissions.
- `modules/fb-marketplace-lister.js` — local-seller-voice page posts (vision model when an image
  exists); notes the Commerce API upgrade path
- `modules/fb-engagement-bot.js` — polls comments every 15 min, Ollama replies, UTM-tagged links,
  dedupe + rate limiting
- `modules/utm-attribution.js`, `shopify-analytics.js`, `shopify-order-sync.js`

All need credentials you may no longer have. Lower priority, but the **copy strategy** in each
(group voice vs. marketplace voice vs. page voice) is the valuable part and is credential-free.

---

## Already covered by Jarvis — don't re-port

| Print Station | Jarvis equivalent |
|---|---|
| `lib/ollama-client.js` (GPU bridge failover, 30s health cache) | `core/gpu_bridge.py` — more capable (queue, reservations, gaming lockout) |
| `lib/wan2gp-video-generator.js` (I2V over SSH/Gradio) | `core/comfy_client.py` — Wan 2.2 TI2V-5B, verified working |
| `modules/market-finder.js`, `trend-monitor.js` | Already ported into `core/agents.py` |
| `modules/ai-sales-agent.js`, `marketing-team.js`, `sales-team.js` | Agent roster + Review queue |
| `lib/telegram-notifier.js`, `telegram-printer-bot.js` | Jarvis notifications + HA push |
| Leonardo.ai stack (`leonardo-ai/api/server/workflow.js`) | Dead — superseded by nano-banana, then by ComfyUI |

**`modules/meshy-client.js`** (18K, Meshy AI text/image→3D) is genuinely uncovered, but it's a
paid API for 3D asset generation — parking it with the hardware phase makes sense.

---

## What's actually gone

Code loss: **none**. Every module above is intact in git.

Data loss:
- The design catalog and model-photo library (lived on `/mnt/websit`, `/mnt/dbFiles`)
- The Studio3 contour training data and trained style profiles
- `nesting-engine.js` is empty in the repo — may predate the server loss

Everything else is a path change (`paths.js` is the single choke point — every output directory
in the system flows from those four `/mnt/` constants) plus swapping Gemini for local models
where quality allows.

---

## Suggested order

1. **Vision cataloging (Tier 2)** — you asked for the artwork scan next, and this is its engine.
   Local LLaVA for the sweep, Gemini/Claude for the ones that matter. Sweep for `.studio3` files
   at the same time to see if the contour trainer can be revived.
2. **Cut files (Tier 1)** — sticker sheets + vectorizer. This is what makes it a *print* station
   rather than an image generator. Budget time to rebuild `nesting-engine.js`.
3. **Reels (Tier 1)** — Remotion, merged into the existing GPU queue. Update brand strings to
   Richmond.
4. **Copy (Tier 3)** — nearly free; already Ollama-shaped.
5. **Pipelines + publishing (Tiers 4–5)** — last, since they need credentials and a live store.

Steps 1 and 2 give a working local print station with no external dependencies and no store.
