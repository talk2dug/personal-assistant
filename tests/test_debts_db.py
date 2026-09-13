"""The debt tracker's storage layer (personal_db.py's debts + debt_observations).

This feature exists because the owner, asked to list his debts so payoff priorities could
be assigned, said he isn't going to -- he doesn't have a list, "most of the debt is in
there" (his mailbox). So the picture gets ASSEMBLED, and the two things that decide
whether an assembled picture is worth anything are pinned here:

  * match_debt -- twelve monthly statements from one card must converge on ONE debt.
  * attach_current_values -- a balance is a dated observation with a source, never a
    mutable number, because watching it trend down is the whole point of a payoff effort.

Also pinned: nothing in here can write a payoff priority on its own, and no account
identifier longer than four characters can enter the database.
"""
import pytest

from assistant.core import db, personal_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    personal_db.init_personal_db(path)
    owner_id = db.upsert_user(path, "111", "Dug", "owner")
    return path, owner_id


# --- creditor normalization (the dedup key) ------------------------------------------

@pytest.mark.parametrize("raw", [
    "Capital One", "CAPITAL ONE", "capital one", "Capital One Bank (USA), N.A.",
    "Capital One Bank USA NA", "Capital One Card Services", "  Capital  One  ",
])
def test_the_same_creditor_written_many_ways_normalizes_to_one_key(raw):
    assert personal_db.normalize_creditor(raw) == "capitalone"


@pytest.mark.parametrize("a,b", [
    ("Capital One", "Credit One Bank"),
    ("Navient", "Nelnet"),
    ("Discover Card", "Discover Student Loans"),
    ("Chase", "Citi"),
])
def test_genuinely_different_creditors_never_collide(a, b):
    """The one direction this must never fail in: a collision silently folds two real
    debts into one and money disappears from his picture."""
    assert personal_db.normalize_creditor(a) != personal_db.normalize_creditor(b)


def test_brand_words_that_look_generic_are_not_stripped():
    """'Credit One' and 'OneMain Financial' are real brands whose distinguishing part is
    exactly the word a naive stopword list would throw away."""
    assert personal_db.normalize_creditor("Credit One Bank") == "creditone"
    assert personal_db.normalize_creditor("OneMain Financial") == "onemain"
    assert personal_db.normalize_creditor("OneMain") == "onemain"


def test_a_name_that_strips_down_to_almost_nothing_keeps_its_full_form():
    """"M&T Bank" -> "mt" is too short to be distinctive, so the un-stripped form is used
    instead. Erring toward not matching is the safe side."""
    assert personal_db.normalize_creditor("M&T Bank") == "mtbank"


def test_an_empty_creditor_has_no_key():
    assert personal_db.normalize_creditor("") == ""
    assert personal_db.normalize_creditor(None) == ""


# --- account identifiers: a full account number must be impossible to store ----------

@pytest.mark.parametrize("raw,expected", [
    ("4821", "4821"),
    ("ending in 4821", "4821"),
    ("XXXX-XXXX-XXXX-4821", "4821"),
    ("4111 1111 1111 1234", "1234"),
    ("****4821", "4821"),
    ("acct 987654321", "4321"),
])
def test_only_the_last_four_characters_of_an_account_ever_survive(raw, expected):
    """This is a safety function, not formatting: it is the only way an account identifier
    enters the table, so a model handing over a full card number cannot store one."""
    assert personal_db.sanitize_account_last4(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "-", "x"])
def test_something_that_is_not_an_identifier_at_all_is_dropped(raw):
    assert personal_db.sanitize_account_last4(raw) == ""


def test_a_full_account_number_cannot_be_stored_even_when_handed_in_directly(db_path):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4111111111111234")
    assert personal_db.get_debt(path, owner, debt_id)["account_last4"] == "1234"


# --- match_debt: the hardest correctness problem in the feature ----------------------

def _candidate(debt_id, creditor_key, last4="", tracking_state="proposed"):
    return {"id": debt_id, "creditor": creditor_key, "creditor_key": creditor_key,
            "account_last4": last4, "tracking_state": tracking_state}


def test_nothing_tracked_yet_is_a_clean_new_debt():
    result = personal_db.match_debt([], "capitalone", "4821")
    assert result["debt_id"] is None
    assert result["ambiguous"] is False


def test_same_creditor_and_same_account_matches():
    candidates = [_candidate(1, "capitalone", "4821")]
    assert personal_db.match_debt(candidates, "capitalone", "4821")["debt_id"] == 1


def test_a_statement_naming_an_account_adopts_the_one_debt_that_never_knew_its_number():
    """The ordinary sequence: the first statement found says only "Capital One", a later
    one says "ending 4821". Two debts here would be wrong."""
    candidates = [_candidate(1, "capitalone", "")]
    result = personal_db.match_debt(candidates, "capitalone", "4821")
    assert result["debt_id"] == 1
    assert result["learned_account_last4"] == "4821"


def test_a_second_card_from_the_same_creditor_is_a_separate_debt():
    """Two Capital One cards is an ordinary thing, not a duplicate."""
    candidates = [_candidate(1, "capitalone", "4821")]
    result = personal_db.match_debt(candidates, "capitalone", "9900")
    assert result["debt_id"] is None
    assert result["ambiguous"] is False


def test_an_unnumbered_account_is_not_adopted_once_another_account_is_already_known():
    """Deliberately refused: with a known 4821 already on file, the unnumbered row is just
    as likely to be a third card as it is to be this one. Guessing here either merges two
    real debts or splits one."""
    candidates = [_candidate(1, "capitalone", "4821"), _candidate(2, "capitalone", "")]
    result = personal_db.match_debt(candidates, "capitalone", "9900")
    assert result["debt_id"] is None
    assert result["ambiguous"] is True


def test_a_message_with_no_account_number_matches_the_only_account_for_that_creditor():
    candidates = [_candidate(1, "navient", "")]
    assert personal_db.match_debt(candidates, "navient", "")["debt_id"] == 1


def test_a_message_with_no_account_number_is_ambiguous_when_he_has_two_such_accounts():
    """Ask rather than guess -- this is the case that would otherwise silently pile one
    card's balances onto another card's trend line."""
    candidates = [_candidate(1, "capitalone", "4821"), _candidate(2, "capitalone", "9900")]
    result = personal_db.match_debt(candidates, "capitalone", "")
    assert result["debt_id"] is None
    assert result["ambiguous"] is True


def test_matching_searches_proposed_debts_too_which_is_what_makes_dedup_work(db_path):
    """A proposal left by statement #1 has to be findable by statement #2, or a sweep of
    twelve statements proposes twelve debts."""
    path, owner = db_path
    debt_id = personal_db.create_debt(
        path, owner, "Capital One", account_last4="4821", tracking_state="proposed", origin="email")
    assert personal_db.find_matching_debt(path, owner, "Capital One", "4821")["debt_id"] == debt_id


def test_a_dismissed_debt_is_never_matched_again(db_path):
    """He said no. Re-attaching to it would resurrect a rejected proposal."""
    path, owner = db_path
    debt_id = personal_db.create_debt(
        path, owner, "Capital One", account_last4="4821", tracking_state="proposed", origin="email")
    personal_db.update_debt(path, owner, debt_id, tracking_state="dismissed")
    assert personal_db.find_matching_debt(path, owner, "Capital One", "4821")["debt_id"] is None


# --- twelve statements, one debt ------------------------------------------------------

def test_twelve_monthly_statements_from_one_card_become_one_debt_with_twelve_observations(db_path):
    """The explicit requirement, end to end through the storage layer. Each statement
    writes the creditor slightly differently and only some of them name the account."""
    path, owner = db_path
    written_as = [
        "Capital One", "CAPITAL ONE", "Capital One Bank (USA), N.A.", "Capital One",
        "Capital One Card Services", "capital one", "Capital One", "CAPITAL ONE BANK USA NA",
        "Capital One", "Capital One", "Capital One", "Capital One",
    ]
    balances = [5100.00, 5010.55, 4900.10, 4810.00, 4700.25, 4600.00,
                4520.80, 4430.00, 4350.15, 4290.00, 4240.60, 4200.00]

    for month, (creditor, balance) in enumerate(zip(written_as, balances), start=1):
        # Only the later statements name the account -- the earlier ones don't.
        last4 = "4821" if month >= 5 else ""
        match = personal_db.find_matching_debt(path, owner, creditor, last4)
        debt_id = match["debt_id"]
        if debt_id is None:
            debt_id = personal_db.create_debt(
                path, owner, creditor, account_last4=last4, kind="credit_card",
                tracking_state="proposed", origin="email")
        elif match.get("learned_account_last4"):
            personal_db.update_debt(path, owner, debt_id,
                                    account_last4=match["learned_account_last4"])
        personal_db.add_debt_observation(
            path, debt_id, observed_on=f"2026-{month:02d}-02",
            balance_text=f"${balance:,.2f}", balance=balance,
            source="email", source_ref=f"INBOX:{100 + month}")

    all_debts = personal_db.list_debts(path, owner, tracking_state=None)
    assert len(all_debts) == 1, [d["creditor"] for d in all_debts]
    debt = all_debts[0]
    assert debt["observation_count"] == 12
    assert debt["account_last4"] == "4821"
    assert debt["current_balance"] == 4200.00
    assert debt["current_balance_observed_on"] == "2026-12-02"
    assert [p["balance"] for p in debt["balance_trend"]] == balances


def test_the_same_statement_filed_in_two_folders_is_one_observation(db_path):
    """A historical sweep searches archive folders as well as INBOX, so it will meet the
    same statement twice under two different uids. The source-ref index can't catch that."""
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Navient", account_last4="7781")
    personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-03-02", balance_text="$18,400.00", balance=18400.0,
        source="email", source_ref="INBOX:55")

    assert personal_db.has_equivalent_observation(path, debt_id, "2026-03-02", "$18,400.00") is True
    assert personal_db.has_equivalent_observation(path, debt_id, "2026-04-02", "$18,400.00") is False


def test_recording_the_same_source_message_twice_writes_one_observation(db_path):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Navient")
    first = personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-03-02", balance=18400.0,
        source="email", source_ref="INBOX:55")
    second = personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-03-02", balance=18400.0,
        source="email", source_ref="INBOX:55")

    assert first is not None
    assert second is None
    assert len(personal_db.list_debt_observations(path, debt_id)) == 1


def test_he_can_record_as_many_balances_as_he_likes_from_chat(db_path):
    """The source-ref uniqueness is partial on purpose -- chat observations carry no ref,
    and a plain UNIQUE would cap him at one balance per debt forever."""
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One")
    for balance in (4200.0, 4100.0, 3980.0):
        assert personal_db.add_debt_observation(
            path, debt_id, balance=balance, source="chat", confirmed=True) is not None
    assert len(personal_db.list_debt_observations(path, debt_id)) == 3


def test_creating_the_same_creditor_and_account_twice_returns_the_same_debt(db_path):
    path, owner = db_path
    first = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    second = personal_db.create_debt(path, owner, "CAPITAL ONE BANK", account_last4="4821")
    assert first == second
    assert len(personal_db.list_debts(path, owner)) == 1


# --- current values are derived, never overwritten -----------------------------------

def test_the_balance_trend_survives_every_new_observation(db_path):
    """The entire point of a payoff effort is seeing the number go down, so nothing may
    overwrite a balance in place."""
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    for observed_on, balance in [("2026-01-02", 5100.0), ("2026-02-02", 4800.0), ("2026-03-02", 4200.0)]:
        personal_db.add_debt_observation(
            path, debt_id, observed_on=observed_on, balance=balance, source="chat", confirmed=True)

    debt = personal_db.get_debt(path, owner, debt_id)
    assert debt["current_balance"] == 4200.0
    assert [p["balance"] for p in debt["balance_trend"]] == [5100.0, 4800.0, 4200.0]


def test_an_apr_stated_once_is_still_current_after_later_balance_only_statements(db_path):
    """Resolved per field, not "the newest observation wins" -- otherwise a statement that
    reported only a balance would blank out a perfectly good APR."""
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-01-02", balance=5100.0,
        apr_text="24.99%", apr=24.99, minimum_payment_text="$125.00", minimum_payment=125.0,
        source="email", source_ref="INBOX:1")
    personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-03-02", balance=4200.0, source="email", source_ref="INBOX:2")

    debt = personal_db.get_debt(path, owner, debt_id)
    assert debt["current_balance"] == 4200.0
    assert debt["current_balance_observed_on"] == "2026-03-02"
    assert debt["current_apr"] == 24.99
    assert debt["current_apr_observed_on"] == "2026-01-02"
    assert debt["current_minimum_payment"] == 125.0


def test_a_balance_that_was_only_ever_a_range_keeps_its_wording_and_no_number(db_path):
    """Same refusal to invent precision as mail_bills' amount_text/amount pairing."""
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Some Clinic", kind="medical")
    personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-02-02", balance_text="$800-$1,200", balance=None,
        source="email", source_ref="INBOX:9")

    debt = personal_db.get_debt(path, owner, debt_id)
    assert debt["current_balance"] is None
    assert debt["current_balance_text"] == "$800-$1,200"
    assert debt["balance_trend"] == []


def test_a_later_unreadable_balance_does_not_erase_the_last_real_number(db_path):
    """"Sign in to view your balance" after a statement that named one: the number is
    still the last one actually readable, and it still carries its own date."""
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Chase", account_last4="1002")
    personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-01-02", balance_text="$2,000.00", balance=2000.0,
        source="email", source_ref="INBOX:1")
    personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-02-02", balance_text="see statement", balance=None,
        source="email", source_ref="INBOX:2")

    debt = personal_db.get_debt(path, owner, debt_id)
    assert debt["current_balance"] == 2000.0
    assert debt["current_balance_observed_on"] == "2026-01-02"


def test_every_current_value_carries_where_it_came_from(db_path):
    """The provenance requirement: the UI has to be able to say "this came from a
    statement email" versus "he told me this directly"."""
    path, owner = db_path
    inferred = personal_db.create_debt(path, owner, "Navient", origin="email")
    personal_db.add_debt_observation(
        path, inferred, observed_on="2026-03-02", balance=18400.0, confidence="medium",
        source="email", source_ref="INBOX:55", source_detail="statement email from Navient")
    told = personal_db.create_debt(path, owner, "Capital One", origin="chat")
    personal_db.add_debt_observation(
        path, told, observed_on="2026-03-10", balance=4200.0, source="chat", confirmed=True)

    from_email = personal_db.get_debt(path, owner, inferred)
    from_him = personal_db.get_debt(path, owner, told)
    assert from_email["current_balance_source"] == "email"
    assert from_email["current_balance_confirmed"] is False
    assert from_him["current_balance_source"] == "chat"
    assert from_him["current_balance_confirmed"] is True


def test_a_debt_with_no_observations_at_all_reads_as_unknown_not_zero(db_path):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Some Collector", kind="collections")
    debt = personal_db.get_debt(path, owner, debt_id)
    assert debt["current_balance"] is None
    assert debt["observation_count"] == 0
    assert debt["estimated_monthly_interest"] is None


def test_monthly_interest_is_only_computed_when_both_numbers_are_real(db_path):
    path, owner = db_path
    both = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    personal_db.add_debt_observation(
        path, both, balance=4800.0, apr=25.0, source="chat", confirmed=True)
    no_apr = personal_db.create_debt(path, owner, "Chase", account_last4="1002")
    personal_db.add_debt_observation(path, no_apr, balance=4800.0, source="chat", confirmed=True)

    assert personal_db.get_debt(path, owner, both)["estimated_monthly_interest"] == 100.0
    assert personal_db.get_debt(path, owner, no_apr)["estimated_monthly_interest"] is None


# --- tracking_state keeps a classifier's guesses out of his real picture --------------

def test_proposed_debts_are_invisible_to_every_read_that_totals_his_money(db_path):
    path, owner = db_path
    tracked = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    personal_db.add_debt_observation(path, tracked, balance=4200.0, source="chat", confirmed=True)
    proposed = personal_db.create_debt(
        path, owner, "Some Collector", tracking_state="proposed", origin="email")
    personal_db.add_debt_observation(
        path, proposed, balance=9999.0, source="email", source_ref="INBOX:3")

    assert [d["id"] for d in personal_db.list_debts(path, owner)] == [tracked]
    summary = personal_db.debt_summary(path, owner)
    assert summary["total_balance"] == 4200.0
    assert summary["debt_count"] == 1
    assert summary["proposed_count"] == 1


def test_confirming_a_proposal_keeps_its_id_and_its_whole_observation_history(db_path):
    """Confirmation is a one-field flip, not a copy into another table -- the statements
    that produced it stay attached, which is the provenance."""
    path, owner = db_path
    debt_id = personal_db.create_debt(
        path, owner, "Capital One", account_last4="4821", tracking_state="proposed", origin="email")
    for month, balance in [(1, 5100.0), (2, 4800.0)]:
        personal_db.add_debt_observation(
            path, debt_id, observed_on=f"2026-{month:02d}-02", balance=balance,
            source="email", source_ref=f"INBOX:{month}")

    assert personal_db.update_debt(path, owner, debt_id, tracking_state="tracked") is True
    debt = personal_db.list_debts(path, owner)[0]
    assert debt["id"] == debt_id
    assert debt["observation_count"] == 2
    assert debt["origin"] == "email"


def test_the_status_lifecycle_moves_independently_of_tracking_state(db_path):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    for status in ("paid_off", "in_dispute", "closed", "active"):
        assert personal_db.update_debt(path, owner, debt_id, status=status) is True
        assert personal_db.get_debt(path, owner, debt_id)["status"] == status
        assert personal_db.get_debt(path, owner, debt_id)["tracking_state"] == "tracked"


@pytest.mark.parametrize("field,value", [
    ("status", "kind-of-paid"), ("tracking_state", "maybe"), ("kind", "vibes"),
])
def test_an_invalid_lifecycle_value_is_refused(db_path, field, value):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One")
    with pytest.raises(ValueError):
        personal_db.update_debt(path, owner, debt_id, **{field: value})


def test_renaming_a_creditor_moves_its_dedup_key_with_it(db_path):
    """Otherwise the row stops matching its own future statements and starts duplicating."""
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Cap 1", account_last4="4821")
    personal_db.update_debt(path, owner, debt_id, creditor="Capital One")
    assert personal_db.find_matching_debt(path, owner, "CAPITAL ONE BANK USA", "4821")["debt_id"] == debt_id


def test_another_users_debt_is_never_readable_or_writable(db_path):
    path, owner = db_path
    intruder = db.upsert_user(path, "222", "Someone", "guest")
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    assert personal_db.get_debt(path, intruder, debt_id) is None
    assert personal_db.update_debt(path, intruder, debt_id, status="closed") is False
    assert personal_db.set_debt_priority(path, intruder, debt_id, 1) is False


# --- priority is his, and only his ---------------------------------------------------

def test_priority_starts_unset_because_he_has_not_decided(db_path):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    assert personal_db.get_debt(path, owner, debt_id)["priority"] is None


def test_he_can_set_and_clear_a_payoff_priority(db_path):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    assert personal_db.set_debt_priority(path, owner, debt_id, 1) is True
    assert personal_db.get_debt(path, owner, debt_id)["priority"] == 1
    assert personal_db.set_debt_priority(path, owner, debt_id, None) is True
    assert personal_db.get_debt(path, owner, debt_id)["priority"] is None


def test_prioritized_debts_lead_the_list_in_his_order(db_path):
    path, owner = db_path
    a = personal_db.create_debt(path, owner, "Aaa Bank", account_last4="1111")
    b = personal_db.create_debt(path, owner, "Zzz Loans", account_last4="2222")
    c = personal_db.create_debt(path, owner, "Mmm Card", account_last4="3333")
    personal_db.set_debt_priority(path, owner, b, 1)
    personal_db.set_debt_priority(path, owner, c, 2)

    assert [d["id"] for d in personal_db.list_debts(path, owner)] == [b, c, a]


def test_suggesting_a_payoff_order_never_assigns_one(db_path):
    """He said assigning priorities is a joint, ongoing effort. Suggestions are offered;
    nothing writes them in as a decision."""
    path, owner = db_path
    high_rate = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    personal_db.add_debt_observation(
        path, high_rate, balance=4200.0, apr=27.99, source="chat", confirmed=True)
    small = personal_db.create_debt(path, owner, "Some Clinic", kind="medical")
    personal_db.add_debt_observation(
        path, small, balance=300.0, apr=0.0, source="chat", confirmed=True)

    summary = personal_db.debt_summary(path, owner)
    orders = summary["suggested_payoff_orders"]
    assert [o["debt_id"] for o in orders["avalanche"]["order"]] == [high_rate, small]
    assert [o["debt_id"] for o in orders["snowball"]["order"]] == [small, high_rate]
    assert all(personal_db.get_debt(path, owner, d)["priority"] is None for d in (high_rate, small))
    assert summary["unprioritized_count"] == 2


def test_a_debt_with_no_number_to_rank_by_is_listed_apart_not_sorted_as_zero(db_path):
    """Sorting an unknown balance as 0 would park it at the top of the snowball order as
    the "easiest win", which is the opposite of true."""
    path, owner = db_path
    known = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    personal_db.add_debt_observation(
        path, known, balance=4200.0, apr=27.99, source="chat", confirmed=True)
    unknown = personal_db.create_debt(path, owner, "Some Collector", kind="collections")

    orders = personal_db.debt_summary(path, owner)["suggested_payoff_orders"]
    assert [o["debt_id"] for o in orders["snowball"]["order"]] == [known]
    assert [o["debt_id"] for o in orders["snowball"]["unranked"]] == [unknown]
    assert [o["debt_id"] for o in orders["avalanche"]["unranked"]] == [unknown]


# --- summary totals are honest about what they don't know ----------------------------

def test_the_total_says_how_many_balances_it_could_not_read(db_path):
    """The whole feature exists because he doesn't know what he owes. A total presented as
    complete when it isn't would be the one genuinely harmful thing this could tell him."""
    path, owner = db_path
    known = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    personal_db.add_debt_observation(
        path, known, balance=4200.0, minimum_payment=125.0, source="chat", confirmed=True)
    personal_db.create_debt(path, owner, "Some Collector", kind="collections")

    summary = personal_db.debt_summary(path, owner)
    assert summary["total_balance"] == 4200.0
    assert summary["known_balance_count"] == 1
    assert summary["unknown_balance_count"] == 1
    assert summary["total_minimum_payment"] == 125.0


def test_the_costliest_debt_is_the_one_bleeding_the_most_interest_not_the_biggest(db_path):
    """Rates are the whole point: a big cheap mortgage costs less per month than a small
    vicious card."""
    path, owner = db_path
    big_cheap = personal_db.create_debt(path, owner, "Big Mortgage Co", kind="mortgage")
    personal_db.add_debt_observation(
        path, big_cheap, balance=200000.0, apr=3.0, source="chat", confirmed=True)
    small_vicious = personal_db.create_debt(path, owner, "Payday Lender", kind="loan")
    personal_db.add_debt_observation(
        path, small_vicious, balance=2000.0, apr=399.0, source="chat", confirmed=True)

    summary = personal_db.debt_summary(path, owner)
    assert summary["costliest_debt"]["debt_id"] == small_vicious
    assert summary["costliest_debt"]["current_apr"] == 399.0


def test_a_paid_off_debt_leaves_the_totals_but_keeps_its_history(db_path):
    path, owner = db_path
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    personal_db.add_debt_observation(path, debt_id, balance=4200.0, source="chat", confirmed=True)
    personal_db.update_debt(path, owner, debt_id, status="paid_off")

    assert personal_db.debt_summary(path, owner)["total_balance"] == 0.0
    assert personal_db.debt_summary(path, owner)["debt_count"] == 0
    assert personal_db.get_debt(path, owner, debt_id)["observation_count"] == 1


def test_an_empty_debt_tracker_summarizes_cleanly(db_path):
    path, owner = db_path
    summary = personal_db.debt_summary(path, owner)
    assert summary["debt_count"] == 0
    assert summary["total_balance"] == 0.0
    assert summary["costliest_debt"] is None
    assert summary["suggested_payoff_orders"]["avalanche"]["order"] == []
