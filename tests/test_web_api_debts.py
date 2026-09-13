"""Covers /api/debts: the debt list and its derived current values, the dated-observation
history, his payoff priority, and the confirm/dismiss verdict on what the mail sweep found.

Written alongside the feature deliberately, for the reason test_web_api_credit.py's
docstring records: /api/credit shipped with routes and components but no tests, a later
squash-merge silently deleted the twelve personal_db functions it depended on, and every
call 500'd unnoticed. The same shape of accident here would report a wrong debt total to a
man who is using this feature precisely because he doesn't know what he owes.

The rule this file pins hardest: a debt found in his email is a PROPOSAL, and no endpoint
that totals or lists debt may show it as his without him saying so.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db, mail_db, personal_db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    personal_db.init_personal_db(path)
    mail_db.init_mail_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    return path


@pytest.fixture
def owner_id(db_path):
    return next(u["id"] for u in db.all_users(db_path) if u["role"] == "owner")


def _client(db_path, login_as="Dug"):
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="pw2"),
    ])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    c = TestClient(app)
    pw = "pw" if login_as == "Dug" else "pw2"
    assert c.post("/api/login", json={"name": login_as, "password": pw}).status_code == 200
    return c


def _tracked_card(db_path, owner_id, creditor="Capital One", last4="4821",
                  balance=4218.66, apr=24.99, minimum=125.0, observed_on="2026-03-02"):
    debt_id = personal_db.create_debt(
        db_path, owner_id, creditor, account_last4=last4, kind="credit_card")
    personal_db.add_debt_observation(
        db_path, debt_id, observed_on=observed_on, balance_text=f"${balance:,.2f}",
        balance=balance, apr_text=f"{apr}%", apr=apr,
        minimum_payment_text=f"${minimum:,.2f}", minimum_payment=minimum,
        source="chat", confirmed=True)
    return debt_id


def _proposal(db_path, owner_id, creditor="Some Collector", balance=9999.0):
    debt_id = personal_db.create_debt(
        db_path, owner_id, creditor, kind="collections",
        tracking_state="proposed", origin="email",
        origin_detail="collections notice read from a message dated 2026-02-01")
    personal_db.add_debt_observation(
        db_path, debt_id, observed_on="2026-02-01", balance_text=f"${balance:,.2f}",
        balance=balance, source="email", source_ref="INBOX:7",
        source_detail="FINAL NOTICE — from collections@example.com", confidence="medium")
    business_db.create_review_item(
        db_path, owner_id, title=f"Debt found in your mail: {creditor}",
        kind="other", source_agent="mail_debts", ref_table="debts", ref_id=debt_id)
    return debt_id


# --- auth ------------------------------------------------------------------------------

def test_requires_login(db_path):
    cfg = FakeConfig(db_path=db_path)
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/debts").status_code == 401


@pytest.mark.parametrize("path", ["", "/summary", "/proposals", "/sweep-status"])
def test_partner_is_refused_owner_only_access(db_path, path):
    """His debts are his, not the household's."""
    assert _client(db_path, login_as="Partner").get(f"/api/debts{path}").status_code == 403


# --- listing ---------------------------------------------------------------------------

def test_an_empty_tracker_lists_nothing_and_totals_zero(db_path):
    c = _client(db_path)
    assert c.get("/api/debts").json() == []
    summary = c.get("/api/debts/summary").json()
    assert summary["debt_count"] == 0
    assert summary["total_balance"] == 0.0


def test_a_debt_comes_back_with_its_current_values_and_their_dates(db_path, owner_id):
    _tracked_card(db_path, owner_id)
    debt = _client(db_path).get("/api/debts").json()[0]

    assert debt["creditor"] == "Capital One"
    assert debt["account_last4"] == "4821"
    assert debt["current_balance"] == 4218.66
    assert debt["current_balance_observed_on"] == "2026-03-02"
    assert debt["current_apr"] == 24.99
    assert debt["current_minimum_payment"] == 125.0
    assert debt["estimated_monthly_interest"] == 87.85


def test_a_debt_carries_the_provenance_of_its_current_balance(db_path, owner_id):
    """"He told me this" versus "this came from a statement email" is the distinction the
    UI has to be able to draw."""
    told = _tracked_card(db_path, owner_id)
    inferred = personal_db.create_debt(db_path, owner_id, "Navient", kind="student_loan")
    personal_db.add_debt_observation(
        db_path, inferred, observed_on="2026-03-02", balance=18400.0,
        source="email", source_ref="INBOX:55", confidence="medium")

    debts = {d["id"]: d for d in _client(db_path).get("/api/debts").json()}
    assert debts[told]["current_balance_source"] == "chat"
    assert debts[told]["current_balance_confirmed"] is True
    assert debts[inferred]["current_balance_source"] == "email"
    assert debts[inferred]["current_balance_confirmed"] is False


def test_the_balance_trend_comes_back_ready_to_plot(db_path, owner_id):
    debt_id = personal_db.create_debt(db_path, owner_id, "Capital One", account_last4="4821")
    for observed_on, balance in [("2026-01-02", 5100.0), ("2026-02-02", 4800.0), ("2026-03-02", 4200.0)]:
        personal_db.add_debt_observation(
            db_path, debt_id, observed_on=observed_on, balance=balance, source="chat", confirmed=True)

    trend = _client(db_path).get("/api/debts").json()[0]["balance_trend"]
    assert [p["balance"] for p in trend] == [5100.0, 4800.0, 4200.0]
    assert [p["observed_on"] for p in trend] == ["2026-01-02", "2026-02-02", "2026-03-02"]


def test_a_proposal_is_hidden_from_the_debt_list_and_every_total(db_path, owner_id):
    """A classifier's guess must never move his debt total."""
    _tracked_card(db_path, owner_id)
    _proposal(db_path, owner_id)
    c = _client(db_path)

    assert len(c.get("/api/debts").json()) == 1
    summary = c.get("/api/debts/summary").json()
    assert summary["total_balance"] == 4218.66
    assert summary["proposed_count"] == 1


def test_proposals_can_be_asked_for_explicitly(db_path, owner_id):
    _tracked_card(db_path, owner_id)
    proposed = _proposal(db_path, owner_id)
    c = _client(db_path)

    assert len(c.get("/api/debts?include_proposed=true").json()) == 2
    proposals = c.get("/api/debts/proposals").json()
    assert [p["id"] for p in proposals] == [proposed]
    assert proposals[0]["origin"] == "email"
    assert "collections notice" in proposals[0]["origin_detail"]


def test_the_list_can_be_narrowed_by_status(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    personal_db.update_debt(db_path, owner_id, debt_id, status="paid_off")
    c = _client(db_path)

    assert c.get("/api/debts?status=active").json() == []
    assert len(c.get("/api/debts?status=paid_off").json()) == 1


def test_a_nonsense_status_filter_is_refused(db_path):
    assert _client(db_path).get("/api/debts?status=sort-of-paid").status_code == 400


# --- summary ----------------------------------------------------------------------------

def test_the_summary_reports_the_total_as_a_floor_when_a_balance_is_unknown(db_path, owner_id):
    _tracked_card(db_path, owner_id)
    personal_db.create_debt(db_path, owner_id, "Some Collector", kind="collections")

    summary = _client(db_path).get("/api/debts/summary").json()
    assert summary["total_balance"] == 4218.66
    assert summary["known_balance_count"] == 1
    assert summary["unknown_balance_count"] == 1


def test_the_summary_names_the_debt_that_costs_the_most_not_the_biggest(db_path, owner_id):
    big_cheap = personal_db.create_debt(db_path, owner_id, "Big Mortgage Co", kind="mortgage")
    personal_db.add_debt_observation(
        db_path, big_cheap, balance=200000.0, apr=3.0, source="chat", confirmed=True)
    small_vicious = personal_db.create_debt(db_path, owner_id, "Payday Lender", kind="loan")
    personal_db.add_debt_observation(
        db_path, small_vicious, balance=2000.0, apr=399.0, source="chat", confirmed=True)

    summary = _client(db_path).get("/api/debts/summary").json()
    assert summary["costliest_debt"]["debt_id"] == small_vicious


def test_the_summary_offers_both_payoff_orderings_and_assigns_neither(db_path, owner_id):
    """He said assigning priorities is a joint, ongoing effort."""
    card = _tracked_card(db_path, owner_id, apr=27.99, balance=4200.0)
    clinic = personal_db.create_debt(db_path, owner_id, "Some Clinic", kind="medical")
    personal_db.add_debt_observation(
        db_path, clinic, balance=300.0, apr=0.0, source="chat", confirmed=True)

    orders = _client(db_path).get("/api/debts/summary").json()["suggested_payoff_orders"]
    assert [o["debt_id"] for o in orders["avalanche"]["order"]] == [card, clinic]
    assert [o["debt_id"] for o in orders["snowball"]["order"]] == [clinic, card]
    assert all(d["priority"] is None for d in _client(db_path).get("/api/debts").json())


# --- creating and editing ----------------------------------------------------------------

def test_he_can_add_a_debt_by_hand(db_path):
    c = _client(db_path)
    created = c.post("/api/debts", json={
        "creditor": "Capital One", "account_last4": "4821", "kind": "credit_card", "due_day": 27})
    assert created.status_code == 200

    debt = c.get("/api/debts").json()[0]
    assert debt["id"] == created.json()["debt_id"]
    assert debt["origin"] == "manual"
    assert debt["due_day"] == 27
    assert debt["current_balance"] is None


def test_a_hand_entered_debt_is_tracked_immediately_not_proposed(db_path):
    c = _client(db_path)
    c.post("/api/debts", json={"creditor": "Capital One"})
    assert c.get("/api/debts").json()[0]["tracking_state"] == "tracked"


def test_a_full_account_number_cannot_be_posted_in(db_path):
    c = _client(db_path)
    c.post("/api/debts", json={"creditor": "Capital One", "account_last4": "4111111111114821"})
    assert c.get("/api/debts").json()[0]["account_last4"] == "4821"


@pytest.mark.parametrize("body", [
    {"creditor": ""}, {"creditor": "X", "kind": "vibes"}, {"creditor": "X", "due_day": 55},
])
def test_a_bad_new_debt_is_refused(db_path, body):
    assert _client(db_path).post("/api/debts", json=body).status_code == 400


def test_he_can_correct_a_debt_and_move_it_through_its_lifecycle(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    c = _client(db_path)

    assert c.put(f"/api/debts/{debt_id}", json={"creditor": "Capital One Bank"}).status_code == 200
    assert c.put(f"/api/debts/{debt_id}", json={"status": "paid_off"}).status_code == 200
    assert c.get("/api/debts?status=paid_off").json()[0]["creditor"] == "Capital One Bank"


def test_editing_a_debt_never_touches_its_balance_history(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    c = _client(db_path)
    c.put(f"/api/debts/{debt_id}", json={"creditor": "Capital One Bank", "notes": "moved to autopay"})

    observations = c.get(f"/api/debts/{debt_id}/observations").json()
    assert len(observations) == 1
    assert observations[0]["balance"] == 4218.66


def test_editing_a_debt_that_is_not_his_is_a_404(db_path):
    assert _client(db_path).put("/api/debts/999", json={"status": "closed"}).status_code == 404


def test_an_invalid_status_on_an_edit_is_refused(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    assert _client(db_path).put(
        f"/api/debts/{debt_id}", json={"status": "sort-of-paid"}).status_code == 400


# --- observations ------------------------------------------------------------------------

def test_recording_a_balance_adds_a_dated_observation_rather_than_overwriting(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    c = _client(db_path)

    assert c.post(f"/api/debts/{debt_id}/observations",
                  json={"balance": 3980.0, "observed_on": "2026-04-02"}).status_code == 200

    debt = c.get("/api/debts").json()[0]
    assert debt["current_balance"] == 3980.0
    assert debt["observation_count"] == 2
    assert [p["balance"] for p in debt["balance_trend"]] == [4218.66, 3980.0]


def test_a_hand_entered_observation_counts_as_confirmed_by_him(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    c = _client(db_path)
    c.post(f"/api/debts/{debt_id}/observations", json={"balance": 3980.0, "observed_on": "2026-04-02"})

    latest = c.get(f"/api/debts/{debt_id}/observations").json()[-1]
    assert latest["source"] == "manual"
    assert latest["confirmed"] == 1


def test_the_observation_history_shows_where_each_number_came_from(db_path, owner_id):
    debt_id = _proposal(db_path, owner_id)
    observation = _client(db_path).get(f"/api/debts/{debt_id}/observations").json()[0]
    assert observation["source"] == "email"
    assert observation["source_ref"] == "INBOX:7"
    assert "collections@example.com" in observation["source_detail"]


def test_an_observation_with_nothing_in_it_is_refused(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    assert _client(db_path).post(
        f"/api/debts/{debt_id}/observations", json={"observed_on": "2026-04-02"}).status_code == 400


def test_a_non_numeric_balance_is_refused_rather_than_coerced(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    assert _client(db_path).post(
        f"/api/debts/{debt_id}/observations", json={"balance": "loads"}).status_code == 400


def test_observations_for_a_debt_that_is_not_his_are_a_404(db_path):
    c = _client(db_path)
    assert c.get("/api/debts/999/observations").status_code == 404
    assert c.post("/api/debts/999/observations", json={"balance": 1.0}).status_code == 404


# --- priority ----------------------------------------------------------------------------

def test_he_can_set_and_clear_a_payoff_priority(db_path, owner_id):
    debt_id = _tracked_card(db_path, owner_id)
    c = _client(db_path)

    assert c.put(f"/api/debts/{debt_id}/priority", json={"priority": 1}).status_code == 200
    assert c.get("/api/debts").json()[0]["priority"] == 1
    assert c.put(f"/api/debts/{debt_id}/priority", json={"priority": None}).status_code == 200
    assert c.get("/api/debts").json()[0]["priority"] is None


def test_prioritized_debts_lead_the_list_in_his_order(db_path, owner_id):
    first = _tracked_card(db_path, owner_id, creditor="Aaa Bank", last4="1111")
    second = _tracked_card(db_path, owner_id, creditor="Zzz Loans", last4="2222")
    c = _client(db_path)
    c.put(f"/api/debts/{second}/priority", json={"priority": 1})

    assert [d["id"] for d in c.get("/api/debts").json()] == [second, first]


@pytest.mark.parametrize("priority", [0, -1, "first", 1.5])
def test_a_nonsense_priority_is_refused(db_path, owner_id, priority):
    debt_id = _tracked_card(db_path, owner_id)
    assert _client(db_path).put(
        f"/api/debts/{debt_id}/priority", json={"priority": priority}).status_code == 400


# --- resolving what the sweep proposed -----------------------------------------------------

def test_confirming_a_proposal_tracks_it_and_keeps_its_history(db_path, owner_id):
    debt_id = _proposal(db_path, owner_id)
    c = _client(db_path)

    assert c.post(f"/api/debts/{debt_id}/resolve", json={"verdict": "confirm"}).status_code == 200

    tracked = c.get("/api/debts").json()
    assert [d["id"] for d in tracked] == [debt_id]
    assert tracked[0]["observation_count"] == 1
    assert c.get("/api/debts/summary").json()["total_balance"] == 9999.0


def test_confirming_a_proposal_clears_its_review_card(db_path, owner_id):
    """The web path has to close the card too, or a decision made here leaves the same
    question dangling on the Review page."""
    debt_id = _proposal(db_path, owner_id)
    _client(db_path).post(f"/api/debts/{debt_id}/resolve", json={"verdict": "confirm"})
    assert business_db.list_review_items(db_path, owner_id, status="pending") == []


def test_dismissing_a_proposal_removes_it_from_everything(db_path, owner_id):
    debt_id = _proposal(db_path, owner_id)
    c = _client(db_path)

    assert c.post(f"/api/debts/{debt_id}/resolve", json={"verdict": "dismiss"}).status_code == 200
    assert c.get("/api/debts").json() == []
    assert c.get("/api/debts/proposals").json() == []
    assert business_db.list_review_items(db_path, owner_id, status="pending") == []


def test_a_debt_already_ruled_on_cannot_be_resolved_again(db_path, owner_id):
    debt_id = _proposal(db_path, owner_id)
    c = _client(db_path)
    c.post(f"/api/debts/{debt_id}/resolve", json={"verdict": "confirm"})
    assert c.post(f"/api/debts/{debt_id}/resolve", json={"verdict": "dismiss"}).status_code == 404


def test_an_unrecognised_verdict_is_refused(db_path, owner_id):
    debt_id = _proposal(db_path, owner_id)
    assert _client(db_path).post(
        f"/api/debts/{debt_id}/resolve", json={"verdict": "maybe"}).status_code == 400


# --- sweep progress -----------------------------------------------------------------------

def test_sweep_status_reports_how_far_through_his_mail_it_has_got(db_path, owner_id):
    """A resumable backlog pass has to be able to answer "have you finished looking?"."""
    mail_db.mark_debt_scanned(db_path, owner_id, "INBOX", "1", is_debt=True, debt_id=1)
    mail_db.mark_debt_scanned(db_path, owner_id, "INBOX", "2", is_debt=False)
    mail_db.mark_debt_scanned(db_path, owner_id, "Archive", "9", is_debt=True, debt_id=1)

    status = _client(db_path).get("/api/debts/sweep-status").json()
    assert status["messages_judged"] == 3
    assert status["debt_messages"] == 2
    assert {f["folder"] for f in status["by_folder"]} == {"INBOX", "Archive"}


def test_sweep_status_on_a_mailbox_never_swept_is_empty_not_an_error(db_path):
    status = _client(db_path).get("/api/debts/sweep-status").json()
    assert status["messages_judged"] == 0
    assert status["by_folder"] == []


# --- deciding the sweep's card on the Review page ------------------------------------------
# The sweep files its cards into the Review queue, so this is the PRIMARY path he will
# actually use to confirm a debt. A card that closed without flipping the debt would leave
# an approved proposal invisible in every total, with nothing to show his answer hadn't landed.

def _decide(c, item_id, decision, note=None):
    return c.post(f"/api/review/items/{item_id}/decide",
                  json={"decision": decision, "note": note})


def test_approving_the_review_card_actually_tracks_the_debt(db_path, owner_id):
    debt_id = _proposal(db_path, owner_id)
    c = _client(db_path)
    item_id = business_db.list_review_items(db_path, owner_id, status="pending")[0]["id"]

    assert _decide(c, item_id, "approved").status_code == 200

    tracked = c.get("/api/debts").json()
    assert [d["id"] for d in tracked] == [debt_id]
    assert c.get("/api/debts/summary").json()["total_balance"] == 9999.0


def test_approving_the_review_card_keeps_the_evidence_behind_it(db_path, owner_id):
    debt_id = _proposal(db_path, owner_id)
    c = _client(db_path)
    item_id = business_db.list_review_items(db_path, owner_id, status="pending")[0]["id"]
    _decide(c, item_id, "approved")

    observations = c.get(f"/api/debts/{debt_id}/observations").json()
    assert len(observations) == 1
    assert observations[0]["source"] == "email"
    assert observations[0]["source_ref"] == "INBOX:7"


def test_rejecting_the_review_card_dismisses_the_debt_for_good(db_path, owner_id):
    _proposal(db_path, owner_id)
    c = _client(db_path)
    item_id = business_db.list_review_items(db_path, owner_id, status="pending")[0]["id"]

    assert _decide(c, item_id, "rejected").status_code == 200

    assert c.get("/api/debts").json() == []
    assert c.get("/api/debts/proposals").json() == []
    assert personal_db.find_matching_debt(db_path, owner_id, "Some Collector", "")["debt_id"] is None


def test_the_review_decision_never_assigns_a_payoff_priority(db_path, owner_id):
    """Confirming that a debt is his is not the same as deciding when to pay it."""
    debt_id = _proposal(db_path, owner_id)
    c = _client(db_path)
    item_id = business_db.list_review_items(db_path, owner_id, status="pending")[0]["id"]
    _decide(c, item_id, "approved")

    assert personal_db.get_debt(db_path, owner_id, debt_id)["priority"] is None
    assert c.get("/api/debts/summary").json()["unprioritized_count"] == 1


def test_the_review_decision_reports_what_it_wrote_through(db_path, owner_id):
    _proposal(db_path, owner_id)
    c = _client(db_path)
    item_id = business_db.list_review_items(db_path, owner_id, status="pending")[0]["id"]

    body = _decide(c, item_id, "approved").json()
    assert "debts#" in str(body)
    assert "tracked" in str(body)


def test_a_debt_already_resolved_elsewhere_is_left_alone_by_the_card(db_path, owner_id):
    """He may confirm in chat and only later get to the Review page -- the second decision
    must not flip a debt he has since changed his mind about."""
    debt_id = _proposal(db_path, owner_id)
    c = _client(db_path)
    item_id = business_db.list_review_items(db_path, owner_id, status="pending")[0]["id"]
    c.post(f"/api/debts/{debt_id}/resolve", json={"verdict": "confirm"})
    personal_db.update_debt(db_path, owner_id, debt_id, status="paid_off")

    # The card was already closed by the resolve above, so this is a no-op either way.
    _decide(c, item_id, "rejected")
    debt = personal_db.get_debt(db_path, owner_id, debt_id)
    assert debt["tracking_state"] == "tracked"
    assert debt["status"] == "paid_off"


def test_an_ambiguity_card_with_no_debt_row_decides_without_crashing(db_path, owner_id):
    """The sweep's "which account is this?" card carries ref_id=None -- there is no debt
    row to write through to, and the answer goes through chat instead."""
    business_db.create_review_item(
        db_path, owner_id, title="Which Capital One account is this?", kind="other",
        source_agent="mail_debts", ref_table="debts", ref_id=None)
    c = _client(db_path)
    item_id = business_db.list_review_items(db_path, owner_id, status="pending")[0]["id"]

    assert _decide(c, item_id, "approved").status_code == 200
    assert c.get("/api/debts").json() == []
