"""Verifies the confirmation gate for git ops: merging a PR actually changes what's on
main, so it must never reach the real GitOpsClient except through an explicit user
confirmation. Branch/write/push/PR-open are NOT gated, since none of them touch main or
anything deployed. Mirrors test_engine_kroger.py/test_engine_letterstream.py's suites.
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
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True}


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def make_git_ops():
    mcp = FakeGitOpsClient()
    sensitive = {"git_merge_pr"}
    return engine.GitOpsContext(mcp_client=mcp, git_tools=[
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("git_create_branch", "git_commit_and_push", "git_open_pr", "git_get_pr_status", "git_merge_pr")
    ], sensitive_tools=sensitive), mcp


def test_merge_pr_creates_pending_action_not_a_real_merge(db_path, owner_id):
    git_ops, mcp = make_git_ops()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_merge_pr", "arguments": {"pr_number": 42}}}
        ]},
        {"role": "assistant", "content": "That will merge PR #42 into main — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "merge my PR", git_ops=git_ops)

    assert mcp.calls == [], "a real merge must never happen before explicit confirmation"
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "git_merge_pr"
    assert "confirm" in reply.lower()


def test_confirming_executes_the_real_merge(db_path, owner_id):
    git_ops, mcp = make_git_ops()
    db.create_pending_action(db_path, owner_id, "git_merge_pr", {"pr_number": 42})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", git_ops=git_ops)

    assert mcp.calls == [("git_merge_pr", {"pr_number": 42})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_cancelling_never_merges(db_path, owner_id):
    git_ops, mcp = make_git_ops()
    db.create_pending_action(db_path, owner_id, "git_merge_pr", {"pr_number": 42})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "no", git_ops=git_ops)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is None
    assert "cancel" in reply.lower()


@pytest.mark.parametrize("tool_name,arguments", [
    ("git_create_branch", {"branch_name": "feature/x"}),
    ("git_commit_and_push", {"branch_name": "feature/x", "commit_message": "msg"}),
    ("git_open_pr", {"branch_name": "feature/x", "title": "t"}),
    ("git_get_pr_status", {"pr_number": 1}),
])
def test_non_merge_git_tools_are_not_gated(db_path, owner_id, tool_name, arguments):
    """Branch/write/push/PR-open are reversible and touch nothing deployed, so unlike
    merge they must execute directly -- gating them too would make basic dev work
    require a confirmation round-trip for something with no real consequence."""
    git_ops, mcp = make_git_ops()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": tool_name, "arguments": arguments}}
        ]},
        {"role": "assistant", "content": "Done."},
    ])

    engine.handle_message(db_path, llm, owner_id, "do the git thing", git_ops=git_ops)

    assert len(mcp.calls) == 1 and mcp.calls[0] == (tool_name, arguments)
    assert db.get_pending_action(db_path, owner_id) is None


def test_git_tools_absent_when_no_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "hi", git_ops=None)
    assert reply == "Hi there"


def test_a_pending_git_action_is_detected_with_no_other_context_configured(db_path, owner_id):
    """handle_message's early pending-action check must include git_ops, or a
    confirmation reply sent while no era/phone/mail/HA/kroger/ccxt/letterstream context
    exists would fall through to a normal turn instead of resolving the confirmation."""
    git_ops, mcp = make_git_ops()
    db.create_pending_action(db_path, owner_id, "git_merge_pr", {"pr_number": 7})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", git_ops=git_ops)

    assert mcp.calls == [("git_merge_pr", {"pr_number": 7})]
    assert "done" in reply.lower()


def test_select_tools_includes_git_tools_when_configured():
    git_ops, _ = make_git_ops()
    tools = engine.select_tools("anything", git_ops=git_ops, route=False)
    names = {t["function"]["name"] for t in tools}
    assert names.issuperset({"git_create_branch", "git_commit_and_push", "git_open_pr", "git_get_pr_status", "git_merge_pr"})


def test_select_tools_omits_git_tools_when_not_configured():
    tools = engine.select_tools("anything", git_ops=None, route=False)
    names = {t["function"]["name"] for t in tools}
    assert "git_merge_pr" not in names


def test_build_system_prompt_includes_git_note_when_configured():
    git_ops, _ = make_git_ops()
    prompt = engine.build_system_prompt("America/New_York", git_ops=git_ops)
    assert "git_merge_pr" in prompt


def test_build_system_prompt_omits_git_note_when_not_configured():
    prompt = engine.build_system_prompt("America/New_York", git_ops=None)
    assert "git_merge_pr" not in prompt


# --- unifying the decision surface: an opened PR lands on the Review page too ---

def test_opening_a_pr_creates_a_review_item(db_path, owner_id):
    """A PR sitting open with green CI used to be invisible as a decision until someone
    happened to ask Jarvis to check and merge it -- opening one now puts it in front of
    the owner the same way anything else the team produces does."""
    git_ops, mcp = make_git_ops()
    mcp.call_tool = lambda name, arguments: (
        {"ok": True, "pr_number": 4, "url": "https://github.com/x/y/pull/4", "state": "open"}
        if name == "git_open_pr" else {"ok": True}
    )
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_open_pr", "arguments": {
                "branch_name": "feature/x", "title": "Fix the thing", "body": "details"}}}
        ]},
        {"role": "assistant", "content": "Opened PR #4."},
    ])

    engine.handle_message(db_path, llm, owner_id, "open a PR", git_ops=git_ops)

    items = business_db.list_review_items(db_path, owner_id)
    assert len(items) == 1
    assert items[0]["ref_table"] == "git_pull_requests" and items[0]["ref_id"] == 4
    assert "Fix the thing" in items[0]["title"]


def test_a_failed_pr_open_creates_no_review_item(db_path, owner_id):
    git_ops, mcp = make_git_ops()  # default fake returns {"ok": True} with no pr_number
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "git_open_pr", "arguments": {"branch_name": "feature/x", "title": "t"}}}
        ]},
        {"role": "assistant", "content": "Done."},
    ])

    engine.handle_message(db_path, llm, owner_id, "open a PR", git_ops=git_ops)

    assert business_db.list_review_items(db_path, owner_id) == []
