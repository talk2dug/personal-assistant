"""The owner's written engineering policy has to actually reach an engineer's prompt.

He maintains four policy notes by hand. They say which employee backend work routes to,
that a PR goes to the tester before it reaches him, and that an agent timing out is a bug
to root-cause rather than a fact to report. Until the `policy` feed existed, not one byte
of any of them reached an employee -- they were hired, briefed, run, and then judged
against rules they had never been shown.

Pure retrieval: nothing in this module writes to the vault, and these tests assert that
too. Every test points ObsidianClient at tmp_path; the real vault is never touched.
"""
import pytest

from assistant.core import agent_policy, obsidian_client, staff


@pytest.fixture
def vault(tmp_path):
    """A temp vault seeded with the same four notes the real one holds."""
    client = obsidian_client.ObsidianClient(str(tmp_path / "vault"))
    client.write_note("03-Areas", "Jarvis Engineering Policy",
                      "Any agent assignment that times out is a bug to root-cause, never "
                      "tolerated and noted.")
    client.write_note("03-Areas", "Dev Pipeline Policy",
                      "Before any PR is merged it must be handed to senior_software_tester "
                      "with explicit testing criteria.")
    client.write_note("00-About Me", "Engineering Team - Standing Operating Rules",
                      "All backend development work routes to heavy_backend_systems_engineer.")
    client.write_note("00-About Me", "Working Standards - Jarvis Conduct",
                      "Keep iterating until an actual solution is found. Giving up is "
                      "unacceptable.")
    return client


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "staff.db")
    staff.init_staff_db(path)
    return path


# --- the briefing itself ------------------------------------------------------

def test_every_policy_note_reaches_the_briefing(vault):
    text = agent_policy.build_policy_briefing(vault)

    assert "root-cause" in text
    assert "senior_software_tester" in text
    assert "heavy_backend_systems_engineer" in text
    assert "Giving up is unacceptable" in text


def test_the_briefing_says_his_policy_outranks_the_models_defaults(vault):
    """Same reasoning as mail_importance.format_examples telling the model his rulings
    outrank the general rules: a policy the model treats as advisory is not a policy."""
    text = agent_policy.build_policy_briefing(vault)

    assert "outrank" in text.lower()
    assert "owner" in text.lower()


def test_the_briefing_is_bounded_however_long_the_notes_get(vault):
    """This is injected on every run of every engineering employee, so it must not grow
    just because he added a paragraph to a note."""
    vault.write_note("03-Areas", "Dev Pipeline Policy", "x" * 50_000)

    text = agent_policy.build_policy_briefing(vault)

    assert len(text) < agent_policy.POLICY_CONTEXT_CHARS + 1200
    assert "continues past what you were shown" in text, \
        "a truncated policy must announce itself, or the employee will assert a rule doesn't exist"


def test_a_short_note_releases_its_unused_budget(vault):
    """Water-filling, not a flat split: three short notes and one long one should not each
    be cut to a quarter while most of the budget goes unspent."""
    sizes = [100, 100, 100, 5000]

    allocations = agent_policy._allocate(sizes, 2000)

    assert allocations[:3] == [100, 100, 100]
    assert allocations[3] == 1700, "the long note should receive everything the others left"


def test_no_vault_client_is_a_quiet_empty_string():
    assert agent_policy.build_policy_briefing(None) == ""


def test_a_missing_note_is_announced_not_silently_dropped(tmp_path):
    """These notes are hand-maintained. A rename in Obsidian must not silently drop a
    policy nobody notices is gone until an employee breaks the rule it held."""
    client = obsidian_client.ObsidianClient(str(tmp_path / "vault"))
    client.write_note("03-Areas", "Jarvis Engineering Policy", "Timeouts are bugs.")

    text = agent_policy.build_policy_briefing(client)

    assert "Timeouts are bugs." in text
    assert "could not be read" in text
    assert "Dev Pipeline Policy" in text


def test_a_completely_unreadable_vault_says_so_rather_than_going_quiet(tmp_path):
    empty = obsidian_client.ObsidianClient(str(tmp_path / "nothing"))

    text = agent_policy.build_policy_briefing(empty)

    assert "could not be read" in text
    assert "say in your report" in text


def test_a_raising_vault_client_never_propagates(tmp_path):
    class BrokenVault:
        def read_note(self, *a, **k):
            raise OSError("the drive is not mounted")

    text = agent_policy.build_policy_briefing(BrokenVault())

    assert "could not be read" in text


def test_the_briefing_never_writes_to_the_vault(vault):
    """Pure retrieval. No new store, no new writes -- asserted, not just asserted about."""
    before = {(n["folder"], n["title"]) for n in vault.list_notes()["notes"]}

    agent_policy.build_policy_briefing(vault)

    assert {(n["folder"], n["title"]) for n in vault.list_notes()["notes"]} == before


# --- the feed grant -----------------------------------------------------------

def test_policy_is_a_valid_feed_and_grantable(db_path):
    staff.hire(db_path, "Senior React Engineer",
               "Builds and reviews the React dashboard, its components and its tests.")

    assert staff.set_data_feeds(db_path, "senior_react_engineer", "policy") is True
    granted = agent_policy.policy_employees(db_path)
    assert [e["key"] for e in granted] == ["senior_react_engineer"]


def test_an_unknown_feed_is_still_rejected(db_path):
    staff.hire(db_path, "Senior React Engineer",
               "Builds and reviews the React dashboard, its components and its tests.")

    with pytest.raises(ValueError, match="unknown feed"):
        staff.set_data_feeds(db_path, "senior_react_engineer", "policy,telepathy")


def test_policy_is_never_inferred_from_a_job_description(db_path):
    """Same rule as `paper` and `journal`: a job description must not grant its own feeds."""
    feeds = staff.infer_data_feeds(
        "Senior Software Architect",
        "Owns engineering policy, the dev pipeline policy and the team's standing rules.")

    assert "policy" not in feeds


# --- assign() actually splices it in ------------------------------------------

class CapturingLLM:
    """Captures the prompt instead of calling anything. No network, no real model."""

    def __init__(self):
        self.prompt = None

    def research(self, prompt, system_prompt=None, timeout=None, tools=None, employee_key=None):
        self.prompt = prompt
        return "Done."


def test_an_employee_with_the_feed_gets_the_policy_in_its_prompt(db_path, vault):
    staff.hire(db_path, "Senior React Engineer",
               "Builds and reviews the React dashboard, its components and its tests.")
    staff.set_data_feeds(db_path, "senior_react_engineer", "policy")
    llm = CapturingLLM()

    result = staff.assign(db_path, llm, "senior_react_engineer", "Fix the Review page.",
                          obsidian=vault)

    assert result["ok"] is True
    assert "senior_software_tester" in llm.prompt
    assert "Giving up is unacceptable" in llm.prompt
    assert "Fix the Review page." in llm.prompt


def test_an_employee_without_the_feed_gets_no_policy(db_path, vault):
    staff.hire(db_path, "Senior React Engineer",
               "Builds and reviews the React dashboard, its components and its tests.")
    llm = CapturingLLM()

    staff.assign(db_path, llm, "senior_react_engineer", "Fix the Review page.", obsidian=vault)

    assert "STANDING POLICY" not in llm.prompt


def test_an_employee_with_the_feed_still_runs_with_no_vault_wired(db_path):
    """A missing vault costs context and nothing else -- the work must still get done."""
    staff.hire(db_path, "Senior React Engineer",
               "Builds and reviews the React dashboard, its components and its tests.")
    staff.set_data_feeds(db_path, "senior_react_engineer", "policy")
    llm = CapturingLLM()

    result = staff.assign(db_path, llm, "senior_react_engineer", "Fix the Review page.",
                          obsidian=None)

    assert result["ok"] is True
    assert "STANDING POLICY" not in llm.prompt
