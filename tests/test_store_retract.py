"""The automated store's only human control.

Jack, 2026-09-22: *"this is the automated store, i should NOT be approving anything. This
is 100% AI driven... I dont need to aprove but I can retract something meaning remove it
from being sold, but i need to give a reason why so it continues to learn."*

The shape is the opposite of an approval queue, deliberately. An approval queue costs him
attention on every item including the good ones and stops the line whenever he is busy.
This costs him attention only on what he objects to, and the judgement is about a real
product he can see rather than a description of one that does not exist yet.
"""
import pytest

from assistant.core import (business_db, db as core_db, store_policy, store_publish,
                            store_retract)
from tests.test_store_publish import FakePrintify, _listing, art, path  # noqa: F401


class FakeShopify:
    def __init__(self, found=True):
        self._found = found
        self.status_set = []

    def find_product_by_title(self, title):
        return {"id": 99, "title": title} if self._found else None

    def set_product_status(self, product_id, status):
        self.status_set.append((product_id, status))
        return {"ok": True}


class TestHeApprovesNothing:
    def test_the_automated_store_goes_live_by_itself(self, path):
        """A switch defaulted off is an approval queue with extra steps. This shipped
        that way for a few hours on 2026-09-22 and it was wrong."""
        assert store_policy.go_live(path) is True

    def test_it_is_a_brake_he_can_still_pull(self, path):
        store_policy.set_go_live(path, False, reason="pausing while I look at these")
        assert store_policy.go_live(path) is False

    def test_the_brake_is_recorded_with_his_reason(self, path):
        store_policy.set_go_live(path, False, reason="too many stickers")
        entry = store_policy.policy_history(path)[0]
        assert entry["field"] == "go_live" and entry["reason"] == "too many stickers"


class TestPulling:
    def _live(self, path, art_path):
        listing = _listing(path, art_path)
        store_publish.publish_listing(path, 1, listing, FakePrintify(), live=True)
        return listing

    def test_a_pull_needs_a_reason(self, path, art):
        """Without one the team makes the same thing again next week, which is the
        failure the whole mechanism exists to avoid."""
        listing = self._live(path, art)
        out = store_retract.retract(path, 1, listing, "")
        assert out["ok"] is False and "reason" in out["why"]

    def test_the_product_stops_being_for_sale(self, path, art):
        listing = self._live(path, art)
        store_retract.retract(path, 1, listing, "too close to a licensed character")
        assert business_db.get_store_listing(path, 1, listing)["status"] == "delisted"

    def test_it_comes_off_shopify_and_out_of_printify(self, path, art):
        listing = self._live(path, art)
        printify, shopify = FakePrintify(), FakeShopify()
        out = store_retract.retract(path, 1, listing, "wrong for the brand",
                                    printify=printify, shopify=shopify)
        assert shopify.status_set == [(99, "archived")]
        assert printify.deleted == ["prod-1"]
        assert out["ok"] and len(out["did"]) == 2

    def test_shopify_is_archived_not_deleted(self, path, art):
        """An archived product keeps its order history, and something he pulled may still
        have sales to account for."""
        listing = self._live(path, art)
        shopify = FakeShopify()
        store_retract.retract(path, 1, listing, "nope", shopify=shopify)
        assert shopify.status_set[0][1] == "archived"

    def test_a_failure_at_printify_still_leaves_it_unbuyable(self, path, art):
        """Shopify goes first on purpose. If the second step fails, what is left is a
        tidy-up rather than a thing still for sale under his name."""
        class Broken(FakePrintify):
            def delete_product(self, shop_id, product_id):
                raise RuntimeError("Printify is down")

        listing = self._live(path, art)
        shopify = FakeShopify()
        out = store_retract.retract(path, 1, listing, "nope", printify=Broken(),
                                    shopify=shopify)
        assert out["ok"] and shopify.status_set == [(99, "archived")]
        assert business_db.get_store_listing(path, 1, listing)["status"] == "delisted"

    def test_the_mission_stops_counting_it(self, path, art):
        from assistant.core import executive

        listing = self._live(path, art)
        assert executive.measure_store(path, 1)[0] == 1.0
        store_retract.retract(path, 1, listing, "not selling")
        assert executive.measure_store(path, 1)[0] == 0.0


class TestTheReasonTeaches:
    """The recording is worthless without this half. A table nobody reads is a log, and
    the store repeats the mistake at whatever rate it produces."""

    def test_nothing_pulled_adds_nothing_to_a_prompt(self, path):
        assert store_retract.lessons(path, 1) == ""

    def test_what_he_said_reaches_the_prompt_verbatim(self, path, art):
        listing = _listing(path, art)
        store_retract.retract(path, 1, listing, "too close to a licensed character")
        block = store_retract.lessons(path, 1)
        assert "too close to a licensed character" in block

    def test_the_product_type_goes_with_it(self, path, art):
        """'He pulled a sticker for this reason' is a rule; 'he pulled listing 34' is
        not, to a model that cannot look 34 up."""
        listing = _listing(path, art, product_type="sticker")
        store_retract.retract(path, 1, listing, "we have too many of these")
        assert "(sticker)" in store_retract.lessons(path, 1)

    def test_it_is_framed_as_a_rule_not_an_anecdote(self, path, art):
        store_retract.retract(path, 1, _listing(path, art), "off brand")
        block = store_retract.lessons(path, 1)
        assert "not as a one-off" in block
        assert "does not approve anything" in block

    def test_the_newest_pull_is_first(self, path, art):
        store_retract.retract(path, 1, _listing(path, art), "first reason")
        store_retract.retract(path, 1, _listing(path, art), "second reason")
        block = store_retract.lessons(path, 1)
        assert block.index("second reason") < block.index("first reason")

    def test_it_does_not_swamp_the_prompt(self, path, art):
        for i in range(20):
            store_retract.retract(path, 1, _listing(path, art), f"reason {i}")
        assert store_retract.lessons(path, 1).count("\n- ") == store_retract.LESSON_LIMIT

    def test_the_four_agents_that_could_have_prevented_it_read_it(self):
        """The connection, which is the part that is easy to leave out and makes the rest
        pointless. trend_scout and market_finder are deliberately excluded: they look at
        what the world wants, and a pull is a statement about what Jack wants."""
        import inspect

        from assistant.core import agents

        source = inspect.getsource(agents)
        for agent in ("product_creator", "art_director", "store_manager",
                      "social_director"):
            marker = f'read_journal(obsidian, "{agent}")'
            after = source.split(marker, 1)[1][:160]
            assert "store_retract.lessons" in after, agent
