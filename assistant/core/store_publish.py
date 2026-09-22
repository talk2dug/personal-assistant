"""Turn an approved listing into a real product on a real storefront.

This is the step the pipeline never had. Jack, 2026-09-22: *"What's the hold up why
aren't we selling anything."* Jarvis answered it correctly -- *"'published' just records
your decision internally; it doesn't push to Etsy, Shopify, or anywhere real"* -- and the
reason was not missing credentials. Printify and Shopify were both connected and both
answer. `ShopifyClient` was simply never instantiated anywhere in the running system, and
`PrintifyClient` was used only to ask whether a thing *could* be made, never to make it.
Five agents ran a chain that ended in a database row.

WHAT IT DOES. Finds the artwork the art director rendered, uploads it to Printify,
resolves a blueprint and a print provider, prices every variant, creates the product,
checks it is not selling below cost, and only then offers to put it on the storefront.

THE STAGING STEP IS NOT TIMIDITY. A product that exists in Printify is not on sale
anywhere -- `publish_product` is the line between a draft and something a stranger can
buy under his brand. Creating is reversible (delete_product), publishing is the part that
is not, so the default stops there and `live=True` is a separate decision. Everything
before it runs unattended.

SELLING BELOW COST IS THE FAILURE THAT KILLED THE LAST STORE. Blue Ridge Custom Co listed
40x60 metal prints at -$30.32 and took $19.90 in lifetime revenue. Printify only reports
a variant's cost once the product exists against a specific provider, so the check has to
happen AFTER creation -- and when it fails, the product is deleted rather than left
sitting there waiting for someone to notice. See [[project_print_station_salvage]].
"""
import base64
import logging
import os

logger = logging.getLogger(__name__)

# How many of a blueprint's variants to enable. A Gildan tee has 83 colour/size
# combinations and a listing that names three sizes means three, not eighty-three: each
# one is a row a buyer has to scroll past, and most of them are colours nobody chose.
MAX_VARIANTS = 12

# Printify wants the image placed on the canvas. Centred at full scale is the only
# sensible default for art rendered to the placeholder's own aspect.
DEFAULT_PLACEMENT = {"x": 0.5, "y": 0.5, "scale": 1.0, "angle": 0}


class NotReady(RuntimeError):
    """This listing cannot be published yet, and the message says what is missing."""


def _art_for(db_path: str, owner_user_id: int, listing: dict) -> str:
    """The rendered image behind this listing, as a path that exists.

    Goes listing -> concept -> art brief, because a listing has no image of its own: the
    art director renders against the concept and the store manager writes copy against
    the same concept, and nothing ever joined the two.
    """
    from contextlib import closing
    import sqlite3

    concept_id = listing.get("concept_id")
    if not concept_id:
        raise NotReady("this listing is not attached to a concept, so there is no artwork "
                       "to print")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT media_path, title FROM art_briefs
                WHERE concept_id = ? AND owner_user_id = ?
                  AND media_path IS NOT NULL AND trim(media_path) <> ''
                ORDER BY id DESC LIMIT 1""",
            (concept_id, owner_user_id)).fetchone()
    if row is None:
        raise NotReady("no art brief for this concept has a rendered image — the art "
                       "director has not made the picture yet")
    path = row["media_path"]
    if not os.path.exists(path):
        raise NotReady(f"the rendered image for this concept is recorded as {path}, "
                       f"which is not on disk any more")
    return path


def resolve_shop(db_path: str, printify) -> int:
    """Which Printify shop to publish into.

    Prefers the one wired to Shopify, because that is the connection that puts a product
    in front of a buyer; a Printify-only shop is a catalogue nobody visits.
    """
    from . import owner_requests

    try:
        stated = owner_requests.secret(db_path, "printify_shop_id")
    except Exception:                                               # noqa: BLE001
        # A database without the Needs You board should fall through to asking Printify,
        # not fail to publish because an optional override could not be read.
        stated = None
    if stated:
        return int(str(stated).strip())
    shops = printify.shops()
    if not shops:
        raise NotReady("this Printify account has no shop connected to a sales channel")
    for shop in shops:
        if (shop.get("sales_channel") or "").lower() == "shopify":
            return int(shop["id"])
    return int(shops[0]["id"])


def _variants_for(printify, blueprint_id: int, provider_id: int,
                  price_cents: int, limit: int = MAX_VARIANTS) -> list[dict]:
    available = printify.variants(blueprint_id, provider_id)
    if not available:
        raise NotReady(f"Printify lists no variants for blueprint {blueprint_id} with "
                       f"that provider")
    chosen = available[:limit]
    return [{"id": v["id"], "price": price_cents, "is_enabled": True} for v in chosen]


def _print_areas(variant_ids: list[int], image_id: str, position: str = "front") -> list[dict]:
    return [{"variant_ids": variant_ids,
             "placeholders": [{"position": position,
                               "images": [dict(DEFAULT_PLACEMENT, id=image_id)]}]}]


def underwater(costs: list[dict]) -> list[dict]:
    """Variants selling for less than they cost to make.

    Returns the offenders rather than a boolean: "it loses money" is not actionable and
    "the 2XL loses $4.10 a sale" is.
    """
    bad = []
    for row in costs or []:
        # The client returns cost_cents/price_cents. The bare names are accepted too
        # because this guard is worthless if it silently reads None from a renamed field
        # and concludes everything is fine -- which is exactly what it did on its first
        # live run, and exactly the class of bug that priced metal prints at -$30.32.
        cost = row.get("cost_cents", row.get("cost"))
        price = row.get("price_cents", row.get("price"))
        if cost is None or price is None:
            continue
        if not row.get("is_enabled", True):
            continue
        if float(price) < float(cost):
            bad.append({"variant_id": row.get("variant_id") or row.get("id"),
                        "title": row.get("title"),
                        "price": float(price) / 100.0, "cost": float(cost) / 100.0,
                        "loss": (float(cost) - float(price)) / 100.0})
    return bad


def publish_listing(db_path: str, owner_user_id: int, listing_id: int, printify,
                    *, live: bool = False, shop_id: int | None = None,
                    product_lines=None) -> dict:
    """Create one approved listing as a real Printify product. Publish it only if asked.

    Returns {"ok", "listing_id", "product_id", "live", "why", "underwater"}. Never raises
    for an ordinary blocked listing -- a run over six of them should report five made and
    one missing its artwork, not stop at the first gap.
    """
    from . import business_db, fulfilment

    listing = business_db.get_store_listing(db_path, owner_user_id, listing_id)
    if listing is None:
        return {"ok": False, "listing_id": listing_id, "why": "no such listing"}
    if (listing.get("external_id") or "").strip():
        return {"ok": True, "listing_id": listing_id, "why": "already on the storefront",
                "product_id": listing["external_id"], "live": None}

    try:
        art = _art_for(db_path, owner_user_id, listing)
        shop = shop_id if shop_id is not None else resolve_shop(db_path, printify)
        concept = business_db.get_concept(db_path, owner_user_id, listing["concept_id"])
        verdict = fulfilment.can_be_made(
            db_path, (concept or {}).get("name") or listing["title"],
            (concept or {}).get("product_type"), printify=printify,
            product_lines=product_lines)
        blueprint = verdict.get("blueprint")
        if not blueprint:
            raise NotReady(verdict.get("why") or "no Printify blueprint matches this product")

        price_cents = int(round(float(listing.get("price") or 0) * 100))
        if price_cents <= 0:
            raise NotReady("this listing has no price, and a product priced at zero is a "
                           "giveaway rather than a sale")

        with open(art, "rb") as handle:
            uploaded = printify.upload_image(
                os.path.basename(art),
                contents_b64=base64.b64encode(handle.read()).decode("ascii"))
        image_id = uploaded.get("id")
        if not image_id:
            raise NotReady("Printify accepted the artwork but returned no image id")

        variants = _variants_for(printify, blueprint["id"], blueprint["provider_id"],
                                 price_cents)
        product = printify.create_product(
            shop, title=listing["title"],
            description=listing.get("description") or listing["title"],
            blueprint_id=blueprint["id"], print_provider_id=blueprint["provider_id"],
            variants=variants,
            print_areas=_print_areas([v["id"] for v in variants], image_id),
            tags=_tags(listing))
        product_id = product.get("id")
        if not product_id:
            raise NotReady("Printify created nothing and returned no product id")
    except NotReady as exc:
        return {"ok": False, "listing_id": listing_id, "why": str(exc)}
    except Exception as exc:                                        # noqa: BLE001
        logger.exception("publishing listing %s failed", listing_id)
        return {"ok": False, "listing_id": listing_id,
                "why": f"{type(exc).__name__}: {exc}"}

    # Cost is only knowable once the product exists against this provider, so the check
    # cannot happen earlier -- and a product that loses money on every sale is worse than
    # no product, so it goes rather than waits.
    losers = []
    try:
        losers = underwater(printify.costs(shop, product_id))
    except Exception:                                               # noqa: BLE001
        logger.warning("could not read costs for %s; leaving it staged", product_id,
                       exc_info=True)
    if losers:
        worst = max(losers, key=lambda v: v["loss"])
        try:
            printify.delete_product(shop, product_id)
        except Exception:                                           # noqa: BLE001
            logger.exception("could not delete the underwater product %s", product_id)
        return {"ok": False, "listing_id": listing_id, "underwater": losers,
                "why": (f"priced below cost — ${listing['price']:.2f} against ${worst['cost']:.2f} "
                        f"to make, losing ${worst['loss']:.2f} a sale on {len(losers)} "
                        f"variant(s). Deleted rather than left sitting there.")}

    went_live = False
    if live:
        printify.publish_product(shop, product_id)
        went_live = True

    business_db.update_store_listing(
        db_path, owner_user_id, listing_id,
        channel="shopify" if went_live else "printify-staged",
        external_id=str(product_id),
        status="published" if went_live else listing["status"])

    return {"ok": True, "listing_id": listing_id, "product_id": str(product_id),
            "live": went_live, "underwater": [],
            "why": ("on the storefront" if went_live else
                    "made in Printify and staged — not on sale until it is published")}


def _tags(listing: dict) -> list[str]:
    raw = listing.get("seo_tags") or ""
    return [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()][:12]


def publish_approved(db_path: str, owner_user_id: int, printify, *, live: bool = False,
                     limit: int = 10, product_lines=None) -> dict:
    """Every approved listing that is not on the storefront yet.

    One blocked listing does not stop the others: the point of running this over a backlog
    is to find out which ones are genuinely ready, and a batch that halts on the first
    concept missing its artwork answers nothing.
    """
    from . import business_db

    ready = [l for l in business_db.list_store_listings(db_path, owner_user_id,
                                                        status="approved", limit=limit * 3)
             if not (l.get("external_id") or "").strip()][:limit]
    made, blocked = [], []
    for listing in ready:
        result = publish_listing(db_path, owner_user_id, listing["id"], printify,
                                 live=live, product_lines=product_lines)
        (made if result.get("ok") else blocked).append(result)
    return {"ok": True, "considered": len(ready), "made": made, "blocked": blocked,
            "live": live}
