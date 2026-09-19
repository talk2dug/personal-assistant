"""LetterStream must survive a rate limit, and must not cause one.

A real incident, and the two halves of it compounded. A crash loop restarted JarvisCore
roughly 190 times in six hours; every boot spent a metered status request checking the
account balance, and the daily status quota ran out. Then the rate-limit error was treated
as a fatal capability failure, so the ability to MAIL anything was withdrawn -- because of
a quota on a completely different endpoint. Nothing had ever been mailed, and no money was
ever at stake.

Both halves are pinned here: the balance check is cached so restarts are cheap, and a
failed check degrades to "balance unknown" rather than to "no letters at all".
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import db as core_db, setup
from assistant.core.letterstream_client import LetterStreamError


class FakeConfig:
    letterstream_api_id = "id"
    letterstream_api_key = "key"
    letterstream_from_name = "Jack"
    letterstream_from_address = "1 Main St"
    letterstream_from_address_2 = ""
    letterstream_from_city = "Richmond"
    letterstream_from_state = "VA"
    letterstream_from_zip = "23220"

    def __init__(self, db_path):
        self.db_path = db_path


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def account_status(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def cfg(tmp_path):
    path = str(tmp_path / "ls.db")
    core_db.init_db(path)
    return FakeConfig(path)


RATE_LIMIT = LetterStreamError(
    -997, "Rate Limit: (Your account has reached the daily limit for status requests.)")


class TestATransientFailureIsRecognised:
    def test_a_rate_limit_is_transient(self):
        assert RATE_LIMIT.transient is True

    def test_a_rate_limit_by_wording_alone_is_transient(self):
        assert LetterStreamError(-1, "rate limit exceeded").transient is True

    def test_an_ordinary_error_is_not(self):
        assert LetterStreamError(-100, "Invalid API credentials").transient is False


class TestTheCheckDoesNotCostAQuotaEveryBoot:
    def test_the_first_check_is_made_and_remembered(self, cfg):
        client = FakeClient({"balance": "12.34"})
        status, note = setup._letterstream_status(cfg, client)
        assert status["balance"] == "12.34" and client.calls == 1
        assert core_db.get_setting(cfg.db_path, setup.LETTERSTREAM_STATUS_KEY, None)

    def test_a_restart_reuses_it_rather_than_spending_another(self, cfg):
        """The whole cause of the incident: 190 restarts, 190 status requests."""
        client = FakeClient({"balance": "12.34"})
        setup._letterstream_status(cfg, client)
        for _ in range(50):
            status, note = setup._letterstream_status(cfg, client)
        assert client.calls == 1, "a restart is not new information about a balance"
        assert status["balance"] == "12.34"
        assert "cached" in note

    def test_a_stale_reading_is_refreshed(self, cfg):
        client = FakeClient({"balance": "99.00"})
        old = (datetime.now(timezone.utc)
               - timedelta(hours=setup.LETTERSTREAM_STATUS_TTL_HOURS + 1))
        core_db.set_setting(cfg.db_path, setup.LETTERSTREAM_STATUS_KEY, json.dumps(
            {"status": {"balance": "1.00"}, "checked_at": old.isoformat()}))
        status, _ = setup._letterstream_status(cfg, client)
        assert status["balance"] == "99.00" and client.calls == 1

    def test_a_corrupt_cache_is_ignored_rather_than_fatal(self, cfg):
        core_db.set_setting(cfg.db_path, setup.LETTERSTREAM_STATUS_KEY, "not json")
        status, _ = setup._letterstream_status(cfg, FakeClient({"balance": "5.00"}))
        assert status["balance"] == "5.00"

    def test_a_failed_live_check_falls_back_to_the_last_known_balance(self, cfg):
        setup._letterstream_status(cfg, FakeClient({"balance": "7.00"}))
        old = (datetime.now(timezone.utc)
               - timedelta(hours=setup.LETTERSTREAM_STATUS_TTL_HOURS + 1))
        cached = json.loads(core_db.get_setting(cfg.db_path, setup.LETTERSTREAM_STATUS_KEY, "{}"))
        cached["checked_at"] = old.isoformat()
        core_db.set_setting(cfg.db_path, setup.LETTERSTREAM_STATUS_KEY, json.dumps(cached))

        status, note = setup._letterstream_status(cfg, FakeClient(RATE_LIMIT))
        assert status["balance"] == "7.00"
        assert "stale" in note

    def test_with_no_cache_at_all_a_failure_reports_unknown(self, cfg):
        status, note = setup._letterstream_status(cfg, FakeClient(RATE_LIMIT))
        assert status is None
        assert "Rate Limit" in note


class TestMailingSurvivesAStatusQuota:
    def _build(self, cfg, monkeypatch, result):
        client = FakeClient(result)
        monkeypatch.setattr("assistant.core.letterstream_client.LetterStreamClient",
                            lambda *a, **k: client)
        return setup.build_letterstream_context(cfg)

    def test_a_rate_limited_status_does_not_remove_the_ability_to_mail(self, cfg, monkeypatch):
        """The second half of the incident: a quota on the STATUS endpoint took away
        letters entirely, which is the one thing the account was for."""
        ctx = self._build(cfg, monkeypatch, RATE_LIMIT)
        assert ctx is not None
        assert ctx.mcp_client is not None

    def test_mailing_still_requires_his_explicit_confirmation(self, cfg, monkeypatch):
        """Degrading gracefully must not quietly loosen the money gate."""
        ctx = self._build(cfg, monkeypatch, RATE_LIMIT)
        assert "letterstream_authorize_mail" in ctx.sensitive_tools

    def test_a_healthy_account_still_connects_normally(self, cfg, monkeypatch):
        ctx = self._build(cfg, monkeypatch, {"balance": "25.00"})
        assert ctx is not None and ctx.mcp_client is not None

    def test_missing_credentials_still_disable_it(self, cfg, monkeypatch):
        """This one IS a configuration failure, and offering letters with no account
        would be a promise that cannot be kept."""
        cfg.letterstream_api_id = ""
        assert setup.build_letterstream_context(cfg) is None

    def test_an_incomplete_return_address_still_disables_it(self, cfg, monkeypatch):
        """A letter with no sender cannot be mailed, so there is nothing to offer."""
        cfg.letterstream_from_zip = ""
        assert setup.build_letterstream_context(cfg) is None
