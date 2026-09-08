"""git_ops.check_diff_scope -- the automated substitute for what manual PR review has
actually been catching (see the module's own comment for the three real incidents this
targets). Tested against a fake GitOpsClient (get_pr_files) and a fake llm.research, so
these check the combining/fail-closed logic without a real GitHub or model call --
live verification against a real historical PR is done separately, not as a unit test.
"""
from assistant.core.git_ops import (
    _heuristic_concerns, _parse_scope_verdict, check_diff_scope,
)


class FakeGitOpsClient:
    def __init__(self, files):
        self._files = files

    def get_pr_files(self, pr_number):
        return self._files


class FakeLLM:
    def __init__(self, verdict_line):
        self.calls = []
        self._verdict_line = verdict_line

    def research(self, instructions, system_prompt=None, timeout=None):
        self.calls.append(instructions)
        return self._verdict_line


def _file(name, status="modified", additions=5, deletions=1):
    return {"filename": name, "status": status, "additions": additions, "deletions": deletions,
            "changes": additions + deletions}


# --- _heuristic_concerns -------------------------------------------------------

def test_heuristic_flags_a_deleted_file():
    concerns = _heuristic_concerns([_file("assistant/core/junk_filter.py", status="removed", deletions=80)])
    assert len(concerns) == 1
    assert "deleted entirely" in concerns[0]


def test_heuristic_flags_large_net_deletion():
    concerns = _heuristic_concerns([_file("assistant/core/config.py", additions=18, deletions=42)])
    assert len(concerns) == 1
    assert "42 deletions" in concerns[0]


def test_heuristic_does_not_flag_a_normal_addition():
    concerns = _heuristic_concerns([_file("assistant/core/thing.py", additions=40, deletions=3)])
    assert concerns == []


def test_heuristic_does_not_flag_a_small_deletion():
    concerns = _heuristic_concerns([_file("assistant/core/thing.py", additions=5, deletions=8)])
    assert concerns == []


# --- _parse_scope_verdict --------------------------------------------------------

def test_parse_scope_verdict_reads_trailing_json():
    result = _parse_scope_verdict('Looks fine.\n{"safe": true, "concerns": []}')
    assert result == {"safe": True, "concerns": []}


def test_parse_scope_verdict_tolerates_a_code_fence():
    result = _parse_scope_verdict('```\n{"safe": false, "concerns": ["unrelated file touched"]}\n```')
    assert result == {"safe": False, "concerns": ["unrelated file touched"]}


def test_parse_scope_verdict_fails_closed_on_no_output():
    result = _parse_scope_verdict(None)
    assert result["safe"] is False


def test_parse_scope_verdict_fails_closed_on_unparseable_output():
    result = _parse_scope_verdict("I'm not sure how to answer that.")
    assert result["safe"] is False


# --- check_diff_scope (combining both signals) -----------------------------------

def test_check_diff_scope_passes_a_clean_pr():
    git_ops = FakeGitOpsClient([_file("assistant/core/git_tools.py", additions=30, deletions=1)])
    llm = FakeLLM('{"safe": true, "concerns": []}')

    result = check_diff_scope(git_ops, llm, 6, "Add git_list_files/git_read_file tools")

    assert result["safe"] is True
    assert result["concerns"] == []
    assert llm.calls  # the LLM was actually consulted, not skipped


def test_check_diff_scope_flags_a_heuristic_concern_even_if_the_llm_says_safe():
    """Either signal raising a concern is enough -- a model that misses an obvious large
    deletion must not override the cheap, model-free heuristic that already caught it."""
    git_ops = FakeGitOpsClient([_file("assistant/core/config.py", additions=18, deletions=42)])
    llm = FakeLLM('{"safe": true, "concerns": []}')

    result = check_diff_scope(git_ops, llm, 12, "Add a new config field")

    assert result["safe"] is False
    assert any("42 deletions" in c for c in result["concerns"])


def test_check_diff_scope_flags_an_llm_concern_even_with_no_heuristic_hit():
    git_ops = FakeGitOpsClient([_file("assistant/core/mail_client.py", additions=15, deletions=10)])
    llm = FakeLLM('{"safe": false, "concerns": ["mail_client.py was rewritten with a simpler API"]}')

    result = check_diff_scope(git_ops, llm, 14, "Restore the lost junk-filter files")

    assert result["safe"] is False
    assert "mail_client.py was rewritten with a simpler API" in result["concerns"]


def test_check_diff_scope_fails_closed_when_no_files_can_be_fetched():
    git_ops = FakeGitOpsClient([])
    llm = FakeLLM('{"safe": true, "concerns": []}')

    result = check_diff_scope(git_ops, llm, 999, "Some task")

    assert result["safe"] is False
    assert not llm.calls  # nothing to judge, so the model is never even consulted


def test_check_diff_scope_fails_closed_when_the_llm_call_raises():
    class BrokenLLM:
        def research(self, *a, **k):
            raise RuntimeError("backend unavailable")

    git_ops = FakeGitOpsClient([_file("assistant/core/thing.py")])
    result = check_diff_scope(git_ops, BrokenLLM(), 1, "Some task")

    assert result["safe"] is False
    assert any("scope judgment call failed" in c for c in result["concerns"])
