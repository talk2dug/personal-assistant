"""Jarvis reads his own standing preferences back, and pays for them exactly once.

Before this, zero bytes of the vault reached Jarvis's own system prompt -- read_note and
search_notes were on-demand only, so they fired only when something in the conversation
made him think to look. A standing preference is precisely what he would never think to
look up, because he does not know it exists. The owner had written down that he wants
short answers, and Jarvis had never read it.

The caching property is the one that matters most and is tested hardest. build_system_prompt
prompt-caches on an exact prefix match, so anything varying per turn re-bills the ~25k-token
Claude Code preamble on every message. A per-turn vault read would be the most expensive
possible way to implement memory: ~4KB of value for ~25k tokens of waste, per message,
forever. The digest is therefore built once in setup.build_obsidian_context and carried on
the context as a fixed string.

Every test points ObsidianClient at tmp_path. The real vault is never touched.
"""
import pytest

from assistant.core import engine, obsidian_client, standing_digest


@pytest.fixture
def vault(tmp_path):
    client = obsidian_client.ObsidianClient(str(tmp_path / "vault"))
    client.write_note("00-About Me", "Standing Preferences",
                      "He prefers to be addressed as sir.")
    client.write_note("00-About Me", "Communication Preferences",
                      "Wants concise, limited responses at all times. Cut unnecessary detail.")
    client.write_note("00-About Me", "Working Standards - Jarvis Conduct",
                      "No partial answers dressed as complete.")
    # Something personal in the same folder that must NOT be swept in.
    client.write_note("00-About Me", "Landlord Dispute",
                      "Ongoing dispute about the deposit on the old rental.")
    return client


# --- what the digest contains -------------------------------------------------

def test_the_digest_carries_his_recorded_preferences(vault):
    text = standing_digest.build_digest(vault)

    assert "concise, limited responses" in text
    assert "addressed as sir" in text
    assert "No partial answers" in text


def test_the_digest_is_a_fixed_list_not_a_folder_sweep(vault):
    """00-About Me also holds notes on his family, his health and a rental dispute. None
    of that belongs in every prompt."""
    text = standing_digest.build_digest(vault)

    assert "Landlord" not in text
    assert "deposit on the old rental" not in text


def test_the_digest_says_it_outranks_the_models_defaults(vault):
    text = standing_digest.build_digest(vault)

    assert "outrank" in text.lower()
    assert "wrote these himself" in text


def test_a_missing_note_is_simply_absent(tmp_path):
    """These are hand-maintained; he may just not have written one. An absence, not a fault."""
    client = obsidian_client.ObsidianClient(str(tmp_path / "vault"))
    client.write_note("00-About Me", "Standing Rules", "Never send mail without asking.")

    text = standing_digest.build_digest(client)

    assert "Never send mail without asking." in text


def test_no_vault_and_an_empty_vault_both_yield_nothing(tmp_path):
    assert standing_digest.build_digest(None) == ""
    assert standing_digest.build_digest(
        obsidian_client.ObsidianClient(str(tmp_path / "empty"))) == ""


def test_a_broken_vault_never_raises(tmp_path):
    class BrokenVault:
        def read_note(self, *a, **k):
            raise OSError("the drive is not mounted")

    assert standing_digest.build_digest(BrokenVault()) == ""


def test_the_digest_is_bounded_if_a_note_grows_unnoticed(vault):
    vault.write_note("00-About Me", "Standing Rules", "x" * 200_000)

    text = standing_digest.build_digest(vault)

    assert len(text) < standing_digest.DIGEST_CHARS + 1000


# --- the caching property -----------------------------------------------------

def test_the_system_prompt_carries_the_digest_from_the_context(vault):
    ctx = engine.ObsidianContext(mcp_client=vault,
                                 standing_digest=standing_digest.build_digest(vault))

    prompt = engine.build_system_prompt("America/New_York", obsidian=ctx)

    assert "concise, limited responses" in prompt


def test_build_system_prompt_never_reads_the_vault_itself(vault):
    """The load-bearing test. If build_system_prompt ever touches the vault, the prompt
    prefix varies per turn and the ~25k-token Claude Code preamble is re-billed on every
    single message -- the same reasoning that keeps `now` out of the system prompt."""
    class ExplodingVault:
        def read_note(self, *a, **k):
            raise AssertionError("build_system_prompt must never read the vault per turn")

        def list_notes(self, *a, **k):
            raise AssertionError("build_system_prompt must never read the vault per turn")

        def search_notes(self, *a, **k):
            raise AssertionError("build_system_prompt must never read the vault per turn")

    ctx = engine.ObsidianContext(mcp_client=ExplodingVault(), standing_digest="cached text")

    prompt = engine.build_system_prompt("America/New_York", obsidian=ctx)

    assert "cached text" in prompt


def test_the_prompt_is_byte_identical_across_turns(vault):
    """What the prompt cache actually requires: an exact prefix match."""
    ctx = engine.ObsidianContext(mcp_client=vault,
                                 standing_digest=standing_digest.build_digest(vault))

    first = engine.build_system_prompt("America/New_York", obsidian=ctx)
    vault.write_note("00-About Me", "Standing Preferences", "A change made mid-session.")
    second = engine.build_system_prompt("America/New_York", obsidian=ctx)

    assert first == second, "editing a note mid-session must not change the cached prefix"
    assert "A change made mid-session." not in second


def test_a_context_without_a_digest_still_builds_a_prompt(vault):
    """Every existing construction site passes only mcp_client."""
    prompt = engine.build_system_prompt(
        "America/New_York", obsidian=engine.ObsidianContext(mcp_client=vault))

    assert "Obsidian" in prompt


def test_setup_builds_the_digest_once_at_startup(tmp_path, vault):
    """The digest must be produced by build_obsidian_context, not lazily later."""
    from assistant.core import setup

    class Cfg:
        obsidian_vault_path = str(vault.vault_path)

    ctx = setup.build_obsidian_context(Cfg())

    assert ctx is not None
    assert "concise, limited responses" in ctx.standing_digest


# --- mail drafts use the voice he actually recorded ---------------------------

def test_mail_triage_drafts_against_his_recorded_voice(vault):
    """This module always asked for a draft 'in his voice' while showing the model nothing
    about what his voice is -- so it meant the model's default business register."""
    from assistant.core import mail_triage

    seen = {}

    class FakeLLM:
        def chat(self, messages, tools=None, think=False):
            seen["system"] = messages[0]["content"]
            return {"content": '{"needs_reply": false, "category": "x", "reasoning": "y",'
                               ' "draft_subject": "", "draft_body": ""}'}

    mail_triage.classify_and_draft(
        FakeLLM(), {"from": "a@b.com", "subject": "Hi", "body": "Question?"},
        voice=mail_triage.build_voice_note(vault))

    assert "concise, limited responses" in seen["system"]
    assert "what \"his voice\" actually means" in seen["system"]


def test_mail_triage_still_works_with_no_vault():
    from assistant.core import mail_triage

    assert mail_triage.build_voice_note(None) == ""


def test_the_notes_own_h1_is_not_repeated_under_the_heading(vault):
    """Same duplication as agent_policy's, and it costs system-prompt bytes on every turn."""
    text = standing_digest.build_digest(vault)

    assert "## Communication Preferences" in text
    assert text.count("Communication Preferences") == 1
