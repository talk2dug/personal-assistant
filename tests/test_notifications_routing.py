"""Covers outbound notifications: the HA notify/presence calls, and the policy that
decides where Jarvis's self-initiated messages go.

The fallback cases matter most. A reminder is the thing you most need when you're away
from a screen, so every failure path here has to end with the message still being
delivered somewhere rather than vanishing.
"""
import httpx
import pytest

from assistant.core import db
from assistant.core.engine import DEFAULT_NOTIFY_POLICY, NOTIFY_POLICY_KEY, HomeAssistantContext
from assistant.core.home_assistant_client import HomeAssistantClient
from assistant.core.setup import build_notifier


class FakeConfig:
    def __init__(self, db_path):
        self.db_path = db_path


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


# --- the HA client ------------------------------------------------------------

def test_notify_sends_no_entity_id(monkeypatch):
    """Verified against the real instance: notify services 400 if handed an entity_id,
    which is why this can't just go through call_service."""
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = HomeAssistantClient("http://ha.test", "tok", "notify.mobile_app_upstream")
    result = client.notify("hello", title="Jarvis")

    assert result["ok"] is True
    assert seen["url"].endswith("/api/services/notify/mobile_app_upstream")
    assert "entity_id" not in seen["json"]
    assert seen["json"]["message"] == "hello" and seen["json"]["title"] == "Jarvis"


def test_notify_actions_become_buttons(monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, **k: (
        seen.update(k.get("json") or {}), httpx.Response(200, json={}, request=httpx.Request("POST", url)))[1])
    HomeAssistantClient("http://ha.test", "tok").notify(
        "Unlock?", actions=[{"action": "JARVIS_CONFIRM", "title": "Yes"}])
    assert seen["data"]["actions"][0]["action"] == "JARVIS_CONFIRM"


def test_notify_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        400, text="bad", request=httpx.Request("POST", url)))
    result = HomeAssistantClient("http://ha.test", "tok").notify("hi")
    assert "error" in result and "400" in result["error"]


def test_call_service_omits_entity_id_when_absent(monkeypatch):
    """The bug this fixes: an always-present entity_id made notify and other
    target-less services fail."""
    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, **k: (
        seen.update({"json": k.get("json")}),
        httpx.Response(200, json={}, request=httpx.Request("POST", url)))[1])
    HomeAssistantClient("http://ha.test", "tok").call_service("script", "turn_on")
    assert "entity_id" not in seen["json"]


def test_presence_reports_home_or_away(monkeypatch):
    states = [
        {"entity_id": "person.jack", "state": "not_home", "attributes": {"friendly_name": "Jack"}},
        {"entity_id": "device_tracker.phone", "state": "not_home", "attributes": {}},
        {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
    ]
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json=states, request=httpx.Request("GET", url)))
    presence = HomeAssistantClient("http://ha.test", "tok").presence()
    assert presence["home"] is False
    assert [p["name"] for p in presence["people"]] == ["Jack"]
    assert not any("light" in t["entity_id"] for t in presence["device_trackers"])


# --- the policy ---------------------------------------------------------------

class FakeHAClient:
    def __init__(self, home=True, fail=False):
        self._home = home
        self.fail = fail
        self.pushed = []

    def presence(self):
        if self._home is None:
            raise RuntimeError("presence unavailable")
        return {"home": self._home}

    def notify(self, message, title=None, target=None, actions=None):
        if self.fail:
            return {"error": "phone unreachable"}
        self.pushed.append(message)
        return {"ok": True}


def make(db_path, home=True, fail=False, policy=None):
    if policy:
        db.set_setting(db_path, NOTIFY_POLICY_KEY, policy)
    ha_client = FakeHAClient(home=home, fail=fail)
    sent_telegram = []
    notify = build_notifier(
        FakeConfig(db_path), lambda chat, text: sent_telegram.append(text),
        HomeAssistantContext(mcp_client=ha_client, sensitive_domains=set()),
    )
    return notify, ha_client, sent_telegram


def test_auto_pushes_to_the_phone_only_when_away(db_path):
    notify, ha, telegram = make(db_path, home=False, policy="auto")
    notify("chat", "Reminder: call the vet")
    assert ha.pushed == ["Reminder: call the vet"] and telegram == []


def test_auto_uses_telegram_when_home(db_path):
    notify, ha, telegram = make(db_path, home=True, policy="auto")
    notify("chat", "Reminder")
    assert ha.pushed == [] and telegram == ["Reminder"]


def test_unknown_presence_does_not_guess(db_path):
    """If we can't tell where he is, Telegram — not a push to a phone in his pocket at
    3am on a guess."""
    notify, ha, telegram = make(db_path, home=None, policy="auto")
    notify("chat", "Reminder")
    assert ha.pushed == [] and telegram == ["Reminder"]


def test_phone_policy_pushes_even_when_home(db_path):
    notify, ha, telegram = make(db_path, home=True, policy="phone")
    notify("chat", "Reminder")
    assert ha.pushed == ["Reminder"] and telegram == []


def test_telegram_policy_never_pushes(db_path):
    notify, ha, telegram = make(db_path, home=False, policy="telegram")
    notify("chat", "Reminder")
    assert ha.pushed == [] and telegram == ["Reminder"]


def test_both_sends_to_each(db_path):
    notify, ha, telegram = make(db_path, home=True, policy="both")
    notify("chat", "Reminder")
    assert ha.pushed == ["Reminder"] and telegram == ["Reminder"]


def test_a_failed_push_still_reaches_telegram(db_path):
    """The important one: a reminder must never be lost because the phone was
    unreachable."""
    notify, ha, telegram = make(db_path, home=False, fail=True, policy="phone")
    notify("chat", "Reminder")
    assert ha.pushed == [] and telegram == ["Reminder"]


def test_with_no_home_assistant_everything_goes_to_telegram(db_path):
    sent = []
    notify = build_notifier(FakeConfig(db_path), lambda chat, text: sent.append(text), None)
    db.set_setting(db_path, NOTIFY_POLICY_KEY, "phone")
    notify("chat", "Reminder")
    assert sent == ["Reminder"]


def test_policy_is_read_per_send_not_cached(db_path):
    """Changing it in conversation should take effect on the next reminder, not the next
    restart."""
    notify, ha, telegram = make(db_path, home=True, policy="telegram")
    notify("chat", "first")
    db.set_setting(db_path, NOTIFY_POLICY_KEY, "phone")
    notify("chat", "second")
    assert telegram == ["first"] and ha.pushed == ["second"]


def test_default_policy_when_nothing_has_been_set(db_path):
    assert db.get_setting(db_path, NOTIFY_POLICY_KEY, DEFAULT_NOTIFY_POLICY) == "auto"
