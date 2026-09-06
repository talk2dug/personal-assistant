"""Covers request_capability -- the one tool every employee has regardless of tier, so
that a job needing a capability it doesn't have gets an explicit ask instead of a
guessed or stubbed-around workaround. A real incident motivated this: an employee
assigned real development work had no way to even see the actual repo, so it invented a
tech stack from nothing rather than saying it was blocked.
"""
import json

import pytest

from assistant.core import business_db, db, staff
from assistant.core.engine import _dispatch_tool_call


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    staff.init_staff_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def test_request_capability_creates_a_review_item(db_path, owner_id):
    key = staff.hire(db_path, "Research Analyst", "Tracks industry trends.")["key"]

    result = json.loads(_dispatch_tool_call(
        db_path, "America/New_York", owner_id, "request_capability",
        {"requested_tier": "execute", "reason": "needs to open a PR"},
        None, None, employee_key=key,
    ))

    assert result["ok"] is True
    items = business_db.list_review_items(db_path, owner_id)
    assert len(items) == 1
    item = items[0]
    assert item["ref_table"] == "capability_requests"
    assert "execute" in item["title"]
    payload = json.loads(item["detail"])
    assert payload == {"employee_key": key, "requested_tier": "execute", "reason": "needs to open a PR"}


def test_approving_the_request_grants_the_tier(db_path, owner_id):
    from assistant.core.business_tools import apply_review_decision

    key = staff.hire(db_path, "Research Analyst", "Tracks industry trends.")["key"]
    _dispatch_tool_call(
        db_path, "America/New_York", owner_id, "request_capability",
        {"requested_tier": "execute", "reason": "needs to open a PR"},
        None, None, employee_key=key,
    )
    item = business_db.list_review_items(db_path, owner_id)[0]
    decided = business_db.decide_review_item(db_path, owner_id, item["id"], "approved")

    apply_review_decision(db_path, owner_id, decided, "approved")

    assert staff.get_staff(db_path, key)["capability_tier"] == "execute"


def test_rejecting_the_request_grants_nothing(db_path, owner_id):
    from assistant.core.business_tools import apply_review_decision

    key = staff.hire(db_path, "Research Analyst", "Tracks industry trends.")["key"]
    _dispatch_tool_call(
        db_path, "America/New_York", owner_id, "request_capability",
        {"requested_tier": "execute", "reason": "needs to open a PR"},
        None, None, employee_key=key,
    )
    item = business_db.list_review_items(db_path, owner_id)[0]
    decided = business_db.decide_review_item(db_path, owner_id, item["id"], "rejected")

    apply_review_decision(db_path, owner_id, decided, "rejected")

    assert staff.get_staff(db_path, key)["capability_tier"] == "research"


def test_assign_gives_every_tier_the_request_capability_tool(db_path):
    """Even the otherwise tool-less research tier gets exactly this one tool -- it can't
    act on anything by itself (it only ever files a review item), so this doesn't loosen
    'employees produce, they do not act'."""
    from assistant.core.business_tools import REQUEST_CAPABILITY_TOOLS

    research_key = staff.hire(db_path, "Copywriter", "Writes marketing copy.")["key"]
    execute_key = staff.hire(db_path, "Systems Engineer", "Runs infrastructure.")["key"]
    staff.set_capability_override(db_path, execute_key, "execute")

    class RecordingLLM:
        def __init__(self):
            self.research_kwargs = None
            self.engineer_kwargs = None

        def research(self, prompt, system_prompt=None, timeout=None, tools=None, employee_key=None):
            self.research_kwargs = {"tools": tools, "employee_key": employee_key}
            return "done"

        def engineer(self, prompt, system_prompt=None, tools=None, timeout=None, employee_key=None):
            self.engineer_kwargs = {"tools": tools, "employee_key": employee_key}
            return "done"

    llm = RecordingLLM()
    staff.assign(db_path, llm, research_key, "write something")
    staff.assign(db_path, llm, execute_key, "ship something")

    assert llm.research_kwargs["employee_key"] == research_key
    assert llm.research_kwargs["tools"] == REQUEST_CAPABILITY_TOOLS
    assert llm.engineer_kwargs["employee_key"] == execute_key
    request_tool_names = {t["function"]["name"] for t in REQUEST_CAPABILITY_TOOLS}
    engineer_tool_names = {t["function"]["name"] for t in llm.engineer_kwargs["tools"]}
    assert request_tool_names <= engineer_tool_names
