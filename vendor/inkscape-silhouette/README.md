# inkscape-silhouette (vendored)

**This directory is third-party GPL-2.0 code. It is not covered by this repository's own
license, and nothing here should be treated as Jarvis code.** Everything else under
`vendor/` and outside it is separate; the boundary is this directory.

## What this is

A working USB driver for the **Silhouette Cameo** cutting plotter — the machine protocol,
the media/mat tables, and the travel-optimisation path strategies.

| | |
|---|---|
| Upstream project | inkscape-silhouette |
| Upstream URL | https://github.com/fablabnbg/inkscape-silhouette |
| Version | **1.29** (from `sendto_silhouette.py.__version__`) |
| Primary author | Jürgen (Juergen) Weigert `<juergen@fabmail.org>` — "and contributors" |
| Other copyright holders | `jw@suse.de` (2013, 2014), `juewei@fabmail.org` (2016), Alexander Wenger (2016), Johann Gail (2017) |
| License | **GPL-2.0** — see `LICENSE` |
| Date vendored | 2026-09-13 |

## Why it is vendored here rather than installed

It was recovered from `D:\Vinyl Stuff\StickerSheets\`, part of the retired *print-station*
system (`talk2dug/CustomVinylRetail`). **That copy was gitignored in its own repository and
the production server that ran it is permanently dead** — so that one local disk was the only
surviving copy in this project's history. Vendoring it makes the preservation durable.

The upstream project is alive and this is not a fork: if you want newer hardware support,
go upstream. This copy exists so the working configuration cannot be lost again.

## Licensing

Both licence notices are preserved **verbatim** in the source files and are reproduced here
for visibility:

- `silhouette/Graphtec.py` — *"(c) 2013,2014 jw@suse.de / (c) 2016 juewei@fabmail.org /
  (c) 2016 Alexander Wenger / (c) 2017 Johann Gail — Distribute under GPLv2 or ask."*
- `sendto_silhouette.py` — *"(C) 2013 jw@suse.de. Licensed under CC-BY-SA-3.0 or GPL-2.0 at
  your choice. (C) 2014 - 2023 juewei@fabmail.org and contributors"*

`sendto_silhouette.py` is dual-licensed and we **take it under GPL-2.0**, so the whole
directory is uniformly GPL-2.0. The full upstream licence text is in `LICENSE`, retrieved
from https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

**No copyright header in any file was stripped, edited or reformatted.**

## What was changed

**No file was modified.** Every `.py` file here is byte-identical to the recovered copy
(verified with `diff -r` at vendoring time).

One directory was **removed**:

- `silhouette/pyusb-1.0.2/` — a vendored copy of pyusb 1.0.2 (BSD-3, a different licence from
  the rest of this tree). Removed in favour of declaring `pyusb` in the project's
  `requirements.txt`.

  This is safe and was verified, not assumed. `Graphtec.py:33` does
  `sys.path.append(.../pyusb-1.0.2)` — an **append**, so it was only ever a *fallback* behind
  whatever pyusb is already installed. A `sys.path` entry pointing at a directory that does
  not exist is silently ignored, which is why the line could be left untouched. Confirmed by
  importing `silhouette.Graphtec` against a pip-installed **pyusb 1.3.1** with the vendored
  directory absent: imports clean, `MEDIA` has 27 entries and `CAMEO_MATS` 7.

  Unlike the driver itself, pyusb 1.0.2 is a public PyPI package and is not at risk of being
  lost. The original still sits untouched on `D:\Vinyl Stuff` regardless.

## What is in here

**Hardware / geometry — the part worth keeping:**

- `silhouette/Graphtec.py` (58 KB) — the machine protocol. `MEDIA`, `CAMEO_MATS`,
  pressure/speed/depth handling, USB device discovery, Cameo 1–4 and Portrait 4 support.
- `silhouette/Strategy.py` (41 KB) — path ordering and travel optimisation (`MatFree`).
- `silhouette/StrategyMinTraveling.py` — nearest-neighbour travel minimisation.
- `silhouette/Geometry.py` (15 KB) — point/segment maths (`dist_sq`, `XY_a`).

`Graphtec`, `Strategy` and `Geometry` import only the standard library plus `usb`, so they
are usable headlessly — no Inkscape, no wxPython.

**Inkscape / wxPython plumbing — kept deliberately, not used by Jarvis:**

- `sendto_silhouette.py` — the Inkscape extension entry point. Needs `inkex`.
- `silhouette/MultiFrame.py` (20 KB) — checked specifically because the size suggested real
  path processing. It is **not**: it imports `wx`, `ultimatelistctrl`, `ScrolledPanel` and
  `PyEmbeddedImage`, and is the multi-colour-job GUI.
- `silhouette/ColorSeparation.py`, `Dialog.py` — wxPython dialogs.
- `silhouette/beutil.py`, `convert2dashes.py`, `read_dump.py` — helpers; the latter two are
  imported by `sendto_silhouette.py`, `beutil.py` by nothing in this tree.

Nothing outside this directory imports `MultiFrame`, `ColorSeparation`, `Dialog` or
`beutil`. They were kept anyway — the whole tree is well under a megabyte, and the standing
instruction on this salvage was that losing something is the expensive outcome, not keeping it.

## How Jarvis uses it

`assistant/core/vinyl_cutter.py` puts this directory on `sys.path` lazily to read the
calibration constants out of `Graphtec.MEDIA` / `CAMEO_MATS`.

**Jarvis does not drive the blade.** Sending a job to the hardware is a deliberate manual
step; there is no chat tool that cuts.
