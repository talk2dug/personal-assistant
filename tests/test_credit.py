"""Credit: the deadlines, the arithmetic, and whether Jarvis can actually work it.

The goal behind all of it is a mortgage in a few years, which means the failure that
matters is not a crash — it is a number stated confidently and wrongly. Three things carry
that risk and each is pinned here:

  - A dispute deadline runs from RECEIPT, not from posting. Getting that wrong either
    chases a bureau early (and the dispute is dismissed as premature) or lets a blown
    window pass unnoticed, which is the one piece of real leverage in the whole process.
  - What a bureau SAYS and what he OWES are different facts. Paying a collection does not
    remove the mark.
  - Every tool is executed, not just declared. A tool that is listed but never wired
    returns success and changes nothing.
"""
from datetime import date, timedelta

import pytest

from assistant.core import credit, db as core_db, personal_db
from assistant.core.personal_tools import PERSONAL_TOOLS, PersonalClient

TODAY = date(2026, 9, 16)

CREDIT_TOOLS = ("get_credit_picture", "add_credit_report", "add_tradeline",
                "suggest_credit_line", "update_credit_recommendation",
                "record_letter_delivered", "record_dispute_response")


@pytest.fixture
def client(tmp_path):
    path = str(tmp_path / "credit.db")
    core_db.init_db(path)
    personal_db.init_personal_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    owner = core_db.get_user_by_chat_id(path, "111")["id"]
    return PersonalClient(path, owner, tz_name="America/New_York")


class TestTheDisputeClock:
    """FCRA §611: 30 days from receipt, 45 if the consumer adds information during it."""

    def test_delivery_confirmation_starts_the_clock(self):
        deadline = credit.response_deadline("2026-09-01", delivered_at="2026-09-04")
        assert deadline["due_on"] == "2026-10-04"
        assert deadline["certain"] is True

    def test_without_delivery_it_estimates_and_says_so(self):
        """A deadline you cannot defend is one you cannot enforce, so an estimate has to
        announce itself rather than pass as fact."""
        deadline = credit.response_deadline("2026-09-01")
        assert deadline["certain"] is False
        assert "estimated" in deadline["basis"]

    def test_the_estimate_is_conservative_rather_than_optimistic(self):
        """Chasing a bureau early is how a well-founded dispute gets dismissed."""
        certain = credit.response_deadline("2026-09-01", delivered_at="2026-09-01")
        estimated = credit.response_deadline("2026-09-01")
        assert estimated["due_on"] > certain["due_on"]

    def test_the_extended_window_is_forty_five_days(self):
        deadline = credit.response_deadline("2026-09-01", delivered_at="2026-09-01",
                                            extended=True)
        assert deadline["due_on"] == "2026-10-16"
        assert deadline["window_days"] == 45

    def test_a_letter_never_posted_has_no_deadline(self):
        assert credit.response_deadline(None) is None

    def test_an_unparseable_date_does_not_invent_a_deadline(self):
        assert credit.response_deadline("not a date") is None


class TestWhenAMarkAgesOffByItself:
    def test_seven_years_from_first_delinquency(self):
        """Outlasting costs nothing and works every time, so the date is worth knowing
        before recommending a fight."""
        assert credit.falls_off_on("2021-03-11") == "2028-03-11"

    def test_an_unknown_first_delinquency_gives_no_date(self):
        assert credit.falls_off_on(None) is None
        assert credit.falls_off_on("sometime in 2021") is None


class TestUtilisation:
    def _card(self, creditor, balance, limit, **kw):
        return {"creditor": creditor, "kind": "credit_card", "balance": balance,
                "credit_limit": limit, **kw}

    def test_it_reports_the_overall_ratio(self):
        util = credit.utilisation([self._card("A", 300, 1000), self._card("B", 200, 1000)])
        assert util["overall"] == pytest.approx(0.25)

    def test_it_reports_each_card_worst_first(self):
        """One maxed card drags the score even when the total looks fine, and an overall
        figure alone hides exactly that."""
        util = credit.utilisation([self._card("Healthy", 100, 5000),
                                   self._card("Maxed", 950, 1000)])
        assert [c["creditor"] for c in util["cards"]] == ["Maxed", "Healthy"]
        assert util["cards"][0]["ratio"] == pytest.approx(0.95)

    def test_it_says_what_reaching_each_threshold_would_cost(self):
        """Turns "reduce utilisation" into a number he can decide about."""
        util = credit.utilisation([self._card("A", 800, 1000)])
        assert util["to_reach_30"] == pytest.approx(500.0)
        assert util["to_reach_10"] == pytest.approx(700.0)

    def test_already_under_a_threshold_costs_nothing(self):
        util = credit.utilisation([self._card("A", 50, 1000)])
        assert util["to_reach_30"] == 0.0 and util["to_reach_10"] == 0.0

    def test_closed_cards_and_loans_are_excluded(self):
        """A car loan is not revolving, and a closed card has no limit to use."""
        util = credit.utilisation([
            self._card("Open", 500, 1000),
            self._card("Closed", 0, 2000, status="closed"),
            {"creditor": "Car", "kind": "auto", "balance": 9000, "credit_limit": 0}])
        assert util["total_limit"] == 1000

    def test_no_cards_at_all_reports_unknown_not_zero(self):
        """Zero utilisation and no revolving credit are different facts, and only one of
        them is good news."""
        assert credit.utilisation([])["overall"] is None


class TestTheToolsJarvisActuallyHas:
    def test_every_credit_tool_is_declared(self):
        declared = {t["function"]["name"] for t in PERSONAL_TOOLS}
        assert set(CREDIT_TOOLS) <= declared

    def test_every_credit_tool_dispatches(self, client):
        for name in CREDIT_TOOLS:
            result = client.call_tool(name, {
                "bureau": "experian", "pulled_on": "2026-09-16", "report_id": 1,
                "creditor": "X", "name": "Y", "why": "z", "rec_id": 1,
                "letter_id": 1, "delivered_on": "2026-09-16", "summary": "s"})
            assert isinstance(result, dict), name
            assert "unknown tool" not in str(result).lower(), f"{name} was never dispatched"

    def test_he_can_upload_a_report_and_its_accounts(self, client):
        report = client.call_tool("add_credit_report", {
            "bureau": "experian", "pulled_on": "2026-09-16", "score": 612,
            "score_model": "FICO 8"})
        assert report["ok"] is True
        client.call_tool("add_tradeline", {
            "report_id": report["report_id"], "creditor": "Capital One",
            "kind": "credit_card", "balance": 800, "credit_limit": 1000})
        picture = client.call_tool("get_credit_picture", {})
        assert picture["report"]["score"] == 612
        assert picture["utilisation"]["overall"] == pytest.approx(0.8)

    def test_a_bad_bureau_is_refused_with_a_reason(self, client):
        assert "error" in client.call_tool("add_credit_report",
                                           {"bureau": "fico", "pulled_on": "2026-09-16"})

    def test_derogatory_marks_get_their_own_expiry_date(self, client):
        report = client.call_tool("add_credit_report",
                                  {"bureau": "equifax", "pulled_on": "2026-09-16"})
        client.call_tool("add_tradeline", {
            "report_id": report["report_id"], "creditor": "Jefferson Capital",
            "kind": "collections", "balance": 1087.92, "derogatory": "collection",
            "derogatory_on": "2021-03-11"})
        mark = client.call_tool("get_credit_picture", {})["derogatories"][0]
        assert mark["falls_off_on"] == "2028-03-11"

    def test_he_can_be_offered_a_card_and_act_on_it(self, client):
        rec = client.call_tool("suggest_credit_line", {
            "name": "Discover it Secured", "kind": "secured_card",
            "why": "Thin file with collections; a secured card adds age and on-time history "
                   "without an approval he will not get.",
            "reward": "2% at gas and restaurants", "est_approval": "likely"})
        assert rec["ok"] is True
        assert client.call_tool("update_credit_recommendation", {
            "rec_id": rec["recommendation_id"], "status": "applied"})["ok"] is True
        assert client.call_tool("get_credit_picture", {})["recommendations"][0]["status"] \
            == "applied"

    def test_a_recommendation_is_never_an_application(self, client):
        """A hard inquiry and a new account move his score in both directions, so nothing
        here acts on its own."""
        rec = client.call_tool("suggest_credit_line", {"name": "Some card", "why": "reason"})
        stored = client.call_tool("get_credit_picture", {})["recommendations"][0]
        assert stored["status"] == "suggested" and stored["task_id"] is None
        assert rec["ok"]


class TestDisputesInFlight:
    def _mailed_dispute(self, client, mailed_on, delivered_on=None):
        item = personal_db.create_dispute_item(
            client.db_path, client.owner_user_id, bureau="experian",
            creditor_name="Jefferson Capital", item_description="Not my account",
            reason="This account was never mine")
        letter = personal_db.create_dispute_letter(
            client.db_path, item, letter_text="...", recipient_name="Experian",
            recipient_address="PO Box 4500", recipient_city="Allen",
            recipient_state="TX", recipient_zip="75013")
        personal_db.mark_dispute_letter_mailed(
            client.db_path, client.owner_user_id, letter, tracking_number="X")
        import sqlite3
        with sqlite3.connect(client.db_path) as conn:
            conn.execute("UPDATE dispute_letters SET mailed_at = ? WHERE id = ?",
                         (mailed_on, letter))
        if delivered_on:
            client.call_tool("record_letter_delivered",
                             {"letter_id": letter, "delivered_on": delivered_on})
        return item, letter

    def test_a_blown_window_is_surfaced_as_overdue(self, client):
        """The one piece of real leverage: a bureau that misses the window has to delete
        the item, and that only happens if somebody notices."""
        self._mailed_dispute(client, "2026-07-01", delivered_on="2026-07-05")
        overdue = credit.open_disputes(client.db_path, client.owner_user_id, TODAY)[0]
        assert overdue["overdue_by_days"] > 0
        assert overdue["answered"] is False

    def test_a_dispute_still_inside_its_window_is_not_overdue(self, client):
        self._mailed_dispute(client, "2026-09-10", delivered_on="2026-09-12")
        dispute = credit.open_disputes(client.db_path, client.owner_user_id, TODAY)[0]
        assert dispute["overdue_by_days"] < 0

    def test_recording_an_answer_stops_it_reading_as_overdue(self, client):
        _, letter = self._mailed_dispute(client, "2026-07-01", delivered_on="2026-07-05")
        client.call_tool("record_dispute_response",
                         {"letter_id": letter, "summary": "Deleted from file"})
        dispute = credit.open_disputes(client.db_path, client.owner_user_id, TODAY)[0]
        assert dispute["answered"] is True
        assert dispute["overdue_by_days"] is None

    def test_delivery_makes_the_deadline_defensible(self, client):
        _, letter = self._mailed_dispute(client, "2026-09-01", delivered_on="2026-09-04")
        dispute = credit.open_disputes(client.db_path, client.owner_user_id, TODAY)[0]
        assert dispute["deadline"]["certain"] is True
        assert dispute["deadline"]["due_on"] == "2026-10-04"

    def test_overdue_disputes_come_first(self, client):
        self._mailed_dispute(client, "2026-09-10", delivered_on="2026-09-12")
        self._mailed_dispute(client, "2026-07-01", delivered_on="2026-07-05")
        disputes = credit.open_disputes(client.db_path, client.owner_user_id, TODAY)
        assert disputes[0]["overdue_by_days"] > 0


class TestTheFeedRefusesToGuess:
    def test_with_nothing_uploaded_it_says_so_rather_than_estimating(self, client):
        brief = credit.briefing(client.db_path, client.owner_user_id, TODAY)
        assert "NO SCORE ON RECORD" in brief
        assert "Do not estimate" in brief
        assert "NO CREDIT REPORT UPLOADED" in brief

    def test_a_failure_becomes_an_instruction_not_an_exception(self, client, monkeypatch):
        monkeypatch.setattr(credit, "picture",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gone")))
        brief = credit.briefing(client.db_path, client.owner_user_id, TODAY)
        assert "unavailable" in brief and "remembered" in brief

    def test_the_brief_dates_every_figure_it_reports(self, client):
        personal_db.create_credit_score_entry(client.db_path, client.owner_user_id,
                                              bureau="experian", score=612,
                                              recorded_on="2026-09-01")
        brief = credit.briefing(client.db_path, client.owner_user_id, TODAY)
        assert "612" in brief and "2026-09-01" in brief
