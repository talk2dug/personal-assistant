"""The business agents' Obsidian journal.

Jack: "all agents should be using obsidian", and for the dashboard, "i want to see their
thoughts on the market research and how they are coming to the conclusions". Those are one
requirement seen twice: the reasoning has to be written down, per agent, per run.

The rule agent_notes states about itself is what these tests actually defend: **anything
written must be read back into a later prompt, or it is a diary, not learning.** So writing
without reading back would be a failure even though every note existed.
"""
import pytest

from assistant.core import agent_notes
from assistant.core.obsidian_client import AGENT_FOLDER, ObsidianClient


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return ObsidianClient(str(root))


class TestWriting:
    def test_a_run_is_recorded_with_its_reasoning(self, vault):
        result = agent_notes.write_journal(
            vault, "trend_scout", "Found 3 leads",
            reasoning="Ruled out anything already extended; the rest were seasonal.")
        assert result["written"] is True
        note = vault.read_note(AGENT_FOLDER, agent_notes.journal_title("trend_scout"))
        assert "Found 3 leads" in note["content"]
        assert "already extended" in note["content"]

    def test_several_runs_in_a_day_append_rather_than_overwrite(self, vault):
        """Several runs a day is normal. A note per run would bury the folder the owner
        also reads, and overwriting would lose the morning's thinking by lunchtime."""
        agent_notes.write_journal(vault, "trend_scout", "morning run", reasoning="first look")
        agent_notes.write_journal(vault, "trend_scout", "afternoon run", reasoning="second look")
        content = vault.read_note(AGENT_FOLDER, agent_notes.journal_title("trend_scout"))["content"]
        assert "morning run" in content and "afternoon run" in content

    def test_decisions_are_listed_when_given(self, vault):
        agent_notes.write_journal(vault, "store_manager", "listed 1", reasoning="why",
                                  decisions=["priced at $24.99", "routed to SwiftPOD"])
        content = vault.read_note(AGENT_FOLDER, agent_notes.journal_title("store_manager"))["content"]
        assert "- priced at $24.99" in content and "- routed to SwiftPOD" in content

    def test_no_vault_is_not_an_error(self, vault):
        """A vault on a disconnected drive costs an agent its note, never its work --
        everything real is committed to the database before this is reached."""
        assert agent_notes.write_journal(None, "trend_scout", "x")["written"] is False

    def test_a_broken_vault_never_raises(self, vault):
        class Broken:
            def read_note(self, *a, **k):
                raise OSError("drive gone")

            def write_note(self, *a, **k):
                raise OSError("drive gone")

        assert agent_notes.write_journal(Broken(), "trend_scout", "x")["written"] is False

    def test_a_runaway_response_is_truncated(self, vault):
        agent_notes.write_journal(vault, "trend_scout", "ok", reasoning="x" * 50_000)
        content = vault.read_note(AGENT_FOLDER, agent_notes.journal_title("trend_scout"))["content"]
        assert len(content) < agent_notes.JOURNAL_BODY_CHARS + 2000


class TestReadingBack:
    def test_prior_notes_come_back_for_the_next_prompt(self, vault):
        """The half that makes the writing worth doing."""
        agent_notes.write_journal(vault, "trend_scout", "Found 3 leads",
                                  reasoning="seasonal demand is rising")
        block = agent_notes.read_journal(vault, "trend_scout")
        assert "seasonal demand is rising" in block
        assert "YOUR OWN RECENT NOTES" in block

    def test_an_agent_only_reads_its_own_notes(self, vault):
        """Otherwise the trend scout inherits the store manager's conclusions and both
        drift toward the same undifferentiated opinion."""
        agent_notes.write_journal(vault, "store_manager", "priced a listing",
                                  reasoning="margin maths")
        assert agent_notes.read_journal(vault, "trend_scout") == ""

    def test_nothing_written_yields_an_empty_block(self, vault):
        """Callers concatenate this unconditionally, so it must be safe when empty."""
        assert agent_notes.read_journal(vault, "trend_scout") == ""

    def test_no_vault_yields_an_empty_block(self):
        assert agent_notes.read_journal(None, "trend_scout") == ""

    def test_the_block_is_bounded(self, vault):
        """This goes into a prompt on every run. An agent that spends its context
        re-reading a fortnight of its own musings has no room left to think."""
        for i in range(10):
            agent_notes.write_journal(vault, "trend_scout", f"run {i}", reasoning="y" * 3000)
        assert len(agent_notes.read_journal(vault, "trend_scout")) < agent_notes.JOURNAL_PRIOR_CHARS + 400

    def test_a_broken_vault_reads_as_empty_rather_than_failing_the_run(self, vault):
        class Broken:
            def list_notes(self, *a, **k):
                raise OSError("drive gone")

        assert agent_notes.read_journal(Broken(), "trend_scout") == ""


def test_the_agents_still_parse_json_now_that_they_reason_first(vault):
    """The prompts were changed from "Return ONLY a JSON array, no prose" to reasoning
    followed by a fenced block. _extract_json prefers the fence, so both shapes work --
    but this is the regression that would silently empty every pipeline."""
    from assistant.core.agents import _extract_json

    reasoned = (
        "REASONING: I looked at the tracked set and ruled out anything already extended.\n\n"
        "```json\n[{\"name\": \"Autumn Fair\", \"fit_score\": 80}]\n```")
    assert _extract_json(reasoned) == [{"name": "Autumn Fair", "fit_score": 80}]
    assert _extract_json('[{"a": 1}]') == [{"a": 1}]
