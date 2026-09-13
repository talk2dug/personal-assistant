"""Completed research briefs must actually reach the vault, in the agent folder.

Both research queues used to call complete_research(...) and stop. Nine business briefs
and three personal ones had finished that way while 01-Research's newest note was weeks
old -- the exact "write-only or worse" pattern the audit found everywhere except
mail_importance and the crypto journal.

Two properties are load-bearing and both are tested here:
  * the note lands in AGENT_FOLDER, never in the owner's own hand-written folders
  * a vault failure costs the note and never the findings, which are already in SQLite

Every test points ObsidianClient at tmp_path. Nothing here may touch the real vault.
"""
import pytest

from assistant.config import BusinessProfile
from assistant.core import agent_notes, agents, obsidian_client, personal_agents, personal_db, db
from assistant.core.obsidian_client import AGENT_FOLDER


@pytest.fixture
def vault(tmp_path):
    return obsidian_client.ObsidianClient(str(tmp_path / "vault"))


class ResearchLLM:
    """Stands in for the Claude CLI backend. Never touches a network."""

    def __init__(self, findings="Three dentists take his insurance. Sources: example.com"):
        self.findings = findings
        self.calls = []

    def research(self, prompt, system_prompt=None, timeout=None, tools=None, employee_key=None):
        self.calls.append(prompt)
        return self.findings


# --- the writer itself --------------------------------------------------------

def test_a_brief_lands_in_the_agent_folder(vault):
    result = agent_notes.write_research_brief(
        vault, "Dentist search, Glen Allen VA", "Dr Smith on Broad St takes his plan.",
        question="Who takes my insurance?", source="personal")

    assert result["written"] is True
    assert result["path"].startswith(AGENT_FOLDER)
    notes = vault.list_notes(AGENT_FOLDER)["notes"]
    assert len(notes) == 1
    body = vault.read_note(AGENT_FOLDER, notes[0]["title"])["content"]
    assert "Dr Smith on Broad St takes his plan." in body
    assert "Who takes my insurance?" in body


def test_a_brief_never_lands_in_the_owners_own_folders(vault):
    agent_notes.write_research_brief(vault, "Topic", "Findings.", source="business")

    for owners_folder in ("01-Research", "02-Projects", "03-Areas", "00-About Me"):
        assert vault.list_notes(owners_folder)["notes"] == [], \
            f"agent output must stay out of {owners_folder}"


def test_no_vault_client_is_a_quiet_no_op(tmp_path):
    assert agent_notes.write_research_brief(None, "Topic", "Findings.")["written"] is False


def test_empty_findings_are_not_filed(vault):
    assert agent_notes.write_research_brief(vault, "Topic", "   ")["written"] is False
    assert vault.list_notes(AGENT_FOLDER)["notes"] == []


def test_a_broken_vault_is_reported_not_raised(vault):
    class BrokenVault:
        def write_note(self, *a, **k):
            raise OSError("the drive is not mounted")

    result = agent_notes.write_research_brief(BrokenVault(), "Topic", "Findings.")

    assert result["written"] is False
    assert "OSError" in result["reason"]


def test_a_runaway_response_is_capped(vault):
    agent_notes.write_research_brief(vault, "Topic", "x" * (agent_notes.BRIEF_BODY_CHARS * 3))

    title = vault.list_notes(AGENT_FOLDER)["notes"][0]["title"]
    body = vault.read_note(AGENT_FOLDER, title)["content"]
    assert len(body) < agent_notes.BRIEF_BODY_CHARS + 2000


# --- the two queues actually call it ------------------------------------------

PROFILE = BusinessProfile(
    name="Blue Ridge Custom Co", location="Richmond, VA", radius_miles=60,
    product_lines=["Vinyl stickers", "DTF apparel", "Metal prints"],
)


@pytest.fixture
def db_path(tmp_path):
    from assistant.core import business_db
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    personal_db.init_personal_db(path)
    return path


def test_business_research_queue_files_its_brief(db_path, vault):
    from assistant.core import business_db
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    business_db.create_research(db_path, owner_id, "Wholesale vinyl suppliers", "Who is cheapest per roll?")

    result = agents.run_research_queue(db_path, ResearchLLM(), PROFILE, obsidian=vault)

    assert result["done"] == 1
    notes = vault.list_notes(AGENT_FOLDER)["notes"]
    assert len(notes) == 1
    assert "Wholesale vinyl suppliers" in notes[0]["title"]


def test_personal_research_queue_files_its_brief(db_path, vault):
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    personal_db.create_research(db_path, owner_id, "Dentist search", "Who takes my insurance?")

    result = personal_agents.run_personal_research_queue(
        db_path, ResearchLLM(), owner_id, obsidian=vault)

    assert result["done"] == 1
    notes = vault.list_notes(AGENT_FOLDER)["notes"]
    assert len(notes) == 1
    assert "Dentist search" in notes[0]["title"]


def test_findings_survive_a_vault_that_is_down(db_path):
    """The whole point of the best-effort contract: the research is the deliverable."""
    from assistant.core import business_db

    class BrokenVault:
        def write_note(self, *a, **k):
            raise OSError("the drive is not mounted")

    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    business_db.create_research(db_path, owner_id, "Wholesale vinyl suppliers", "Cheapest per roll?")

    result = agents.run_research_queue(db_path, ResearchLLM(), PROFILE, obsidian=BrokenVault())

    assert result["done"] == 1, "a vault failure must never fail the research run"
    done = business_db.pending_research(db_path)
    assert done == [], "the findings were still committed to the database"


def test_queues_still_work_with_no_vault_wired_at_all(db_path):
    from assistant.core import business_db
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    business_db.create_research(db_path, owner_id, "Wholesale vinyl suppliers", "Cheapest per roll?")

    assert agents.run_research_queue(db_path, ResearchLLM(), PROFILE)["done"] == 1


# --- the folder list has exactly one source of truth --------------------------

def test_the_agent_folder_is_reachable_from_the_models_tool_enum():
    """engine.OBSIDIAN_FOLDERS is the enum write_note is constrained to. It used to be a
    second hand-maintained copy of this list; a folder that code can write but the model
    cannot name (or the reverse) is the failure this guards."""
    from assistant.core import engine

    assert engine.OBSIDIAN_FOLDERS is obsidian_client.FOLDERS
    assert AGENT_FOLDER in engine.OBSIDIAN_FOLDERS
