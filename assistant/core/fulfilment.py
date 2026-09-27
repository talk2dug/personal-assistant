"""Can this actually be made? Asked before anything is designed.

Jack: *"before they decide to make a product, they need to make sure the fulfiller can
actually make it.... Tumblers stickers whatever... if it can't be made there's no point
going through the process."*

He is right about the cost. A concept that cannot be fulfilled still consumes a GPU render,
a listing write-up, social copy and a slot in his one-a-day rate — and then fails at the
only step that was ever going to tell us, which is the publish. So the check moves to the
front.

Two different questions, because there are two pipelines:

  * **automated** (print-on-demand) — can Printify make it? That is answerable against the
    real catalogue: a blueprint has to exist AND have at least one print provider behind
    it. A blueprint with no provider is a catalogue entry nobody actually produces.
  * **local** — can *he* make it, on the equipment he owns? That is his configured product
    lines, nothing cleverer. Jarvis does not get to decide his workshop grew a UV printer.

**This fails open.** If Printify is unreachable or unconfigured, concepts pass with
`checked=False` rather than being blocked. Replacing "we made something unmakeable" with
"we made nothing because an API was down" is not an improvement, and the pipeline had just
been unstuck from exactly that kind of silent stall. The uncertainty is recorded instead.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from . import db as core_db, pipelines

logger = logging.getLogger(__name__)

CACHE_KEY = "store.printify_catalogue"
CACHE_TTL_HOURS = 24

# What each product_type has to find in the Printify catalogue. Several terms per type
# because the catalogue names things by garment, not by category: searching "apparel"
# returns nothing useful, searching "t-shirt" returns 200 blueprints.
_CATALOGUE_TERMS = {
    "apparel": ("t-shirt", "hoodie", "sweatshirt", "tee", "hat", "cap"),
    "sticker": ("sticker", "decal"),
}

# The local side. Keyed to the product_type vocabulary the agents already use, with the
# words that have to appear in one of his configured product lines for it to count.
_LOCAL_TERMS = {
    "laser": ("laser", "engrav", "tumbler", "drinkware"),
    "metal": ("metal", "aluminum", "aluminium"),
    "3d": ("3d", "print", "multiboard", "organization"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def catalogue(db_path: str, printify, *, force: bool = False) -> list[dict]:
    """The Printify blueprint list, cached.

    blueprints() is 2,567 rows on every call, and the feasibility check runs once per
    proposed concept. Without this a single product-creator run would pull the whole
    catalogue four times for no new information.
    """
    if not force:
        raw = core_db.get_setting(db_path, CACHE_KEY)
        if raw:
            try:
                cached = json.loads(raw)
                fetched = datetime.fromisoformat(cached["fetched_at"])
                if _now() - fetched < timedelta(hours=CACHE_TTL_HOURS):
                    return cached["blueprints"]
            except (ValueError, KeyError, TypeError):
                pass                       # a corrupt cache is a refetch, not a crash

    blueprints = printify.blueprints()
    slim = [{"id": b.get("id"), "title": b.get("title"), "brand": b.get("brand")}
            for b in blueprints if b.get("id")]
    core_db.set_setting(db_path, CACHE_KEY, json.dumps(
        {"fetched_at": _now().isoformat(), "blueprints": slim}))
    logger.info("printify catalogue cached: %d blueprints", len(slim))
    return slim


def _search(blueprints: list[dict], term: str, limit: int = 8) -> list[dict]:
    term = term.lower()
    return [b for b in blueprints
            if term in (b.get("title") or "").lower()
            or term in (b.get("brand") or "").lower()][:limit]


def _local_verdict(product_type: str, name: str, product_lines) -> dict:
    """Whether his own workshop makes this."""
    haystack = " ".join(product_lines or []).lower()
    terms = _LOCAL_TERMS.get(product_type, ())
    if not terms:
        return {"ok": False, "checked": True,
                "why": f"'{product_type}' is not a product type this shop makes"}
    if any(t in haystack for t in terms):
        return {"ok": True, "checked": True,
                "why": f"made in-house ({product_type})"}
    return {"ok": False, "checked": True,
            "why": (f"nothing in his product lines covers '{product_type}' — "
                    f"he makes: {'; '.join(product_lines or []) or 'nothing configured'}")}


def can_be_made(db_path: str, name: str, product_type: str | None, *,
                printify=None, product_lines=None) -> dict:
    """Can this concept be produced? {"ok", "checked", "why", "blueprint"}.

    `checked` is False when the question could not be answered at all — see the module
    docstring on why that still passes.
    """
    kind = (product_type or "").strip().lower()
    market = pipelines.default_market(kind)

    if market == "local":
        verdict = _local_verdict(kind, name, product_lines)
        verdict["market"] = market
        verdict["blueprint"] = None
        return verdict

    terms = _CATALOGUE_TERMS.get(kind)
    if not terms:
        return {"ok": False, "checked": True, "market": market, "blueprint": None,
                "why": f"'{kind}' is not a print-on-demand type we know how to source"}

    if printify is None:
        return {"ok": True, "checked": False, "market": market, "blueprint": None,
                "why": "Printify not configured — could not verify, letting it through"}

    try:
        blueprints = catalogue(db_path, printify)
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("could not read the Printify catalogue: %s", exc)
        return {"ok": True, "checked": False, "market": market, "blueprint": None,
                "why": f"could not reach Printify ({type(exc).__name__}) — letting it through"}

    for term in terms:
        for blueprint in _search(blueprints, term):
            try:
                providers = printify.print_providers(blueprint["id"])
            except Exception as exc:                                # noqa: BLE001
                logger.warning("providers for blueprint %s failed: %s", blueprint["id"], exc)
                return {"ok": True, "checked": False, "market": market, "blueprint": None,
                        "why": "could not confirm a print provider — letting it through"}
            # A blueprint with no provider behind it is a catalogue entry nobody makes.
            # Publishing against one fails at the last step, which is the whole thing this
            # check exists to move earlier.
            if providers:
                return {"ok": True, "checked": True, "market": market,
                        "blueprint": {"id": blueprint["id"], "title": blueprint.get("title"),
                                      "provider_id": providers[0].get("id"),
                                      "provider": providers[0].get("title")},
                        "why": (f"Printify makes it: {blueprint.get('title')} "
                                f"via {providers[0].get('title')}")}

    return {"ok": False, "checked": True, "market": market, "blueprint": None,
            "why": f"no Printify blueprint with a print provider matches a {kind}"}


def catalogue_briefing(db_path: str, printify, product_lines=None) -> str:
    """What can actually be made, for the product creator's prompt.

    Rejecting a bad concept after the fact wastes the model call that produced it. Telling
    it up front what the fulfiller stocks is the cheaper half of the same fix.
    """
    lines = ["\n\nWHAT CAN ACTUALLY BE MADE — propose nothing outside this:"]
    lines.append("  In-house (he makes these himself): "
                 + ("; ".join(product_lines or []) or "nothing configured"))

    if printify is None:
        lines.append("  Print-on-demand: Printify is not configured, so apparel and sticker "
                     "concepts cannot be verified. Prefer in-house types until it is.")
        return "\n".join(lines)

    try:
        blueprints = catalogue(db_path, printify)
    except Exception:                                               # noqa: BLE001
        lines.append("  Print-on-demand: could not read the Printify catalogue this run.")
        return "\n".join(lines)

    for kind, terms in _CATALOGUE_TERMS.items():
        found = []
        for term in terms:
            found.extend(b.get("title") for b in _search(blueprints, term, limit=3))
        unique = list(dict.fromkeys(t for t in found if t))[:6]
        if unique:
            lines.append(f"  Print-on-demand {kind}: " + "; ".join(unique))
    lines.append("  A product type not listed above cannot be fulfilled. Do not propose it.")
    return "\n".join(lines)
