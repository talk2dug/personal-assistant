"""Phase 5's auto-merge governance: git_merge_pr auto-merges only when CI is 100% green
AND the diff-scope check (git_ops.check_diff_scope) finds no concerns -- otherwise it
falls back to exactly today's manual pending_actions gate (test_engine_git_ops.py),
now with the specific reason attached. Fails closed throughout: anything ambiguous or
unavailable falls back to manual, never to an unattended merge on a guess.
"""
import pytest

from assistant.core import business_db, db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeGitOpsClient:
    def __init__(self, pr_status, pr_files=None, merge_result=None):
        self.calls = []
        self._pr_status = pr_status
        self._pr_files = pr_files if pr_files is not None else [
            {"filename": "assistant/core/thing.py", "status": "modified", "additions": 10, "deletions": 1},
        ]
        self._merge_result = merge_result if merge_result is not None else {"ok": True, "merged": True, "sha": "abc"}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True}

    def get_pr_status(self, pr_number):
        return self._pr_status

    def get_pr_files(self, pr_number):
        return self._pr_files

    def merge_pr(self, pr_number, merge_method="squash"):
        self.calls.append(("merge_pr_direct", {"pr_number": pr_number, "merge_method": merge_method}))
        return self._merge_result


class FakeLLM:
    """chat() drives handle_message's own tool loop; research() is check_diff_scope's
    one-shot judgment call."""

    def __init__(self, chat_responses, research_output='{"safe": true, "concerns": []}'):
        self._chat_responses = list(chat_responses)
        self.research_calls = []
        self._research_output = research_output

    def chat(self, messages, tools=None, think=False):
        return self._chat_responses.pop(0)

    def research(self, instructions, system_prompt=None, timeout=None):
        self.research_calls.append(instructions)
        return self._research_output


def make_git_ops(mcp):
    sensitive = {"git_merge_pr"}
    return engine.GitOpsContext(mcp_client=mcp, git_tools=[
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("git_create_branch", "git_commit_and_push", "git_open_pr", "git_get_pr_status", "git_merge_pr")
    ], sensitive_tools=sensitive)


def _green_status():
    return {"ok": True, "state": "open", "mergeable": True, "merged": False,
            "checks": [{"name": "backend", "status": "completed", "conclusion": "success"}]}


def _open_pr_review_item(db_path, owner_id, pr_number, title="Fix the thing", body="details"):
    """Auto-merge looks up the PR's own review item (created when it was opened) for a
    task description to judge scope against -- set that up directly, same as the real
    git_open_pr dispatch branch already does."""
    business_db.create_review_item(
        db_path, owner_id, title=f"PR #{pr_number}: {title}", kind="other",
        summary=f"feature/x -> main", detail=body, source_agent="git",
        ref_table="git_pull_requests", ref_id=pr_number,
    )


def test_auto_merges_when_ci_is_green_and_diff_scope_is_clean(db_path, owner_id):
    _open_pr_review_item(db_path, owner_id, 42)
    mcp = FakeGitOpsClient(pr_status=_green_status())
    git_ops = make_git_ops(mcp)
    llm = FakeLLM(chat_responses=[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "Merged it."},
    ], research_output='{"safe": true, "concerns": []}')

    reply = engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert ("merge_pr_direct", {"pr_number": 42, "merge_method": "squash"}) in mcp.calls
    assert db.get_pending_action(db_path, owner_id) is None, "a clean auto-merge needs no approval"
    assert llm.research_calls, "the diff-scope judgment was actually consulted"
    assert "Merged it" in reply


def test_auto_merge_marks_the_prs_review_item_approved_with_a_note(db_path, owner_id):
    _open_pr_review_item(db_path, owner_id, 42)
    mcp = FakeGitOpsClient(pr_status=_green_status())
    git_ops = make_git_ops(mcp)
    llm = FakeLLM(chat_responses=[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "Merged it."},
    ])

    engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    item = business_db.get_review_item_by_ref(db_path, owner_id, "git_pull_requests", 42)
    assert item["status"] == "approved"
    assert "Auto-merged" in item["decision_note"]


def test_falls_back_to_manual_when_ci_is_not_green(db_path, owner_id):
    _open_pr_review_item(db_path, owner_id, 42)
    red_status = {"ok": True, "state": "open", "mergeable": True, "merged": False,
                  "checks": [{"name": "backend", "status": "completed", "conclusion": "failure"}]}
    mcp = FakeGitOpsClient(pr_status=red_status)
    git_ops = make_git_ops(mcp)
    llm = FakeLLM(chat_responses=[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "Needs your OK."},
    ])

    engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert not any(c[0] == "merge_pr_direct" for c in mcp.calls), "must not merge with red CI"
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None and pending["tool_name"] == "git_merge_pr"
    assert not llm.research_calls, "no point judging scope when CI already fails the gate"

    item = business_db.get_review_item_by_ref(db_path, owner_id, "pending_actions", pending["id"])
    assert "not 100% green" in item["summary"] or "not 100% green" in item["detail"]


def test_falls_back_to_manual_when_diff_scope_raises_a_concern(db_path, owner_id):
    _open_pr_review_item(db_path, owner_id, 42)
    mcp = FakeGitOpsClient(
        pr_status=_green_status(),
        pr_files=[{"filename": "assistant/core/junk_filter.py", "status": "modified",
                   "additions": 5, "deletions": 120}],
    )
    git_ops = make_git_ops(mcp)
    llm = FakeLLM(chat_responses=[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "Needs your OK."},
    ], research_output='{"safe": true, "concerns": []}')  # even if the model misses it

    engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert not any(c[0] == "merge_pr_direct" for c in mcp.calls)
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    item = business_db.get_review_item_by_ref(db_path, owner_id, "pending_actions", pending["id"])
    assert "deletions" in item["detail"]


def test_falls_back_to_manual_when_the_llm_flags_a_concern(db_path, owner_id):
    _open_pr_review_item(db_path, owner_id, 42)
    mcp = FakeGitOpsClient(pr_status=_green_status())
    git_ops = make_git_ops(mcp)
    llm = FakeLLM(chat_responses=[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "Needs your OK."},
    ], research_output='{"safe": false, "concerns": ["touches an unrelated auth module"]}')

    engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert not any(c[0] == "merge_pr_direct" for c in mcp.calls)
    pending = db.get_pending_action(db_path, owner_id)
    item = business_db.get_review_item_by_ref(db_path, owner_id, "pending_actions", pending["id"])
    assert "unrelated auth module" in item["detail"]


def test_falls_back_to_manual_when_no_task_description_is_on_record(db_path, owner_id):
    """No Review item means no PR-open record to judge scope against -- fails closed
    rather than guessing at what the PR was supposed to do."""
    mcp = FakeGitOpsClient(pr_status=_green_status())
    git_ops = make_git_ops(mcp)
    llm = FakeLLM(chat_responses=[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 999}}}
        ]},
        {"role": "assistant", "content": "Needs your OK."},
    ])

    engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert not any(c[0] == "merge_pr_direct" for c in mcp.calls)
    assert db.get_pending_action(db_path, owner_id) is not None
    assert not llm.research_calls


def test_falls_back_to_manual_when_llm_backend_has_no_research(db_path, owner_id):
    class NoResearchLLM:
        def __init__(self, responses):
            self._responses = list(responses)

        def chat(self, messages, tools=None, think=False):
            return self._responses.pop(0)

    _open_pr_review_item(db_path, owner_id, 42)
    mcp = FakeGitOpsClient(pr_status=_green_status())
    git_ops = make_git_ops(mcp)
    llm = NoResearchLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "Needs your OK."},
    ])

    engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert not any(c[0] == "merge_pr_direct" for c in mcp.calls)
    assert db.get_pending_action(db_path, owner_id) is not None


def test_falls_back_to_manual_when_pr_status_lookup_fails(db_path, owner_id):
    _open_pr_review_item(db_path, owner_id, 42)
    mcp = FakeGitOpsClient(pr_status={"ok": False, "error": "not found"})
    git_ops = make_git_ops(mcp)
    llm = FakeLLM(chat_responses=[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "Needs your OK."},
    ])

    engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert not any(c[0] == "merge_pr_direct" for c in mcp.calls)
    assert db.get_pending_action(db_path, owner_id) is not None
