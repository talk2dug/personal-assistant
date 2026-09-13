"""The creative pipeline's feedback loop: his verdicts reach the agent that earned them,
and a rejection now has to say why.

review_items held 84 decided cards and decision_note was read by nothing. One real
rejection note says "I already have this created. No need to make it again" -- a rule
about his own workshop that the product creator, which opens every run knowing only a list
of titles not to repeat, had no way to learn.

The selection strategy mirrors mail_importance.select_examples exactly, so these tests
mirror test_mail_importance.py's: the properties that matter are recency, the ASYMMETRIC
backfill (spare approval slots go to rejections, never the reverse), the hard cap, and his
note surviving into the prompt.
"""
import pytest

from assistant.core import business_db, db, review_examples


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def _decided(db_path, owner_id, title, status, note=None, agent="product_creator"):
    item_id = business_db.create_review_item(
        db_path, owner_id, title, kind="concept", source_agent=agent)
    business_db.decide_review_item(db_path, owner_id, item_id, status, note=note)
    return item_id


# --- selection ----------------------------------------------------------------

def test_his_note_reaches_the_prompt(db_path, owner_id):
    """The single most valuable line in the block, and the whole reason it exists."""
    _decided(db_path, owner_id, "RVA skyline decal", "rejected",
             note="I already have this created. No need to make it again")

    text = review_examples.build_verdict_briefing(db_path, owner_id, "product_creator")

    assert "I already have this created" in text
    assert "REJECTED" in text
    assert "RVA skyline decal" in text


def test_pending_cards_are_never_used_as_examples(db_path, owner_id):
    """An undecided card carries no verdict; treating it as one would invent a ruling."""
    business_db.create_review_item(db_path, owner_id, "Still waiting", kind="concept",
                                   source_agent="product_creator")

    assert review_examples.select_examples(db_path, owner_id, "product_creator") == []


def test_examples_are_scoped_to_the_agent_that_earned_them(db_path, owner_id):
    """A rejected sticker concept says nothing about a social caption."""
    _decided(db_path, owner_id, "A concept", "rejected", note="too generic",
             agent="product_creator")
    _decided(db_path, owner_id, "A caption", "rejected", note="too salesy",
             agent="social_director")

    text = review_examples.build_verdict_briefing(db_path, owner_id, "product_creator")

    assert "too generic" in text
    assert "too salesy" not in text


def test_spare_approval_slots_are_backfilled_with_rejections_only(db_path, owner_id):
    """The asymmetry is the point. A prompt weighted toward 'approved' teaches the model
    its last batch was good and to make more of it -- the exact live failure, where
    product_creator is already rejected almost 2:1."""
    _decided(db_path, owner_id, "The one good one", "approved", note="yes")
    for i in range(10):
        _decided(db_path, owner_id, f"Reject {i}", "rejected", note=f"no {i}")

    examples = review_examples.select_examples(db_path, owner_id, "product_creator")

    assert len(examples) == review_examples.MAX_FEWSHOT_EXAMPLES
    rejected = [e for e in examples if e["status"] == "rejected"]
    assert len(rejected) == 7, "the 3 unused approval slots must go to rejections"


def test_approvals_never_backfill_beyond_their_own_cap(db_path, owner_id):
    """The reverse must not happen: plentiful approvals cannot crowd out the rejections."""
    for i in range(10):
        _decided(db_path, owner_id, f"Approve {i}", "approved", note=f"yes {i}")
    _decided(db_path, owner_id, "The one rejection", "rejected", note="no")

    examples = review_examples.select_examples(db_path, owner_id, "product_creator")

    approved = [e for e in examples if e["status"] == "approved"]
    assert len(approved) <= review_examples.MAX_EXAMPLES_PER_LABEL
    assert any(e["title"] == "The one rejection" for e in examples)


def test_the_block_is_hard_capped_however_many_decisions_accumulate(db_path, owner_id):
    for i in range(60):
        _decided(db_path, owner_id, f"Concept {i}", "rejected", note="x" * 500)

    examples = review_examples.select_examples(db_path, owner_id, "product_creator")
    text = review_examples.build_verdict_briefing(db_path, owner_id, "product_creator")

    assert len(examples) == review_examples.MAX_FEWSHOT_EXAMPLES
    assert len(text) < 4000, "the prompt must not grow with the size of the decision history"


def test_examples_are_mixed_into_one_recency_ordered_list(db_path, owner_id):
    """Not an all-yes block followed by an all-no block -- that reads as a list with a
    trailing bias rather than a standard to calibrate against."""
    _decided(db_path, owner_id, "Older rejection", "rejected", note="no")
    _decided(db_path, owner_id, "Newer approval", "approved", note="yes")

    statuses = [e["status"] for e in
                review_examples.select_examples(db_path, owner_id, "product_creator")]

    assert statuses == ["approved", "rejected"], "most recent first, regardless of verdict"


def test_no_history_yet_says_so_rather_than_going_silent(db_path, owner_id):
    text = review_examples.build_verdict_briefing(db_path, owner_id, "product_creator")

    assert "has not ruled on anything from you yet" in text


def test_the_block_says_his_words_outrank_the_instructions(db_path, owner_id):
    _decided(db_path, owner_id, "A concept", "rejected", note="too generic")

    text = review_examples.build_verdict_briefing(db_path, owner_id, "product_creator")

    assert "outrank" in text.lower()


def test_a_broken_read_never_stops_the_agent_producing_work(db_path, owner_id, monkeypatch):
    """Failing closed here would trade a working pipeline for a better one that sometimes
    doesn't run -- uncalibrated output is exactly the state before any of this existed."""
    monkeypatch.setattr(
        business_db, "list_verdict_examples",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))

    assert review_examples.build_verdict_briefing(db_path, owner_id, "product_creator") == ""


def test_selection_writes_nothing(db_path, owner_id):
    item_id = _decided(db_path, owner_id, "A concept", "rejected", note="too generic")
    before = business_db.get_review_item(db_path, owner_id, item_id)

    review_examples.build_verdict_briefing(db_path, owner_id, "product_creator")

    assert business_db.get_review_item(db_path, owner_id, item_id) == before


# --- the agents actually splice it in -----------------------------------------

class CapturingLLM:
    def __init__(self, reply="[]"):
        self.prompts = []
        self._reply = reply

    def research(self, prompt, system_prompt=None, timeout=None, tools=None, employee_key=None):
        self.prompts.append(prompt)
        return self._reply


def test_the_product_creator_sees_his_past_verdicts(db_path, owner_id):
    from assistant.config import BusinessProfile
    from assistant.core import agents

    _decided(db_path, owner_id, "RVA skyline decal", "rejected",
             note="I already have this created. No need to make it again")
    business_db.upsert_trend_lead(db_path, owner_id, topic="Local cycling", source="web",
                                  score=8, product_idea="bike stickers", reasoning="trending")
    llm = CapturingLLM("[]")

    agents.run_product_creator(
        db_path, llm, owner_id,
        BusinessProfile(name="BRCC", location="Richmond, VA", radius_miles=60,
                        product_lines=["Vinyl stickers"]))

    assert llm.prompts, "the agent should have run"
    assert "I already have this created" in llm.prompts[0]
