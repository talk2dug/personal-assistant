"""Printify — the fulfiller that actually makes and ships what the store sells.

Chosen over Printful and Gelato for one reason that matters to this business: a blueprint
carries *competing* print providers (the Gildan Softstyle tee has 18), so the same product
can be routed to whichever one prints it cheapest. That is the lever a volume strategy
pulls, and it is why provider selection is a first-class argument here rather than a
default buried in a helper.

Division of labour worth being explicit about, because it is not obvious: **Printify
creates the product in Shopify, not us.** Once a Printify shop is connected to the store,
`publish_product` pushes the listing across with its mockups and variants. So nothing here
writes products into Shopify directly -- shopify_client's job starts afterwards, making
sure what Printify pushed is visible on every sales channel.

Nothing in this module spends money. Creating and publishing a product lists it for sale;
an order is only ever charged when a customer buys, and routing that order to production
is a separate deliberate call.
"""
import logging

from .store_http import request_json, require_secret

logger = logging.getLogger(__name__)

BASE = "https://api.printify.com/v1"
SERVICE = "Printify"

# The board entry this client is blocked on when unconfigured.
TOKEN_NAME = "printify_api_key"

# Printify allows 600 calls/minute overall but only 200 publishes per 30 minutes. Pacing
# every call at ~7/second keeps well inside the general limit; the publish path is slower
# still (see PUBLISH_INTERVAL_S) because that is the one with the punishing ceiling.
INTERVAL_S = 0.15
PUBLISH_INTERVAL_S = 9.5


class PrintifyClient:
    """Read the catalogue, create products, route orders.

    The token is resolved per call rather than stored on the instance, so a token Jack
    supplies (or rotates) through the board takes effect immediately -- see store_http.
    """

    def __init__(self, db_path: str, config_token: str | None = None):
        self.db_path = db_path
        self._config_token = config_token

    def _call(self, path: str, method: str = "GET", body: dict | None = None,
              interval: float = INTERVAL_S):
        token = require_secret(self.db_path, TOKEN_NAME, SERVICE, self._config_token)
        data, _ = request_json(
            SERVICE, f"{BASE}/{path}", method=method, body=body, min_interval_s=interval,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "User-Agent": "jarvis-store"})
        return data

    # --- shops -----------------------------------------------------------------

    def shops(self) -> list[dict]:
        return self._call("shops.json") or []

    def connected_shop(self) -> dict | None:
        """The shop that can actually receive products.

        A Printify account always has a shop row; an *unconnected* one has no storefront
        behind it, so publishing succeeds locally and puts the product precisely nowhere.
        That state reads as healthy in every other view, which is why it is checked by
        name here rather than left for a caller to notice.
        """
        for shop in self.shops():
            if (shop.get("sales_channel") or "").lower() not in ("", "disconnected"):
                return shop
        return None

    # --- catalogue -------------------------------------------------------------

    def blueprints(self) -> list[dict]:
        return self._call("catalog/blueprints.json") or []

    def find_blueprints(self, term: str, limit: int = 10) -> list[dict]:
        term = term.lower()
        hits = [b for b in self.blueprints()
                if term in (b.get("title") or "").lower()
                or term in (b.get("brand") or "").lower()]
        return hits[:limit]

    def print_providers(self, blueprint_id: int) -> list[dict]:
        return self._call(f"catalog/blueprints/{blueprint_id}/print_providers.json") or []

    def variants(self, blueprint_id: int, provider_id: int) -> list[dict]:
        data = self._call(
            f"catalog/blueprints/{blueprint_id}/print_providers/{provider_id}/variants.json")
        return (data or {}).get("variants", [])

    def placeholder_size(self, blueprint_id: int, provider_id: int,
                         position: str = "front") -> dict | None:
        """The pixel canvas a print position expects.

        Art generated at the wrong aspect gets letterboxed or cropped by Printify without
        complaint, so the generator needs these numbers *before* it renders rather than
        after somebody looks at a bad mockup.
        """
        for variant in self.variants(blueprint_id, provider_id):
            for holder in variant.get("placeholders") or []:
                if holder.get("position") == position:
                    return {"width": holder["width"], "height": holder["height"],
                            "decoration_method": holder.get("decoration_method")}
        return None

    # --- artwork + products ----------------------------------------------------

    def upload_image(self, file_name: str, *, url: str | None = None,
                     contents_b64: str | None = None) -> dict:
        """Put artwork in Printify's library and get the id a product references."""
        if not url and not contents_b64:
            raise ValueError("upload_image needs either a url or base64 contents")
        body = {"file_name": file_name}
        body["url" if url else "contents"] = url or contents_b64
        return self._call("uploads/images.json", method="POST", body=body)

    def create_product(self, shop_id: int, *, title: str, description: str,
                       blueprint_id: int, print_provider_id: int,
                       variants: list[dict], print_areas: list[dict],
                       tags: list[str] | None = None) -> dict:
        """Create the product in Printify. It is NOT on sale until publish_product().

        `variants` are {id, price (integer cents), is_enabled}; `print_areas` place
        uploaded images onto positions. Prices are cents on purpose -- Printify rejects
        floats, and a rounding error here is a product that sells below cost, which is
        exactly how the previous store listed 40x60 metal prints at -$30.32.
        """
        body = {"title": title, "description": description, "blueprint_id": blueprint_id,
                "print_provider_id": print_provider_id, "variants": variants,
                "print_areas": print_areas}
        if tags:
            body["tags"] = tags
        return self._call(f"shops/{shop_id}/products.json", method="POST", body=body)

    def product(self, shop_id: int, product_id: str) -> dict:
        return self._call(f"shops/{shop_id}/products/{product_id}.json")

    def costs(self, shop_id: int, product_id: str, enabled_only: bool = True) -> list[dict]:
        """What each variant costs to make, against what it sells for.

        Cost only appears once a product exists against a specific provider, so comparing
        providers on price means creating the product against each and reading this back.

        `enabled_only` defaults True and matters more than it looks: Printify returns
        **every variant the blueprint has** -- all 83 colours and sizes of a Gildan tee --
        not just the handful you enabled, and the ones you never touched carry Printify's
        own default price rather than yours. Averaging or sampling that full list produces
        margin figures that are quietly meaningless; it first showed up here as a $24.99
        shirt reporting a margin of exactly $0.00.

        Margin is computed, never assumed. The previous store kept cost and price side by
        side and never subtracted them, and shipped 40x60 metal prints at -$30.32.
        """
        product = self.product(shop_id, product_id)
        variants = product.get("variants", [])
        if enabled_only:
            variants = [v for v in variants if v.get("is_enabled")]
        return [{"variant_id": v["id"], "title": v.get("title"),
                 "cost_cents": v.get("cost"), "price_cents": v.get("price"),
                 "margin_cents": (v.get("price") or 0) - (v.get("cost") or 0),
                 "is_enabled": bool(v.get("is_enabled"))}
                for v in variants]

    def cheapest_provider(self, shop_id: int, blueprint_id: int, *, price_cents: int,
                          image_id: str, providers: list[dict] | None = None,
                          sample: int = 3) -> list[dict]:
        """Rank providers by what they actually charge, by pricing a real product.

        There is no catalogue endpoint for cost, so this genuinely creates a product per
        provider, reads its cost, and deletes it again. Deliberately explicit rather than
        hidden inside create: it is several API calls per provider and should be a choice
        the caller makes, not a surprise.
        """
        ranked = []
        for prov in (providers or self.print_providers(blueprint_id))[:sample]:
            variants = self.variants(blueprint_id, prov["id"])
            picked = variants[:1]
            if not picked:
                continue
            product = None
            try:
                product = self.create_product(
                    shop_id, title="ZZ cost probe - delete me", description="cost probe",
                    blueprint_id=blueprint_id, print_provider_id=prov["id"],
                    variants=[{"id": v["id"], "price": price_cents, "is_enabled": True}
                              for v in picked],
                    print_areas=[{"variant_ids": [v["id"] for v in picked],
                                  "placeholders": [{"position": "front", "images": [
                                      {"id": image_id, "x": 0.5, "y": 0.5,
                                       "scale": 1, "angle": 0}]}]}])
                rows = self.costs(shop_id, product["id"])
                if rows:
                    ranked.append({"provider_id": prov["id"], "provider": prov["title"],
                                   **rows[0]})
            except Exception as exc:                  # noqa: BLE001 - reported per provider
                logger.warning("could not price provider %s: %s", prov.get("title"), exc)
            finally:
                if product:
                    try:
                        self.delete_product(shop_id, product["id"])
                    except Exception:                 # noqa: BLE001
                        logger.warning("left a cost-probe product behind: %s", product["id"])
        return sorted(ranked, key=lambda r: r["cost_cents"] or 10**9)

    def publish_product(self, shop_id: int, product_id: str) -> dict:
        """Push the product to the connected storefront. Rate-limited hard by Printify."""
        return self._call(
            f"shops/{shop_id}/products/{product_id}/publish.json", method="POST",
            interval=PUBLISH_INTERVAL_S,
            body={"title": True, "description": True, "images": True, "variants": True,
                  "tags": True, "keyFeatures": True, "shipping_template": True})

    def delete_product(self, shop_id: int, product_id: str) -> None:
        self._call(f"shops/{shop_id}/products/{product_id}.json", method="DELETE")

    # --- orders ----------------------------------------------------------------

    def orders(self, shop_id: int, limit: int = 20) -> list[dict]:
        data = self._call(f"shops/{shop_id}/orders.json?limit={limit}")
        return (data or {}).get("data", [])
