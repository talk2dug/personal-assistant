"""Can the fulfiller actually make it?

Jack: *"before they decide to make a product, they need to make sure the fulfiller can
actually make it... if it can't be made there's no point going through the process."*

The two failures that matter pull in opposite directions: letting through something nobody
can produce (which burns a render, a listing, social copy and a slot in his one-a-day rate
before failing at the publish), and blocking everything because an API was down — which is
the same silent stall the pipeline had just been dug out of.
"""
import json

import pytest

from assistant.core import agents, business_db, db as core_db, fulfilment


LINES = ["Custom vinyl stickers & decals", "DTF-printed apparel (t-shirts, hoodies, hats)",
         "Metal photo prints on aluminum", "Laser-engraved tumblers & drinkware",
         "3D printed wall organization systems (Multiboard)"]


class FakePrintify:
    """Stands in for the real catalogue. `providers` maps blueprint id -> provider list,
    so a blueprint that exists but nobody produces is expressible."""

    def __init__(self, blueprints=None, providers=None, blueprints_raise=None):
        self._blueprints = blueprints if blueprints is not None else [
            {"id": 1, "title": "Unisex Softstyle T-Shirt", "brand": "Gildan"},
            {"id": 2, "title": "Square Stickers", "brand": "Generic"},
        ]
        self._providers = providers if providers is not None else {1: [{"id": 9, "title": "Monster Digital"}], 2: [{"id": 7, "title": "SPOKE"}]}
        self._raise = blueprints_raise
        self.blueprint_calls = 0

    def blueprints(self):
        self.blueprint_calls += 1
        if self._raise:
            raise self._raise
        return self._blueprints

    def print_providers(self, blueprint_id):
        return self._providers.get(blueprint_id, [])


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "f.db")
    core_db.init_db(path)
    business_db.init_business_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    return path


class TestPrintOnDemand:
    def test_a_tee_is_makeable_and_says_which_blueprint(self, db):
        v = fulfilment.can_be_made(db, "Youth Tee", "apparel",
                                   printify=FakePrintify(), product_lines=LINES)
        assert v["ok"] and v["checked"]
        assert v["blueprint"]["title"] == "Unisex Softstyle T-Shirt"
        assert v["blueprint"]["provider"] == "Monster Digital"

    def test_a_blueprint_nobody_produces_is_not_makeable(self, db):
        """A catalogue entry with no print provider behind it publishes fine and is made
        by nobody -- which is exactly the late failure this check exists to move early."""
        printify = FakePrintify(providers={1: [], 2: []})
        v = fulfilment.can_be_made(db, "Youth Tee", "apparel",
                                   printify=printify, product_lines=LINES)
        assert v["ok"] is False and v["checked"] is True
        assert "print provider" in v["why"]

    def test_a_type_the_catalogue_cannot_source_is_refused(self, db):
        v = fulfilment.can_be_made(db, "Scented Candle", "candle",
                                   printify=FakePrintify(), product_lines=LINES)
        assert v["ok"] is False


class TestInHouse:
    @pytest.mark.parametrize("kind", ["laser", "metal", "3d"])
    def test_what_his_own_equipment_makes(self, db, kind):
        v = fulfilment.can_be_made(db, "Thing", kind, printify=None, product_lines=LINES)
        assert v["ok"] and v["checked"]

    def test_a_local_type_he_has_no_equipment_for_is_refused(self, db):
        """Jarvis does not get to decide his workshop grew a new machine."""
        v = fulfilment.can_be_made(db, "Blown Glass Vase", "metal", printify=None,
                                   product_lines=["Custom vinyl stickers & decals"])
        assert v["ok"] is False
        assert "product lines" in v["why"]

    def test_in_house_never_needs_printify(self, db):
        printify = FakePrintify(blueprints_raise=OSError("printify is down"))
        assert fulfilment.can_be_made(db, "Tumbler", "laser", printify=printify,
                                      product_lines=LINES)["ok"] is True


class TestItFailsOpen:
    def test_an_unreachable_printify_lets_the_concept_through_but_flags_it(self, db):
        """Replacing "we made something unmakeable" with "we made nothing because an API
        was down" is not an improvement."""
        printify = FakePrintify(blueprints_raise=OSError("connection reset"))
        v = fulfilment.can_be_made(db, "Youth Tee", "apparel",
                                   printify=printify, product_lines=LINES)
        assert v["ok"] is True, "the pipeline keeps moving"
        assert v["checked"] is False, "but it is recorded as unverified"

    def test_no_printify_configured_is_also_open(self, db):
        v = fulfilment.can_be_made(db, "Youth Tee", "apparel", printify=None, product_lines=LINES)
        assert v["ok"] is True and v["checked"] is False


class TestTheCatalogueIsCached:
    def test_it_is_not_refetched_for_every_concept(self, db):
        """blueprints() is 2,500+ rows per call and the check runs once per concept."""
        printify = FakePrintify()
        for _ in range(4):
            fulfilment.can_be_made(db, "Tee", "apparel", printify=printify, product_lines=LINES)
        assert printify.blueprint_calls == 1

    def test_a_corrupt_cache_refetches_instead_of_crashing(self, db):
        core_db.set_setting(db, fulfilment.CACHE_KEY, "{not json")
        printify = FakePrintify()
        assert fulfilment.can_be_made(db, "Tee", "apparel", printify=printify,
                                      product_lines=LINES)["ok"] is True


class FakeLLM:
    def __init__(self, concepts):
        self.concepts = concepts
        self.prompts = []

    def research(self, prompt, system_prompt=None, timeout=None, **kw):
        self.prompts.append(prompt)
        return "REASONING: x\n\n```json\n" + json.dumps(self.concepts) + "\n```"


class Profile:
    name, location, radius_miles, notes, min_fit_score = "Blue Ridge", "RVA", 60, "", 50
    product_lines = LINES


def _concept(name, kind):
    return {"name": name, "product_type": kind, "description": "d", "target_customer": "t",
            "price_estimate": 20.0, "production_notes": "n", "trend_topic": "t1"}


class TestInThePipeline:
    def test_an_unmakeable_concept_never_becomes_a_product(self, db):
        owner = core_db.get_user_by_chat_id(db, "111")["id"]
        business_db.upsert_trend_lead(db, owner, "t1", "web", 90, "idea", "rising")
        llm = FakeLLM([_concept("Scented Candle", "candle"), _concept("Youth Tee", "apparel")])

        result = agents.run_product_creator(db, llm, owner, Profile(), printify=FakePrintify())

        names = [c["name"] for c in business_db.list_product_concepts(db, owner, limit=10)]
        assert names == ["Youth Tee"], "the candle never got made"
        assert result["new"] == 1

    def test_what_was_dropped_is_recorded_not_silently_lost(self, db):
        """Four concepts vanishing without a trace is the same invisible stall this
        pipeline just came out of -- the run summary has to say why."""
        owner = core_db.get_user_by_chat_id(db, "111")["id"]
        business_db.upsert_trend_lead(db, owner, "t1", "web", 90, "idea", "rising")
        agents.run_product_creator(
            db, FakeLLM([_concept("Scented Candle", "candle")]), owner, Profile(),
            printify=FakePrintify())

        run = business_db.recent_agent_runs(db, limit=5)[0]
        assert "unmakeable" in (run.get("summary") or "")
        assert "Scented Candle" in (run.get("summary") or "")

    def test_the_prompt_tells_it_what_can_be_made(self, db):
        """Rejecting a bad concept afterwards still wastes the call that produced it."""
        owner = core_db.get_user_by_chat_id(db, "111")["id"]
        business_db.upsert_trend_lead(db, owner, "t1", "web", 90, "idea", "rising")
        llm = FakeLLM([_concept("Youth Tee", "apparel")])
        agents.run_product_creator(db, llm, owner, Profile(), printify=FakePrintify())

        assert "WHAT CAN ACTUALLY BE MADE" in llm.prompts[0]
        assert "Laser-engraved tumblers" in llm.prompts[0]
