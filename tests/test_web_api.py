from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, ui_content, vision
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "Hello from Jarvis"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    # /api/chat/message unconditionally checks for a pending show_camera/show_content result.
    vision.init_vision_db(path)
    ui_content.init_ui_content_db(path)
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(
        db_path=db_path,
        users=[
            UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass"),
            UserConfig(telegram_chat_id="222", display_name="GF", role="partner", web_password="partnerpass"),
        ],
    )


@pytest.fixture
def seeded_users(db_path, cfg):
    for u in cfg.users:
        db.upsert_user(db_path, u.telegram_chat_id, u.display_name, u.role)


@pytest.fixture
def client(cfg, seeded_users):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    return TestClient(app)


def test_login_with_correct_password_succeeds(client):
    resp = client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "owner"


def test_login_with_wrong_password_fails(client):
    resp = client.post("/api/login", json={"name": "Dug", "password": "wrong"})
    assert resp.status_code == 401


def test_login_with_unknown_name_fails(client):
    resp = client.post("/api/login", json={"name": "Nobody", "password": "x"})
    assert resp.status_code == 401


def test_protected_route_requires_login(client):
    resp = client.get("/api/chat/history")
    assert resp.status_code == 401


def test_chat_message_round_trip_after_login(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.post("/api/chat/message", json={"text": "hi"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Hello from Jarvis"

    history = client.get("/api/chat/history").json()
    assert any(m["content"] == "hi" for m in history)
    assert any(m["content"] == "Hello from Jarvis" for m in history)


def test_finance_routes_require_owner_role(client):
    client.post("/api/login", json={"name": "GF", "password": "partnerpass"})
    resp = client.get("/api/finance/summary")
    assert resp.status_code == 403


def test_finance_summary_accessible_to_owner(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.get("/api/finance/summary")
    assert resp.status_code == 200
    assert resp.json() == {
        "accounts": [], "recurring_charges": [], "total_balance": 0,
        "cash_balance": 0, "investment_balance": 0, "liability_balance": 0,
    }


def test_savings_goal_crud(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})

    create = client.post("/api/finance/goals", json={"name": "Japan trip", "target_amount": 3000, "target_date": "2027-06-01"})
    assert create.status_code == 200
    goal_id = create.json()["id"]

    goals = client.get("/api/finance/goals").json()
    assert len(goals) == 1
    assert goals[0]["name"] == "Japan trip"

    update = client.put(f"/api/finance/goals/{goal_id}", json={"target_amount": 3500})
    assert update.status_code == 200
    goals = client.get("/api/finance/goals").json()
    assert goals[0]["target_amount"] == 3500

    delete = client.delete(f"/api/finance/goals/{goal_id}")
    assert delete.status_code == 200
    assert client.get("/api/finance/goals").json() == []


def test_delete_nonexistent_goal_returns_404(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.delete("/api/finance/goals/9999")
    assert resp.status_code == 404


def test_logout_clears_session(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert client.get("/api/chat/history").status_code == 200


def test_spending_with_no_cached_data_is_empty(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.get("/api/finance/spending?period=this_month")
    assert resp.status_code == 200
    assert resp.json() == {"period": "this_month", "categories": []}


def test_spending_rejects_invalid_period(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.get("/api/finance/spending?period=nonsense")
    assert resp.status_code == 422


def test_spending_annotates_categories_with_budget_limit(client, db_path):
    from assistant.core import db

    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    db.upsert_era_category_spending(db_path, "fcat_dining", "this_month", "Dining out", 320.0, 40.0, 12)

    create = client.post("/api/finance/budgets", json={
        "category_key": "fcat_dining", "category_label": "Dining out", "monthly_limit": 400.0,
    })
    assert create.status_code == 200

    resp = client.get("/api/finance/spending?period=this_month").json()
    assert resp["categories"][0]["amount"] == 320.0
    assert resp["categories"][0]["budget_limit"] == 400.0


def test_budget_with_no_spending_still_shows_up(client, db_path):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    client.post("/api/finance/budgets", json={
        "category_key": "fcat_travel", "category_label": "Travel", "monthly_limit": 200.0,
    })
    resp = client.get("/api/finance/spending?period=this_month").json()
    assert resp["categories"] == [
        {"category_key": "fcat_travel", "label": "Travel", "amount": 0.0,
         "percent_of_total": None, "transaction_count": 0, "budget_limit": 200.0},
    ]


def test_budget_crud(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})

    create = client.post("/api/finance/budgets", json={
        "category_key": "fcat_dining", "category_label": "Dining out", "monthly_limit": 400.0,
    })
    assert create.status_code == 200
    budget_id = create.json()["id"]

    budgets = client.get("/api/finance/budgets").json()
    assert len(budgets) == 1
    assert budgets[0]["monthly_limit"] == 400.0

    delete = client.delete(f"/api/finance/budgets/{budget_id}")
    assert delete.status_code == 200
    assert client.get("/api/finance/budgets").json() == []


def test_budget_routes_require_owner_role(client):
    client.post("/api/login", json={"name": "GF", "password": "partnerpass"})
    assert client.get("/api/finance/budgets").status_code == 403
    assert client.get("/api/finance/spending").status_code == 403

    client.post("/api/logout")
    assert client.get("/api/chat/history").status_code == 401


def test_manual_recurring_charge_crud(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})

    create = client.post("/api/finance/recurring/manual", json={
        "description": "Rent (15th)", "amount": 925.0, "direction": "expense",
        "cadence": "monthly_on_day", "next_expected_date": "2026-09-15",
    })
    assert create.status_code == 200
    charge_id = create.json()["id"]

    listing = client.get("/api/finance/recurring").json()
    assert len(listing["manual"]) == 1
    assert listing["manual"][0]["description"] == "Rent (15th)"
    assert listing["era"] == []

    delete = client.delete(f"/api/finance/recurring/manual/{charge_id}")
    assert delete.status_code == 200
    assert client.get("/api/finance/recurring").json()["manual"] == []


def test_manual_recurring_charge_rejects_invalid_cadence(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.post("/api/finance/recurring/manual", json={
        "description": "X", "amount": 10.0, "direction": "expense",
        "cadence": "fortnightly", "next_expected_date": "2026-09-15",
    })
    assert resp.status_code == 422


def test_manual_recurring_charge_feeds_projection(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    client.post("/api/finance/recurring/manual", json={
        "description": "Rent", "amount": 925.0, "direction": "expense",
        "cadence": "monthly_on_day", "next_expected_date": "2026-09-15",
    })
    resp = client.get("/api/finance/projection?horizon_days=30").json()
    # somewhere in the 30-day series the balance should dip by 925 relative to day 0
    balances = [p["balance"] for p in resp["series"]]
    assert min(balances) <= balances[0] - 925.0


def test_toggle_era_recurring_exclusion(client, db_path):
    from assistant.core import db as db_module

    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    db_module.upsert_era_recurring_charge(
        db_path, "key-1", "Moved From Chime", 418.94, "income", "monthly", "2026-09-28",
    )

    resp = client.put("/api/finance/recurring/era/key-1", json={"excluded": True})
    assert resp.status_code == 200

    listing = client.get("/api/finance/recurring").json()
    assert listing["era"][0]["excluded"] is True

    # excluded charges must not appear in the summary's forecast-facing list
    summary = client.get("/api/finance/summary").json()
    assert summary["recurring_charges"] == []


def test_toggle_era_exclusion_on_unknown_key_returns_404(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.put("/api/finance/recurring/era/nonexistent", json={"excluded": True})
    assert resp.status_code == 404


def test_recurring_routes_require_owner_role(client):
    client.post("/api/login", json={"name": "GF", "password": "partnerpass"})
    assert client.get("/api/finance/recurring").status_code == 403
    assert client.post("/api/finance/recurring/manual", json={}).status_code == 403


def test_summary_groups_balances_by_account_type(client, db_path):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    db.upsert_era_account(db_path, "acct-checking", "Checking", "Checking", 500.0, 500.0)
    db.upsert_era_account(db_path, "acct-401k", "401k", "401k", 20000.0, 20000.0)
    db.upsert_era_account(db_path, "acct-cc", "Credit Card", "Credit Card", 300.0, 300.0)

    resp = client.get("/api/finance/summary").json()
    assert resp["cash_balance"] == 500.0
    assert resp["investment_balance"] == 20000.0
    assert resp["liability_balance"] == 300.0
    # total_balance is the spendable-cash total, not every account blended together
    assert resp["total_balance"] == 500.0

    groups_by_key = {a["account_key"]: a["balance_group"] for a in resp["accounts"]}
    assert groups_by_key["acct-checking"] == "cash"
    assert groups_by_key["acct-401k"] == "investment"
    assert groups_by_key["acct-cc"] == "liability"


def test_projection_excludes_investment_and_liability_from_starting_balance(client, db_path):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    db.upsert_era_account(db_path, "acct-checking", "Checking", "Checking", 500.0, 500.0)
    db.upsert_era_account(db_path, "acct-401k", "401k", "401k", 20000.0, 20000.0)

    resp = client.get("/api/finance/projection?horizon_days=5").json()
    assert resp["series"][0]["balance"] == 500.0


def test_safety_buffer_get_and_put_round_trip(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert client.get("/api/finance/safety-buffer").json() == {"safety_buffer": 0.0}

    resp = client.put("/api/finance/safety-buffer", json={"safety_buffer": 150.0})
    assert resp.status_code == 200
    assert client.get("/api/finance/safety-buffer").json() == {"safety_buffer": 150.0}


def test_safety_buffer_rejects_negative_value(client):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    resp = client.put("/api/finance/safety-buffer", json={"safety_buffer": -5})
    assert resp.status_code == 422


def test_safe_to_spend_without_enough_pay_period_data(client, db_path):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    db.upsert_era_account(db_path, "acct-checking", "Checking", "Checking", 500.0, 500.0)
    resp = client.get("/api/finance/safe-to-spend").json()
    assert resp["safe_to_spend"] is None


def test_safe_to_spend_with_real_pay_period_data(client, db_path):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    db.upsert_era_account(db_path, "acct-checking", "Checking", "Checking", 1000.0, 1000.0)
    dug = db.get_user_by_chat_id(db_path, "111")
    db.create_manual_recurring_charge(
        db_path, dug["id"], "Paycheck (15th)", 2000.0, "income", "monthly_on_day", "2026-09-15")
    db.create_manual_recurring_charge(
        db_path, dug["id"], "Paycheck (last day)", 2000.0, "income", "monthly_on_last_day", "2026-09-30")

    resp = client.get("/api/finance/safe-to-spend").json()
    assert resp["safe_to_spend"] is not None
    assert resp["payday"] is not None

    # the same number should also show up embedded in /projection
    projection = client.get("/api/finance/projection").json()
    assert projection["safe_to_spend"] == resp


def test_insights_endpoint_returns_cached_era_payloads(client, db_path):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert client.get("/api/finance/insights").json() == {}

    db.upsert_era_insight(db_path, "forecast_spending", {"projected_total": 2100.5})
    resp = client.get("/api/finance/insights").json()
    assert resp["forecast_spending"]["payload"] == {"projected_total": 2100.5}


def test_net_worth_endpoint_returns_history(client, db_path):
    client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert client.get("/api/finance/net-worth").json() == []

    db.upsert_net_worth_snapshot(db_path, "2026-09-01", 500.0, 20000.0, 300.0, 20200.0)
    db.upsert_net_worth_snapshot(db_path, "2026-09-02", 600.0, 20000.0, 300.0, 20300.0)
    resp = client.get("/api/finance/net-worth").json()
    assert [r["date"] for r in resp] == ["2026-09-01", "2026-09-02"]
    assert resp[1]["net_worth"] == 20300.0


def test_insights_and_net_worth_routes_require_owner_role(client):
    client.post("/api/login", json={"name": "GF", "password": "partnerpass"})
    assert client.get("/api/finance/insights").status_code == 403
    assert client.get("/api/finance/net-worth").status_code == 403
    assert client.get("/api/finance/safe-to-spend").status_code == 403
    assert client.get("/api/finance/safety-buffer").status_code == 403
