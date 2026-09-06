# Mockup generation — how it actually works

Reference notes recovered from `D:\Vinyl Stuff` (repo: `github.com/talk2dug/CustomVinylRetail`).
Written so this never has to be figured out from scratch again.

**Repo status:** local `main` is exactly in sync with `origin/main` (`git rev-list --left-right
--count` → `0 0`), and the `claude/cranky-goodall` branch holds nothing that isn't in main.
Losing the hosted server cost **no code**. It cost the *runtime* — the mounted drives and the
public site — not the source.

---

## The one thing to know first

There are **two generations** of mockup code in that repo, and the older one is a trap.

| | Old (deprecated) | Current |
|---|---|---|
| File | `server/lib/mockup-compositor.js`, `server/apparel-mockup-pipeline.js` | `server/ai-apparel-mockup.js` |
| Method | `sharp` pastes the graphic at hardcoded x/y/w/h | Gemini composites two images together |
| Result | Flat sticker floating on the shirt | Follows wrinkles, folds, shadows; hands/hair occlude it correctly |
| Still used? | Only by `facebook-post-scheduler.js` and `sales-pipeline-monitor.js`, for non-apparel | Everything else |

Both deprecated files carry an `@deprecated` header pointing at the AI path. **Don't restart from
the compositor.** The hardcoded `artworkPosition: { x: 250, y: 180, width: 500, height: 500 }`
tables in `apparel-mockup-pipeline.js` are a dead end — they only ever worked for one specific
blank image.

---

## The working architecture

```
ai-apparel-mockup.js      ← orchestration: front/back, multi-placement, side-by-side
        │
        ▼
nano-banana-service.js    ← THE PROMPTS. This is the hard-won file.
        │
        ▼
nano-banana-ai.js         ← thin Gemini client (generate / editImage / compositeImages)
```

"Nano banana" = Google Gemini image model. The naming is from the model's community nickname,
not a separate product.

### Model + config that works

```js
model: 'gemini-2.5-flash-image'
generationConfig: { responseModalities: ['TEXT', 'IMAGE'] }
```

`responseModalities` is mandatory — without it the model returns text describing the image
instead of the image. Images come back as `response.candidates[0].content.parts[*].inlineData`
(`{ data: base64, mimeType }`). If `parts` has no `inlineData`, the model refused; the text part
holds the reason, and the code surfaces it rather than failing silently:

```js
const textResponse = parts.filter(p => p.text).map(p => p.text).join(' ');
throw new Error(`No image generated. Model response: ${textResponse.substring(0, 200)}`);
```

Keep that. A silent empty result is what makes this stuff maddening to debug.

### Compositing = one call, N images

`compositeImages(imagePaths, prompt)` builds `[{text: prompt}, {inlineData}, {inlineData}, ...]`
and sends it as a single `generateContent`. Image 1 is the model photo, image 2 is the graphic.
The prompt refers to them by number.

---

## The two fixes that were the actual PITA

### 1. White-on-transparent graphics are invisible to Gemini

Most designs are white line art on a transparent background. Gemini renders transparency as
white, so the model sees a blank white square and either does nothing or invents its own design.

The fix (in `placeGraphicOnApparel`) samples the visible pixels first:

```js
// average the RGB of every pixel with alpha > 20
if (avgBrightness > 200) {
  await sharp(graphicPath).flatten({ background: { r: 30, g: 30, b: 30 } }).toFile(tmpPath);
}
```

Flatten onto near-black **only when the art is actually light**, then tell the model about it:

> The graphic has a dark background — extract just the visible design elements (the light/white
> artwork) and apply them to the garment. Do NOT place the dark background rectangle on the shirt.

Then delete the temp file. Skipping this step is the single biggest source of "it put a random
different design on the shirt."

### 2. Gemini re-draws the design unless you forbid it

The model's instinct is to "improve" the artwork. The prompt block that stops it:

```
CRITICAL RULES:
- Reproduce the EXACT design from Image 2 — same text, artwork, colors, layout
- Do NOT create, invent, or substitute a different design
- The design must look naturally screen-printed on the fabric
- Follow the fabric's wrinkles, folds, and shadows
- Preserve the person's pose, face, expression exactly
- Hair or hands overlapping the graphic area should appear IN FRONT of the design
- Do NOT change anything else about the image
- Result should look like a professional product photo
```

Every line there is load-bearing. The occlusion line ("hair or hands ... IN FRONT") is what makes
output look photographed rather than pasted.

### Placement is described, not computed

No coordinates. Zones and sizes are natural language:

- `front-chest` → "centered on the chest area of the front of the garment"
- `back` → "centered on the upper back area of the garment"
- `left-sleeve` / `right-sleeve` → "on the outer side of the left/right sleeve"
- sizes: `auto` / `small` (15-20% of garment area) / `medium` (30-40%) / `large` (50-60%) / `full`

Nudges are converted to phrases, with a deadband so tiny values are ignored:

```js
if (adjustX < -5) hints.push(`shifted ${Math.abs(adjustX)}% to the left of center`);
if (adjustScale > 110) hints.push(`${adjustScale - 100}% larger than default`);
```

### Multi-placement = sequential passes

`placeMultipleGraphics` feeds each result back in as the next pass's input image (chest, then
sleeve, then...), with a 2s gap between calls. Not parallel — each pass must see the previous
one's output or the placements overwrite each other.

Rate limit is **2000 ms between every Gemini call**, everywhere in this codebase. Respect it.

---

## Garment recoloring — two implementations

**A. Gemini** (`recolorGarment` in `nano-banana-service.js`). Hex is converted to a *color name*
first (`hexToColorName`) because "#2f4f4f" means nothing to the model but "dark slate gray" does.
There's a lookup table of ~28 named colors with a hue-approximation fallback. Results are cached
as `ai_recolor_{modelId}_{hex}.png` so each model+color pair is generated once.

**B. Replicate** (`D:\Vinyl Stuff\Garment_color_changer\`) — Grounded SAM segments the garment,
SDXL inpainting recolors just that mask. ~$0.004/image. Also cached.

> ⚠️ **`Garment_color_changer/` is gitignored** (`.gitignore` line 147, because of its `.env`).
> It exists **only** on the D: drive of this laptop. Nothing is backing it up. It holds
> `recolor.js`, `server.js` (HTTP service on :3500) and a thorough README.

Practical notes from that README, worth keeping regardless of which path is used:
- Start from **white or light gray** garments — far easier to recolor accurately
- Be specific: "forest green", "burnt orange", "dusty rose" — never bare "green"
- Clean even lighting, simple background, 1024px+

---

## Other scene generators built on the same service

- `metal-print-scenes.js` — 7 room presets (modern living room, home office, bedroom, gallery,
  restaurant/bar, man cave, gaming room). Each prompt explicitly describes the **glossy
  reflective metallic surface catching ambient light** — that detail is what sells "metal print"
  rather than "poster."
- `multiboard-mockup.js`, `modules/product-blank-mockup.js`, `vectorize-service.js`
  (clean/bold/minimal vector styles), `art-modifier.js` (aspect ratio via outpainting, smart crop
  via sharp's `attention` strategy, variations, iterative refinement).

Aspect-ratio logic worth copying: if the current and target ratios differ by more than 0.05, it
**outpaints with Gemini** then resizes to exact pixels; if they're close, it skips the AI entirely
and just uses `sharp`. Don't spend a generation on something a resize handles.

---

## The editor UI

`web/mockup-pro/` — vanilla JS, ~89KB, no framework.

- **Fabric.js** 2D canvas: layers, text, flip/center/z-order, per-zone state saved and restored
  when switching zones (`saveCurrentZoneState` / `loadZoneState`)
- **Three.js** 3D preview: loads GLB garments from `models/`, with procedurally generated
  fallback geometry (`createTShirtGeometry`, `createHoodieGeometry`, ...) when a model is missing
- Design texture is captured off the 2D canvas and applied as a **decal** per zone
  (`applyZoneDecal`, `getDecalConfig`) — one decal per zone, removable independently
- Catalog browser is paginated at 24 items

This is the piece most tied to the old server: it fetches the design catalog from API routes and
resolves `/library/...` URLs. The canvas/decal machinery is reusable; the data plumbing is not.

---

## What still works vs. what died with the server

**Works anywhere, needs only a `GEMINI_API_KEY`:**
`nano-banana-ai.js`, `nano-banana-service.js`, `ai-apparel-mockup.js`, `metal-print-scenes.js`,
`vectorize-service.js`, `art-modifier.js`. These take file paths in and write file paths out.
They have no database or server dependency beyond an output directory.

**Assumed the server, needs rework:**
- `paths.js` hardcodes `/mnt/websit`, `/mnt/dbFiles`, `/mnt/stlFiles`, `/mnt/usrdata` — every
  output dir flows from these. Repoint at Windows paths and most things follow.
- `_getModelImagePath()` queries a `human_models` table and resolves `/library/` URLs
- Shopify publishing, the campaign pipelines, the `mockup-pro` catalog fetch

**Gone entirely:** the model photo library and design catalog that lived on the mounted drives.
The code that used them is intact; the images are not.

---

## Porting into Jarvis

The pipeline is a shape Jarvis already has most of:

1. **Model photos** — needed either way. Gemini composites onto a *photo of a person*; it doesn't
   invent the person. Either re-shoot, or generate a consistent model set with ComfyUI on simrig.
2. **Compositing** — currently Gemini (paid API). The local Z-Image/ComfyUI setup on simrig can
   likely replace it, but **the prompt engineering above is model-agnostic and worth carrying over
   verbatim** — the flatten trick and the CRITICAL RULES block are about how image models behave,
   not about Gemini specifically. Start by porting the prompts, then swap the backend.
3. **Paths** — replace `paths.js` with Jarvis config pointing at local storage.
4. **Artwork source** — feeds from the artwork catalog Jarvis is going to build by scanning the
   drives, replacing the lost `catalog.db`.

Keep Gemini as the fallback for anything the local model can't hold quality on — the service
wrapper is small and already written.

---

## Immediate risk

`D:\Vinyl Stuff\Garment_color_changer\` is untracked and unbacked-up. Worth copying somewhere
safe (minus the `.env`) before anything happens to that drive.
