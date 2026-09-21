"""Shopify — the storefront, and the hub every sales channel hangs off.

What this client is *for* is narrower than it first appears. Printify creates the products
(see printify_client), so nothing here needs to build a listing from scratch. The job that
remains is the one that quietly decided the last store's fate: making sure a product that
exists is actually **visible**, everywhere it should be.

That matters because this store already carries live channels for Facebook & Instagram,
Meta, TikTok, Google & YouTube, Shop, POS and the Online Store. Products reach Facebook
Marketplace and TikTok Shop *through* those channels rather than through separate
integrations -- which is why publishing to them is a first-class operation here and why
there is no TikTok or Meta catalogue client in this codebase at all. A product that is
created but published to nothing is invisible while looking completely healthy in the
admin, and "looked healthy, sold nothing" is the exact failure this pipeline inherited.

Credentials resolve at use time from the owner's board (see store_http), so a rotated
token takes effect on the next call rather than the next restart.
"""
import logging

from .store_http import request_json, require_secret

logger = logging.getLogger(__name__)

SERVICE = "Shopify"
API_VERSION = "2024-10"

# Board entries this client is blocked on when unconfigured.
TOKEN_NAME = "shopify_admin_token"
SHOP_NAME = "shopify_shop"

# The Basic plan refills a 40-call bucket at 2 calls/second. Pacing at ~1.8/s stays under
# the leak rate, so a long job never trips the bucket rather than trip-and-recovering.
INTERVAL_S = 0.55

# Channels whose absence means a product is invisible where the money is. Matched on the
# publication name because publication ids differ per store and would make this file
# store-specific.
SELLING_CHANNELS = ("Online Store", "Shop", "Facebook & Instagram", "Meta", "TikTok",
                    "Google & YouTube")


class ShopifyClient:
    def __init__(self, db_path: str, config_token: str | None = None,
                 config_shop: str | None = None):
        self.db_path = db_path
        self._config_token = config_token
        self._config_shop = config_shop

    def _creds(self) -> tuple:
        return (require_secret(self.db_path, TOKEN_NAME, SERVICE, self._config_token),
                require_secret(self.db_path, SHOP_NAME, SERVICE, self._config_shop))

    def _rest(self, path: str, method: str = "GET", body: dict | None = None):
        token, shop = self._creds()
        data, headers = request_json(
            SERVICE, f"https://{shop}/admin/api/{API_VERSION}/{path}",
            method=method, body=body, min_interval_s=INTERVAL_S,
            headers={"X-Shopify-Access-Token": token, "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": "jarvis-store"})
        return data, headers

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """GraphQL, with userErrors treated as failures.

        Shopify answers a rejected mutation with HTTP 200 and the reason buried in
        `userErrors`, so a client that only checks the status code reports success for
        work that did not happen -- the single most dangerous shape of bug for a pipeline
        nobody is watching.
        """
        token, shop = self._creds()
        data, _ = request_json(
            SERVICE, f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
            method="POST", body={"query": query, "variables": variables or {}},
            min_interval_s=INTERVAL_S,
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json",
                     "User-Agent": "jarvis-store"})
        if data.get("errors"):
            raise RuntimeError(f"Shopify GraphQL error: {str(data['errors'])[:300]}")
        payload = data.get("data") or {}
        for value in payload.values():
            errors = (value or {}).get("userErrors") if isinstance(value, dict) else None
            if errors:
                raise RuntimeError(f"Shopify rejected the mutation: {str(errors)[:300]}")
        return payload

    # --- store -----------------------------------------------------------------

    def shop(self) -> dict:
        data, _ = self._rest("shop.json")
        return data["shop"]

    def product_count(self) -> int:
        data, _ = self._rest("products/count.json")
        return data["count"]

    def products(self, limit: int = 50, status: str | None = None) -> list[dict]:
        query = f"products.json?limit={min(limit, 250)}"
        if status:
            query += f"&status={status}"
        data, _ = self._rest(query)
        return data.get("products", [])

    def find_product_by_title(self, title: str) -> dict | None:
        for product in self.products(limit=250):
            if (product.get("title") or "").strip().lower() == title.strip().lower():
                return product
        return None

    # --- sales channels --------------------------------------------------------

    def publications(self) -> list[dict]:
        data, _ = self._rest("publications.json")
        return data.get("publications", [])

    def selling_publication_ids(self) -> list[int]:
        """Every channel a product should normally be on.

        Point of Sale is deliberately excluded: this is a dropship catalogue with no
        physical till, and publishing to POS clutters the register with items that can
        never be rung up in person.
        """
        return [p["id"] for p in self.publications()
                if (p.get("name") or "") in SELLING_CHANNELS]

    def publish_everywhere(self, product_id: int,
                           publication_ids: list[int] | None = None) -> dict:
        """Publish one product to every selling channel. Returns what actually worked.

        This is the step that turns a created product into a visible one. Printify pushes
        products into the Online Store only, so without this a listing exists, looks
        correct in the admin, and is absent from Facebook Marketplace and TikTok Shop --
        the two places the volume is supposed to come from.

        Published one channel at a time on purpose. The real store carries **duplicate
        publications** -- four "Facebook & Instagram", three "Google & YouTube" -- left
        behind by old app installs, and at least some are certainly dead. In a single
        batch mutation one dead publication fails the whole call, so the product lands
        nowhere; the safest-looking code would be the one that loses every channel over a
        stale row. Partial success is the honest outcome here, so the failures are
        returned rather than raised, and only a total failure is an error.
        """
        ids = publication_ids if publication_ids is not None else self.selling_publication_ids()
        if not ids:
            raise RuntimeError(
                "no selling channels found on this store -- publishing would put the "
                "product nowhere. Check the sales channels in the Shopify admin.")

        published, failed = [], {}
        for pid in ids:
            try:
                self._graphql(
                    """mutation publish($id: ID!, $input: [PublicationInput!]!) {
                           publishablePublish(id: $id, input: $input) {
                               publishable { availablePublicationsCount { count } }
                               userErrors { field message }
                           }
                       }""",
                    {"id": f"gid://shopify/Product/{product_id}",
                     "input": [{"publicationId": f"gid://shopify/Publication/{pid}"}]})
                published.append(pid)
            except Exception as exc:                 # noqa: BLE001 - reported, not swallowed
                failed[pid] = str(exc)[:200]
                logger.warning("product %s could not be published to publication %s: %s",
                               product_id, pid, exc)

        if not published:
            raise RuntimeError(
                f"product {product_id} could not be published to ANY of {len(ids)} channels; "
                f"it is listed nowhere. Errors: {failed}")
        return {"published": published, "failed": failed}

    def published_channels(self, product_id: int) -> list[str]:
        """Which channels a product is actually live on -- the check, not the intent."""
        payload = self._graphql(
            """query channels($id: ID!) {
                   product(id: $id) {
                       resourcePublicationsV2(first: 25) {
                           edges { node { isPublished publication { name } } }
                       }
                   }
               }""",
            {"id": f"gid://shopify/Product/{product_id}"})
        product = payload.get("product") or {}
        edges = (product.get("resourcePublicationsV2") or {}).get("edges", [])
        return [e["node"]["publication"]["name"] for e in edges if e["node"].get("isPublished")]

    # --- orders ----------------------------------------------------------------

    def orders(self, status: str = "any", limit: int = 50) -> list[dict]:
        data, _ = self._rest(f"orders.json?status={status}&limit={min(limit, 250)}")
        return data.get("orders", [])

    def order_count(self, status: str = "any") -> int:
        data, _ = self._rest(f"orders/count.json?status={status}")
        return data["count"]

    # --- maintenance -----------------------------------------------------------

    def set_product_status(self, product_id: int, status: str) -> dict:
        if status not in ("active", "draft", "archived"):
            raise ValueError("status must be active, draft or archived")
        data, _ = self._rest(f"products/{product_id}.json", method="PUT",
                             body={"product": {"id": product_id, "status": status}})
        return data["product"]

    def delete_product(self, product_id: int) -> None:
        self._rest(f"products/{product_id}.json", method="DELETE")
