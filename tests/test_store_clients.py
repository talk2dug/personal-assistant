"""The Printify and Shopify clients.

No network: every call is served by a fake transport, so these pin behaviour rather than
re-testing two third-party APIs. What is worth pinning is the handful of things that would
each produce a pipeline that looks healthy while selling nothing -- a throttle ignored, a
GraphQL mutation that failed inside a 200 response, a product published to no channel, a
credential read once at import instead of per call.
"""
import json
import urllib.error
import urllib.request

import pytest

from assistant.core import owner_requests, printify_client, shopify_client, store_http
from assistant.core.store_http import StoreAPIError, StoreCredentialMissing


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "store.db")
    owner_requests.init_owner_requests(path)
    return path


def supply(db, name, value, kind="secret"):
    rid = owner_requests.raise_request(db, 1, title=name, kind=kind, name=name)
    owner_requests.provide(db, 1, rid, value)
    return rid


class FakeHTTP:
    """Stands in for urllib. Records every request and replies from a scripted queue."""

    def __init__(self):
        self.calls = []
        self.replies = {}
        self.sequence = []

    def reply(self, fragment, body, status=200, headers=None, times=1):
        self.replies.setdefault(fragment, []).extend(
            [(status, body, headers or {})] * times)

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append({"url": url, "method": req.get_method(),
                           "headers": dict(req.headers),
                           "body": json.loads(req.data) if req.data else None})
        for fragment, queued in self.replies.items():
            if fragment in url and queued:
                status, body, headers = queued.pop(0)
                if status >= 400:
                    raise urllib.error.HTTPError(url, status, "err", headers,
                                                 _FakeBody(json.dumps(body)))
                return _FakeResponse(body, headers)
        raise AssertionError(f"unscripted request: {req.get_method()} {url}")


class _FakeBody:
    def __init__(self, text):
        self._text = text.encode()

    def read(self, n=None):
        return self._text

    def close(self):
        # urllib hands the error body to a temp-file wrapper that closes it on GC; without
        # this the teardown raises into pytest as an unraisable-exception warning.
        pass


class _FakeResponse:
    def __init__(self, body, headers):
        self._body = json.dumps(body).encode() if body is not None else b""
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(store_http.time, "sleep", lambda s: None)   # no real waiting
    store_http._LAST_CALL.clear()
    return fake


class TestCredentialsComeFromTheBoard:
    def test_a_missing_credential_names_the_board_entry(self, db, http):
        with pytest.raises(StoreCredentialMissing) as exc:
            printify_client.PrintifyClient(db).shops()
        assert exc.value.name == "printify_api_key"
        assert "Needs You board" in str(exc.value)
        assert not http.calls, "it must not call the API without a credential"

    def test_a_token_supplied_later_works_without_restarting(self, db, http):
        """The design point: read at USE time. A token pasted at noon has to work on the
        12:05 run, not after a Windows service restart."""
        client = printify_client.PrintifyClient(db)
        with pytest.raises(StoreCredentialMissing):
            client.shops()
        supply(db, "printify_api_key", "tok_abcdef123456")
        http.reply("shops.json", [{"id": 1, "title": "S", "sales_channel": "shopify"}])
        assert client.shops()[0]["id"] == 1
        assert http.calls[0]["headers"]["Authorization"] == "Bearer tok_abcdef123456"

    def test_a_rotated_token_is_picked_up_on_the_next_call(self, db, http):
        supply(db, "printify_api_key", "first_token_1111")
        client = printify_client.PrintifyClient(db)
        http.reply("shops.json", [], times=2)
        client.shops()
        supply(db, "printify_api_key", "second_token_2222")
        client.shops()
        assert http.calls[-1]["headers"]["Authorization"] == "Bearer second_token_2222"

    def test_config_is_only_a_fallback(self, db, http):
        """Values from the board win, so replacing a dead credential is a screen he can
        use rather than a file he has never opened."""
        client = printify_client.PrintifyClient(db, config_token="from_config_999")
        http.reply("shops.json", [], times=2)
        client.shops()
        assert http.calls[-1]["headers"]["Authorization"] == "Bearer from_config_999"
        supply(db, "printify_api_key", "from_board_888")
        client.shops()
        assert http.calls[-1]["headers"]["Authorization"] == "Bearer from_board_888"


class TestThrottling:
    def test_a_429_is_waited_out_exactly_as_instructed(self, db, http, monkeypatch):
        slept = []
        monkeypatch.setattr(store_http.time, "sleep", lambda s: slept.append(s))
        supply(db, "printify_api_key", "tok_abcdef123456")
        http.reply("shops.json", {"error": "rate limited"}, status=429,
                   headers={"Retry-After": "3"})
        http.reply("shops.json", [{"id": 7}])
        assert printify_client.PrintifyClient(db).shops()[0]["id"] == 7
        assert any(abs(s - 3.25) < 0.01 for s in slept), f"waited {slept}, not the 3s it was told"

    def test_a_server_error_is_retried_but_a_refusal_is_not(self, db, http):
        supply(db, "printify_api_key", "tok_abcdef123456")
        http.reply("shops.json", {"e": 1}, status=503)
        http.reply("shops.json", [{"id": 9}])
        assert printify_client.PrintifyClient(db).shops()[0]["id"] == 9

        http.reply("catalog/blueprints.json", {"error": "bad token"}, status=401)
        with pytest.raises(StoreAPIError) as exc:
            printify_client.PrintifyClient(db).blueprints()
        assert exc.value.status == 401


class TestPrintify:
    @pytest.fixture
    def client(self, db, http):
        supply(db, "printify_api_key", "tok_abcdef123456")
        return printify_client.PrintifyClient(db)

    def test_a_disconnected_shop_is_not_treated_as_usable(self, client, http):
        """The exact state the real account is in. Publishing to it succeeds and puts the
        product nowhere, which reads as healthy everywhere else."""
        http.reply("shops.json", [{"id": 1, "title": "My new store",
                                   "sales_channel": "disconnected"}])
        assert client.connected_shop() is None

    def test_a_connected_shop_is_returned(self, client, http):
        http.reply("shops.json", [{"id": 1, "sales_channel": "disconnected"},
                                  {"id": 2, "sales_channel": "shopify"}])
        assert client.connected_shop()["id"] == 2

    def test_blueprints_can_be_searched_by_title_or_brand(self, client, http):
        http.reply("catalog/blueprints.json",
                   [{"id": 145, "title": "Unisex Softstyle T-Shirt", "brand": "Gildan"},
                    {"id": 9, "title": "Ceramic Mug", "brand": "Generic"}])
        assert [b["id"] for b in client.find_blueprints("gildan")] == [145]

    def test_the_print_canvas_is_discoverable_before_art_is_generated(self, client, http):
        """Art made at the wrong aspect is silently cropped by Printify, so the generator
        needs the pixel canvas up front rather than after a bad mockup."""
        http.reply("variants.json", {"variants": [
            {"id": 1, "placeholders": [
                {"position": "back", "width": 100, "height": 100, "decoration_method": "dtg"},
                {"position": "front", "width": 2829, "height": 3300, "decoration_method": "dtg"}]}]})
        assert client.placeholder_size(145, 41) == {
            "width": 2829, "height": 3300, "decoration_method": "dtg"}

    def test_creating_a_product_does_not_publish_it(self, client, http):
        http.reply("products.json", {"id": "p1", "title": "Tee"})
        client.create_product(1, title="Tee", description="d", blueprint_id=145,
                              print_provider_id=41, variants=[], print_areas=[])
        assert len(http.calls) == 1 and http.calls[0]["method"] == "POST"
        assert "publish" not in http.calls[0]["url"]

    def test_upload_needs_something_to_upload(self, client):
        with pytest.raises(ValueError):
            client.upload_image("art.png")


class TestShopify:
    @pytest.fixture
    def client(self, db, http):
        supply(db, "shopify_admin_token", "shpat_abcdef1234567890")
        supply(db, "shopify_shop", "teststore.myshopify.com", kind="text")
        return shopify_client.ShopifyClient(db)

    def test_selling_channels_exclude_point_of_sale(self, client, http):
        """A dropship catalogue has no till. Publishing to POS clutters a register with
        items that can never be rung up in person."""
        http.reply("publications.json", {"publications": [
            {"id": 1, "name": "Online Store"}, {"id": 2, "name": "Point of Sale"},
            {"id": 3, "name": "TikTok"}, {"id": 4, "name": "Facebook & Instagram"}]})
        assert client.selling_publication_ids() == [1, 3, 4]

    def test_publishing_targets_each_channel(self, client, http):
        http.reply("publications.json", {"publications": [
            {"id": 1, "name": "Online Store"}, {"id": 3, "name": "TikTok"}]})
        http.reply("graphql.json", {"data": {"publishablePublish": {
            "publishable": {"availablePublicationsCount": {"count": 2}}, "userErrors": []}}},
            times=2)
        assert client.publish_everywhere(555) == {"published": [1, 3], "failed": {}}
        sent = [c["body"]["variables"]["input"][0]["publicationId"]
                for c in http.calls if "graphql" in c["url"]]
        assert sent == ["gid://shopify/Publication/1", "gid://shopify/Publication/3"]

    def test_one_dead_channel_does_not_cost_the_others(self, client, http):
        """The real store has duplicate publications -- four "Facebook & Instagram", three
        "Google & YouTube" -- left by old app installs, and some are surely dead. Batched,
        one stale row fails the whole mutation and the product lands nowhere."""
        http.reply("publications.json", {"publications": [
            {"id": 1, "name": "Online Store"}, {"id": 2, "name": "Meta"},
            {"id": 3, "name": "TikTok"}]})
        ok = {"data": {"publishablePublish": {
            "publishable": {"availablePublicationsCount": {"count": 1}}, "userErrors": []}}}
        http.reply("graphql.json", ok)
        http.reply("graphql.json", {"data": {"publishablePublish": {
            "publishable": None,
            "userErrors": [{"field": "id", "message": "Publication does not exist"}]}}})
        http.reply("graphql.json", ok)
        result = client.publish_everywhere(555)
        assert result["published"] == [1, 3]
        assert "Publication does not exist" in result["failed"][2]

    def test_failing_on_every_channel_is_still_an_error(self, client, http):
        """Partial success is honest; total failure means the product is listed nowhere,
        and that must never be reported as success."""
        http.reply("publications.json", {"publications": [{"id": 1, "name": "TikTok"}]})
        http.reply("graphql.json", {"data": {"publishablePublish": {
            "publishable": None, "userErrors": [{"message": "nope"}]}}})
        with pytest.raises(RuntimeError, match="listed nowhere"):
            client.publish_everywhere(555)

    def test_publishing_to_no_channels_is_refused_rather_than_silently_nowhere(self, client, http):
        """A product published to nothing is invisible while looking perfectly healthy in
        the admin -- the failure this whole pipeline inherited."""
        http.reply("publications.json", {"publications": []})
        with pytest.raises(RuntimeError, match="no selling channels"):
            client.publish_everywhere(555)

    def test_a_rejected_mutation_inside_a_200_is_treated_as_failure(self, client, http):
        """Shopify answers a refused mutation with HTTP 200 and the reason in userErrors.
        A client that only checks the status code reports success for work that did not
        happen -- the worst shape of bug for a pipeline nobody watches."""
        http.reply("graphql.json", {"data": {"someMutation": {
            "userErrors": [{"field": "id", "message": "Product does not exist"}]}}})
        with pytest.raises(RuntimeError, match="Product does not exist"):
            client._graphql("mutation { someMutation { userErrors { message } } }")

    def test_graphql_top_level_errors_are_raised(self, client, http):
        http.reply("graphql.json", {"errors": [{"message": "Access denied"}]})
        with pytest.raises(RuntimeError, match="Access denied"):
            client.published_channels(555)

    def test_it_can_report_where_a_product_is_actually_live(self, client, http):
        http.reply("graphql.json", {"data": {"product": {"resourcePublicationsV2": {"edges": [
            {"node": {"isPublished": True, "publication": {"name": "Online Store"}}},
            {"node": {"isPublished": False, "publication": {"name": "TikTok"}}}]}}}})
        assert client.published_channels(555) == ["Online Store"]

    def test_an_invalid_product_status_is_refused(self, client):
        with pytest.raises(ValueError):
            client.set_product_status(1, "live")

    def test_both_shopify_credentials_are_required(self, db, http):
        supply(db, "shopify_admin_token", "shpat_abcdef1234567890")
        with pytest.raises(StoreCredentialMissing) as exc:
            shopify_client.ShopifyClient(db).shop()
        assert exc.value.name == "shopify_shop"


class TestCostAndMargin:
    """Margin arithmetic, which is the one thing this business has already got wrong.

    The previous store kept cost and price side by side and never subtracted them, and
    shipped 40x60 metal prints at -$30.32.
    """

    @pytest.fixture
    def client(self, db, http):
        supply(db, "printify_api_key", "tok_abcdef123456")
        return printify_client.PrintifyClient(db)

    def test_only_the_variants_we_actually_sell_are_costed(self, client, http):
        """Printify returns EVERY variant the blueprint has -- all 83 colours and sizes of
        a Gildan tee -- not just the few you enabled, and the untouched ones carry
        Printify's own default price rather than yours. Reading that full list produced a
        $24.99 shirt reporting a margin of exactly $0.00 the first time this ran live."""
        http.reply("products/p1.json", {"variants": [
            {"id": 1, "title": "unset", "cost": 1384, "price": 1384, "is_enabled": False},
            {"id": 2, "title": "Black / S", "cost": 1029, "price": 2499, "is_enabled": True}]})
        costs = client.costs(1, "p1")
        assert [c["variant_id"] for c in costs] == [2]
        assert costs[0]["margin_cents"] == 1470

    def test_the_full_list_is_still_available_when_asked_for(self, client, http):
        http.reply("products/p1.json", {"variants": [
            {"id": 1, "cost": 1384, "price": 1384, "is_enabled": False},
            {"id": 2, "cost": 1029, "price": 2499, "is_enabled": True}]})
        assert len(client.costs(1, "p1", enabled_only=False)) == 2

    def test_a_loss_making_variant_reads_as_negative(self, client, http):
        http.reply("products/p1.json", {"variants": [
            {"id": 1, "cost": 1132, "price": 999, "is_enabled": True}]})
        assert client.costs(1, "p1")[0]["margin_cents"] == -133

    def test_ranking_providers_deletes_every_probe_it_creates(self, client, http):
        """It prices providers by really creating a product against each. Leaving those
        behind would fill the shop with junk that could be published by accident."""
        http.reply("print_providers.json", [{"id": 41, "title": "Duplium"},
                                            {"id": 39, "title": "SwiftPOD"}])
        http.reply("variants.json", {"variants": [{"id": 100, "options": {}}]}, times=2)
        http.reply("products.json", {"id": "probe1"})
        http.reply("products/probe1.json",
                   {"variants": [{"id": 100, "cost": 1384, "price": 2499, "is_enabled": True}]})
        http.reply("products.json", {"id": "probe2"})
        http.reply("products/probe2.json",
                   {"variants": [{"id": 100, "cost": 1029, "price": 2499, "is_enabled": True}]})
        ranked = client.cheapest_provider(1, 145, price_cents=2499, image_id="img1")
        assert [r["provider"] for r in ranked] == ["SwiftPOD", "Duplium"], "cheapest first"
        deletes = [c["url"] for c in http.calls if c["method"] == "DELETE"]
        assert any("probe1" in d for d in deletes) and any("probe2" in d for d in deletes)

    def test_a_provider_that_cannot_be_priced_does_not_sink_the_comparison(self, client, http):
        http.reply("print_providers.json", [{"id": 41, "title": "Broken"},
                                            {"id": 39, "title": "SwiftPOD"}])
        http.reply("variants.json", {"variants": [{"id": 100, "options": {}}]}, times=2)
        http.reply("products.json", {"error": "blueprint unavailable"}, status=400)
        http.reply("products.json", {"id": "probe2"})
        http.reply("products/probe2.json",
                   {"variants": [{"id": 100, "cost": 1029, "price": 2499, "is_enabled": True}]})
        ranked = client.cheapest_provider(1, 145, price_cents=2499, image_id="img1")
        assert [r["provider"] for r in ranked] == ["SwiftPOD"]
