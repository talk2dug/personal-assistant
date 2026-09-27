"""The automated pipeline must not wait on him.

He asked for the print-on-demand store to run without his approval, and store_policy was
built to say so -- but nothing ever read it, so every concept queued for approval anyway
and the "fully automated" pipeline sat blocked. These tests pin the wiring, because the
failure is silent: the queue just fills up and every downstream agent reports "no approved
concepts" and looks idle rather than blocked.
"""
import json

import pytest

from assistant.core import agents, business_db, db as core_db, store_policy


class FakeLLM:
    """Returns one concept of each market, so a run exercises both paths."""

    def __init__(self, concepts):
        self.concepts = concepts

    def research(self, prompt, system_prompt=None, timeout=None, **kw):
        return "REASONING: found things.\n\n```json\n" + json.dumps(self.concepts) + "\n```"


class Profile:
    name = "Blue Ridge Custom Co"
    location = "Richmond, VA"
    radius_miles = 60
    product_lines = ["apparel", "laser"]
    notes = ""
    min_fit_score = 50


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "store.db")
    core_db.init_db(path)
    business_db.init_business_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db):
    return core_db.get_user_by_chat_id(db, "111")["id"]


def seed_lead(db, owner, topic="rva skyline"):
    business_db.upsert_trend_lead(db, owner, topic, "web", 90, "a decal", "trending")


def concept(name, product_type, topic="rva skyline"):
    return {"name": name, "product_type": product_type, "description": "d",
            "target_customer": "t", "price_estimate": 20.0,
            "production_notes": "n", "trend_topic": topic}


def statuses(db, owner):
    return {c["name"]: c["status"]
            for c in business_db.list_product_concepts(db, owner, limit=50)}


def pending_titles(db, owner):
    return [i["title"] for i in business_db.list_review_items(db, owner, status="pending")]


class TestAutopublishOn:
    def test_a_print_on_demand_concept_never_reaches_the_queue(self, db, owner):
        """The whole point. This is the pipeline he said should run without him."""
        seed_lead(db, owner)
        llm = FakeLLM([concept("RVA Decal", "sticker")])
        agents.run_product_creator(db, llm, owner, Profile())

        assert statuses(db, owner)["RVA Decal"] == "approved"
        assert pending_titles(db, owner) == [], "nothing should be waiting on him"

    def test_apparel_counts_as_automated_too(self, db, owner):
        seed_lead(db, owner)
        agents.run_product_creator(db, FakeLLM([concept("Youth Tee", "apparel")]), owner, Profile())
        assert statuses(db, owner)["Youth Tee"] == "approved"
        assert pending_titles(db, owner) == []

    def test_a_local_concept_still_advances_but_stays_visible(self, db, owner):
        """Made on his own equipment, so the card is worth keeping -- but it must not
        block, or the pipeline stalls exactly the way it did before."""
        seed_lead(db, owner)
        agents.run_product_creator(db, FakeLLM([concept("Laser Tumbler", "laser")]), owner, Profile())

        assert statuses(db, owner)["Laser Tumbler"] == "approved", "it moved anyway"
        assert pending_titles(db, owner) == ["Laser Tumbler"], "and he can still see it"

    def test_the_visible_card_says_it_is_not_blocking(self, db, owner):
        """A pending card that is already approved reads as a blocker unless it says
        otherwise, which would be a worse lie than not showing it."""
        seed_lead(db, owner)
        agents.run_product_creator(db, FakeLLM([concept("Laser Tumbler", "laser")]), owner, Profile())
        item = business_db.list_review_items(db, owner, status="pending")[0]
        assert "ALREADY APPROVED" in (item.get("detail") or "")


class TestHeCanPutTheGateBack:
    def test_turning_autopublish_off_restores_approval_for_everything(self, db, owner):
        seed_lead(db, owner)
        store_policy.set_autopublish(db, False, reason="want to eyeball the first few")
        agents.run_product_creator(
            db, FakeLLM([concept("RVA Decal", "sticker"), concept("Laser Tumbler", "laser")]),
            owner, Profile())

        assert set(statuses(db, owner).values()) == {"proposed"}
        assert sorted(pending_titles(db, owner)) == ["Laser Tumbler", "RVA Decal"]


def test_downstream_can_actually_pick_up_what_was_auto_approved(db, owner):
    """The real end-to-end assertion: store_manager reads concepts by APPROVED status, so
    auto-approval is only worth anything if it lands in that query."""
    seed_lead(db, owner)
    agents.run_product_creator(db, FakeLLM([concept("RVA Decal", "sticker")]), owner, Profile())
    waiting = business_db.concepts_without(db, owner, "store_listings", limit=5)
    assert [c["name"] for c in waiting] == ["RVA Decal"]


def test_a_database_with_no_settings_table_does_not_kill_the_run(tmp_path):
    """business_db can be initialised without core_db, and the first policy read then
    raised OperationalError straight out of run_product_creator. A missing settings table
    is 'unset', which is what the documented default is for."""
    path = str(tmp_path / "bare.db")
    business_db.init_business_db(path)
    assert store_policy.autopublish(path) is True


class TestEveryStageNotJustTheFirst:
    """Fixing only the concept stage would have him blocked at the art stage 12 hours
    later. Each stage picks up only what the previous one APPROVED, so the gate has to
    lift at all four or the pipeline just stalls one step further along."""

    def test_an_automated_artefact_is_advanced_at_every_stage(self, db, owner):
        for table, make in (
            ("art_briefs", lambda: business_db.create_art_brief(
                db, owner, title="t", concept_id=None, style_direction="s",
                image_prompt="p", negative_prompt="n", aspect="1:1", notes=None)),
            ("store_listings", lambda: business_db.create_store_listing(
                db, owner, title="t", concept_id=None, description="d",
                seo_tags="a", price=9.0, variants=None)),
            ("social_posts", lambda: business_db.create_social_post(
                db, owner, platform="tiktok", hook="h", caption="c", hashtags="#x",
                call_to_action="buy", reason="launch", listing_id=None, concept_id=None)),
        ):
            ref_id = make()
            advanced = agents.file_for_review(
                db, owner, market="automated", ref_table=table, ref_id=ref_id,
                title="x", kind="other", summary="s", detail="d", source_agent="test")
            assert advanced is True, f"{table} should not wait on him"

        assert business_db.list_review_items(db, owner, status="pending") == []

    def test_a_local_artefact_is_advanced_but_leaves_a_card(self, db, owner):
        brief_id = business_db.create_art_brief(
            db, owner, title="t", concept_id=None, style_direction="s",
            image_prompt="p", negative_prompt="n", aspect="1:1", notes=None)
        assert agents.file_for_review(
            db, owner, market="local", ref_table="art_briefs", ref_id=brief_id,
            title="Art direction: tumbler", kind="art", summary="s", detail="d",
            source_agent="art_director") is True
        assert [i["title"] for i in business_db.list_review_items(db, owner, status="pending")] \
            == ["Art direction: tumbler"]
