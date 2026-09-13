"""The watchdog turns must stay in the audit trail without eating Jarvis's memory.

Both watchdogs (scheduler.run_review_watchdog / run_github_watchdog) reach the owner by
synthesizing a prompt and pushing it through handle_message. That is deliberate and safe
-- it inherits the pending_actions confirmation gate rather than routing around it -- but
handle_message persists every turn it handles as a real *user* turn, and
db.recent_messages hands back only the last 20 rows. With the GitHub watchdog polling
every 180s, the measurement on the live database was 45 of the last 100 user turns being
watchdog chatter: roughly half of Jarvis's working memory was PR noise.

So the property under test is precisely two-sided, and both sides matter:
  * the row is still written and still readable (it is the only record that answers "did
    the watchdog ever actually nudge me about this?")
  * it does not come back from recent_messages, which is the window the model sees
"""
import sqlite3

import pytest

from assistant.core import business_db, db, engine, github_client, personal_db, scheduler


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeLLM:
    """A plain non-agentic backend: one canned reply, no tools, no network."""

    def __init__(self, reply="Understood, sir."):
        self._reply = reply

    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": self._reply}


# --- the storage layer --------------------------------------------------------

def test_a_tagged_turn_is_persisted_but_stays_out_of_the_window(db_path, owner_id):
    db.add_message(db_path, owner_id, "user", "what did the roofer quote?")
    db.add_message(db_path, owner_id, "assistant", "Eleven thousand, sir.")
    db.add_message(db_path, owner_id, "user", "PR #14 just went red", source="github_watchdog")
    db.add_message(db_path, owner_id, "assistant", "Its backend check failed.", source="github_watchdog")

    window = db.recent_messages(db_path, owner_id, limit=20)

    assert [m["content"] for m in window] == ["what did the roofer quote?", "Eleven thousand, sir."]
    # ...but nothing was thrown away.
    full = db.recent_messages(db_path, owner_id, limit=20, include_background=True)
    assert len(full) == 4
    assert "PR #14 just went red" in [m["content"] for m in full]


def test_watchdog_turns_cannot_push_real_conversation_out_of_the_window(db_path, owner_id):
    """The actual failure that was happening, reproduced at the window's real size.

    Without the filter, 20 watchdog rows fill a 20-row window completely and the owner's
    own sentence is gone.
    """
    db.add_message(db_path, owner_id, "user", "remember: the deposit is due Friday")
    for i in range(20):
        db.add_message(db_path, owner_id, "user", f"PR #{i} changed", source="github_watchdog")
        db.add_message(db_path, owner_id, "assistant", f"noted #{i}", source="github_watchdog")

    window = db.recent_messages(db_path, owner_id, limit=20)

    assert [m["content"] for m in window] == ["remember: the deposit is due Friday"]


def test_an_untagged_turn_is_unaffected(db_path, owner_id):
    """Chat, voice and location routines pass no source and must behave exactly as before."""
    db.add_message(db_path, owner_id, "user", "hello")

    assert db.recent_messages(db_path, owner_id) == [{"role": "user", "content": "hello"}]


def test_the_source_migration_is_idempotent_on_a_pre_existing_table(tmp_path):
    """Same PRAGMA table_info guard as every other migration here. Built by hand at the
    pre-column schema so this exercises the real ALTER path, not a fresh CREATE TABLE."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_chat_id TEXT,
                               display_name TEXT, role TEXT, created_at TEXT);
           CREATE TABLE conversations (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER NOT NULL REFERENCES users(id),
               role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
               content TEXT NOT NULL,
               created_at TEXT NOT NULL);
           INSERT INTO users (telegram_chat_id, display_name, role, created_at)
               VALUES ('111', 'Dug', 'owner', '2020-01-01T00:00:00+00:00');
           INSERT INTO conversations (user_id, role, content, created_at)
               VALUES (1, 'user', 'a turn from before the column existed', '2020-01-01T00:00:00+00:00');"""
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    db.init_db(path)  # second pass must be a no-op, not a duplicate-column error

    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(conversations)")}
    assert "source" in cols
    # A row that predates the column is a real turn, so it must still be in the window.
    assert db.recent_messages(path, 1) == [
        {"role": "user", "content": "a turn from before the column existed"}
    ]


# --- handle_message threads the tag through both halves -----------------------

def test_handle_message_tags_both_halves_of_a_background_turn(db_path, owner_id):
    engine.handle_message(db_path, FakeLLM(), owner_id, "PR #7 changed", source="github_watchdog")

    assert db.recent_messages(db_path, owner_id) == []
    full = db.recent_messages(db_path, owner_id, include_background=True)
    # Both halves, or the answer would sit in the window as an orphan with no question.
    assert [(m["role"], m["content"]) for m in full] == [
        ("user", "PR #7 changed"), ("assistant", "Understood, sir.")]


def test_handle_message_without_a_source_still_occupies_the_window(db_path, owner_id):
    engine.handle_message(db_path, FakeLLM(), owner_id, "what's on today?")

    assert [m["role"] for m in db.recent_messages(db_path, owner_id)] == ["user", "assistant"]


# --- the two watchdogs actually pass it ---------------------------------------

def _watchdog_db(tmp_path):
    path = str(tmp_path / "wd.db")
    db.init_db(path)
    personal_db.init_personal_db(path)
    business_db.init_business_db(path)
    github_client.init_github_db(path)
    return path


def test_review_watchdog_turn_persists_but_does_not_occupy_the_window(tmp_path):
    path = _watchdog_db(tmp_path)
    db.upsert_user(path, "111", "Dug", "owner")
    owner = db.get_user_by_chat_id(path, "111")
    db.add_message(path, owner["id"], "user", "the deposit is due Friday")

    item_id = business_db.create_review_item(path, owner["id"], "Approve the new logo", kind="art")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE review_items SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                 (item_id,))
    conn.commit()
    conn.close()

    scheduler.run_review_watchdog(path, FakeLLM("Still waiting on you, sir."),
                                  notify=lambda *a: None, hours=2)

    window = db.recent_messages(path, owner["id"])
    assert [m["content"] for m in window] == ["the deposit is due Friday"]

    full = [m["content"] for m in db.recent_messages(path, owner["id"], include_background=True)]
    assert any("Approve the new logo" in c for c in full), "the nudge must still be on record"


class FakeGitOpsClient:
    def __init__(self, open_prs, statuses):
        self._open_prs, self._statuses = open_prs, statuses

    def list_open_prs(self):
        return self._open_prs

    def get_pr_status(self, pr_number):
        return self._statuses[pr_number]


def _pr_status(conclusion="success"):
    return {"ok": True, "state": "open", "mergeable": True, "merged": False,
            "url": "https://github.com/o/r/pull/7",
            "checks": [{"name": "backend", "status": "completed", "conclusion": conclusion}]}


def test_github_watchdog_turn_persists_but_does_not_occupy_the_window(tmp_path):
    path = _watchdog_db(tmp_path)
    db.upsert_user(path, "111", "Dug", "owner")
    owner = db.get_user_by_chat_id(path, "111")
    db.add_message(path, owner["id"], "user", "the deposit is due Friday")

    open_prs = [{"number": 7, "title": "Add feature", "url": "https://github.com/o/r/pull/7"}]
    github_client.refresh(path, FakeGitOpsClient(open_prs, {7: _pr_status("success")}))

    scheduler.run_github_watchdog(
        path, FakeGitOpsClient(open_prs, {7: _pr_status("failure")}),
        llm=FakeLLM("PR #7 went red, sir."), notify=lambda *a: None)

    window = db.recent_messages(path, owner["id"])
    assert [m["content"] for m in window] == ["the deposit is due Friday"]

    full = [m["content"] for m in db.recent_messages(path, owner["id"], include_background=True)]
    assert any("PR #7" in c for c in full), "the nudge must still be on record"
