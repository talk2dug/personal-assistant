"""Radio awareness: the tables, the delivery rules, the parsing, and the tools.

What has to be right here is not the audio -- it is the two ways a radio watch goes
wrong quietly: pushing the same warning three times (the SAME header literally repeats,
and the RF report re-lists a vehicle every poll), and calling a dead feed "quiet". Every
tool is executed, not merely declared.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import db as core_db, radio, radio_tools, staff
from assistant.core.radio_tools import RADIO_TOOLS, RADIO_TOOL_NAMES

NOW = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "radio.db")
    core_db.init_db(p)
    radio.init_radio_db(p)
    return p


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, messages, tools=None, think=False):
        self.calls.append(messages)
        return {"role": "assistant", "content": self.content}


class TestItems:
    def test_same_key_is_one_row(self, path):
        assert radio.add_item(path, "eas:ZCZC-1", "eas", "urgent", "Tornado Warning") is True
        assert radio.add_item(path, "eas:ZCZC-1", "eas", "urgent", "Tornado Warning") is False
        assert len(radio.recent_items(path)) == 1

    def test_unknown_severity_becomes_notice(self, path):
        radio.add_item(path, "k", "scanner", "bogus", "x")
        assert radio.recent_items(path)[0]["severity"] == "notice"

    def test_active_eas_honours_expiry(self, path):
        radio.add_item(path, "a", "eas", "urgent", "expired", at=NOW - timedelta(hours=2),
                       meta={"expires_at": (NOW - timedelta(hours=1)).isoformat()})
        radio.add_item(path, "b", "eas", "urgent", "live", at=NOW - timedelta(minutes=5),
                       meta={"expires_at": (NOW + timedelta(minutes=40)).isoformat()})
        assert [e["title"] for e in radio.active_eas(path, now=NOW)] == ["live"]


class TestDelivery:
    def test_urgent_goes_alone_notices_batch_info_stays_quiet(self, path):
        radio.add_item(path, "u", "eas", "urgent", "Tornado Warning for Henrico", at=NOW)
        radio.add_item(path, "n1", "scanner", "notice", "Structure fire on Main St", at=NOW)
        radio.add_item(path, "n2", "rf_vehicle", "notice", "Unfamiliar vehicle back again", at=NOW)
        radio.add_item(path, "i", "rf_new", "info", "New transmitter", at=NOW)
        sent = []
        assert radio.deliver_pending(path, sent.append, now=NOW) == 2
        assert sent[0].startswith("URGENT: Tornado Warning")
        assert "2 items" in sent[1] and "Structure fire" in sent[1] and "Unfamiliar vehicle" in sent[1]
        # everything is marked, including the info item that was never pushed
        assert radio.pending_notifications(path) == []
        # and nothing is sent twice
        assert radio.deliver_pending(path, sent.append, now=NOW) == 0

    def test_stale_pending_item_is_not_pushed_as_new(self, path):
        radio.add_item(path, "old", "eas", "urgent", "Yesterday's warning", at=NOW - timedelta(hours=30))
        sent = []
        assert radio.deliver_pending(path, sent.append, now=NOW) == 0
        assert sent == [] and radio.pending_notifications(path) == []

    def test_notify_failure_leaves_item_pending(self, path):
        radio.add_item(path, "u", "eas", "urgent", "Warning", at=NOW)

        def boom(text):
            raise RuntimeError("telegram down")

        radio.deliver_pending(path, boom, now=NOW)
        assert len(radio.pending_notifications(path)) == 1


class TestFeedHealth:
    def test_everything_stale_when_never_heard(self, path):
        st = radio.feed_status(path, now=NOW)
        assert st["weather_stale"] and st["scanner_stale"] and st["eas_stale"] and st["rf_stale"]
        text = radio.briefing(path, now=NOW)
        assert "radio worker is not running" in text

    def test_fresh_heartbeat_is_not_stale(self, path):
        radio.set_state(path, "audio:weather", "ok", now=NOW - timedelta(seconds=30))
        radio.set_state(path, "audio:scanner", "ok", now=NOW - timedelta(seconds=400))
        st = radio.feed_status(path, now=NOW)
        assert st["weather_stale"] is False
        assert st["scanner_stale"] is True


class TestTranscriptsAndConditions:
    def test_transcript_text_is_chronological(self, path):
        radio.record_transcript(path, "weather", NOW - timedelta(minutes=2), NOW - timedelta(minutes=1), "first")
        radio.record_transcript(path, "weather", NOW - timedelta(minutes=1), NOW, "second")
        assert radio.transcript_text(path, "weather", minutes=10, now=NOW) == "first second"
        assert radio.record_transcript(path, "weather", NOW, NOW, "   ") is None

    def test_extract_conditions_parses_fenced_json(self, path):
        llm = FakeLLM('Sure:\n```json\n{"temperature_f": "87", "humidity_pct": 58, "wind": "north 12 mph",'
                      ' "pressure_in": 30.07, "pressure_trend": "falling", "sky": "mostly sunny",'
                      ' "summary": "Hot and mostly sunny.", "hazards": "", "forecast": "Storms late."}\n```')
        cond = radio.extract_conditions(llm, "Temperature 87, relative humidity 58 percent, " * 5)
        assert cond["temperature_f"] == "87"
        radio.record_conditions(path, cond, at=NOW)
        latest = radio.latest_conditions(path)
        assert latest["temperature_f"] == 87.0 and latest["pressure_trend"] == "falling"

    def test_extract_conditions_refuses_empty_and_garbage(self):
        assert radio.extract_conditions(FakeLLM("{}"), "x" * 200) is None
        assert radio.extract_conditions(FakeLLM("no json here"), "x" * 200) is None
        assert radio.extract_conditions(FakeLLM('{"temperature_f": 80}'), "too short") is None

    def test_classify_scanner_needs_a_boolean(self):
        assert radio.classify_scanner(FakeLLM('{"important": "yes"}'), "engine 5 responding to a fire") is None
        v = radio.classify_scanner(FakeLLM('{"important": true, "severity": "silly", "summary": "Fire"}'),
                                   "working fire two story residence")
        assert v["important"] is True and v["severity"] == "info"
        assert radio.classify_scanner(FakeLLM('{"important": true}'), "ok") is None


class TestTools:
    def test_every_declared_tool_runs(self, path):
        for name in RADIO_TOOL_NAMES:
            out = json.loads(radio_tools.handle(path, name, {}))
            assert isinstance(out, dict), name
        assert {t["function"]["name"] for t in RADIO_TOOLS} == RADIO_TOOL_NAMES

    def test_weather_tool_reports_stale_and_alerts(self, path):
        radio.add_item(path, "e", "eas", "urgent", "Severe Thunderstorm Warning for Henrico",
                       meta={"affects_me": True, "area_names": ["Henrico"],
                             "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        out = json.loads(radio_tools.handle(path, "get_weather_conditions", {}))
        assert out["feed_stale"] is True and "down" in out["note"]
        assert out["active_alerts"][0]["affects_home_counties"] is True

    def test_scanner_tool_counts_and_flags(self, path):
        radio.set_state(path, "audio:scanner", "ok")
        now = datetime.now(timezone.utc)
        radio.record_transcript(path, "scanner", now - timedelta(minutes=3), now - timedelta(minutes=2), "engine 5 on scene")
        radio.add_item(path, "s1", "scanner", "notice", "Working fire on Broad St", body="Heard: ...",
                       meta={"category": "fire", "location": "Broad St"})
        out = json.loads(radio_tools.handle(path, "get_scanner_activity", {"minutes": 60, "include_all": True}))
        assert out["transmissions_heard"] == 1
        assert out["flagged"][0]["category"] == "fire"
        assert out["transcripts"][0]["text"] == "engine 5 on scene"

    def test_rf_tool_reads_the_stored_report(self, path):
        radio.set_state(path, "poll:rf", "ok")
        radio.set_state(path, "rf_baseline", {
            "generated_at": NOW.isoformat(), "counts": {"vehicle-watch": 1},
            "traffic": {"vehicle_visits_last_24h": 3},
            "alerts": [{"text": "Unfamiliar vehicle ..."}],
            "devices": [{"fingerprint": "Citroen/id=957fdfe1", "label": None, "class": "vehicle-watch",
                         "visits": 4, "days_seen": 2, "typical_hours": [16, 20],
                         "first_seen": NOW.isoformat(), "last_seen": NOW.isoformat()}],
        })
        out = json.loads(radio_tools.handle(path, "get_rf_surroundings", {}))
        assert out["vehicles_to_check"][0]["device"] == "Citroen/id=957fdfe1"
        assert out["alerts"] == ["Unfamiliar vehicle ..."]

    def test_alert_list_marks_what_was_pushed(self, path):
        radio.add_item(path, "a", "eas", "urgent", "Warning")
        radio.deliver_pending(path, lambda t: None)
        out = json.loads(radio_tools.handle(path, "list_radio_alerts", {"hours": 1}))
        assert out["alerts"][0]["pushed_to_owner"] is True


class TestStaffFeed:
    def test_radio_feed_is_inferred_and_accepted(self, path):
        assert "radio" in staff.infer_data_feeds("Neighbourhood watch", "keep an eye on the scanner").split(",")
        assert "radio" not in staff.infer_data_feeds("Crypto analyst", "watch bitcoin").split(",")
        staff.init_staff_db(path)
        key = staff.hire(path, "Radio watch", "monitor the weather radio and the scanner",
                         cadence="interval", interval_minutes=15)["key"]
        staff.set_data_feeds(path, key, "journal,radio")
        text = staff.build_feed_briefing(path, "radio")
        assert "RADIO / RF AWARENESS FEED" in text
