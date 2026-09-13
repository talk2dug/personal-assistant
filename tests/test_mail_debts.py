"""The historical debt sweep (mail_debts.py) -- assembling his debt picture from his mail.

Same safety shape as test_mail_bills.py/test_mail_triage.py, and for the same reason: the
FakeMailClient below has NO send/archive/delete/mark_read/flag_as_junk method at all, so
if this pass ever grew a mailbox mutation these tests would fail outright rather than
quietly pass. It also has no list_recent, which structurally pins the other half of the
design -- this sweep NARROWS with a search before it classifies, and a version of it that
walked whole folders would not run here.

The load-bearing test in this file is
test_twelve_monthly_statements_from_one_card_become_one_debt: twelve statements must
converge on one debt with twelve balance observations, not twelve debts.
"""
import json
from datetime import date

import pytest

from assistant.core import business_db, db, mail_db, mail_debts, personal_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    personal_db.init_personal_db(path)
    mail_db.init_mail_db(path)
    owner_id = db.upsert_user(path, "111", "Dug", "owner")
    return path, owner_id


class FakeMailClient:
    """Read-only by construction -- see the module docstring.

    search_uids applies the real narrowing contract: it returns only messages that
    actually match one of the terms it is handed, so a test can prove the sweep never sees
    a message the shortlist would have excluded.
    """

    def __init__(self, folders=None, messages=None):
        # {folder: [{"name","flags"}]} shaped like MailClient.list_folders
        self._folders = folders if folders is not None else [{"name": "INBOX", "flags": []}]
        # {folder: {uid: message}}
        self._messages = messages or {}
        self.searched_folders = []

    def list_folders(self):
        return {"folders": self._folders}

    def search_uids(self, terms, folder="INBOX", limit=500, exclude=None):
        self.searched_folders.append(folder)
        haystacks = {}
        for uid, message in self._messages.get(folder, {}).items():
            haystacks[uid] = {
                "TEXT": " ".join(str(message.get(k) or "") for k in ("from", "subject", "body")).lower(),
                "SUBJECT": str(message.get("subject") or "").lower(),
                "FROM": str(message.get("from") or "").lower(),
            }
        hits = [uid for uid, fields in haystacks.items()
                if any(str(value).lower() in fields.get(criterion, "") for criterion, value in terms)]
        # Mirrors the real client: exclusion happens BEFORE the cap, so a capped run
        # takes the newest unjudged uids rather than re-serving the same newest slice.
        matched = len(hits)
        if exclude:
            hits = [uid for uid in hits if uid not in exclude]
        hits.sort(key=lambda u: int(u) if u.isdigit() else 0, reverse=True)
        return {"folder": folder, "uids": hits[:limit], "matched": matched}

    def read_message(self, uid, folder="INBOX"):
        return self._messages.get(folder, {}).get(uid, {"error": f"no message with uid {uid}"})


class FakeLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def chat(self, messages, tools=None, think=False):
        self.prompts.append(messages)
        return {"role": "assistant", "content": self._replies.pop(0)}


class ScriptedLLM:
    """Answers per message subject rather than per call order, so a test doesn't have to
    predict the order a multi-folder sweep visits things in."""

    def __init__(self, by_subject, default=None):
        self._by_subject = by_subject
        self._default = default or json.dumps({"is_debt": False, "reasoning": "not a debt"})
        self.calls = 0

    def chat(self, messages, tools=None, think=False):
        self.calls += 1
        prompt = messages[1]["content"]
        for needle, reply in self._by_subject.items():
            if needle in prompt:
                return {"role": "assistant", "content": reply}
        return {"role": "assistant", "content": self._default}


def _debt_json(**overrides):
    payload = {
        "is_debt": True, "creditor": "Capital One", "account_last4": "4821",
        "kind": "credit_card", "balance": "$4,218.66", "apr": "24.99%",
        "minimum_payment": "$125.00", "statement_date": "2026-03-02", "due_date": "2026-03-27",
        "confidence": "high", "reasoning": "Monthly credit card statement with a carried balance.",
    }
    payload.update(overrides)
    return json.dumps(payload)


NOT_A_DEBT_JSON = json.dumps({
    "is_debt": False, "creditor": "", "account_last4": "", "kind": "other", "balance": "",
    "apr": "", "minimum_payment": "", "statement_date": "", "due_date": "",
    "confidence": "high", "reasoning": "A utility bill, not a carried balance.",
})


def _message(uid="1", from_addr="statements@capitalone.com", subject="Your statement is ready",
             body="Current balance $4,218.66. Minimum payment $125.00 due 03/27/2026.",
             sent="Mon, 2 Mar 2026 09:00:00 -0500"):
    return {"uid": uid, "from": from_addr, "to": "me@example.com", "subject": subject,
            "date": sent, "body": body}


# --- classify_debt: a debt is not a bill ---------------------------------------------

def test_a_credit_card_statement_yields_creditor_balance_rate_and_minimum():
    llm = FakeLLM([_debt_json()])
    result = mail_debts.classify_debt(llm, _message(), today=date(2026, 9, 13))
    assert result["creditor"] == "Capital One"
    assert result["account_last4"] == "4821"
    assert result["kind"] == "credit_card"
    assert result["balance_text"] == "$4,218.66"
    assert result["balance"] == 4218.66
    assert result["apr"] == 24.99
    assert result["minimum_payment"] == 125.0
    assert result["statement_date"] == "2026-03-02"


def test_a_utility_bill_is_not_a_debt():
    """The distinction the whole module turns on: an amount due for something recently
    supplied is a bill (mail_bills.py's job), not a balance carried at interest."""
    llm = FakeLLM([NOT_A_DEBT_JSON])
    assert mail_debts.classify_debt(llm, _message(subject="Your electric bill")) is None


def test_the_prompt_teaches_the_bill_versus_debt_distinction_explicitly():
    system = mail_debts.DEBT_SYSTEM.lower()
    assert "balance owed" in system
    assert "utility" in system and "not a debt" in system
    assert "collections" in system


def test_a_promotional_pre_approval_from_a_lender_is_not_a_debt():
    """Sender-domain shortlisting drags in every marketing email a creditor ever sent, so
    this is the single highest-volume false positive the classifier has to refuse."""
    llm = FakeLLM([json.dumps({
        "is_debt": False, "creditor": "Capital One", "account_last4": "", "kind": "other",
        "balance": "", "apr": "0%", "minimum_payment": "", "statement_date": "",
        "due_date": "", "confidence": "high", "reasoning": "A 0% balance transfer offer.",
    })])
    assert mail_debts.classify_debt(llm, _message(subject="You're pre-approved")) is None


def test_a_claimed_debt_with_no_creditor_is_dropped():
    """Evidence that can't be filed against anyone isn't evidence."""
    llm = FakeLLM([_debt_json(creditor="")])
    assert mail_debts.classify_debt(llm, _message()) is None


def test_a_statement_notice_with_no_figures_still_counts_when_it_names_the_account():
    """"Sign in to view your balance" states no number but is real evidence the account
    EXISTS -- worth something to a man assembling a debt list he doesn't have. Every
    numeric field stays NULL, so it can invent no precision."""
    llm = FakeLLM([_debt_json(balance="", minimum_payment="", apr="", confidence="high")])
    result = mail_debts.classify_debt(llm, _message())
    assert result is not None
    assert result["balance"] is None
    assert result["balance_text"] == ""
    assert result["account_last4"] == "4821"


def test_a_vague_low_confidence_hit_with_no_numbers_and_no_account_is_dropped():
    llm = FakeLLM([_debt_json(balance="", minimum_payment="", account_last4="", confidence="low")])
    assert mail_debts.classify_debt(llm, _message()) is None


@pytest.mark.parametrize("garbage", ["not json at all", "", '{"is_debt": true}'])
def test_malformed_llm_output_is_treated_as_not_a_debt(garbage):
    assert mail_debts.classify_debt(FakeLLM([garbage]), _message()) is None


def test_prose_wrapped_json_is_still_parsed():
    llm = FakeLLM([f"Here you go:\n```json\n{_debt_json()}\n```"])
    assert mail_debts.classify_debt(llm, _message())["balance"] == 4218.66


def test_a_full_account_number_from_the_model_is_cut_down_before_it_can_be_stored():
    """The prompt asks for four characters; this is the structural guard behind it."""
    llm = FakeLLM([_debt_json(account_last4="4111111111114821")])
    assert mail_debts.classify_debt(llm, _message())["account_last4"] == "4821"


@pytest.mark.parametrize("raw", ["$800-$1,200", "0% APR for 12 months", "varies", "see statement"])
def test_an_ambiguous_number_keeps_its_wording_and_stores_no_figure(raw):
    llm = FakeLLM([_debt_json(balance=raw)])
    result = mail_debts.classify_debt(llm, _message())
    assert result["balance_text"] == raw
    assert result["balance"] is None


def test_an_unrecognised_debt_kind_falls_back_to_other():
    llm = FakeLLM([_debt_json(kind="payday-ish")])
    assert mail_debts.classify_debt(llm, _message())["kind"] == "other"


# --- dating an observation ------------------------------------------------------------

def test_a_statement_is_dated_by_its_statement_date_not_by_today():
    assert mail_debts._observed_on(
        {"statement_date": "2026-03-02"}, _message(), date(2026, 9, 13)) == "2026-03-02"


def test_without_a_statement_date_the_message_send_date_is_used():
    """Dating a 2019 statement as today would flatten the balance trend into a vertical
    line and make a long-paid-down card look like it's still at its old balance."""
    assert mail_debts._observed_on(
        {"statement_date": None}, _message(sent="Tue, 5 Nov 2019 08:30:00 -0500"),
        date(2026, 9, 13)) == "2019-11-05"


def test_an_unparseable_send_date_falls_back_to_today_rather_than_guessing():
    assert mail_debts._observed_on(
        {"statement_date": None}, _message(sent="whenever"), date(2026, 9, 13)) == "2026-09-13"


@pytest.mark.parametrize("raw,expected", [
    ("Mon, 2 Mar 2026 09:00:00 -0500", "2026-03-02"),
    ("2026-03-02", "2026-03-02"),
    ("", None), ("nonsense", None),
])
def test_message_date_reads_real_headers_and_refuses_the_rest(raw, expected):
    assert mail_debts.message_date({"date": raw}) == expected


# --- folder selection -----------------------------------------------------------------

def test_archived_and_filed_folders_are_swept_not_just_the_inbox():
    """Old statements live in archive folders -- an inbox-only sweep would miss most of
    what this exists to find."""
    mail = FakeMailClient(folders=[
        {"name": "INBOX", "flags": []},
        {"name": "Archive", "flags": ["\\Archive"]},
        {"name": "Finances", "flags": []},
    ])
    assert mail_debts.sweep_folders(mail) == ["INBOX", "Archive", "Finances"]


@pytest.mark.parametrize("folder", [
    {"name": "Junk", "flags": ["\\Junk"]},
    {"name": "Deleted Messages", "flags": ["\\Trash"]},
    {"name": "Sent Messages", "flags": ["\\Sent"]},
    {"name": "Drafts", "flags": ["\\Drafts"]},
    {"name": "Spam", "flags": []},
    {"name": "Containers", "flags": ["\\Noselect"]},
])
def test_junk_trash_sent_and_draft_folders_are_never_swept(folder):
    mail = FakeMailClient(folders=[{"name": "INBOX", "flags": []}, folder])
    assert mail_debts.sweep_folders(mail) == ["INBOX"]


def test_the_inbox_leads_even_when_the_server_lists_it_last():
    mail = FakeMailClient(folders=[{"name": "Archive", "flags": []}, {"name": "INBOX", "flags": []}])
    assert mail_debts.sweep_folders(mail) == ["INBOX", "Archive"]


def test_a_server_that_will_not_list_folders_degrades_to_the_inbox():
    class Broken:
        def list_folders(self):
            raise RuntimeError("LIST failed")
    assert mail_debts.sweep_folders(Broken()) == ["INBOX"]


def test_an_explicit_folder_list_overrides_discovery():
    mail = FakeMailClient(folders=[{"name": "INBOX", "flags": []}])
    assert mail_debts.sweep_folders(mail, ["Statements"]) == ["Statements"]


# --- the sweep: narrowing -------------------------------------------------------------

def test_only_shortlisted_messages_are_ever_classified(db_path):
    """The narrowing contract. A birthday email matches no search term, so it must never
    reach the model -- classifying everything is what makes this unaffordable."""
    path, owner = db_path
    messages = {"INBOX": {
        "2": _message(uid="2", subject="Your statement is ready"),
        "1": {"uid": "1", "from": "mum@example.com", "to": "me", "subject": "Happy birthday!",
              "date": "Mon, 2 Mar 2026 09:00:00 -0500", "body": "Have a lovely day, love Mum"},
    }}
    llm = ScriptedLLM({"Your statement is ready": _debt_json()})

    result = mail_debts.run_debt_mail_sweep_once(
        path, llm, FakeMailClient(messages=messages), owner, today=date(2026, 9, 13))

    assert result["scanned"] == 1
    assert llm.calls == 1
    assert mail_db.has_scanned_for_debt(path, owner, "INBOX", "1") is False


def test_creditor_sender_domains_catch_a_statement_nothing_else_would(db_path):
    """The phrase list can't anticipate every issuer's wording; the sending domain doesn't
    change. This message contains none of the phrase terms."""
    path, owner = db_path
    messages = {"INBOX": {"1": {
        "uid": "1", "from": "noreply@navient.com", "to": "me", "subject": "Your loan update",
        "date": "Mon, 2 Mar 2026 09:00:00 -0500", "body": "Log in to see your account.",
    }}}
    llm = ScriptedLLM({"navient.com": _debt_json(
        creditor="Navient", kind="student_loan", account_last4="7781", balance="$18,400.00")})

    result = mail_debts.run_debt_mail_sweep_once(
        path, llm, FakeMailClient(messages=messages), owner, today=date(2026, 9, 13))

    assert result["scanned"] == 1
    assert personal_db.list_debts(path, owner, tracking_state="proposed")[0]["creditor"] == "Navient"


# --- the sweep: twelve statements, one debt -------------------------------------------

def _statement_bodies(count=12, balances=None):
    balances = balances or [5100.00, 5010.55, 4900.10, 4810.00, 4700.25, 4600.00,
                            4520.80, 4430.00, 4350.15, 4290.00, 4240.60, 4200.00]
    messages, replies = {}, {}
    for month in range(1, count + 1):
        uid = str(month)
        subject = f"Your Capital One statement for {month:02d}/2026"
        messages[uid] = _message(
            uid=uid, subject=subject,
            sent=f"Mon, 2 {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month-1]} 2026 09:00:00 -0500",
            body=f"Current balance ${balances[month-1]:,.2f}. Minimum payment $125.00.")
        replies[subject] = _debt_json(
            balance=f"${balances[month-1]:,.2f}", statement_date=f"2026-{month:02d}-02")
    return messages, replies, balances


def test_twelve_monthly_statements_from_one_card_become_one_debt(db_path):
    """THE test. Twelve statements, one debt, twelve balance observations, one review card
    asking one question."""
    path, owner = db_path
    messages, replies, balances = _statement_bodies()
    llm = ScriptedLLM(replies)

    result = mail_debts.run_debt_mail_sweep_once(
        path, llm, FakeMailClient(messages={"INBOX": messages}), owner,
        per_run_limit=50, today=date(2026, 9, 13))

    assert result["scanned"] == 12
    assert result["detected"] == 12
    assert result["debts_proposed"] == 1
    assert result["observations"] == 12

    debts = personal_db.list_debts(path, owner, tracking_state=None)
    assert len(debts) == 1, [d["creditor"] for d in debts]
    debt = debts[0]
    assert debt["observation_count"] == 12
    assert debt["current_balance"] == 4200.00
    assert [p["balance"] for p in debt["balance_trend"]] == sorted(balances, reverse=True)

    cards = business_db.list_review_items(path, owner, status="pending")
    assert len(cards) == 1
    assert cards[0]["ref_table"] == "debts"
    assert cards[0]["ref_id"] == debt["id"]


def test_the_one_card_describes_all_twelve_statements_not_just_the_first(db_path):
    """The card is filed on the first statement and refreshed as the rest arrive, so what
    he eventually reads has to describe the debt as it now stands."""
    path, owner = db_path
    messages, replies, _ = _statement_bodies()
    mail_debts.run_debt_mail_sweep_once(
        path, ScriptedLLM(replies), FakeMailClient(messages={"INBOX": messages}), owner,
        per_run_limit=50, today=date(2026, 9, 13))

    card = business_db.list_review_items(path, owner, status="pending")[0]
    assert "12 messages" in card["summary"]
    assert "$5,100.00" in card["detail"] and "$4,200.00" in card["detail"]


def test_statements_split_across_folders_still_converge_on_one_debt(db_path):
    """Old statements are archived and recent ones aren't -- the commonest real shape."""
    path, owner = db_path
    messages, replies, _ = _statement_bodies()
    archived = {uid: m for uid, m in messages.items() if int(uid) <= 6}
    recent = {uid: m for uid, m in messages.items() if int(uid) > 6}
    mail = FakeMailClient(
        folders=[{"name": "INBOX", "flags": []}, {"name": "Archive", "flags": ["\\Archive"]}],
        messages={"INBOX": recent, "Archive": archived})

    result = mail_debts.run_debt_mail_sweep_once(
        path, ScriptedLLM(replies), mail, owner, per_run_limit=50, today=date(2026, 9, 13))

    assert result["folders_searched"] == 2
    assert len(personal_db.list_debts(path, owner, tracking_state=None)) == 1
    assert personal_db.list_debts(path, owner, tracking_state=None)[0]["observation_count"] == 12


def test_the_same_statement_filed_in_two_folders_is_recorded_once(db_path):
    """Two folders, two uids, one fact. The scan ledger can't catch this -- it is keyed on
    folder AND uid precisely because a uid means different things in different folders."""
    path, owner = db_path
    message = _message(subject="Your Capital One statement")
    mail = FakeMailClient(
        folders=[{"name": "INBOX", "flags": []}, {"name": "Archive", "flags": ["\\Archive"]}],
        messages={"INBOX": {"1": message}, "Archive": {"77": dict(message, uid="77")}})
    llm = ScriptedLLM({"Your Capital One statement": _debt_json()})

    result = mail_debts.run_debt_mail_sweep_once(
        path, llm, mail, owner, per_run_limit=50, today=date(2026, 9, 13))

    assert result["scanned"] == 2
    assert result["detected"] == 2
    assert result["observations"] == 1
    debt = personal_db.list_debts(path, owner, tracking_state=None)[0]
    assert debt["observation_count"] == 1


def test_a_second_card_from_the_same_creditor_stays_a_separate_debt(db_path):
    path, owner = db_path
    messages = {
        "1": _message(uid="1", subject="Statement for card A"),
        "2": _message(uid="2", subject="Statement for card B"),
    }
    llm = ScriptedLLM({
        "Statement for card A": _debt_json(account_last4="4821", balance="$4,218.66"),
        "Statement for card B": _debt_json(account_last4="9900", balance="$1,010.00"),
    })

    mail_debts.run_debt_mail_sweep_once(
        path, llm, FakeMailClient(messages={"INBOX": messages}), owner,
        per_run_limit=50, today=date(2026, 9, 13))

    debts = personal_db.list_debts(path, owner, tracking_state=None)
    assert sorted(d["account_last4"] for d in debts) == ["4821", "9900"]


def test_a_statement_that_could_be_either_of_two_cards_asks_rather_than_guesses(db_path):
    """Attaching one card's balance to another card's history corrupts both silently and
    there is no way to spot it afterwards, so this refuses to apply it."""
    path, owner = db_path
    personal_db.create_debt(path, owner, "Capital One", account_last4="4821",
                            tracking_state="proposed", origin="email")
    personal_db.create_debt(path, owner, "Capital One", account_last4="9900",
                            tracking_state="proposed", origin="email")
    llm = ScriptedLLM({"Statement": _debt_json(account_last4="", balance="$500.00")})

    result = mail_debts.run_debt_mail_sweep_once(
        path, llm, FakeMailClient(messages={"INBOX": {"1": _message(subject="Statement")}}), owner,
        per_run_limit=50, today=date(2026, 9, 13))

    assert result["ambiguous"] == 1
    assert result["observations"] == 0
    assert all(d["observation_count"] == 0 for d in personal_db.list_debts(path, owner, tracking_state=None))
    titles = [c["title"] for c in business_db.list_review_items(path, owner, status="pending")]
    assert any("Which Capital One account" in t for t in titles)


def test_a_later_statement_teaches_an_existing_proposal_its_account_number(db_path):
    """The first statement found says only "Capital One"; a later one names the account.
    One debt, not two."""
    path, owner = db_path
    messages = {
        "1": _message(uid="1", subject="Statement no number"),
        "2": _message(uid="2", subject="Statement with number"),
    }
    llm = ScriptedLLM({
        "Statement no number": _debt_json(account_last4="", balance="$5,100.00",
                                          statement_date="2026-01-02"),
        "Statement with number": _debt_json(account_last4="4821", balance="$4,200.00",
                                            statement_date="2026-02-02"),
    })

    mail_debts.run_debt_mail_sweep_once(
        path, llm, FakeMailClient(messages={"INBOX": messages}), owner,
        per_run_limit=50, today=date(2026, 9, 13))

    debts = personal_db.list_debts(path, owner, tracking_state=None)
    assert len(debts) == 1
    assert debts[0]["account_last4"] == "4821"
    assert debts[0]["observation_count"] == 2


# --- bounded, resumable ----------------------------------------------------------------

def test_a_run_stops_at_its_cap_and_the_next_run_continues_the_backlog(db_path):
    """Messages past the cap are left UNJUDGED, not skipped -- no ledger row means the
    next run picks them up. A backlog is worked through over many runs, never one heroic
    pass."""
    path, owner = db_path
    messages, replies, _ = _statement_bodies()
    mail = FakeMailClient(messages={"INBOX": messages})
    llm = ScriptedLLM(replies)

    first = mail_debts.run_debt_mail_sweep_once(
        path, llm, mail, owner, per_run_limit=5, today=date(2026, 9, 13))
    assert first["scanned"] == 5
    assert first["capped"] is True

    second = mail_debts.run_debt_mail_sweep_once(
        path, llm, mail, owner, per_run_limit=5, today=date(2026, 9, 13))
    assert second["scanned"] == 5

    third = mail_debts.run_debt_mail_sweep_once(
        path, llm, mail, owner, per_run_limit=50, today=date(2026, 9, 13))
    assert third["scanned"] == 2
    assert third["capped"] is False

    debt = personal_db.list_debts(path, owner, tracking_state=None)[0]
    assert debt["observation_count"] == 12
    assert mail_db.debt_scan_stats(path, owner)["messages_judged"] == 12


def test_rerunning_a_finished_sweep_costs_nothing_and_changes_nothing(db_path):
    path, owner = db_path
    messages, replies, _ = _statement_bodies(count=3)
    mail = FakeMailClient(messages={"INBOX": messages})
    llm = ScriptedLLM(replies)

    mail_debts.run_debt_mail_sweep_once(path, llm, mail, owner, per_run_limit=50, today=date(2026, 9, 13))
    calls_after_first = llm.calls
    second = mail_debts.run_debt_mail_sweep_once(
        path, llm, mail, owner, per_run_limit=50, today=date(2026, 9, 13))

    assert second["scanned"] == 0
    assert llm.calls == calls_after_first
    assert len(personal_db.list_debts(path, owner, tracking_state=None)) == 1
    assert len(business_db.list_review_items(path, owner, status="pending")) == 1


def test_a_message_that_is_not_a_debt_is_never_re_asked_about(db_path):
    path, owner = db_path
    mail = FakeMailClient(messages={"INBOX": {"1": _message(subject="Your electric bill statement")}})
    llm = ScriptedLLM({"electric": NOT_A_DEBT_JSON})

    mail_debts.run_debt_mail_sweep_once(path, llm, mail, owner, today=date(2026, 9, 13))
    second = mail_debts.run_debt_mail_sweep_once(path, llm, mail, owner, today=date(2026, 9, 13))

    assert second["scanned"] == 0
    assert llm.calls == 1
    assert mail_db.has_scanned_for_debt(path, owner, "INBOX", "1") is True
    assert personal_db.list_debts(path, owner, tracking_state=None) == []


def test_an_unreadable_message_is_retried_on_a_later_run(db_path):
    """A transient IMAP failure must not burn the uid -- in a one-time pass over his
    history, "burnt in the ledger" means "invisible forever"."""
    path, owner = db_path

    class FlakyMail(FakeMailClient):
        def read_message(self, uid, folder="INBOX"):
            return {"error": "connection reset"}

    mail = FlakyMail(messages={"INBOX": {"1": _message(subject="Statement")}})
    result = mail_debts.run_debt_mail_sweep_once(
        path, FakeLLM([]), mail, owner, today=date(2026, 9, 13))

    assert result["scanned"] == 0
    assert mail_db.has_scanned_for_debt(path, owner, "INBOX", "1") is False


def test_a_classification_failure_is_retried_on_a_later_run(db_path):
    path, owner = db_path

    class BrokenLLM:
        def chat(self, **kwargs):
            raise RuntimeError("boom")

    mail = FakeMailClient(messages={"INBOX": {"1": _message(subject="Statement")}})
    result = mail_debts.run_debt_mail_sweep_once(
        path, BrokenLLM(), mail, owner, today=date(2026, 9, 13))

    assert result["scanned"] == 1
    assert result["detected"] == 0
    assert mail_db.has_scanned_for_debt(path, owner, "INBOX", "1") is False


def test_one_unsearchable_folder_does_not_end_the_sweep(db_path):
    path, owner = db_path

    class PartlyBrokenMail(FakeMailClient):
        def search_uids(self, terms, folder="INBOX", limit=500, exclude=None):
            if folder == "Archive":
                raise RuntimeError("SELECT failed")
            return super().search_uids(terms, folder=folder, limit=limit, exclude=exclude)

    mail = PartlyBrokenMail(
        folders=[{"name": "INBOX", "flags": []}, {"name": "Archive", "flags": ["\\Archive"]},
                 {"name": "Finances", "flags": []}],
        messages={"INBOX": {}, "Finances": {"1": _message(subject="Statement")}})
    llm = ScriptedLLM({"Statement": _debt_json()})

    result = mail_debts.run_debt_mail_sweep_once(
        path, llm, mail, owner, per_run_limit=50, today=date(2026, 9, 13))

    assert result["folders_searched"] == 2
    assert result["detected"] == 1


def test_mail_the_junk_scan_flagged_is_never_treated_as_debt_evidence(db_path):
    """A fake collections notice is a classic scam and reads exactly like a real debt."""
    path, owner = db_path
    mail_db.log_junk_action(path, "9", "INBOX", "scam@example.com",
                            "FINAL NOTICE past due balance", 8.0, ["urgent"], moved=False)
    mail = FakeMailClient(messages={"INBOX": {"9": _message(
        uid="9", from_addr="scam@example.com", subject="FINAL NOTICE past due balance")}})
    llm = FakeLLM([])  # must never be reached

    result = mail_debts.run_debt_mail_sweep_once(path, llm, mail, owner, today=date(2026, 9, 13))

    assert result["scanned"] == 0
    assert personal_db.list_debts(path, owner, tracking_state=None) == []


# --- propose, don't create -------------------------------------------------------------

def test_a_swept_debt_is_proposed_and_stays_out_of_his_debt_picture(db_path):
    path, owner = db_path
    mail = FakeMailClient(messages={"INBOX": {"1": _message(subject="Statement")}})
    mail_debts.run_debt_mail_sweep_once(
        path, ScriptedLLM({"Statement": _debt_json()}), mail, owner, today=date(2026, 9, 13))

    assert personal_db.list_debts(path, owner) == []
    summary = personal_db.debt_summary(path, owner)
    assert summary["total_balance"] == 0.0
    assert summary["proposed_count"] == 1


def test_a_proposal_lands_in_the_personal_review_lane(db_path):
    path, owner = db_path
    mail = FakeMailClient(messages={"INBOX": {"1": _message(subject="Statement")}})
    mail_debts.run_debt_mail_sweep_once(
        path, ScriptedLLM({"Statement": _debt_json()}), mail, owner, today=date(2026, 9, 13))

    assert business_db.list_review_items(path, owner, status="pending")[0]["pipeline"] == "personal"


def test_the_card_says_plainly_that_nothing_was_done_and_no_priority_was_assigned(db_path):
    """He was explicit that payoff priority is a joint call -- a card that read as though
    something had been decided for him would be the system overstepping."""
    path, owner = db_path
    mail = FakeMailClient(messages={"INBOX": {"1": _message(subject="Statement")}})
    mail_debts.run_debt_mail_sweep_once(
        path, ScriptedLLM({"Statement": _debt_json()}), mail, owner, today=date(2026, 9, 13))

    detail = business_db.list_review_items(path, owner, status="pending")[0]["detail"].lower()
    assert "not in your debt list" in detail
    assert "payoff priority" in detail
    assert "nothing was paid" in detail
    assert personal_db.list_debts(path, owner, tracking_state=None)[0]["priority"] is None


def test_the_sweep_never_writes_manual_recurring_charges(db_path):
    """Same line mail_bills holds: the owner's budget projections key off that table, and
    a fuzzy classifier writing there would corrupt real financial math."""
    path, owner = db_path
    mail = FakeMailClient(messages={"INBOX": {"1": _message(subject="Statement")}})
    mail_debts.run_debt_mail_sweep_once(
        path, ScriptedLLM({"Statement": _debt_json()}), mail, owner, today=date(2026, 9, 13))

    assert db.list_manual_recurring_charges(path, owner) == []
    assert db.list_reminders(path, owner) == []


def test_a_confirmed_debt_gets_new_statements_without_a_fresh_question(db_path):
    """Once he's said "yes, track this", a later statement is an observation, not another
    thing to ask about."""
    path, owner = db_path
    messages = {"1": _message(uid="1", subject="First statement"),
                "2": _message(uid="2", subject="Second statement")}
    llm = ScriptedLLM({
        "First statement": _debt_json(balance="$5,100.00", statement_date="2026-01-02"),
        "Second statement": _debt_json(balance="$4,200.00", statement_date="2026-02-02"),
    })
    mail = FakeMailClient(messages={"INBOX": {"1": messages["1"]}})
    mail_debts.run_debt_mail_sweep_once(path, llm, mail, owner, today=date(2026, 9, 13))

    debt_id = personal_db.list_debts(path, owner, tracking_state=None)[0]["id"]
    personal_db.update_debt(path, owner, debt_id, tracking_state="tracked")

    mail_debts.run_debt_mail_sweep_once(
        path, llm, FakeMailClient(messages={"INBOX": messages}), owner, today=date(2026, 9, 13))

    assert len(business_db.list_review_items(path, owner, status="pending")) == 1
    assert personal_db.get_debt(path, owner, debt_id)["observation_count"] == 2
    assert personal_db.debt_summary(path, owner)["total_balance"] == 4200.00


def test_the_sweep_walks_past_the_shortlist_cap_on_later_runs(db_path):
    """The shortlist cap must bound each RUN, not the reachable history.

    Regression: the cap used to be applied inside the search, before the ledger was
    subtracted. Once the newest `shortlist_limit` matches were all judged, every later
    run re-served that same slice, subtracted all of it, and found nothing -- so anything
    older was permanently unreachable while the sweep looked finished. With a mailbox
    where old statements are exactly where the debt history lives, that silently defeats
    the whole feature.
    """
    path, owner = db_path
    # Six matching messages, a shortlist cap of two: three runs must reach all six.
    messages = {str(uid): _message(uid=str(uid), subject=f"Statement {uid}") for uid in range(1, 7)}
    mail = FakeMailClient(messages={"INBOX": messages})
    llm = ScriptedLLM({})  # every message classified "not a debt" -- we only care about reach

    seen = set()
    for _ in range(3):
        stats = mail_debts.run_debt_mail_sweep_once(
            path, llm, mail, owner, per_run_limit=2, shortlist_limit=2, today=date(2026, 9, 13))
        assert stats["scanned"] == 2
        seen |= set(mail_db.scanned_debt_uids(path, owner, "INBOX"))

    assert seen == {"1", "2", "3", "4", "5", "6"}, "older mail was stranded behind the cap"

    # A fourth run has genuinely nothing left rather than re-serving the newest slice.
    assert mail_debts.run_debt_mail_sweep_once(
        path, llm, mail, owner, per_run_limit=2, shortlist_limit=2,
        today=date(2026, 9, 13))["scanned"] == 0


def test_every_observation_records_which_message_it_came_from(db_path):
    """The provenance requirement -- "this came from a statement email dated X"."""
    path, owner = db_path
    mail = FakeMailClient(messages={"INBOX": {"1": _message(subject="Statement")}})
    mail_debts.run_debt_mail_sweep_once(
        path, ScriptedLLM({"Statement": _debt_json()}), mail, owner, today=date(2026, 9, 13))

    debt_id = personal_db.list_debts(path, owner, tracking_state=None)[0]["id"]
    observation = personal_db.list_debt_observations(path, debt_id)[0]
    assert observation["source"] == "email"
    assert observation["source_ref"] == "INBOX:1"
    assert "statements@capitalone.com" in observation["source_detail"]
    assert observation["confirmed"] == 0
    assert observation["confidence"] == "high"


# --- scheduler registration -------------------------------------------------------------

class _MailContext:
    def __init__(self, client):
        self.mcp_client = client


def test_the_debt_sweep_is_registered_on_its_own_slow_backlog_cadence(tmp_path):
    """Slowest of the four mail passes on purpose: it is always behind by design, working
    through a finite backlog, so a fast tick would only spend LLM calls sooner."""
    from assistant.core import scheduler

    path = str(tmp_path / "sched.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    started = scheduler.start(
        path, notify=lambda *a: None, poll_interval_seconds=3600,
        mail=_MailContext(FakeMailClient()), llm=FakeLLM([]),
        mail_debts_interval_minutes=360,
    )
    try:
        job = started.get_job("mail_debts_agent")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 360 * 60
    finally:
        started.shutdown(wait=False)


def test_no_debt_sweep_without_an_llm(tmp_path):
    from assistant.core import scheduler

    path = str(tmp_path / "sched.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    started = scheduler.start(
        path, notify=lambda *a: None, poll_interval_seconds=3600,
        mail=_MailContext(FakeMailClient()))
    try:
        assert started.get_job("mail_debts_agent") is None
    finally:
        started.shutdown(wait=False)


def test_no_debt_sweep_without_a_mailbox(tmp_path):
    from assistant.core import scheduler

    path = str(tmp_path / "sched.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    started = scheduler.start(
        path, notify=lambda *a: None, poll_interval_seconds=3600, llm=FakeLLM([]))
    try:
        assert started.get_job("mail_debts_agent") is None
    finally:
        started.shutdown(wait=False)
