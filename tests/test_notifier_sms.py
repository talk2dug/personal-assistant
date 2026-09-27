"""Where Jarvis's own notifications go.

Jack, on the Home Assistant push this replaces: *"i dont like the homeassistant
notifications, i cant open them and they lack enough space for the entire message. Lets
start using text messaging."*

A banner that truncates is a bad carrier for a reminder, because the message IS the
content. The failure worth defending against is the quiet one: a notification that reports
success having gone somewhere he is not looking.
"""
import pytest

from assistant.core import cellular, db as core_db, setup


class Cfg:
    def __init__(self, db_path, numbers=("+12027408240",), enabled=True):
        self.db_path = db_path
        self.sms_allowed_numbers = list(numbers)
        self.sms_sending_enabled = enabled


class FakeHA:
    def __init__(self):
        self.sent = []
        self.client = self

    @property
    def mcp_client(self):
        return self

    def notify(self, text, title=None):
        self.sent.append(text)
        return {"ok": True}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "n.db")
    core_db.init_db(path)
    cellular.init_cellular_db(path)
    return path


def queued(db):
    return [m["text"] for m in cellular.pending_outbound(db, limit=20)]


class TestItTextsHim:
    def test_a_reminder_goes_out_as_a_text(self, db):
        sent = []
        notify = setup.build_notifier(Cfg(db), lambda cid, t: sent.append(t))
        notify("111", "⏰ Reminder: call the bank")

        assert queued(db) == ["⏰ Reminder: call the bank"]
        assert sent == [], "and does not also go to Telegram"

    def test_auto_texts_too_rather_than_asking_where_he_is(self, db):
        """There is no presence question left to answer: a text reaches him whether he is
        home or not, which was the only reason 'auto' consulted Home Assistant."""
        core_db.set_setting(db, "notify_policy", "auto")
        ha = FakeHA()
        notify = setup.build_notifier(Cfg(db), lambda cid, t: None, ha)
        notify("111", "the dishwasher finished")

        assert queued(db) == ["the dishwasher finished"]
        assert ha.sent == [], "the HA push is not used"

    def test_both_sends_the_text_and_the_push(self, db):
        core_db.set_setting(db, "notify_policy", "both")
        ha = FakeHA()
        sent = []
        notify = setup.build_notifier(Cfg(db), lambda cid, t: sent.append(t), ha)
        notify("111", "rent is due")

        assert queued(db) == ["rent is due"]
        assert ha.sent == ["rent is due"]
        assert sent == ["rent is due"]

    def test_he_can_still_choose_the_old_push(self, db):
        core_db.set_setting(db, "notify_policy", "phone")
        ha = FakeHA()
        notify = setup.build_notifier(Cfg(db), lambda cid, t: None, ha)
        notify("111", "back to banners")

        assert queued(db) == []
        assert ha.sent == ["back to banners"]


class TestItNeverVanishes:
    def test_with_no_number_configured_it_falls_back_to_telegram(self, db):
        sent = []
        notify = setup.build_notifier(Cfg(db, numbers=()), lambda cid, t: sent.append(t))
        notify("111", "still delivered")
        assert sent == ["still delivered"]

    def test_with_sending_disabled_it_falls_back_too(self, db):
        sent = []
        notify = setup.build_notifier(Cfg(db, enabled=False), lambda cid, t: sent.append(t))
        notify("111", "still delivered")
        assert sent == ["still delivered"]

    def test_a_queue_failure_falls_back_rather_than_raising(self, db, monkeypatch):
        """A notifier that raises takes down the scheduler job that called it, and every
        later reminder with it."""
        monkeypatch.setattr(cellular, "queue_outbound",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("modem gone")))
        sent = []
        notify = setup.build_notifier(Cfg(db), lambda cid, t: sent.append(t))
        notify("111", "must not be lost")
        assert sent == ["must not be lost"]


def test_it_goes_only_to_the_allowed_number(db):
    """The allow-list that decides who may text Jarvis decides who Jarvis may text, so a
    notification cannot become a way to message someone new."""
    notify = setup.build_notifier(Cfg(db, numbers=("+12027408240", "+15406540555")),
                                  lambda cid, t: None)
    notify("111", "hello")
    numbers = [m["number"] for m in cellular.pending_outbound(db, limit=20)]
    assert numbers == ["+12027408240"]
