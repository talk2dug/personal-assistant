"""Provisional importance flagging and its feedback loop (mail_importance.py) -- project
13's "flags important emails touching personal finances, relationships, or personal
business".

Same safety shape as test_mail_bills.py/test_mail_triage.py, and for the same reason: the
FakeMailClient below has NO send/archive/delete/mark_read method at all, so if this pass
ever grew a mailbox mutation these tests would fail outright rather than quietly pass.

The tests that matter most here are the learning-loop ones (the "the loop actually
closes" section): that a verdict he gives really does change what the NEXT run puts in
front of the model, that the example selection stays bounded and stays tilted toward his
rejections, and that a low-confidence guess never reaches him.
"""
import json

import pytest

from assistant.core import business_db, db, mail_db, mail_importance


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    mail_db.init_mail_db(path)
    owner_id = db.upsert_user(path, "111", "Dug", "owner")
    return path, owner_id


class FakeMailClient:
    """No send/archive/delete/mark_read on purpose -- see module docstring."""

    def __init__(self, headers, bodies):
        self._headers = headers
        self._bodies = bodies

    def list_recent(self, folder="INBOX", limit=10):
        return {"emails": self._headers[:limit]}

    def read_message(self, uid, folder="INBOX"):
        return self._bodies[uid]


class FakeLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def chat(self, messages, tools=None, think=False):
        self.prompts.append(messages)
        return {"role": "assistant", "content": self._replies.pop(0)}

    @property
    def last_user_prompt(self):
        return self.prompts[-1][1]["content"]


def _important_json(**overrides):
    payload = {
        "important": True, "category": "personal_finances", "confidence": 0.9,
        "reason": "Your mortgage servicer says a payment was returned.",
    }
    payload.update(overrides)
    return json.dumps(payload)


NOT_IMPORTANT_JSON = json.dumps({
    "important": False, "category": "other", "confidence": 0.95,
    "reason": "A marketing newsletter.",
})


def _message(from_addr="service@mortgage.example", subject="Payment returned",
             body="Your scheduled payment was returned by your bank."):
    return {"from": from_addr, "to": "me@example.com", "subject": subject,
            "date": "2026-09-13", "body": body}


# --- classify_importance -------------------------------------------------------------

def test_classify_returns_category_confidence_and_reason():
    llm = FakeLLM([_important_json()])
    result = mail_importance.classify_importance(llm, _message())
    assert result["category"] == "personal_finances"
    assert result["confidence"] == 0.9
    assert result["reason"] == "Your mortgage servicer says a payment was returned."


def test_a_message_the_model_calls_unimportant_is_not_flagged():
    llm = FakeLLM([NOT_IMPORTANT_JSON])
    assert mail_importance.classify_importance(llm, _message(subject="50% off everything")) is None


@pytest.mark.parametrize("garbage", ["not json at all", "", '{"important": true}'])
def test_malformed_or_incomplete_output_is_treated_as_not_important(garbage):
    llm = FakeLLM([garbage])
    assert mail_importance.classify_importance(llm, _message()) is None


def test_a_claimed_hit_with_no_reason_is_dropped():
    """The reason IS the card ("I flagged this because X -- was it?"). A flag he can't
    evaluate is a question he can't answer, so it's not worth his attention."""
    llm = FakeLLM([_important_json(reason="  ")])
    assert mail_importance.classify_importance(llm, _message()) is None


def test_an_unrecognised_category_falls_back_to_other_rather_than_being_invented():
    llm = FakeLLM([_important_json(category="health emergency")])
    assert mail_importance.classify_importance(llm, _message())["category"] == "other"


def test_llm_prose_wrapped_json_is_still_parsed():
    llm = FakeLLM([f"Sure:\n```json\n{_important_json()}\n```"])
    assert mail_importance.classify_importance(llm, _message())["confidence"] == 0.9


@pytest.mark.parametrize("raw", ["high", "95", 95, "0.8-0.9", None, True, -0.1, 1.5, "very sure"])
def test_a_confidence_that_is_not_a_plain_0_to_1_number_is_never_guessed_at(raw):
    """Strictness is the safety: a None confidence can never clear the threshold, so a
    model that stops answering the way it was asked goes quiet instead of flagging
    everything at some invented certainty."""
    llm = FakeLLM([_important_json(confidence=raw)])
    result = mail_importance.classify_importance(llm, _message())
    assert result is not None and result["confidence"] is None


# --- the threshold and the per-run cap (not nagging him) ------------------------------

def test_a_flag_below_the_confidence_threshold_is_recorded_but_never_surfaced(db_path):
    path, owner = db_path
    headers = [{"uid": "1"}]
    llm = FakeLLM([_important_json(confidence=0.4)])
    result = mail_importance.run_mail_importance_scan_once(
        path, llm, FakeMailClient(headers, {"1": _message()}), owner, threshold=0.75)

    assert result["flagged"] == 0 and result["below_threshold"] == 1
    assert mail_db.list_importance_flags(path, owner) == []
    assert business_db.list_review_items(path, owner) == []
    # ...but it is judged, and the near-miss is kept so the threshold can be tuned on
    # evidence rather than on a hunch.
    assert mail_db.has_scanned_for_importance(path, owner, "INBOX", "1")
    stats = mail_db.importance_stats(path, owner)
    assert stats["messages_judged"] == 1 and stats["flagged_total"] == 0


def test_an_unreadable_confidence_fails_closed_and_raises_nothing(db_path):
    path, owner = db_path
    llm = FakeLLM([_important_json(confidence="very high")])
    result = mail_importance.run_mail_importance_scan_once(
        path, llm, FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)
    assert result["flagged"] == 0
    assert mail_db.list_importance_flags(path, owner) == []


def test_the_per_run_cap_bounds_how_many_cards_one_pass_can_raise(db_path):
    path, owner = db_path
    headers = [{"uid": str(i)} for i in range(1, 6)]
    bodies = {str(i): _message(subject=f"Thing {i}") for i in range(1, 6)}
    llm = FakeLLM([_important_json()] * 5)

    result = mail_importance.run_mail_importance_scan_once(
        path, llm, FakeMailClient(headers, bodies), owner, max_flags=2)

    assert result["flagged"] == 2 and result["capped"] is True
    assert len(business_db.list_review_items(path, owner)) == 2
    # And it stopped paying for classifications it couldn't use.
    assert len(llm.prompts) == 2


def test_messages_past_the_cap_are_left_unjudged_so_the_next_pass_picks_them_up(db_path):
    """A late flag is fine; a lost one is not. Nothing past the cap gets a ledger row."""
    path, owner = db_path
    headers = [{"uid": str(i)} for i in range(1, 4)]
    bodies = {str(i): _message(subject=f"Thing {i}") for i in range(1, 4)}
    mail_client = FakeMailClient(headers, bodies)

    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]), mail_client, owner, max_flags=1)
    assert not mail_db.has_scanned_for_importance(path, owner, "INBOX", "2")

    second = mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]), mail_client, owner, max_flags=1)
    assert second["flagged"] == 1
    assert mail_db.has_scanned_for_importance(path, owner, "INBOX", "2")


# --- the scan pass --------------------------------------------------------------------

def test_scan_flags_only_what_the_model_calls_important(db_path):
    path, owner = db_path
    headers = [{"uid": "1"}, {"uid": "2"}]
    bodies = {"1": _message(), "2": _message(subject="50% off", from_addr="deals@shop.example")}
    llm = FakeLLM([_important_json(), NOT_IMPORTANT_JSON])

    result = mail_importance.run_mail_importance_scan_once(
        path, llm, FakeMailClient(headers, bodies), owner)

    assert result == {"scanned": 2, "flagged": 1, "below_threshold": 0,
                      "examples_used": 0, "capped": False}
    flags = mail_db.list_importance_flags(path, owner)
    assert len(flags) == 1 and flags[0]["uid"] == "1"
    assert flags[0]["status"] == "flagged"
    assert flags[0]["category"] == "personal_finances"


def test_each_flag_becomes_a_review_card_that_asks_him_the_question(db_path):
    path, owner = db_path
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]),
        FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)

    items = business_db.list_review_items(path, owner)
    assert len(items) == 1
    item = items[0]
    assert item["ref_table"] == "email_importance_flags"
    assert item["source_agent"] == "mail_importance"
    assert "Payment returned" in item["title"]
    # The model's reason is read back verbatim, and the card makes the ask explicit.
    assert "Your mortgage servicer says a payment was returned." in item["detail"]
    assert "Was it?" in item["detail"]
    # And it says plainly that the message itself was not touched.
    assert "hasn't been read, moved, archived or replied to" in item["detail"]


def test_a_flag_lands_in_the_personal_review_lane(db_path):
    path, owner = db_path
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]),
        FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)
    assert business_db.list_review_items(path, owner)[0]["pipeline"] == "personal"


def test_rerunning_duplicates_nothing_and_never_re_asks_the_llm(db_path):
    path, owner = db_path
    mail_client = FakeMailClient([{"uid": "1"}], {"1": _message()})
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]), mail_client, owner)

    second_llm = FakeLLM([])  # popping a reply would IndexError if it asked again
    result = mail_importance.run_mail_importance_scan_once(path, second_llm, mail_client, owner)

    assert result["scanned"] == 0 and second_llm.prompts == []
    assert len(mail_db.list_importance_flags(path, owner)) == 1
    assert len(business_db.list_review_items(path, owner)) == 1


def test_a_message_judged_unimportant_is_never_re_asked_about_either(db_path):
    path, owner = db_path
    mail_client = FakeMailClient([{"uid": "1"}], {"1": _message()})
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([NOT_IMPORTANT_JSON]), mail_client, owner)

    second_llm = FakeLLM([])
    assert mail_importance.run_mail_importance_scan_once(
        path, second_llm, mail_client, owner)["scanned"] == 0
    assert second_llm.prompts == []


def test_messages_the_junk_scan_already_flagged_are_skipped(db_path):
    """Urgent-sounding fraud is exactly what scores as junk AND reads as something that
    matters -- the same guard mail_bills.py uses, for the same reason."""
    path, owner = db_path
    mail_db.log_junk_action(
        path, uid="1", folder="INBOX", from_address="scam@example.com",
        subject="URGENT: account suspended", score=9.0, reasons=["urgency"], moved=False)

    llm = FakeLLM([])
    result = mail_importance.run_mail_importance_scan_once(
        path, llm, FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)

    assert result["scanned"] == 0 and llm.prompts == []
    assert mail_db.list_importance_flags(path, owner) == []


def test_scan_skips_unreadable_messages_and_retries_them_next_pass(db_path):
    path, owner = db_path
    bodies = {"1": {"error": "IMAP timeout"}}
    mail_client = FakeMailClient([{"uid": "1"}], bodies)
    mail_importance.run_mail_importance_scan_once(path, FakeLLM([]), mail_client, owner)
    assert not mail_db.has_scanned_for_importance(path, owner, "INBOX", "1")

    bodies["1"] = _message()
    assert mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]), mail_client, owner)["flagged"] == 1


def test_scan_survives_a_classification_error_and_retries_that_uid_later(db_path):
    path, owner = db_path

    class BoomLLM:
        def chat(self, messages, tools=None, think=False):
            raise RuntimeError("model exploded")

    mail_client = FakeMailClient([{"uid": "1"}], {"1": _message()})
    result = mail_importance.run_mail_importance_scan_once(path, BoomLLM(), mail_client, owner)
    assert result["flagged"] == 0
    assert not mail_db.has_scanned_for_importance(path, owner, "INBOX", "1")


def test_the_scan_never_creates_a_reminder(db_path):
    """A flag is a question, not a task. Unlike mail_bills this pass schedules nothing."""
    path, owner = db_path
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]),
        FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)
    assert db.list_reminders(path, owner) == []


# --- the loop actually closes: his verdicts steer the next run ------------------------

def test_a_verdict_is_recorded_as_a_labelled_example(db_path):
    path, owner = db_path
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]),
        FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)
    flag_id = mail_db.list_importance_flags(path, owner)[0]["id"]

    updated = mail_db.record_importance_verdict(
        path, owner, flag_id, important=True, note="yes — this one is my mortgage")

    assert updated["status"] == "confirmed"
    assert updated["verdict_note"] == "yes — this one is my mortgage"
    examples = mail_db.list_importance_examples(path, owner)
    assert len(examples) == 1
    assert examples[0]["label"] == 1
    assert examples[0]["note"] == "yes — this one is my mortgage"
    assert examples[0]["source"] == "review"
    assert examples[0]["flag_id"] == flag_id


def test_a_rejection_is_recorded_as_a_negative_example(db_path):
    path, owner = db_path
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()]),
        FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)
    flag_id = mail_db.list_importance_flags(path, owner)[0]["id"]

    mail_db.record_importance_verdict(
        path, owner, flag_id, important=False, note="that account is closed")

    assert mail_db.get_importance_flag(path, owner, flag_id)["status"] == "rejected"
    assert mail_db.list_importance_examples(path, owner, label=False)[0]["label"] == 0


def test_the_next_run_prompt_carries_his_real_verdicts(db_path):
    """THE test for this feature. A verdict he gave must change what the model is shown
    next time -- otherwise this is a classifier with a scoreboard, not a learning loop."""
    path, owner = db_path
    mail_client = FakeMailClient(
        [{"uid": "1"}, {"uid": "2"}], {"1": _message(), "2": _message(subject="Second thing")})

    first_llm = FakeLLM([_important_json()])
    mail_importance.run_mail_importance_scan_once(path, first_llm, mail_client, owner)
    assert mail_importance.NO_EXAMPLES_NOTE in first_llm.last_user_prompt

    flag_id = mail_db.list_importance_flags(path, owner)[0]["id"]
    mail_db.record_importance_verdict(
        path, owner, flag_id, important=False, note="stop flagging servicer notices")

    second_llm = FakeLLM([NOT_IMPORTANT_JSON])
    result = mail_importance.run_mail_importance_scan_once(path, second_llm, mail_client, owner)

    prompt = second_llm.last_user_prompt
    assert result["examples_used"] == 1
    assert mail_importance.NO_EXAMPLES_NOTE not in prompt
    assert "[NOT important]" in prompt
    assert "service@mortgage.example" in prompt
    # His own words are the highest-signal part and must survive into the prompt.
    assert "stop flagging servicer notices" in prompt


def test_a_chat_volunteered_label_about_an_unflagged_message_also_teaches_the_next_run(db_path):
    """The false-negative case a flags-only store could never represent: he says "that one
    WAS important" about mail the scan passed over."""
    path, owner = db_path
    mail_db.record_importance_example(
        path, owner, folder="INBOX", uid="99", from_address="sister@family.example",
        subject="Mum's scan results", category="", model_reason="", label=True,
        note="anything from my sister is important", source="chat")

    llm = FakeLLM([NOT_IMPORTANT_JSON])
    mail_importance.run_mail_importance_scan_once(
        path, llm, FakeMailClient([{"uid": "1"}], {"1": _message()}), owner)

    prompt = llm.last_user_prompt
    assert "[IMPORTANT]" in prompt and "sister@family.example" in prompt
    assert "anything from my sister is important" in prompt


def test_he_can_change_his_mind_and_the_training_set_holds_what_he_believes_now(db_path):
    path, owner = db_path
    for label, note in ((False, "not important"), (True, "actually this did matter")):
        mail_db.record_importance_example(
            path, owner, folder="INBOX", uid="7", from_address="a@b.example",
            subject="A thing", category="", model_reason="", label=label, note=note, source="chat")

    examples = mail_db.list_importance_examples(path, owner)
    assert len(examples) == 1
    assert examples[0]["label"] == 1 and examples[0]["note"] == "actually this did matter"


# --- example selection: balanced, negative-backfilled, bounded ------------------------

def _seed_examples(path, owner, label, count, prefix):
    for i in range(count):
        mail_db.record_importance_example(
            path, owner, folder="INBOX", uid=f"{prefix}{i}",
            from_address=f"{prefix}{i}@example.com", subject=f"{prefix} subject {i}",
            category="other", model_reason="", label=label, note="", source="review")


def test_selection_is_balanced_when_he_has_plenty_of_both(db_path):
    path, owner = db_path
    _seed_examples(path, owner, True, 10, "pos")
    _seed_examples(path, owner, False, 10, "neg")

    selected = mail_importance.select_examples(path, owner)

    assert len(selected) == mail_importance.MAX_FEWSHOT_EXAMPLES
    assert sum(1 for e in selected if e["label"]) == mail_importance.MAX_EXAMPLES_PER_LABEL
    assert sum(1 for e in selected if not e["label"]) == mail_importance.MAX_EXAMPLES_PER_LABEL


def test_spare_slots_are_backfilled_with_rejections_never_with_confirmations(db_path):
    """The one deliberate asymmetry: a prompt tilted toward "yes" teaches this thing to
    nag, and extra negatives can only make it more conservative."""
    path, owner = db_path
    _seed_examples(path, owner, True, 1, "pos")
    _seed_examples(path, owner, False, 10, "neg")

    selected = mail_importance.select_examples(path, owner)
    assert sum(1 for e in selected if e["label"]) == 1
    assert sum(1 for e in selected if not e["label"]) == mail_importance.MAX_FEWSHOT_EXAMPLES - 1

    # ...and the reverse never happens: lots of confirmations don't get to fill the prompt.
    path2, owner2 = path, owner
    _seed_examples(path2, owner2, True, 10, "morepos")
    reselected = mail_importance.select_examples(path2, owner2)
    assert sum(1 for e in reselected if e["label"]) == mail_importance.MAX_EXAMPLES_PER_LABEL


def test_selection_prefers_his_most_recent_rulings(db_path):
    path, owner = db_path
    _seed_examples(path, owner, True, 6, "old")
    _seed_examples(path, owner, True, 2, "new")

    subjects = {e["subject"] for e in mail_importance.select_examples(path, owner) if e["label"]}
    assert "new subject 0" in subjects and "new subject 1" in subjects


def test_the_prompt_block_stays_bounded_however_large_the_training_set_gets(db_path):
    path, owner = db_path
    for i in range(60):
        mail_db.record_importance_example(
            path, owner, folder="INBOX", uid=f"big{i}", from_address="x" * 500,
            subject="y" * 500, category="other", model_reason="", label=bool(i % 2),
            note="z" * 500, source="review")

    block = mail_importance.format_examples(mail_importance.select_examples(path, owner))
    assert len(block.splitlines()) <= 2 + mail_importance.MAX_FEWSHOT_EXAMPLES * 2
    assert "x" * (mail_importance.EXAMPLE_SENDER_CHARS + 1) not in block
    assert "y" * (mail_importance.EXAMPLE_SUBJECT_CHARS + 1) not in block
    assert "z" * (mail_importance.EXAMPLE_NOTE_CHARS + 1) not in block


def test_examples_are_selected_once_per_run_not_per_message(db_path):
    """One pass judges every message by the same standard; a verdict recorded mid-run
    lands on the next run, not halfway through this one."""
    path, owner = db_path
    _seed_examples(path, owner, False, 2, "neg")
    headers = [{"uid": "1"}, {"uid": "2"}]
    bodies = {"1": _message(), "2": _message(subject="Second")}
    llm = FakeLLM([NOT_IMPORTANT_JSON, NOT_IMPORTANT_JSON])

    mail_importance.run_mail_importance_scan_once(path, llm, FakeMailClient(headers, bodies), owner)

    blocks = [p[1]["content"].split("--- The email to judge now ---")[0] for p in llm.prompts]
    assert blocks[0] == blocks[1]


# --- the accuracy summary -------------------------------------------------------------

def test_stats_report_precision_only_once_he_has_actually_ruled_on_something(db_path):
    path, owner = db_path
    assert mail_db.importance_stats(path, owner)["precision"] is None

    headers = [{"uid": "1"}, {"uid": "2"}, {"uid": "3"}]
    bodies = {u: _message(subject=f"Thing {u}") for u in ("1", "2", "3")}
    mail_importance.run_mail_importance_scan_once(
        path, FakeLLM([_important_json()] * 3), FakeMailClient(headers, bodies), owner, max_flags=3)

    flags = mail_db.list_importance_flags(path, owner)
    mail_db.record_importance_verdict(path, owner, flags[0]["id"], important=True)
    mail_db.record_importance_verdict(path, owner, flags[1]["id"], important=False)

    stats = mail_db.importance_stats(path, owner)
    assert stats["flagged_total"] == 3
    assert stats["confirmed"] == 1 and stats["rejected"] == 1 and stats["awaiting_verdict"] == 1
    assert stats["precision"] == 0.5
    assert stats["examples_total"] == 2
    assert stats["examples_positive"] == 1 and stats["examples_negative"] == 1


def test_a_verdict_on_a_flag_that_does_not_exist_is_a_no_op(db_path):
    path, owner = db_path
    assert mail_db.record_importance_verdict(path, owner, 999, important=True) is None
    assert mail_db.list_importance_examples(path, owner) == []


# --- scheduler registration -------------------------------------------------------------

class _MailContext:
    def __init__(self, client):
        self.mcp_client = client


def _started(tmp_path, **kwargs):
    from assistant.core import scheduler

    path = str(tmp_path / "sched.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return scheduler.start(
        path, notify=lambda *a: None, poll_interval_seconds=3600,
        mail=_MailContext(FakeMailClient([], {})), **kwargs)


def test_the_importance_scan_is_registered_on_its_own_slow_cadence(tmp_path):
    """The slowest of the three mail passes on purpose: every card it raises spends the
    owner's attention, not just tokens."""
    started = _started(tmp_path, llm=FakeLLM([]), mail_importance_interval_minutes=240)
    try:
        job = started.get_job("mail_importance_agent")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 240 * 60
    finally:
        started.shutdown(wait=False)


def test_no_importance_scan_without_an_llm(tmp_path):
    started = _started(tmp_path)
    try:
        assert started.get_job("mail_importance_agent") is None
    finally:
        started.shutdown(wait=False)
