"""The engineering employees get the same memory the crypto desk got.

crypto_journal.py was already ~90% generic; this covers the third role added on top of it.
An engineer's entry is built from the work record (its real status and error, not the
model's account of them) plus any PR number its output reported, standing in for the
trader's ledger fills.

Two properties carry most of the weight:
  * a FAILED assignment still journals -- "I tried this and it did not work, here is how
    far I got" is the entry the next run most needs, and the one a model narrating its own
    history is least likely to write
  * engineering notes land in the agent folder, never in the owner's own folders, and the
    crypto desk keeps the folders it already shipped with

Nothing here touches the real vault or calls a real model.
"""
import pytest

from assistant.core import crypto_journal, obsidian_client, staff
from assistant.core.obsidian_client import AGENT_FOLDER


@pytest.fixture
def vault(tmp_path):
    return obsidian_client.ObsidianClient(str(tmp_path / "vault"))


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "staff.db")
    staff.init_staff_db(path)
    return path


def _hire_engineer(db_path, title="Senior Software Developer"):
    emp = staff.hire(db_path, title,
                     "Writes and ships backend Python for the assistant, with tests.",
                     department="engineering")
    staff.set_data_feeds(db_path, emp["key"], "journal")
    return emp["key"]


class CapturingLLM:
    def __init__(self, reply="Done.", fail=False):
        self.prompts = []
        self._reply, self._fail = reply, fail

    def research(self, prompt, system_prompt=None, timeout=None, tools=None, employee_key=None):
        self.prompts.append(prompt)
        if self._fail:
            raise RuntimeError("the CLI backend timed out")
        return self._reply


# --- role resolution ----------------------------------------------------------

def test_role_for_distinguishes_the_three_kinds_of_employee():
    assert crypto_journal.role_for({"data_feeds": "market,paper,journal"}) == "trader"
    assert crypto_journal.role_for({"data_feeds": "journal", "department": "engineering"}) == "engineer"
    assert crypto_journal.role_for({"data_feeds": "market,journal", "department": "research"}) == "analyst"


def test_the_crypto_desk_keeps_the_folders_it_shipped_with():
    """Moving them would orphan the wikilinks between notes that already exist."""
    assert crypto_journal.folders_for("trader") == ("04-Journal", "03-Areas")
    assert crypto_journal.folders_for("analyst") == ("04-Journal", "03-Areas")


def test_engineering_notes_live_in_the_agent_folder():
    assert crypto_journal.folders_for("engineer") == (AGENT_FOLDER, AGENT_FOLDER)


# --- the engineering block ----------------------------------------------------

def test_the_engineer_is_told_to_record_reasoning_not_a_changelog():
    text = crypto_journal.journal_instructions("engineer")

    assert "already in git" in text
    assert "components" in text
    assert "orders" not in text.lower(), "an engineer has no trading ledger"


def test_components_parse_as_the_entry_subject(vault):
    entry = crypto_journal.parse_journal_entry("""```journal
{"kind": "change", "summary": "Split the chat timeout", "components": ["scheduler.py"],
 "detail": "Why it had to be two timeouts.", "lesson": "work_queue owns the long one"}
```""")

    assert entry["kind"] == "change"
    assert entry["tickers"] == ["scheduler.py"], "components and tickers are one field"
    assert entry["lesson"] == "work_queue owns the long one"


def test_a_file_name_is_not_upper_cased_the_way_a_ticker_is():
    """Ticker codes are short and upper-case; module names are neither."""
    entry = crypto_journal.parse_journal_entry(
        '```journal\n{"summary": "x", "detail": "y", "components": ["local_fast_path.py"]}\n```')

    assert entry["tickers"] == ["local_fast_path.py"]


@pytest.mark.parametrize("text,expected", [
    ("Opened PR #14 against main.", [14]),
    ("See https://github.com/talk2dug/personal-assistant/pull/213 for the diff.", [213]),
    ("Fixed #7 and #7 again", [7]),
    ("No pull request this run.", []),
])
def test_pr_numbers_are_scraped_from_the_output(text, expected):
    assert crypto_journal.pr_numbers(text) == expected


# --- what actually gets written -----------------------------------------------

def test_an_engineer_entry_carries_the_work_record_not_the_models_claim(vault):
    entry = {"kind": "change", "summary": "Split the chat timeout", "tickers": ["scheduler.py"],
             "detail": "Two timeouts, not one.", "lesson": None, "direction_change": False}
    work = {"assignment": "Fix the chat timeout", "status": "delivered", "error": None}

    result = crypto_journal.record_run(
        vault, "Senior Software Developer", entry, role="engineer", work=work,
        output="Opened PR #14.")

    assert result["journaled"] is True
    assert result["daily"].startswith(AGENT_FOLDER)
    body = vault.read_note(AGENT_FOLDER, vault.list_notes(AGENT_FOLDER)["notes"][0]["title"])["content"]
    assert "Components: scheduler.py" in body
    assert "Asked to: Fix the chat timeout" in body
    assert "Outcome: delivered" in body
    assert "#14" in body


def test_a_failed_assignment_is_still_journalled(vault):
    """The entry the next run most needs, and the one a model would quietly omit."""
    work = {"assignment": "Fix the chat timeout", "status": "failed",
            "error": "RuntimeError: the CLI backend timed out"}

    result = crypto_journal.record_run(
        vault, "Senior Software Developer", None, role="engineer", work=work, output=None)

    assert result["journaled"] is True
    body = vault.read_note(AGENT_FOLDER, vault.list_notes(AGENT_FOLDER)["notes"][0]["title"])["content"]
    assert "Outcome: failed" in body
    assert "the CLI backend timed out" in body


def test_a_shipped_pr_folds_into_the_running_summary(vault):
    """A PR is the engineer's closed trade: the thing that actually landed, and the thing
    still worth knowing several runs later."""
    entry = {"kind": "change", "summary": "Split the chat timeout", "tickers": [],
             "detail": "d", "lesson": None, "direction_change": False}

    result = crypto_journal.record_run(
        vault, "Senior Software Developer", entry, role="engineer",
        work={"assignment": "a", "status": "delivered"}, output="Opened PR #14.")

    assert result["folded"] is True
    summary = vault.read_note(AGENT_FOLDER, "Senior Software Developer -- Running Summary")
    assert "SHIPPED: #14" in summary["content"]


def test_a_routine_engineer_run_writes_only_the_daily_entry(vault):
    entry = {"kind": "analysis", "summary": "Looked, changed nothing", "tickers": [],
             "detail": "d", "lesson": None, "direction_change": False}

    result = crypto_journal.record_run(
        vault, "Senior Software Developer", entry, role="engineer",
        work={"assignment": "a", "status": "delivered"}, output="No PR needed.")

    assert result["journaled"] is True
    assert result["folded"] is False


def test_an_engineer_never_writes_into_the_owners_own_folders(vault):
    entry = {"kind": "change", "summary": "s", "tickers": ["x.py"], "detail": "d",
             "lesson": "l", "direction_change": False}

    crypto_journal.record_run(vault, "Senior Software Developer", entry, role="engineer",
                              work={"assignment": "a", "status": "delivered"},
                              output="PR #1")

    for owners_folder in ("01-Research", "02-Projects", "03-Areas", "04-Journal", "00-About Me"):
        assert vault.list_notes(owners_folder)["notes"] == []


def test_an_engineers_compacted_summary_keeps_lessons_and_drops_the_ticker_heading(vault):
    for i in range(40):
        crypto_journal.record_run(
            vault, "Senior Software Developer",
            {"kind": "change", "summary": f"Change {i}", "tickers": [], "detail": "x" * 200,
             "lesson": f"lesson {i}", "direction_change": False},
            role="engineer", work={"assignment": "a", "status": "delivered"}, output="")

    result = crypto_journal.compact_summary(vault, "Senior Software Developer",
                                            role="engineer", force=True)

    assert result["compacted"] is True
    body = vault.read_note(AGENT_FOLDER, "Senior Software Developer -- Running Summary")["content"]
    assert "Current thesis by ticker" not in body, "an engineer keeps no per-ticker thesis"
    assert "lesson 39" in body
    assert result["was"] > result["now"]


# --- end to end through assign() ----------------------------------------------

def test_an_engineer_reads_its_own_prior_notes_back_on_the_next_run(db_path, vault):
    """The whole point: written once, read back into a later prompt. Anything else is a
    diary, not learning."""
    key = _hire_engineer(db_path)
    llm = CapturingLLM("""Did the work.
```journal
{"kind": "change", "summary": "Split the chat timeout", "components": ["scheduler.py"],
 "detail": "Two timeouts.", "lesson": "the work queue owns the long timeout"}
```""")

    staff.assign(db_path, llm, key, "Fix the chat timeout", obsidian=vault)
    staff.assign(db_path, llm, key, "Now fix the other one", obsidian=vault)

    assert "the work queue owns the long timeout" in llm.prompts[1], \
        "the second run must see what the first one learned"
    assert "the work queue owns the long timeout" not in llm.prompts[0]


def test_a_failed_assign_still_leaves_a_journal_entry(db_path, vault):
    key = _hire_engineer(db_path)

    result = staff.assign(db_path, CapturingLLM(fail=True), key, "Fix the thing",
                          obsidian=vault)

    assert result["ok"] is False
    notes = vault.list_notes(AGENT_FOLDER)["notes"]
    assert notes, "a failed assignment must still be recorded"
    body = vault.read_note(AGENT_FOLDER, notes[0]["title"])["content"]
    assert "Outcome: failed" in body
    assert "timed out" in body


def test_an_engineer_without_the_journal_feed_writes_nothing(db_path, vault):
    """Every employee journals by default now, so the feed has to be taken away on
    purpose -- and when it is, nothing may be written, or the setting is decorative."""
    emp = staff.hire(db_path, "Senior Code Reviewer",
                     "Reviews pull requests for correctness and regressions.",
                     department="engineering")
    staff.set_data_feeds(db_path, emp["key"], "")

    staff.assign(db_path, CapturingLLM(), emp["key"], "Review PR 14", obsidian=vault)

    assert vault.list_notes(AGENT_FOLDER)["notes"] == []


def test_the_crypto_desk_is_unaffected_by_the_engineering_role(db_path, vault):
    """Its notes still go where they always did."""
    emp = staff.hire(db_path, "Crypto Research Analyst",
                     "Watches the crypto market and reports on what is moving and why.",
                     department="research")
    staff.set_data_feeds(db_path, emp["key"], "journal")
    llm = CapturingLLM("""Read the market.
```journal
{"kind": "analysis", "summary": "BTC range-bound", "tickers": ["BTC"], "detail": "d"}
```""")

    staff.assign(db_path, llm, emp["key"], "What is moving?", obsidian=vault)

    assert vault.list_notes("04-Journal")["notes"], "the analyst still writes to 04-Journal"
    assert vault.list_notes(AGENT_FOLDER)["notes"] == []
