"""The board where the team asks Jack for what it cannot generate itself.

The behaviours worth pinning are the ones that decide whether he keeps reading the board:
it must not repeat itself, it must never hand a secret back to the browser, and an answer
must reach the stopped agent exactly once.
"""
import pytest

from assistant.core import owner_requests as orq


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "owner.db")
    orq.init_owner_requests(path)
    return path


def ask(db, **kw):
    kw.setdefault("title", "Printify API key")
    kw.setdefault("kind", "secret")
    kw.setdefault("name", "printify_api_key")
    return orq.raise_request(db, 1, **kw)


def test_a_request_appears_on_the_board(db):
    rid = ask(db, why="no order can be fulfilled without it", blocks="order routing")
    board = orq.list_requests(db, 1)
    assert len(board) == 1
    item = board[0]
    assert item["id"] == rid and item["status"] == "open"
    assert item["why"] == "no order can be fulfilled without it"
    assert item["has_value"] is False


def test_re_raising_the_same_need_does_not_grow_the_board(db):
    """Agents run on a schedule. Without this the same ask lands every cycle and the
    board becomes unreadable, which is the same as having no board."""
    first = ask(db)
    for _ in range(5):
        again = ask(db)
        assert again == first
    assert len(orq.list_requests(db, 1)) == 1


def test_re_raising_sharpens_the_reasoning_rather_than_discarding_it(db):
    """The one part of a repeat ask that legitimately improves: the agent learns more
    about what it is blocked on, and states it better."""
    rid = ask(db, why="needed eventually", priority=3)
    ask(db, why="every listing is stuck behind this", blocks="all fulfilment", priority=1)
    item = orq.list_requests(db, 1)[0]
    assert item["id"] == rid
    assert item["why"] == "every listing is stuck behind this"
    assert item["blocks"] == "all fulfilment"
    assert item["priority"] == 1, "urgency may rise on a repeat, and must not fall"


def test_a_rejected_ask_is_not_resurrected_by_the_dedupe(db):
    """He said no deliberately. A later run raising the same name must file a NEW request
    rather than silently reopening the one he closed -- otherwise 'no' never sticks."""
    first = ask(db)
    orq.set_status(db, 1, first, "rejected", note="not doing Printify")
    second = ask(db)
    assert second != first


class TestSecrets:
    def test_a_stored_secret_never_leaves_in_a_board_read(self, db):
        rid = ask(db)
        orq.provide(db, 1, rid, "sk_live_abcdef1234567890")
        item = orq.list_requests(db, 1)[0]
        assert item["has_value"] is True and item["is_secret"] is True
        assert item["value"] == "...7890", "only a fingerprint, so he can tell which key it is"
        assert "abcdef" not in str(item)

    def test_a_short_secret_is_hidden_entirely(self, db):
        """Revealing 3 of 6 characters is not a mask."""
        rid = ask(db)
        orq.provide(db, 1, rid, "hunter2")
        assert orq.list_requests(db, 1)[0]["value"] == "(set)"

    def test_a_url_is_shown_back_because_that_is_the_point(self, db):
        rid = orq.raise_request(db, 1, title="Store admin URL", kind="url", name="store_admin")
        orq.provide(db, 1, rid, "https://admin.shopify.com/store/x")
        assert orq.list_requests(db, 1)[0]["value"] == "https://admin.shopify.com/store/x"

    def test_the_agent_reads_the_real_value_at_use_time(self, db):
        """Not at process start: a token supplied at noon must work on the 12:05 run
        without restarting a Windows service."""
        rid = ask(db)
        assert orq.secret(db, "printify_api_key") is None
        orq.provide(db, 1, rid, "sk_live_abcdef1234567890")
        assert orq.secret(db, "printify_api_key") == "sk_live_abcdef1234567890"

    def test_a_rotated_token_supersedes_the_old_one(self, db):
        rid = ask(db)
        orq.provide(db, 1, rid, "old_token_value_1111")
        orq.provide(db, 1, rid, "new_token_value_2222")
        assert orq.secret(db, "printify_api_key") == "new_token_value_2222"

    def test_a_rejected_request_supplies_nothing(self, db):
        rid = ask(db)
        orq.provide(db, 1, rid, "sk_live_abcdef1234567890")
        orq.set_status(db, 1, rid, "rejected")
        assert orq.secret(db, "printify_api_key") is None


class TestTheUnblockSignal:
    def test_the_stopped_agent_is_told_once(self, db):
        rid = ask(db, agent_key="store_manager")
        assert orq.pending_unblocks(db, "store_manager") == []
        orq.provide(db, 1, rid, "sk_live_abcdef1234567890")

        pending = orq.pending_unblocks(db, "store_manager")
        assert [p["id"] for p in pending] == [rid]
        orq.mark_notified(db, [p["id"] for p in pending])
        assert orq.pending_unblocks(db, "store_manager") == [], "announced once, not every run"

    def test_only_the_blocked_agent_hears_about_it(self, db):
        rid = ask(db, agent_key="store_manager")
        orq.provide(db, 1, rid, "sk_live_abcdef1234567890")
        assert orq.pending_unblocks(db, "social_director") == []

    def test_a_rotated_value_is_announced_again(self, db):
        """A replaced credential is news: the agent that failed on the dead one has to
        learn there is a live one to retry with."""
        rid = ask(db, agent_key="store_manager")
        orq.provide(db, 1, rid, "first_token_aaaa1111")
        orq.mark_notified(db, [rid])
        orq.provide(db, 1, rid, "second_token_bbbb2222")
        assert [p["id"] for p in orq.pending_unblocks(db, "store_manager")] == [rid]


def test_providing_does_not_resolve_it(db):
    """Only the agent can confirm the thing works. A token that is present but wrong is
    the same outage as a missing one -- the distinction print-station never drew."""
    rid = ask(db)
    orq.provide(db, 1, rid, "sk_live_abcdef1234567890")
    assert orq.list_requests(db, 1)[0]["status"] == "provided"
    orq.set_status(db, 1, rid, "resolved", note="authenticated against Printify")
    assert orq.list_requests(db, 1)[0]["status"] == "resolved"


def test_the_board_leads_with_what_is_stopped_and_why_it_matters(db):
    ask(db, name="low", title="Nice to have", priority=3)
    urgent = ask(db, name="high", title="Blocks everything", priority=1)
    done = ask(db, name="done", title="Already answered", priority=1)
    orq.provide(db, 1, done, "value_that_is_long_enough")
    board = orq.list_requests(db, 1)
    assert board[0]["id"] == urgent, "open work first, most consequential first"
    assert board[-1]["title"] == "Already answered", "answered work sinks"


def test_the_summary_names_the_single_worst_blocker(db):
    ask(db, name="a", title="Printify token", priority=1, blocks="all fulfilment")
    ask(db, name="b", title="Something else", priority=2)
    s = orq.blocked_summary(db, 1)
    assert s["open"] == 2 and s["top"]["title"] == "Printify token"
    assert s["top"]["blocks"] == "all fulfilment"


def test_requests_are_scoped_per_project_for_the_manager_view(db):
    ask(db, name="store", project_id=3)
    ask(db, name="other", project_id=9)
    assert len(orq.list_requests(db, 1, project_id=3)) == 1


def test_an_unknown_kind_is_refused_rather_than_stored(db):
    with pytest.raises(ValueError):
        orq.raise_request(db, 1, title="x", kind="banana")


def test_one_owners_board_is_not_another_owners(db):
    ask(db)
    assert orq.list_requests(db, 2) == []


class TestGenerationPrompts:
    """A request for a new catalogue model is only actionable if it arrives with prompts
    he can paste straight into the Leonardo web UI -- the team cannot generate one itself.
    """

    def test_prompts_survive_the_round_trip_as_a_list(self, db):
        rid = orq.raise_request(
            db, 1, title="New model: mens tees", kind="file", name="model_mens_tees",
            why="every mens tee mockup needs the same face to look like one brand",
            prompts=["photorealistic man, 30s, athletic build, plain white tee, studio",
                     "same man, three-quarter turn, arms crossed, soft key light"])
        item = orq.list_requests(db, 1)[0]
        assert item["id"] == rid
        assert len(item["prompts"]) == 2
        assert item["prompts"][0].startswith("photorealistic man")

    def test_a_request_without_prompts_reads_as_an_empty_list_not_none(self, db):
        """The board renders these directly, so the absent case must be iterable."""
        ask(db)
        assert orq.list_requests(db, 1)[0]["prompts"] == []

    def test_a_repeat_ask_can_add_prompts_it_did_not_have_before(self, db):
        rid = orq.raise_request(db, 1, title="New model", kind="file", name="model_x",
                                why="needed")
        orq.raise_request(db, 1, title="New model", kind="file", name="model_x",
                          why="needed", prompts=["a full prompt here"])
        assert orq.list_requests(db, 1)[0]["prompts"] == ["a full prompt here"]
        assert orq.list_requests(db, 1)[0]["id"] == rid

    def test_malformed_stored_prompts_degrade_to_empty_rather_than_breaking_the_board(self, db):
        import sqlite3
        rid = ask(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE owner_requests SET prompts = ? WHERE id = ?", ("{not json", rid))
        conn.commit(); conn.close()
        assert orq.list_requests(db, 1)[0]["prompts"] == []


def test_the_unblock_notice_never_breaks_the_assignment_it_rides_on(tmp_path):
    """It is a courtesy prepended to real work. A database without these tables must cost
    the employee its preamble, not its job -- a briefing addition that can raise is one
    that can cancel the work it was meant to restart."""
    empty = str(tmp_path / "no-tables.db")
    import sqlite3 as _s
    _s.connect(empty).close()
    assert orq.unblock_notice(empty, "store_manager") == ""


def test_the_unblock_notice_names_what_to_retry_and_delivers_once(db):
    rid = ask(db, agent_key="store_manager", blocks="all order fulfilment")
    orq.provide(db, 1, rid, "sk_live_abcdef1234567890")
    notice = orq.unblock_notice(db, "store_manager")
    assert "printify_api_key" in notice and "all order fulfilment" in notice
    assert orq.unblock_notice(db, "store_manager") == "", "delivered once, not every run"


class TestSecretDetection:
    """Whether a supplied value is treated as a credential.

    Keying this off `kind` alone got it wrong the first time it mattered: a real Printify
    token, filed as an `account` ask ("go sign up for Printify"), was stored and rendered
    in the clear. An `account` request is answered with a credential far more often than
    not, so the decision has to look at the name and the value too -- and err toward
    masking, because mis-masking a URL costs a glance while mis-revealing a token puts a
    live credential in every response and screenshot of that screen.
    """

    def test_an_account_ask_answered_with_a_token_is_still_a_secret(self, db):
        rid = orq.raise_request(db, 1, title="Printify account", kind="account",
                                name="printify_api_key")
        orq.provide(db, 1, rid, "eyJ0eXAiOiJKV1Qi.eyJhdWQiOiIzN2Q0.nN8X2LY3B_zB3Obb")
        item = orq.list_requests(db, 1)[0]
        assert item["is_secret"] is True
        assert item["value"] == "...3Obb"

    def test_a_jwt_is_recognised_whatever_it_was_filed_as(self, db):
        rid = orq.raise_request(db, 1, title="Some value", kind="text", name="whatever")
        orq.provide(db, 1, rid, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP")
        assert orq.list_requests(db, 1)[0]["is_secret"] is True

    # Deliberately not shaped like the real thing. Detection keys on the PREFIX alone
    # (_SECRET_PREFIXES), so the body proves nothing -- and a fixture that does look
    # real gets the whole branch rejected by GitHub's push protection, which is how
    # this one was found.
    @pytest.mark.parametrize("value", [
        "shpat_EXAMPLE_NOT_A_REAL_TOKEN",   # Shopify admin token
        "sk_live_EXAMPLE_NOT_A_REAL_KEY",   # Stripe-style
        "ghp_EXAMPLE_NOT_A_REAL_TOKEN",     # GitHub PAT
        "r8_EXAMPLE_NOT_A_REAL_TOKEN",      # Replicate
    ])
    def test_known_credential_prefixes_are_masked(self, db, value):
        rid = orq.raise_request(db, 1, title="A value", kind="text", name="plain")
        orq.provide(db, 1, rid, value)
        assert orq.list_requests(db, 1)[0]["is_secret"] is True

    @pytest.mark.parametrize("kind,name,value", [
        ("text", "shopify_shop", "mystore.myshopify.com"),
        ("url", "store_admin", "https://admin.shopify.com/store/x"),
        ("file", "model_catalog_import", "D:/Vinyl Stuff/models"),
        ("purchase", "bought_on", "Amex ending 1234"),
    ])
    def test_ordinary_answers_stay_readable(self, db, kind, name, value):
        """He has to be able to see what the team is now working from."""
        rid = orq.raise_request(db, 1, title="A value", kind=kind, name=name)
        orq.provide(db, 1, rid, value)
        item = orq.list_requests(db, 1)[0]
        assert item["is_secret"] is False and item["value"] == value

    def test_a_name_ending_in_key_is_enough_on_its_own(self, db):
        rid = orq.raise_request(db, 1, title="Legacy key", kind="text", name="some_api_key")
        orq.provide(db, 1, rid, "plainlookingvalue123")
        assert orq.list_requests(db, 1)[0]["is_secret"] is True

    def test_an_explicit_override_still_wins(self, db):
        rid = orq.raise_request(db, 1, title="Public id", kind="text", name="public_key")
        orq.provide(db, 1, rid, "this-is-public", is_secret=False)
        assert orq.list_requests(db, 1)[0]["value"] == "this-is-public"


def test_a_repeat_ask_corrects_its_headline_too(db):
    """The title is the line he reads first. When an agent learns its ask was mis-framed --
    'TikTok Shop seller approval' turning out to be 'TikTok ads API access', because the
    listing side was already handled by a Shopify channel -- a corrected body under a stale
    headline is worse than either, since the headline is what he decides from."""
    rid = ask(db, title="TikTok Shop seller approval", name="tiktok_access_token",
              kind="account", why="need to list products")
    ask(db, title="TikTok ads API access (not the Shop listing)", name="tiktok_access_token",
        kind="account", why="listings already covered by the Shopify channel")
    item = orq.list_requests(db, 1)[0]
    assert item["id"] == rid, "still one request, not two"
    assert item["title"] == "TikTok ads API access (not the Shop listing)"
    assert item["why"] == "listings already covered by the Shopify channel"
