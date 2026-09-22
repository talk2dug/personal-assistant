"""Putting an approved listing on a real storefront.

The step the pipeline never had. Five agents ran a chain that ended in a database row,
and `ShopifyClient` was never instantiated anywhere in the running system. What has to be
right here is mostly about restraint: publishing is the one irreversible act in the
chain, and selling below cost is the specific failure that killed the last store.
"""
import sqlite3

import pytest

from assistant.core import business_db, db as core_db, store_publish


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "store.db")
    core_db.init_db(p)
    business_db.init_business_db(p)
    return p


@pytest.fixture
def art(tmp_path):
    f = tmp_path / "skyline.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return str(f)


_SEQ = iter(range(1, 999))


def _listing(path, art_path=None, price=6.0, product_type="sticker"):
    # A distinct name per call: create_product_concept dedupes by name, so reusing one
    # would silently give three listings the same concept -- and the same artwork.
    # create_product_concept returns (id, was_new).
    concept, _ = business_db.create_product_concept(
        path, 1, name="RVA Skyline Decal %d" % next(_SEQ), product_type=product_type)
    if art_path is not None:
        business_db.create_art_brief(path, 1, title="RVA Skyline", concept_id=concept,
                                     image_prompt="a skyline", media_path=art_path)
    listing = business_db.create_store_listing(
        path, 1, concept_id=concept, title="RVA Skyline Decal",
        description="A decal.", price=price, seo_tags="rva, richmond, decal")
    business_db.update_store_listing(path, 1, listing, status="approved")
    return listing


class FakePrintify:
    """Enough Printify to exercise the path, and a record of what it was asked to do."""

    def __init__(self, costs=None, fail_on=None):
        self.published, self.deleted, self.created = [], [], []
        self._costs = costs if costs is not None else [
            {"variant_id": 1, "title": "2x2", "cost_cents": 153, "price_cents": 600,
             "is_enabled": True}]
        self._fail_on = fail_on

    def shops(self):
        return [{"id": 29022045, "title": "My new store", "sales_channel": "shopify"}]

    def print_providers(self, blueprint_id):
        return [{"id": 1, "title": "Generic Brand"}]

    def blueprints(self):
        return [{"id": 384, "title": "Kiss-Cut Vinyl Decals"},
                {"id": 6, "title": "Unisex Jersey Short Sleeve Tee"}]

    def variants(self, blueprint_id, provider_id):
        return [{"id": 45740 + i, "title": f'{i + 2}" x {i + 2}"'} for i in range(4)]

    def upload_image(self, file_name, url=None, contents_b64=None):
        if self._fail_on == "upload":
            raise RuntimeError("Printify said no")
        assert contents_b64, "the artwork has to actually be sent"
        return {"id": "img-1", "file_name": file_name}

    def create_product(self, shop_id, **kw):
        self.created.append(kw)
        return {"id": "prod-1"}

    def costs(self, shop_id, product_id, enabled_only=True):
        return self._costs

    def publish_product(self, shop_id, product_id):
        self.published.append(product_id)
        return {"ok": True}

    def delete_product(self, shop_id, product_id):
        self.deleted.append(product_id)


class TestItStopsShortOfSelling:
    """Creating is reversible; publishing is the line a stranger can buy across."""

    def test_staging_makes_the_product_but_does_not_publish_it(self, path, art):
        p = FakePrintify()
        out = store_publish.publish_listing(path, 1, _listing(path, art), p)
        assert out["ok"] and out["live"] is False
        assert p.created and p.published == [], "nothing may go on sale by default"

    def test_going_live_is_a_separate_decision(self, path, art):
        p = FakePrintify()
        out = store_publish.publish_listing(path, 1, _listing(path, art), p, live=True)
        assert out["live"] is True and p.published == ["prod-1"]

    def test_a_staged_listing_is_not_counted_as_live_by_the_mission(self, path, art):
        """measure_store requires status published AND an external id. A staged product
        has the id and not the status, which is exactly right: it exists and nobody can
        buy it."""
        from assistant.core import executive

        store_publish.publish_listing(path, 1, _listing(path, art), FakePrintify())
        assert executive.measure_store(path, 1)[0] == 0.0

    def test_going_live_does_count(self, path, art):
        from assistant.core import executive

        store_publish.publish_listing(path, 1, _listing(path, art), FakePrintify(),
                                      live=True)
        assert executive.measure_store(path, 1)[0] == 1.0


class TestSellingBelowCost:
    """Blue Ridge Custom Co listed 40x60 metal prints at -$30.32 and took $19.90 in
    lifetime revenue. Printify only reports cost once the product exists against a
    provider, so this can only be checked after creation."""

    UNDERWATER = [{"variant_id": 9, "title": "40x60", "cost_cents": 4932,
                   "price_cents": 1900, "is_enabled": True}]

    def test_a_product_that_loses_money_is_deleted_not_left_sitting(self, path, art):
        p = FakePrintify(costs=self.UNDERWATER)
        out = store_publish.publish_listing(path, 1, _listing(path, art, price=19.0), p)
        assert out["ok"] is False
        assert p.deleted == ["prod-1"], "leaving it there waits for someone to notice"
        assert p.published == []

    def test_it_says_what_the_loss_actually_is(self, path, art):
        """'It loses money' is not actionable; '$30.32 a sale' is."""
        p = FakePrintify(costs=self.UNDERWATER)
        out = store_publish.publish_listing(path, 1, _listing(path, art, price=19.0), p)
        assert "30.32" in out["why"] and "49.32" in out["why"]

    def test_the_listing_is_not_marked_as_on_the_storefront(self, path, art):
        listing = _listing(path, art, price=19.0)
        store_publish.publish_listing(path, 1, listing, FakePrintify(costs=self.UNDERWATER))
        assert not business_db.get_store_listing(path, 1, listing)["external_id"]

    def test_it_reads_the_field_names_the_client_actually_returns(self):
        """This guard silently read None from cost/price on its first live run and
        concluded a $6 sticker was fine -- which it was, by luck. A check that cannot
        fail loudly is not a check."""
        assert store_publish.underwater(
            [{"variant_id": 1, "cost_cents": 4932, "price_cents": 1900}])
        assert store_publish.underwater(
            [{"variant_id": 1, "cost": 4932, "price": 1900}])

    def test_a_variant_nobody_can_buy_does_not_block_the_product(self):
        assert store_publish.underwater(
            [{"variant_id": 1, "cost_cents": 4932, "price_cents": 1900,
              "is_enabled": False}]) == []


class TestWhenItCannotProceed:
    def test_a_concept_with_no_rendered_art_says_so(self, path):
        out = store_publish.publish_listing(path, 1, _listing(path, None), FakePrintify())
        assert out["ok"] is False and "has not made the picture" in out["why"]

    def test_art_recorded_but_missing_from_disk_says_that_instead(self, path, tmp_path):
        out = store_publish.publish_listing(
            path, 1, _listing(path, str(tmp_path / "gone.png")), FakePrintify())
        assert "not on disk" in out["why"]

    def test_a_listing_with_no_price_is_not_given_away(self, path, art):
        out = store_publish.publish_listing(
            path, 1, _listing(path, art, price=0), FakePrintify())
        assert out["ok"] is False and "giveaway" in out["why"]

    def test_a_listing_already_on_the_storefront_is_left_alone(self, path, art):
        listing = _listing(path, art)
        p = FakePrintify()
        store_publish.publish_listing(path, 1, listing, p)
        again = store_publish.publish_listing(path, 1, listing, p)
        assert again["why"] == "already on the storefront"
        assert len(p.created) == 1, "a second run must not make a duplicate product"

    def test_an_api_failure_is_reported_not_raised(self, path, art):
        out = store_publish.publish_listing(path, 1, _listing(path, art),
                                            FakePrintify(fail_on="upload"))
        assert out["ok"] is False and "Printify said no" in out["why"]


class TestARunOverTheBacklog:
    def test_one_blocked_listing_does_not_stop_the_others(self, path, art):
        """The point of running this over a backlog is finding out which are ready. A
        batch that halts on the first concept missing its artwork answers nothing."""
        _listing(path, None)                 # no art
        _listing(path, art)
        _listing(path, art)
        out = store_publish.publish_approved(path, 1, FakePrintify(), limit=10)
        assert len(out["made"]) == 2 and len(out["blocked"]) == 1

    def test_it_records_where_each_product_went(self, path, art):
        listing = _listing(path, art)
        store_publish.publish_approved(path, 1, FakePrintify(), live=True)
        row = business_db.get_store_listing(path, 1, listing)
        assert row["external_id"] == "prod-1" and row["channel"] == "shopify"

    def test_staged_products_are_labelled_as_staged(self, path, art):
        listing = _listing(path, art)
        store_publish.publish_approved(path, 1, FakePrintify())
        assert business_db.get_store_listing(path, 1, listing)["channel"] == "printify-staged"


class TestChoosingTheShop:
    def test_it_prefers_the_shop_wired_to_a_sales_channel(self, path):
        class TwoShops(FakePrintify):
            def shops(self):
                return [{"id": 1, "sales_channel": "custom_integration"},
                        {"id": 2, "sales_channel": "shopify"}]

        assert store_publish.resolve_shop(path, TwoShops()) == 2

    def test_an_account_with_no_shop_says_so(self, path):
        class NoShops(FakePrintify):
            def shops(self):
                return []

        with pytest.raises(store_publish.NotReady):
            store_publish.resolve_shop(path, NoShops())


class TestTheArtwork:
    def test_the_image_is_actually_uploaded_and_placed(self, path, art):
        p = FakePrintify()
        store_publish.publish_listing(path, 1, _listing(path, art), p)
        areas = p.created[0]["print_areas"]
        placed = areas[0]["placeholders"][0]["images"][0]
        assert placed["id"] == "img-1"
        assert placed["x"] == 0.5 and placed["y"] == 0.5

    def test_every_enabled_variant_carries_the_listing_price_in_cents(self, path, art):
        p = FakePrintify()
        store_publish.publish_listing(path, 1, _listing(path, art, price=6.0), p)
        assert {v["price"] for v in p.created[0]["variants"]} == {600}

    def test_the_newest_rendered_brief_wins(self, path, tmp_path):
        """A concept re-rendered after he rejected the first look should print the
        second one."""
        old = tmp_path / "old.png"; old.write_bytes(b"old")
        new = tmp_path / "new.png"; new.write_bytes(b"new")
        listing = _listing(path, str(old))
        concept = business_db.get_store_listing(path, 1, listing)["concept_id"]
        business_db.create_art_brief(path, 1, title="again", concept_id=concept,
                                     media_path=str(new))
        assert store_publish._art_for(
            path, 1, business_db.get_store_listing(path, 1, listing)) == str(new)
