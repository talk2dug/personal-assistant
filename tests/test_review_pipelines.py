"""Covers the Review page's pipeline lanes and urgency weighting: classify_pipeline
(which lane a review item belongs on), resolve_due_at (the one real due-date join that
exists today, personal_tasks.due_at), and compute_urgency (priority + due date + age,
combined into one sortable number) -- all in assistant/core/business_db.py.

The property that matters most: the taxonomy has to match what real call sites actually
produce (grepped across business_tools.py/engine.py/camera_watch.py/mail_triage.py/
vision_runtime.py), not an invented set of categories -- see classify_pipeline's own
docstring for the mapping this file exercises.
"""
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import business_db, db, personal_db
from assistant.core.engine import create_pending_action_and_review


# --- classify_pipeline: pure, no db needed -----------------------------------------

@pytest.mark.parametrize("kind", ["concept", "art", "listing", "post", "media", "research"])
def test_every_creative_pipeline_kind_is_business(kind):
    """These are the review_items CHECK constraint's own non-'other' kinds -- the
    creative/product pipeline's real output, whether or not a ref_table row backs it."""
    assert business_db.classify_pipeline({"kind": kind, "ref_table": None}) == "business"


@pytest.mark.parametrize("ref_table", ["git_pull_requests", "ops_plans", "capability_requests"])
def test_ops_and_pr_ref_tables_are_dev_ops(ref_table):
    assert business_db.classify_pipeline({"kind": "other", "ref_table": ref_table}) == "dev_ops"


def test_mail_triage_drafts_are_mail():
    assert business_db.classify_pipeline({"kind": "other", "ref_table": "email_drafts"}) == "mail"


def test_unknown_faces_are_personal():
    assert business_db.classify_pipeline({"kind": "other", "ref_table": "unknown_faces"}) == "personal"


def test_detected_bills_are_personal_not_mail():
    """A bill mail_bills.py found arrives BY email, but it's the owner's money and his due
    date -- it belongs beside his finances, not in the mail-hygiene lane where a drafted
    reply (email_drafts) sits."""
    assert business_db.classify_pipeline({"kind": "other", "ref_table": "email_bills"}) == "personal"


def test_importance_flags_are_personal_not_mail():
    """Same reasoning again, and more so: an importance flag asks about his finances,
    relationships and personal business -- the mail is only how it arrived."""
    assert business_db.classify_pipeline(
        {"kind": "other", "ref_table": "email_importance_flags"}) == "personal"


@pytest.mark.parametrize("ref_table", ["personal_tasks", "dispute_items", "dispute_letters"])
def test_personal_life_ref_tables_are_personal(ref_table):
    """No real create_review_item call site sets these ref_tables today (grepped every
    one in the codebase), but a linked personal task or credit dispute is obviously
    personal-life, not business/dev_ops/mail/other, whenever one does exist."""
    assert business_db.classify_pipeline({"kind": "other", "ref_table": ref_table}) == "personal"


@pytest.mark.parametrize("tool_name,expected", [
    ("git_merge_pr", "dev_ops"),
    ("send_email", "mail"),
    ("archive_email", "mail"),
    ("delete_email", "mail"),
    ("bulk_add_to_cart", "personal"),       # Kroger cart write
    ("add_items_to_cart", "personal"),      # Kroger cart write
    ("create_market_order", "personal"),    # CCXT trade
    ("letterstream_authorize_mail", "personal"),  # credit-dispute letter
    ("send_sms", "personal"),               # phone
    ("call_service", "personal"),           # Home Assistant lock/cover/alarm
])
def test_pending_action_sub_classification_by_tool_name(tool_name, expected):
    item = {"kind": "other", "ref_table": "pending_actions", "ref_id": 1}
    assert business_db.classify_pipeline(item, pending_tool_name=tool_name) == expected


def test_pending_action_with_unresolved_tool_name_still_falls_back_to_personal():
    """Not knowing the tool name (e.g. the caller couldn't look it up) means 'don't know
    which pending action this is', not 'not a pending action' -- it still isn't dev_ops
    or mail by accident."""
    item = {"kind": "other", "ref_table": "pending_actions", "ref_id": 1}
    assert business_db.classify_pipeline(item, pending_tool_name=None) == "personal"


def test_kind_other_with_no_ref_table_is_the_generic_catch_all():
    assert business_db.classify_pipeline({"kind": "other", "ref_table": None}) == "other"


def test_kind_other_with_an_unrecognised_ref_table_is_also_the_catch_all():
    assert business_db.classify_pipeline({"kind": "other", "ref_table": "something_new"}) == "other"


def test_default_kind_when_missing_is_treated_as_other():
    assert business_db.classify_pipeline({"ref_table": None}) == "other"


# --- compute_urgency: pure, pinned clock ------------------------------------------

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _item(priority="normal", created_at=None):
    return {"priority": priority, "created_at": (created_at or NOW).isoformat()}


def test_high_priority_outranks_normal_and_low_at_equal_age_with_no_due_date():
    high = business_db.compute_urgency(_item("high"), None, as_of=NOW)
    normal = business_db.compute_urgency(_item("normal"), None, as_of=NOW)
    low = business_db.compute_urgency(_item("low"), None, as_of=NOW)
    assert high > normal > low


def test_something_waiting_a_long_time_rises_even_without_a_due_date():
    """The explicit fallback the task calls for: no hard deadline shouldn't mean an item
    can never rise, or a normal-priority item that's sat for a week is invisible next to
    everything fresh."""
    fresh_normal = business_db.compute_urgency(_item("normal", NOW), None, as_of=NOW)
    stale_normal = business_db.compute_urgency(_item("normal", NOW - timedelta(days=5)), None, as_of=NOW)
    assert stale_normal > fresh_normal


def test_age_boost_is_capped_so_ancient_low_priority_cant_dominate():
    ancient_low = business_db.compute_urgency(_item("low", NOW - timedelta(days=400)), None, as_of=NOW)
    # low(10) + capped age boost(60) = 70, regardless of just how ancient
    assert ancient_low == pytest.approx(70.0)


def test_overdue_beats_a_fresh_high_priority_item_with_no_due_date():
    overdue = business_db.compute_urgency(
        _item("normal", NOW), due_at=(NOW - timedelta(hours=3)).isoformat(), as_of=NOW)
    fresh_high = business_db.compute_urgency(_item("high", NOW), None, as_of=NOW)
    assert overdue > fresh_high


def test_due_sooner_outranks_due_later_at_equal_priority():
    due_soon = business_db.compute_urgency(
        _item("normal", NOW), due_at=(NOW + timedelta(hours=12)).isoformat(), as_of=NOW)
    due_later = business_db.compute_urgency(
        _item("normal", NOW), due_at=(NOW + timedelta(days=5)).isoformat(), as_of=NOW)
    no_due = business_db.compute_urgency(_item("normal", NOW), None, as_of=NOW)
    assert due_soon > due_later > no_due


def test_the_longer_overdue_the_more_urgent():
    barely_overdue = business_db.compute_urgency(
        _item("normal", NOW), due_at=(NOW - timedelta(hours=1)).isoformat(), as_of=NOW)
    very_overdue = business_db.compute_urgency(
        _item("normal", NOW), due_at=(NOW - timedelta(days=10)).isoformat(), as_of=NOW)
    assert very_overdue > barely_overdue


def test_compute_urgency_tolerates_unparseable_or_missing_created_at():
    # Must not raise -- a defensively-missing/garbage created_at falls back to "now".
    business_db.compute_urgency({"priority": "normal", "created_at": None}, None, as_of=NOW)
    business_db.compute_urgency({"priority": "normal", "created_at": "not a date"}, None, as_of=NOW)


# --- resolve_due_at: needs a real db -----------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    personal_db.init_personal_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


def test_resolve_due_at_reads_the_real_personal_task_due_date(db_path, owner_id):
    task_id = personal_db.create_task(db_path, owner_id, "Renew passport", due_at="2026-10-01T00:00:00+00:00")
    item = {"ref_table": "personal_tasks", "ref_id": task_id}
    assert business_db.resolve_due_at(db_path, item) == "2026-10-01T00:00:00+00:00"


def test_resolve_due_at_is_none_when_the_task_has_no_due_date(db_path, owner_id):
    task_id = personal_db.create_task(db_path, owner_id, "Someday maybe")
    item = {"ref_table": "personal_tasks", "ref_id": task_id}
    assert business_db.resolve_due_at(db_path, item) is None


def test_resolve_due_at_is_none_for_ref_tables_with_no_due_date_concept(db_path, owner_id):
    item = {"ref_table": "git_pull_requests", "ref_id": 4}
    assert business_db.resolve_due_at(db_path, item) is None


def test_resolve_due_at_is_none_with_no_ref_at_all(db_path):
    assert business_db.resolve_due_at(db_path, {"ref_table": None, "ref_id": None}) is None


def test_resolve_due_at_does_not_crash_when_personal_db_was_never_initialised(tmp_path):
    """business_db must not assume personal_db.init_personal_db has run -- a review item
    can be classified and listed by a caller that only ever touched db.py/business_db.py."""
    path = str(tmp_path / "bare.db")
    db.init_db(path)
    business_db.init_business_db(path)
    item = {"ref_table": "personal_tasks", "ref_id": 1}
    assert business_db.resolve_due_at(path, item) is None


# --- end-to-end through list_review_items / get_review_item -----------------------

def test_list_review_items_carries_pipeline_and_urgency_score(db_path, owner_id):
    business_db.create_review_item(db_path, owner_id, "Logo mockup", kind="art")
    items = business_db.list_review_items(db_path, owner_id)
    assert items[0]["pipeline"] == "business"
    assert isinstance(items[0]["urgency_score"], float)


def test_pending_action_items_are_sub_classified_through_list_review_items(db_path, owner_id):
    create_pending_action_and_review(db_path, owner_id, "send_email", {"to": "a@b.com"})
    create_pending_action_and_review(db_path, owner_id, "bulk_add_to_cart", {"items": []})
    items = business_db.list_review_items(db_path, owner_id)
    by_title = {i["title"]: i["pipeline"] for i in items}
    assert by_title["Confirm: send_email"] == "mail"
    assert by_title["Confirm: bulk_add_to_cart"] == "personal"


def test_pipeline_filter_only_returns_matching_items(db_path, owner_id):
    business_db.create_review_item(db_path, owner_id, "Logo mockup", kind="art")
    business_db.create_review_item(
        db_path, owner_id, "PR #4: fix the thing", kind="other", ref_table="git_pull_requests", ref_id=4)
    items = business_db.list_review_items(db_path, owner_id, pipeline="dev_ops")
    assert len(items) == 1
    assert items[0]["title"].startswith("PR #4")


def test_get_review_item_also_carries_pipeline_and_urgency(db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Reply draft", ref_table="email_drafts", ref_id=1)
    item = business_db.get_review_item(db_path, owner_id, item_id)
    assert item["pipeline"] == "mail"
    assert isinstance(item["urgency_score"], float)


def test_an_overdue_personal_task_item_outranks_a_fresh_high_priority_item(db_path, owner_id):
    """The full path: a real due date resolved off personal_tasks feeds compute_urgency
    and actually changes the ranking, not just the classification."""
    task_id = personal_db.create_task(
        db_path, owner_id, "File the extension",
        due_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    overdue_id = business_db.create_review_item(
        db_path, owner_id, "File the extension", ref_table="personal_tasks", ref_id=task_id)
    fresh_high_id = business_db.create_review_item(
        db_path, owner_id, "Fresh high-priority thing", priority="high")

    items = {i["id"]: i for i in business_db.list_review_items(db_path, owner_id)}
    assert items[overdue_id]["due_at"] is not None
    assert items[overdue_id]["urgency_score"] > items[fresh_high_id]["urgency_score"]
