"""Grouping the business team's work by the product it belongs to.

The problem, in the owner's words: "I'm drowning in reviews." Twenty-eight pending
approvals turned out to be nine products at different stages, and the flat queue said
nothing about that. These tests pin the grouping, the market split, and the one rule that
makes the view worth opening: a pending approval must appear under the product it is
actually about, not on its own.
"""
import sqlite3

import pytest

from assistant.core import business_db, db, pipelines


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "biz.db")
    db.init_db(path)
    business_db.init_business_db(path)
    pipelines.init_pipelines(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


def _concept(db_path, owner, name, product_type="laser"):
    # create_product_concept upserts by name and returns (id, created).
    concept_id, _created = business_db.create_product_concept(
        db_path, owner, name=name, product_type=product_type,
        description=f"{name} description")
    return concept_id


class TestMarketSplit:
    def test_physical_production_defaults_to_the_local_market(self):
        """Laser, 3D and metal are things he makes and takes to Richmond markets."""
        for kind in ("laser", "3d", "metal"):
            assert pipelines.default_market(kind) == "local"

    def test_print_on_demand_defaults_to_automated(self):
        for kind in ("apparel", "sticker"):
            assert pipelines.default_market(kind) == "automated"

    def test_an_unknown_type_defaults_to_local_rather_than_automated(self):
        """The safer default: 'automated' claims a thing can sell without him, and being
        wrong in that direction is worse than being wrong the other way."""
        assert pipelines.default_market(None) == "local"
        assert pipelines.default_market("something-new") == "local"

    def test_the_backfill_classifies_existing_concepts(self, db_path, owner):
        _concept(db_path, owner, "Laser tumbler", "laser")
        _concept(db_path, owner, "Tee", "apparel")
        rows = {p["name"]: p["market"] for p in pipelines.list_pipelines(db_path, owner)}
        assert rows["Laser tumbler"] == "local"
        assert rows["Tee"] == "automated"

    def test_a_product_can_be_moved_between_tracks(self, db_path, owner):
        """The split is a starting guess, not a rule -- a design changes route once it
        proves itself."""
        cid = _concept(db_path, owner, "Tee", "apparel")
        assert pipelines.set_market(db_path, owner, cid, "local") is True
        assert pipelines.get_pipeline(db_path, owner, cid)["concept"]["market"] == "local"

    def test_an_invalid_market_is_refused(self, db_path, owner):
        cid = _concept(db_path, owner, "Tee", "apparel")
        with pytest.raises(ValueError):
            pipelines.set_market(db_path, owner, cid, "wholesale")

    def test_filtering_returns_only_that_track(self, db_path, owner):
        _concept(db_path, owner, "Laser tumbler", "laser")
        _concept(db_path, owner, "Tee", "apparel")
        local = pipelines.list_pipelines(db_path, owner, market="local")
        assert [p["name"] for p in local] == ["Laser tumbler"]

    def test_a_concept_created_after_the_backfill_still_filters_correctly(self, db_path, owner):
        """The column is only backfilled at startup, so anything the team makes while the
        server is running has market = NULL until the next restart. It must still file
        itself under the track its own card claims -- these were computed by two different
        rules once, and an apparel design showed as Automated while sitting under Local."""
        cid = _concept(db_path, owner, "Tee", "apparel")
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE product_concepts SET market = NULL WHERE id = ?", (cid,))

        card = pipelines.list_pipelines(db_path, owner)[0]
        assert card["market"] == "automated"
        assert [p["id"] for p in pipelines.list_pipelines(db_path, owner, market="automated")] == [cid]
        assert pipelines.list_pipelines(db_path, owner, market="local") == []

    def test_the_sql_rule_and_the_python_rule_agree(self, db_path):
        """MARKET_SQL exists so the database can filter without calling Python. It is
        derived from the same set, and this is what keeps that true."""
        kinds = sorted(pipelines.LOCAL_PRODUCT_TYPES | pipelines.AUTOMATED_PRODUCT_TYPES
                       | {"", "something-new", "APPAREL"})
        with sqlite3.connect(db_path) as conn:
            for kind in kinds:
                got = conn.execute(
                    f"SELECT {pipelines.MARKET_SQL} FROM (SELECT ? AS product_type)",
                    (kind,)).fetchone()[0]
                assert got == pipelines.default_market(kind), kind


class TestGrouping:
    def test_a_bare_concept_is_a_pipeline_at_its_first_stage(self, db_path, owner):
        _concept(db_path, owner, "Idea only")
        row = pipelines.list_pipelines(db_path, owner)[0]
        assert row["stage"] == "concept"
        assert row["counts"] == {"art": 0, "listing": 0, "social": 0}

    def test_downstream_work_is_counted_against_its_concept(self, db_path, owner):
        cid = _concept(db_path, owner, "RVA Playoff Baseball")
        business_db.create_art_brief(db_path, owner, concept_id=cid, title="Art",
                                     image_prompt="a squirrel")
        business_db.create_store_listing(db_path, owner, concept_id=cid, title="Listing",
                                         description="d", price=25.0)
        row = pipelines.list_pipelines(db_path, owner)[0]
        assert row["counts"]["art"] == 1
        assert row["counts"]["listing"] == 1
        assert row["stage"] == "listing", "the stage is the furthest point reached"

    def test_work_on_one_product_never_counts_toward_another(self, db_path, owner):
        a = _concept(db_path, owner, "Product A")
        _concept(db_path, owner, "Product B")
        business_db.create_art_brief(db_path, owner, concept_id=a, title="A art",
                                     image_prompt="x")
        rows = {p["name"]: p for p in pipelines.list_pipelines(db_path, owner)}
        assert rows["Product A"]["counts"]["art"] == 1
        assert rows["Product B"]["counts"]["art"] == 0


class TestPendingReviewsAttachToTheirProduct:
    """The whole point of the view."""

    def test_a_concept_approval_counts_against_its_own_pipeline(self, db_path, owner):
        cid = _concept(db_path, owner, "Thing")
        business_db.create_review_item(
            db_path, owner, title="Confirm concept", kind="concept", summary="s",
            detail="d", source_agent="product_creator",
            ref_table="product_concepts", ref_id=cid)
        assert pipelines.list_pipelines(db_path, owner)[0]["pending_reviews"] == 1

    def test_an_art_approval_counts_against_the_product_not_the_brief(self, db_path, owner):
        """This is what the flat queue could not express: the card is about an art brief,
        but the thing the owner is deciding about is the product."""
        cid = _concept(db_path, owner, "Thing")
        brief = business_db.create_art_brief(db_path, owner, concept_id=cid, title="Art",
                                             image_prompt="x")
        business_db.create_review_item(
            db_path, owner, title="Art direction", kind="art", summary="s", detail="d",
            source_agent="art_director", ref_table="art_briefs", ref_id=brief)
        assert pipelines.list_pipelines(db_path, owner)[0]["pending_reviews"] == 1

    def test_approvals_across_several_stages_add_up_on_one_card(self, db_path, owner):
        cid = _concept(db_path, owner, "Thing")
        brief = business_db.create_art_brief(db_path, owner, concept_id=cid, title="A",
                                             image_prompt="x")
        listing = business_db.create_store_listing(db_path, owner, concept_id=cid,
                                                   title="L", description="d", price=1.0)
        for kind, table, ref in (("concept", "product_concepts", cid),
                                 ("art", "art_briefs", brief),
                                 ("listing", "store_listings", listing)):
            business_db.create_review_item(
                db_path, owner, title=f"Confirm {kind}", kind=kind, summary="s",
                detail="d", source_agent="agent", ref_table=table, ref_id=ref)
        assert pipelines.list_pipelines(db_path, owner)[0]["pending_reviews"] == 3

    def test_a_decided_review_stops_counting(self, db_path, owner):
        cid = _concept(db_path, owner, "Thing")
        item = business_db.create_review_item(
            db_path, owner, title="Confirm", kind="concept", summary="s", detail="d",
            source_agent="a", ref_table="product_concepts", ref_id=cid)
        business_db.decide_review_item(db_path, owner, item, "approved")
        assert pipelines.list_pipelines(db_path, owner)[0]["pending_reviews"] == 0

    def test_an_unrelated_review_never_lands_on_a_pipeline(self, db_path, owner):
        """The queue also carries debts, PRs and mail flags -- none of which belong here."""
        _concept(db_path, owner, "Thing")
        business_db.create_review_item(
            db_path, owner, title="A debt", kind="other", summary="s", detail="d",
            source_agent="mail", ref_table="debts", ref_id=999)
        assert pipelines.list_pipelines(db_path, owner)[0]["pending_reviews"] == 0


class TestDetail:
    def test_every_stage_is_present_even_when_empty(self, db_path, owner):
        """A product with no art is stalled somewhere different from one with no concept,
        and an absent stage has to be visible as absent."""
        cid = _concept(db_path, owner, "Thing")
        detail = pipelines.get_pipeline(db_path, owner, cid)
        assert [s["stage"] for s in detail["stages"]] == list(pipelines.STAGES)

    def test_an_approval_is_attached_to_the_artefact_it_is_about(self, db_path, owner):
        cid = _concept(db_path, owner, "Thing")
        brief = business_db.create_art_brief(db_path, owner, concept_id=cid, title="A",
                                             image_prompt="x")
        business_db.create_review_item(
            db_path, owner, title="Art direction", kind="art", summary="s", detail="d",
            source_agent="art_director", ref_table="art_briefs", ref_id=brief)
        art_stage = next(s for s in pipelines.get_pipeline(db_path, owner, cid)["stages"]
                         if s["stage"] == "art")
        assert art_stage["items"][0]["reviews"][0]["title"] == "Art direction"

    def test_a_filesystem_path_never_reaches_the_client(self, db_path, owner):
        """Artwork is served through the path-checked media endpoint; the stored path is
        a real server path and must not be handed out."""
        cid = _concept(db_path, owner, "Thing")
        item = business_db.create_review_item(
            db_path, owner, title="Pick art", kind="art", summary="s", detail="d",
            source_agent="art_director", ref_table="product_concepts", ref_id=cid,
            options=[{"label": "Bold", "media_path": r"D:\Jarvis Generated\secret.png"}])
        assert item
        detail = pipelines.get_pipeline(db_path, owner, cid)
        blob = str(detail)
        assert "secret.png" not in blob
        assert "Jarvis Generated" not in blob
        option = next(s for s in detail["stages"] if s["stage"] == "concept")["reviews"][0]["options"][0]
        assert option["has_image"] is True

    def test_the_artwork_he_approved_stays_visible_after_the_card_is_decided(self, db_path, owner):
        """Pending cards are only half the board. Approving an art direction takes the
        card out of the queue, and the picture must not leave with it -- otherwise looking
        back at a finished pipeline shows the word "approved" and nothing else, which is
        the text-only view this replaced."""
        cid = _concept(db_path, owner, "Thing")
        brief = business_db.create_art_brief(db_path, owner, concept_id=cid, title="A",
                                             image_prompt="x")
        item = business_db.create_review_item(
            db_path, owner, title="Pick art", kind="art", summary="s", detail="d",
            source_agent="art_director", ref_table="art_briefs", ref_id=brief,
            options=[{"label": "Neon", "media_path": "generated/a.png"},
                     {"label": "Woodcut", "media_path": "generated/b.png"}])
        decided = business_db.get_review_item(db_path, owner, item)
        woodcut = next(o for o in decided["options"] if o["label"] == "Woodcut")
        business_db.decide_review_item(db_path, owner, item, "approved", option_id=woodcut["id"])

        art = next(s for s in pipelines.get_pipeline(db_path, owner, cid)["stages"]
                   if s["stage"] == "art")["items"][0]
        assert art["reviews"] == [], "it is decided, so nothing is waiting"
        assert art["chosen"]["label"] == "Woodcut"
        assert art["chosen"]["has_image"] is True
        assert art["chosen"]["decision"] == "approved"

    def test_an_undecided_card_contributes_no_chosen_option(self, db_path, owner):
        cid = _concept(db_path, owner, "Thing")
        brief = business_db.create_art_brief(db_path, owner, concept_id=cid, title="A",
                                             image_prompt="x")
        business_db.create_review_item(
            db_path, owner, title="Pick art", kind="art", summary="s", detail="d",
            source_agent="art_director", ref_table="art_briefs", ref_id=brief,
            options=[{"label": "Neon", "media_path": "generated/a.png"}])
        art = next(s for s in pipelines.get_pipeline(db_path, owner, cid)["stages"]
                   if s["stage"] == "art")["items"][0]
        assert art["chosen"] is None
        assert len(art["reviews"]) == 1

    def test_a_chosen_option_never_leaks_its_filesystem_path_either(self, db_path, owner):
        cid = _concept(db_path, owner, "Thing")
        item = business_db.create_review_item(
            db_path, owner, title="Pick art", kind="art", summary="s", detail="d",
            source_agent="art_director", ref_table="product_concepts", ref_id=cid,
            options=[{"label": "Neon", "media_path": r"D:\Jarvis Generated\secret.png"}])
        option = business_db.get_review_item(db_path, owner, item)["options"][0]
        business_db.decide_review_item(db_path, owner, item, "approved", option_id=option["id"])

        detail = pipelines.get_pipeline(db_path, owner, cid)
        assert "secret.png" not in str(detail)
        assert detail["stages"][1]["items"][0]["chosen"]["has_image"] is True

    def test_a_missing_pipeline_is_none_not_an_error(self, db_path, owner):
        assert pipelines.get_pipeline(db_path, owner, 424242) is None

    def test_another_users_pipeline_is_not_readable(self, db_path, owner):
        cid = _concept(db_path, owner, "Thing")
        db.upsert_user(db_path, "222", "Someone", "guest")
        other = db.get_user_by_chat_id(db_path, "222")["id"]
        assert pipelines.get_pipeline(db_path, other, cid) is None
