"""The debt chat tools (personal_tools.py) -- the second channel the debt picture is
assembled through.

The owner said it himself: "when text messages come in ill let jarvis kow and he can add
them as well". A balance that arrives by text has no email to sweep and never will, so
"got a text from Capital One, balance is $4,200" has to land against the right creditor
from conversation alone. That, and the refusal to guess when it can't tell which account
he means, is what these tests pin.

Uses a real PersonalClient so the delegation into personal_db is actually exercised, same
as test_personal_tools_finance.py.
"""
import pytest

from assistant.core import business_db, db, mail_db, personal_db
from assistant.core.personal_tools import PERSONAL_SYSTEM_NOTE, PERSONAL_TOOLS, PersonalClient


@pytest.fixture
def client(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    personal_db.init_personal_db(path)
    mail_db.init_mail_db(path)
    owner_id = db.upsert_user(path, "111", "Dug", "owner")
    return PersonalClient(path, owner_id), path, owner_id


DEBT_TOOL_NAMES = {
    "list_debts", "record_debt_balance", "update_debt", "set_debt_priority",
    "get_debt_summary", "list_debt_proposals", "resolve_debt_proposal",
}


def test_every_debt_tool_is_exposed_and_dispatchable(client):
    """The exact failure test_web_api_credit.py's docstring records: routes and components
    both existed, nobody wired the middle, and it 500'd unnoticed."""
    tools, (personal, path, owner) = {t["function"]["name"] for t in PERSONAL_TOOLS}, client
    assert DEBT_TOOL_NAMES <= tools
    for name in DEBT_TOOL_NAMES:
        result = personal.call_tool(name, {"debt_id": 1, "verdict": "confirm", "priority": 1})
        assert "unknown personal tool" not in str(result)


def test_the_system_note_tells_the_model_to_ask_rather_than_guess():
    note = PERSONAL_SYSTEM_NOTE.lower()
    assert "needs_disambiguation" in note
    assert "never guess" in note
    assert "joint" in note and "priority" in note


# --- "got a text from Capital One, balance is $4,200" ---------------------------------

def test_a_balance_he_states_creates_a_tracked_debt_straight_away(client):
    """Not a proposal: a thing he said is not a guess awaiting his own confirmation."""
    personal, path, owner = client
    result = personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 4200.0, "account_last4": "4821"})

    assert result["ok"] is True
    assert result["created_debt"] is True
    debt = personal_db.list_debts(path, owner)[0]
    assert debt["creditor"] == "Capital One"
    assert debt["account_last4"] == "4821"
    assert debt["tracking_state"] == "tracked"
    assert debt["origin"] == "chat"
    assert debt["current_balance"] == 4200.0
    assert debt["current_balance_confirmed"] is True


def test_a_second_balance_for_the_same_creditor_extends_the_trend_not_the_list(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 4200.0, "observed_on": "2026-03-10"})
    personal.call_tool("record_debt_balance", {
        "creditor": "capital one bank", "balance": 3980.0, "observed_on": "2026-04-10"})

    debts = personal_db.list_debts(path, owner)
    assert len(debts) == 1
    assert debts[0]["observation_count"] == 2
    assert debts[0]["current_balance"] == 3980.0
    assert [p["balance"] for p in debts[0]["balance_trend"]] == [4200.0, 3980.0]


def test_a_balance_he_states_attaches_to_a_debt_the_mail_sweep_proposed(client):
    """He confirms by talking about it. The proposal is matched, not duplicated."""
    personal, path, owner = client
    proposed = personal_db.create_debt(
        path, owner, "Navient", account_last4="7781", kind="student_loan",
        tracking_state="proposed", origin="email")

    result = personal.call_tool("record_debt_balance", {
        "creditor": "Navient", "balance": 18400.0, "account_last4": "7781"})

    assert result["debt_id"] == proposed
    assert result["created_debt"] is False
    assert len(personal_db.list_debts(path, owner, tracking_state=None)) == 1


def test_a_balance_he_states_teaches_an_existing_debt_its_account_number(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {"creditor": "Capital One", "balance": 4200.0})
    personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 3980.0, "account_last4": "4821"})

    debts = personal_db.list_debts(path, owner)
    assert len(debts) == 1
    assert debts[0]["account_last4"] == "4821"


def test_two_accounts_with_one_creditor_asks_him_which_and_records_nothing(client):
    """The one case where a wrong guess corrupts two debts at once and can't be spotted
    afterwards."""
    personal, path, owner = client
    a = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    b = personal_db.create_debt(path, owner, "Capital One", account_last4="9900")

    result = personal.call_tool("record_debt_balance", {"creditor": "Capital One", "balance": 4200.0})

    assert result["needs_disambiguation"] is True
    assert sorted(c["debt_id"] for c in result["candidates"]) == sorted([a, b])
    assert "do not pick one" in result["message"].lower()
    assert personal_db.get_debt(path, owner, a)["observation_count"] == 0
    assert personal_db.get_debt(path, owner, b)["observation_count"] == 0


def test_naming_the_account_after_the_question_records_it_against_the_right_debt(client):
    personal, path, owner = client
    a = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")
    b = personal_db.create_debt(path, owner, "Capital One", account_last4="9900")

    result = personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 4200.0, "debt_id": b})

    assert result["ok"] is True
    assert personal_db.get_debt(path, owner, a)["observation_count"] == 0
    assert personal_db.get_debt(path, owner, b)["current_balance"] == 4200.0


def test_a_balance_that_is_not_one_clear_number_keeps_his_wording(client):
    """"Somewhere between 800 and 1200" is real information; inventing a figure from it
    is not."""
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {
        "creditor": "Some Clinic", "balance_text": "somewhere between $800 and $1,200",
        "kind": "medical"})

    debt = personal_db.list_debts(path, owner)[0]
    assert debt["current_balance"] is None
    assert debt["current_balance_text"] == "somewhere between $800 and $1,200"


def test_a_rate_and_a_minimum_payment_ride_along_with_the_balance(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 4200.0, "apr": 24.99, "minimum_payment": 125.0})

    debt = personal_db.list_debts(path, owner)[0]
    assert debt["current_apr"] == 24.99
    assert debt["current_apr_text"] == "24.99%"
    assert debt["current_minimum_payment"] == 125.0
    # 4200 * 24.99% / 12 -- what this card actually costs him to carry for a month.
    assert debt["estimated_monthly_interest"] == 87.46


def test_recording_nothing_at_all_is_refused(client):
    personal, _, _ = client
    result = personal.call_tool("record_debt_balance", {"creditor": "Capital One"})
    assert "error" in result


def test_a_full_account_number_given_in_chat_is_cut_down_before_storage(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 4200.0, "account_last4": "4111 1111 1111 4821"})
    assert personal_db.list_debts(path, owner)[0]["account_last4"] == "4821"


def test_an_invalid_debt_kind_is_refused_rather_than_raising(client):
    personal, _, _ = client
    assert "error" in personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 1.0, "kind": "vibes"})


# --- listing -------------------------------------------------------------------------

def test_listing_debts_hides_unconfirmed_email_finds_by_default(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {"creditor": "Capital One", "balance": 4200.0})
    personal_db.create_debt(path, owner, "Some Collector", tracking_state="proposed", origin="email")

    assert len(personal.call_tool("list_debts", {})["debts"]) == 1
    assert len(personal.call_tool("list_debts", {"include_proposed": True})["debts"]) == 2


def test_proposals_come_back_labelled_as_guesses_not_debts(client):
    personal, path, owner = client
    personal_db.create_debt(path, owner, "Some Collector", tracking_state="proposed", origin="email")

    result = personal.call_tool("list_debt_proposals", {})
    assert len(result["proposals"]) == 1
    assert "not confirmed" in result["note"].lower()
    assert "excluded from every debt total" in result["note"].lower()


def test_listing_can_be_narrowed_to_a_lifecycle_status(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {"creditor": "Capital One", "balance": 4200.0})
    debt_id = personal_db.list_debts(path, owner)[0]["id"]
    personal.call_tool("update_debt", {"debt_id": debt_id, "status": "paid_off"})

    assert personal.call_tool("list_debts", {"status": "active"})["debts"] == []
    assert len(personal.call_tool("list_debts", {"status": "paid_off"})["debts"]) == 1


# --- updating -------------------------------------------------------------------------

def test_he_can_correct_a_creditor_name_and_it_still_matches_its_own_statements(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {"creditor": "Cap 1", "balance": 4200.0})
    debt_id = personal_db.list_debts(path, owner)[0]["id"]

    personal.call_tool("update_debt", {"debt_id": debt_id, "creditor": "Capital One"})
    personal.call_tool("record_debt_balance", {"creditor": "CAPITAL ONE BANK USA", "balance": 3980.0})

    assert len(personal_db.list_debts(path, owner)) == 1


def test_marking_a_debt_paid_off_keeps_every_balance_he_ever_reported(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 4200.0, "observed_on": "2026-03-10"})
    debt_id = personal_db.list_debts(path, owner)[0]["id"]
    personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 0.0, "observed_on": "2026-08-10"})

    assert personal.call_tool("update_debt", {"debt_id": debt_id, "status": "paid_off"})["ok"] is True
    assert personal_db.get_debt(path, owner, debt_id)["observation_count"] == 2
    assert personal.call_tool("get_debt_summary", {})["total_balance"] == 0.0


def test_updating_a_debt_that_does_not_exist_is_an_error_not_a_crash(client):
    personal, _, _ = client
    assert "error" in personal.call_tool("update_debt", {"debt_id": 999, "status": "closed"})


def test_an_invalid_status_is_refused_rather_than_raising(client):
    personal, path, owner = client
    debt_id = personal_db.create_debt(path, owner, "Capital One")
    assert "error" in personal.call_tool("update_debt", {"debt_id": debt_id, "status": "sort of paid"})


# --- priority is his ------------------------------------------------------------------

def test_he_can_set_and_clear_a_payoff_priority_conversationally(client):
    personal, path, owner = client
    debt_id = personal_db.create_debt(path, owner, "Capital One", account_last4="4821")

    assert personal.call_tool("set_debt_priority", {"debt_id": debt_id, "priority": 1})["ok"] is True
    assert personal_db.get_debt(path, owner, debt_id)["priority"] == 1
    assert personal.call_tool("set_debt_priority", {"debt_id": debt_id, "priority": 0})["ok"] is True
    assert personal_db.get_debt(path, owner, debt_id)["priority"] is None


def test_asking_for_the_summary_ranks_both_strategies_and_assigns_neither(client):
    """Suggest and discuss; never hand him a decided plan."""
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {
        "creditor": "Capital One", "balance": 4200.0, "apr": 27.99})
    personal.call_tool("record_debt_balance", {
        "creditor": "Some Clinic", "balance": 300.0, "apr": 0.0, "kind": "medical"})

    summary = personal.call_tool("get_debt_summary", {})
    orders = summary["suggested_payoff_orders"]
    assert orders["avalanche"]["order"][0]["creditor"] == "Capital One"
    assert orders["snowball"]["order"][0]["creditor"] == "Some Clinic"
    assert summary["unprioritized_count"] == 2
    assert all(d["priority"] is None for d in personal_db.list_debts(path, owner))


def test_the_summary_says_how_much_of_the_picture_is_still_unknown(client):
    personal, path, owner = client
    personal.call_tool("record_debt_balance", {"creditor": "Capital One", "balance": 4200.0})
    personal_db.create_debt(path, owner, "Some Collector", kind="collections")

    summary = personal.call_tool("get_debt_summary", {})
    assert summary["total_balance"] == 4200.0
    assert summary["unknown_balance_count"] == 1


# --- resolving what the sweep proposed -------------------------------------------------

def _proposal_with_card(path, owner):
    debt_id = personal_db.create_debt(
        path, owner, "Capital One", account_last4="4821", kind="credit_card",
        tracking_state="proposed", origin="email")
    personal_db.add_debt_observation(
        path, debt_id, observed_on="2026-03-02", balance_text="$4,218.66", balance=4218.66,
        source="email", source_ref="INBOX:1")
    business_db.create_review_item(
        path, owner, title="Debt found in your mail: Capital One ending 4821",
        kind="other", source_agent="mail_debts", ref_table="debts", ref_id=debt_id)
    return debt_id


def test_confirming_a_proposal_tracks_it_and_keeps_its_whole_history(client):
    personal, path, owner = client
    debt_id = _proposal_with_card(path, owner)

    result = personal.call_tool("resolve_debt_proposal", {"debt_id": debt_id, "verdict": "confirm"})

    assert result["tracking_state"] == "tracked"
    tracked = personal_db.list_debts(path, owner)
    assert len(tracked) == 1
    assert tracked[0]["id"] == debt_id
    assert tracked[0]["observation_count"] == 1
    assert personal.call_tool("get_debt_summary", {})["total_balance"] == 4218.66


def test_confirming_a_proposal_clears_its_review_card(client):
    """Otherwise the same question sits on the Review page forever -- the dangling-card
    problem get_review_item_by_ref exists to solve."""
    personal, path, owner = client
    debt_id = _proposal_with_card(path, owner)

    personal.call_tool("resolve_debt_proposal", {"debt_id": debt_id, "verdict": "confirm"})

    assert business_db.list_review_items(path, owner, status="pending") == []


def test_dismissing_a_proposal_keeps_it_out_of_everything_for_good(client):
    personal, path, owner = client
    debt_id = _proposal_with_card(path, owner)

    personal.call_tool("resolve_debt_proposal", {"debt_id": debt_id, "verdict": "dismiss"})

    assert personal_db.list_debts(path, owner) == []
    assert personal.call_tool("list_debt_proposals", {})["proposals"] == []
    assert business_db.list_review_items(path, owner, status="pending") == []
    # And a later statement never resurrects it.
    assert personal_db.find_matching_debt(path, owner, "Capital One", "4821")["debt_id"] is None


def test_a_debt_he_already_ruled_on_cannot_be_resolved_twice(client):
    personal, path, owner = client
    debt_id = _proposal_with_card(path, owner)
    personal.call_tool("resolve_debt_proposal", {"debt_id": debt_id, "verdict": "confirm"})

    again = personal.call_tool("resolve_debt_proposal", {"debt_id": debt_id, "verdict": "dismiss"})
    assert "error" in again
    assert personal_db.get_debt(path, owner, debt_id)["tracking_state"] == "tracked"


@pytest.mark.parametrize("verdict", ["maybe", "", "approve"])
def test_an_unrecognised_verdict_is_refused(client, verdict):
    personal, path, owner = client
    debt_id = _proposal_with_card(path, owner)
    assert "error" in personal.call_tool(
        "resolve_debt_proposal", {"debt_id": debt_id, "verdict": verdict})
