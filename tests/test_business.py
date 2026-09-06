"""Covers the business/second-in-command layer: storage, chat tools, and the background
agents.

The agent tests lean hard on one property: an agent that cannot verify something must
record nothing rather than invent it. The predecessor project's market-finder fed a real
person real event listings, and a hallucinated craft fair means a wasted Saturday and a
non-refundable booth fee.
"""
import json

import pytest

from assistant.config import BusinessProfile
from assistant.core import agents, business_db, ops_plans, staff
from assistant.core.business_tools import BUSINESS_TOOLS, BusinessClient
from assistant.core.engine import BusinessContext, _dispatch_tool_call, build_system_prompt, select_tools

PROFILE = BusinessProfile(
    name="Blue Ridge Custom Co", location="Richmond, VA", radius_miles=60,
    product_lines=["Vinyl stickers", "DTF apparel", "Metal prints"],
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "business.db")
    business_db.init_business_db(path)
    return path


@pytest.fixture
def client(db_path):
    return BusinessClient(db_path, owner_user_id=1, profile=PROFILE)


class FakeResearchLLM:
    """Stands in for ClaudeCLIClient — records prompts, returns canned output."""

    def __init__(self, response=""):
        self.response = response
        self.prompts = []

    def research(self, instructions, system_prompt=None, timeout=None):
        self.prompts.append(instructions)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# --- storage -----------------------------------------------------------------

def test_projects_and_tasks_round_trip(db_path):
    pid = business_db.create_project(db_path, 1, "Richmond relaunch", goal="First market booked")
    business_db.create_task(db_path, 1, "Find a booth", project_id=pid, priority="high")
    business_db.create_task(db_path, 1, "Reprint sticker stock", project_id=pid)

    tasks = business_db.list_tasks(db_path, 1, status="open")
    assert [t["text"] for t in tasks] == ["Find a booth", "Reprint sticker stock"]  # high priority first
    assert tasks[0]["project_name"] == "Richmond relaunch"

    assert business_db.update_task(db_path, 1, tasks[0]["id"], status="done")
    assert len(business_db.list_tasks(db_path, 1, status="open")) == 1


def test_market_leads_dedupe_and_preserve_user_status(db_path):
    """Re-scanning must refresh details without wiping a status the owner set, and must
    report the lead as not-new so the digest doesn't re-announce it."""
    assert business_db.upsert_market_lead(
        db_path, 1, "Maker Fest", "2026-10-04", "Richmond", "http://x", "$75", 85, "Good fit") is True

    lead = business_db.list_market_leads(db_path, 1)[0]
    business_db.set_market_lead_status(db_path, 1, lead["id"], "applied")

    assert business_db.upsert_market_lead(
        db_path, 1, "Maker Fest", "2026-10-04", "Richmond", "http://y", "$80", 90, "Even better") is False

    refreshed = business_db.list_market_leads(db_path, 1)[0]
    assert refreshed["status"] == "applied"   # user's decision survives
    assert refreshed["fit_score"] == 90       # details refreshed
    assert refreshed["url"] == "http://y"


def test_reworded_market_names_are_treated_as_the_same_event(db_path):
    """Caught live on the first real run: one scan produced both "Richmond Makers Market:
    Spooktoberfest" and "Richmond Makers Market — Spooktoberfest" for a single market,
    because the model reworded a title it read off two pages."""
    assert business_db.upsert_market_lead(
        db_path, 1, "Richmond Makers Market: Spooktoberfest", "2026-10-04", "RVA", None, "$70", 82, "fit") is True
    assert business_db.upsert_market_lead(
        db_path, 1, "Richmond Makers Market — Spooktoberfest", "2026-10-04", "RVA", None, "$70", 80, "fit") is False
    assert len(business_db.list_market_leads(db_path, 1)) == 1

    # A genuinely different event on the same day is still its own lead.
    assert business_db.upsert_market_lead(
        db_path, 1, "Richmond Night Market", "2026-10-04", "RVA", None, None, 65, "fit") is True
    assert len(business_db.list_market_leads(db_path, 1)) == 2


def test_reworded_trend_topics_dedupe_too(db_path):
    assert business_db.upsert_trend_lead(db_path, 1, "Cottagecore Mushrooms", "reddit", 70, "stickers", "x") is True
    assert business_db.upsert_trend_lead(db_path, 1, "cottagecore mushrooms!", "google", 75, "shirts", "y") is False
    assert len(business_db.list_trend_leads(db_path, 1)) == 1


def test_expenses_total_respects_since(db_path):
    business_db.add_expense(db_path, 1, "Vinyl roll", 120.0, "2026-08-01")
    business_db.add_expense(db_path, 1, "Booth fee", 75.0, "2026-09-02")
    assert business_db.expense_total(db_path, 1) == pytest.approx(195.0)
    assert business_db.expense_total(db_path, 1, since="2026-09-01") == pytest.approx(75.0)


def test_inventory_upsert_and_low_stock(db_path):
    business_db.upsert_inventory(db_path, 1, "White vinyl", 4, unit="rolls", reorder_at=2)
    business_db.upsert_inventory(db_path, 1, "Transfer tape", 1, unit="rolls", reorder_at=2)
    assert [i["item"] for i in business_db.list_inventory(db_path, 1, low_only=True)] == ["Transfer tape"]

    business_db.upsert_inventory(db_path, 1, "White vinyl", 1)
    low = {i["item"] for i in business_db.list_inventory(db_path, 1, low_only=True)}
    assert low == {"White vinyl", "Transfer tape"}
    # A quantity-only update must not blank the unit/reorder threshold.
    vinyl = next(i for i in business_db.list_inventory(db_path, 1) if i["item"] == "White vinyl")
    assert vinyl["unit"] == "rolls" and vinyl["reorder_at"] == 2


# --- chat tools --------------------------------------------------------------

def test_business_status_gathers_everything(client, db_path):
    business_db.create_project(db_path, 1, "Relaunch")
    business_db.create_task(db_path, 1, "Book a market")
    business_db.upsert_market_lead(db_path, 1, "Maker Fest", "2026-10-04", "Richmond", None, None, 80, "fit")
    business_db.add_expense(db_path, 1, "Vinyl", 50.0, "2026-09-03")

    status = client.call_tool("business_status", {})
    assert len(status["projects"]) == 1
    assert len(status["open_tasks"]) == 1
    assert len(status["new_market_leads"]) == 1
    assert status["spend_this_month"] >= 0


def test_request_research_queues_and_says_so(client, db_path):
    result = client.call_tool("request_research", {"topic": "DTF printers", "question": "Best under $5k?"})
    assert result["ok"]
    # The tool result must actively stop the model claiming it already read something.
    assert "do not have findings yet" in result["message"]
    assert [r["topic"] for r in business_db.pending_research(db_path)] == ["DTF printers"]


def test_run_agent_without_a_web_backend_is_refused_not_faked(db_path):
    class NoWebLLM:
        pass

    client = BusinessClient(db_path, 1, llm=NoWebLLM(), profile=PROFILE)
    result = client.call_tool("run_business_agent", {"agent": "market_finder"})
    assert "cannot search the web" in result["error"]


def test_unknown_tool_is_reported(client):
    assert "error" in client.call_tool("definitely_not_a_tool", {})


# --- agents ------------------------------------------------------------------

def test_extract_json_handles_fenced_and_prose_wrapped_output():
    assert agents._extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert agents._extract_json('Here you go:\n[{"a": 2}]\nHope that helps') == [{"a": 2}]
    assert agents._extract_json("no json here at all") is None


def test_market_agent_stores_scored_leads(db_path):
    llm = FakeResearchLLM(json.dumps([
        {"name": "Richmond Maker Market", "event_date": "2026-10-11", "location": "Richmond, VA",
         "url": "http://example.com", "cost": "$60", "fit_score": 88, "reasoning": "Handmade vendors welcome"},
        {"name": "Farmers Produce Only", "event_date": "2026-10-12", "location": "Richmond, VA",
         "url": None, "cost": None, "fit_score": 10, "reasoning": "Agricultural only"},
    ]))
    result = agents.run_market_agent(db_path, llm, 1, PROFILE)

    assert result["status"] == "ok" and result["new"] == 1
    leads = business_db.list_market_leads(db_path, 1)
    # The poor-fit event is filtered out by min_fit_score, not stored and ignored.
    assert [lead["name"] for lead in leads] == ["Richmond Maker Market"]
    assert "Richmond, VA" in llm.prompts[0] and "60 miles" in llm.prompts[0]


def test_market_agent_records_a_failure_rather_than_inventing_leads(db_path):
    llm = FakeResearchLLM("I could not find any events, sorry.")
    result = agents.run_market_agent(db_path, llm, 1, PROFILE)

    assert result["status"] == "error"
    assert business_db.list_market_leads(db_path, 1) == []
    run = business_db.recent_agent_runs(db_path)[0]
    assert run["status"] == "error" and run["agent"] == "market_finder"


def test_agent_exception_is_journalled_not_swallowed(db_path):
    llm = FakeResearchLLM(RuntimeError("usage limit reached"))
    result = agents.run_market_agent(db_path, llm, 1, PROFILE)
    assert result["status"] == "error"
    assert "usage limit reached" in business_db.recent_agent_runs(db_path)[0]["summary"]


def test_research_queue_completes_items_and_marks_failures(db_path):
    business_db.create_research(db_path, 1, "Booth insurance")
    llm = FakeResearchLLM("You need general liability cover; typically $200-400/yr. Sources: ...")
    result = agents.run_research_queue(db_path, llm, PROFILE)

    assert result["done"] == 1
    done = business_db.list_research(db_path, 1)[0]
    assert done["status"] == "done" and "general liability" in done["findings"]
    assert business_db.pending_research(db_path) == []


def test_digest_is_silent_when_there_is_nothing_to_say(db_path):
    """A briefing that fires every morning saying 'nothing happened' teaches you to
    ignore it."""
    assert agents.build_digest(db_path, 1) is None


def test_digest_reports_new_findings(db_path):
    business_db.upsert_market_lead(db_path, 1, "Maker Fest", "2026-10-04", "Richmond", None, "$75", 85, "Strong fit")
    business_db.upsert_trend_lead(db_path, 1, "Cottagecore mushrooms", "reddit", 72, "Sticker pack", "Rising")
    business_db.upsert_inventory(db_path, 1, "Transfer tape", 0, unit="rolls", reorder_at=2)

    digest = agents.build_digest(db_path, 1)
    assert "Maker Fest" in digest
    assert "Cottagecore mushrooms" in digest
    assert "Transfer tape" in digest


# --- the creative pipeline ---------------------------------------------------

def test_product_creator_turns_trends_into_concepts(db_path):
    business_db.upsert_trend_lead(db_path, 1, "RVA local pride", "reddit", 78, "skyline decal", "rising")
    llm = FakeResearchLLM(json.dumps([
        {"name": "RVA Skyline Die-Cut Decal", "product_type": "sticker",
         "description": "4in matte white vinyl", "target_customer": "Richmond locals",
         "price_estimate": 6.0, "production_notes": "Cut on the vinyl cutter",
         "trend_topic": "RVA local pride"},
    ]))
    result = agents.run_product_creator(db_path, llm, 1, PROFILE)

    assert result["new"] == 1
    concept = business_db.list_product_concepts(db_path, 1)[0]
    assert concept["name"] == "RVA Skyline Die-Cut Decal"
    assert concept["status"] == "proposed"     # never auto-approved
    assert concept["trend_lead_id"] is not None  # traceable back to its signal


def test_downstream_agents_wait_for_owner_approval(db_path):
    """The gate that makes this safe: an unapproved concept is invisible to the Art
    Director and E-Store Manager."""
    business_db.create_product_concept(db_path, 1, "Untested Idea", "sticker", "desc")
    llm = FakeResearchLLM(json.dumps({"style_direction": "s", "image_prompt": "p", "aspect": "1:1"}))

    assert agents.run_art_director(db_path, llm, 1, PROFILE)["status"] == "skipped"
    assert agents.run_store_manager(db_path, llm, 1, PROFILE)["status"] == "skipped"
    assert business_db.list_art_briefs(db_path, 1) == []

    concept_id = business_db.list_product_concepts(db_path, 1)[0]["id"]
    business_db.set_concept_status(db_path, 1, concept_id, "approved")
    assert agents.run_art_director(db_path, llm, 1, PROFILE)["new"] == 1


def test_art_director_uses_medium_specific_direction(db_path):
    """A metal print and an apparel graphic need genuinely different prompts — this was
    the hard-won part of the predecessor project's prompt templates."""
    business_db.create_product_concept(db_path, 1, "Rally Car Print", "metal", "aluminium print")
    business_db.set_concept_status(db_path, 1, business_db.list_product_concepts(db_path, 1)[0]["id"], "approved")

    llm = FakeResearchLLM(json.dumps(
        {"style_direction": "dramatic", "image_prompt": "car on black", "aspect": "3:2", "notes": "n"}))
    agents.run_art_director(db_path, llm, 1, PROFILE)

    assert "pure black background" in llm.prompts[0]  # metal guidance, not sticker guidance
    brief = business_db.list_art_briefs(db_path, 1)[0]
    assert brief["negative_prompt"] and "deformed" in brief["negative_prompt"]
    assert brief["status"] == "draft"


def test_store_manager_then_social_director_chain(db_path):
    business_db.create_product_concept(db_path, 1, "RVA Decal", "sticker", "4in vinyl")
    concept_id = business_db.list_product_concepts(db_path, 1)[0]["id"]
    business_db.set_concept_status(db_path, 1, concept_id, "approved")

    store_llm = FakeResearchLLM(json.dumps(
        {"title": "RVA Skyline Vinyl Decal", "description": "d", "seo_tags": "rva,decal", "price": 6.0}))
    assert agents.run_store_manager(db_path, store_llm, 1, PROFILE)["new"] == 1

    listing = business_db.list_store_listings(db_path, 1)[0]
    assert listing["status"] == "draft"
    # A draft listing isn't announced — social only picks up what the owner approved.
    social_llm = FakeResearchLLM(json.dumps([
        {"platform": "instagram", "hook": "h", "caption": "c", "hashtags": "#rva", "call_to_action": "cta"},
    ]))
    assert agents.run_social_director(db_path, social_llm, 1, PROFILE)["status"] == "skipped"

    business_db.update_store_listing(db_path, 1, listing["id"], status="approved")
    assert agents.run_social_director(db_path, social_llm, 1, PROFILE)["new"] == 1
    post = business_db.list_social_posts(db_path, 1)[0]
    assert post["status"] == "draft" and post["platform"] == "instagram"


def test_concepts_dedupe_on_rewording(db_path):
    _, first = business_db.create_product_concept(db_path, 1, "Blue Ridge Trucker Hat", "apparel")
    _, second = business_db.create_product_concept(db_path, 1, "Blue Ridge trucker hat!", "apparel")
    assert first is True and second is False
    assert len(business_db.list_product_concepts(db_path, 1)) == 1


def test_digest_surfaces_pipeline_items_awaiting_a_decision(db_path):
    business_db.create_product_concept(db_path, 1, "RVA Decal", "sticker", "d")
    business_db.create_store_listing(db_path, 1, "RVA Skyline Decal", price=6.0)
    business_db.create_social_post(db_path, 1, "instagram", caption="c")

    digest = agents.build_digest(db_path, 1)
    assert "RVA Decal" in digest
    assert "RVA Skyline Decal" in digest
    assert "awaiting approval" in digest


def test_all_four_new_agents_are_runnable_from_chat(db_path):
    client = BusinessClient(db_path, 1, llm=FakeResearchLLM("[]"), profile=PROFILE)
    for agent in ("product_creator", "art_director", "store_manager", "social_director"):
        result = client.call_tool("run_business_agent", {"agent": agent})
        assert result["status"] == "started", agent
        assert "do NOT have its results yet" in result["message"]


# --- engine wiring -----------------------------------------------------------

def test_business_tools_reach_the_dispatcher(db_path):
    context = BusinessContext(mcp_client=BusinessClient(db_path, 1, profile=PROFILE), profile=PROFILE)
    result = _dispatch_tool_call(
        db_path, "America/New_York", 1, "create_task", {"text": "Order blanks"},
        era=None, calendar=None, business=context,
    )
    assert json.loads(result)["ok"] is True
    assert business_db.list_tasks(db_path, 1)[0]["text"] == "Order blanks"


# --- set_employee_capability ---------------------------------------------------

def test_set_employee_capability_grants_execute_tier(db_path):
    staff.init_staff_db(db_path)
    staff.hire(db_path, "Systems Engineer", "Runs our infrastructure and deploy pipeline.")
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE)

    result = client.call_tool("set_employee_capability", {"employee": "systems_engineer", "tier": "execute"})

    assert result["ok"] is True
    assert result["capability_tier"] == "execute"
    assert staff.get_staff(db_path, "systems_engineer")["capability_tier"] == "execute"


def test_set_employee_capability_rejects_an_unknown_tier(db_path):
    staff.init_staff_db(db_path)
    staff.hire(db_path, "Systems Engineer", "Runs our infrastructure.")
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE)

    result = client.call_tool("set_employee_capability", {"employee": "systems_engineer", "tier": "root"})

    assert result["ok"] is False
    assert "unknown capability tier" in result["error"]


def test_set_employee_capability_reports_a_missing_employee(db_path):
    staff.init_staff_db(db_path)
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE)

    result = client.call_tool("set_employee_capability", {"employee": "nobody", "tier": "execute"})

    assert result["ok"] is False
    assert "nobody" in result["error"]


def test_business_tools_are_offered_on_every_turn(db_path):
    """Not keyword-gated, for the same reason Obsidian's aren't: capturing what the owner
    says he's going to do only works if the tools are always there."""
    context = BusinessContext(mcp_client=object(), profile=PROFILE)
    names = [t["function"]["name"] for t in select_tools("morning", business=context, route=True)]
    for tool in BUSINESS_TOOLS:
        assert tool["function"]["name"] in names


def test_system_note_names_the_business_and_only_when_configured():
    context = BusinessContext(mcp_client=object(), profile=PROFILE)
    assert "second in command" not in build_system_prompt("America/New_York")
    prompt = build_system_prompt("America/New_York", business=context)
    assert "Blue Ridge Custom Co" in prompt and "Richmond, VA" in prompt


def test_prompt_tells_jarvis_agents_are_on_demand_when_unscheduled():
    """Without this, Jarvis promises "the market agent will pick that up overnight" for a
    job that will never fire."""
    on_demand = build_system_prompt(
        "America/New_York", business=BusinessContext(object(), PROFILE, agents_scheduled=False))
    assert "ONLY when asked" in on_demand
    assert "never say an agent will pick something up later" in on_demand

    scheduled = build_system_prompt(
        "America/New_York", business=BusinessContext(object(), PROFILE, agents_scheduled=True))
    assert "run on their own schedule" in scheduled
    assert "ONLY when asked" not in scheduled


def test_scheduler_registers_no_agent_jobs_when_disabled(tmp_path):
    """The master switch has to actually stop the timers, not just change what Jarvis says."""
    from assistant.core import db, scheduler

    path = str(tmp_path / "sched.db")
    db.init_db(path)
    business_db.init_business_db(path)
    db.upsert_user(path, "111", "Dug", "owner")

    class WebLLM:
        def research(self, *a, **k):
            raise AssertionError("no agent should run")

    context = BusinessContext(mcp_client=object(), profile=PROFILE)
    started = scheduler.start(
        path, notify=lambda *a: None, poll_interval_seconds=3600,
        business=context, llm=WebLLM(), business_agents_enabled=False,
    )
    try:
        job_ids = {j.id for j in started.get_jobs()}
        for agent_job in ("market_agent", "trend_agent", "research_agent", "creative_pipeline",
                          "business_digest"):
            assert agent_job not in job_ids, f"{agent_job} was scheduled while agents are off"
        assert "reminder_poll" in job_ids  # ordinary assistant work is untouched
    finally:
        started.shutdown(wait=False)


# --- ops-plan workflow ----------------------------------------------------------

class FakeSSHOps:
    def __init__(self, hosts=("simrig", "touch1")):
        self._hosts = list(hosts)
        self.run_calls = []

    def list_hosts(self):
        return self._hosts

    def run_command(self, host, command, timeout=120):
        self.run_calls.append((host, command))
        return {"ok": True, "host": host, "command": command, "exit_code": 0, "output": "ok"}


def _plan_steps():
    return [
        {"phase": "change", "host": "simrig", "command": "do the thing", "purpose": "make the change"},
        {"phase": "verify", "host": "simrig", "command": "check the thing", "purpose": "confirm it worked"},
        {"phase": "rollback", "host": "simrig", "command": "undo the thing", "purpose": "revert if needed"},
    ]


def test_list_ssh_hosts_returns_the_real_registry(db_path):
    """The only reliable way Jarvis can see what hosts actually exist -- without this,
    the model can only ever learn a host's name by coincidence, from a past plan that
    happened to target it."""
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE,
                             ssh_ops=FakeSSHOps(hosts=("simrig", "touch1", "jarvisaudio1")))
    result = client.call_tool("list_ssh_hosts", {})
    assert result["hosts"] == ["jarvisaudio1", "simrig", "touch1"]


def test_list_ssh_hosts_with_no_ssh_configured_returns_empty(db_path):
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=None)
    assert client.call_tool("list_ssh_hosts", {}) == {"hosts": []}


def test_propose_ops_plan_creates_a_plan_and_a_linked_review_item(db_path):
    ops_plans.init_ops_plans_db(db_path)
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=FakeSSHOps())

    result = client.call_tool("propose_ops_plan", {"summary": "Fix the thing", "steps": _plan_steps()})

    assert result["ok"] is True
    plan = ops_plans.get_plan(db_path, result["plan_id"])
    assert plan["status"] == "proposed"
    assert plan["review_item_id"] == result["review_item_id"]
    review_item = business_db.list_review_items(db_path, 1, status="pending")[0]
    assert review_item["id"] == result["review_item_id"]
    assert review_item["ref_table"] == "ops_plans" and review_item["ref_id"] == result["plan_id"]
    assert "What will be done" in review_item["detail"]
    assert "Plan of attack" in review_item["detail"]


def test_propose_ops_plan_rejects_an_unregistered_host(db_path):
    ops_plans.init_ops_plans_db(db_path)
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=FakeSSHOps(hosts=["simrig"]))

    steps = _plan_steps()
    steps[0]["host"] = "some-random-box"
    result = client.call_tool("propose_ops_plan", {"summary": "Bad host", "steps": steps})

    assert result["ok"] is False
    assert "some-random-box" in result["error"]
    assert ops_plans.list_plans(db_path, 1) == []  # nothing was created


def test_propose_ops_plan_rejects_a_plan_with_no_rollback_step(db_path):
    ops_plans.init_ops_plans_db(db_path)
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=FakeSSHOps())

    steps = [s for s in _plan_steps() if s["phase"] != "rollback"]
    result = client.call_tool("propose_ops_plan", {"summary": "No rollback", "steps": steps})

    assert result["ok"] is False
    assert "rollback" in result["error"]


def test_propose_ops_plan_works_without_ssh_ops_configured(db_path):
    """No host-validation possible without a registry, but proposing (and the owner
    reviewing) must not require ssh_ops to already be wired up."""
    ops_plans.init_ops_plans_db(db_path)
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=None)

    result = client.call_tool("propose_ops_plan", {"summary": "No ssh yet", "steps": _plan_steps()})

    assert result["ok"] is True


def test_list_and_get_ops_plan_status(db_path):
    ops_plans.init_ops_plans_db(db_path)
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=FakeSSHOps())
    created = client.call_tool("propose_ops_plan", {"summary": "Check on this", "steps": _plan_steps()})

    listed = client.call_tool("list_ops_plans", {})
    assert len(listed["plans"]) == 1 and listed["plans"][0]["id"] == created["plan_id"]

    status = client.call_tool("get_ops_plan_status", {"plan_id": created["plan_id"]})
    assert status["ok"] is True
    assert len(status["plan"]["steps"]) == 3


def test_get_ops_plan_status_refuses_someone_elses_plan(db_path):
    ops_plans.init_ops_plans_db(db_path)
    other_owner_plan = ops_plans.create_plan(db_path, 2, "Not yours", _plan_steps())
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=FakeSSHOps())

    result = client.call_tool("get_ops_plan_status", {"plan_id": other_owner_plan})

    assert result["ok"] is False


def test_approving_the_review_item_actually_runs_the_plan(db_path, monkeypatch):
    """The real trigger: approving on the Review page is the owner's one approval for
    the whole plan, and this is the only place execution may start from."""
    ops_plans.init_ops_plans_db(db_path)
    ssh = FakeSSHOps()
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=ssh)
    proposed = client.call_tool("propose_ops_plan", {"summary": "Run me", "steps": _plan_steps()})

    # Run synchronously in-test rather than on a background thread, for determinism.
    from assistant.core import business_tools
    monkeypatch.setattr(business_tools.ops_plans, "run_plan_async", business_tools.ops_plans.run_plan)

    result = client.call_tool("decide_review_item", {
        "item_id": proposed["review_item_id"], "decision": "approved",
    })

    assert result["ok"] is True
    plan = ops_plans.get_plan(db_path, proposed["plan_id"])
    assert plan["status"] == "succeeded"
    assert ("simrig", "do the thing") in ssh.run_calls
    assert ("simrig", "undo the thing") not in ssh.run_calls  # never failed, so no rollback


def test_rejecting_the_review_item_never_runs_anything(db_path):
    ops_plans.init_ops_plans_db(db_path)
    ssh = FakeSSHOps()
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE, ssh_ops=ssh)
    proposed = client.call_tool("propose_ops_plan", {"summary": "Don't run me", "steps": _plan_steps()})

    result = client.call_tool("decide_review_item", {
        "item_id": proposed["review_item_id"], "decision": "rejected",
    })

    assert result["ok"] is True
    plan = ops_plans.get_plan(db_path, proposed["plan_id"])
    assert plan["status"] == "rejected"
    assert ssh.run_calls == []


@pytest.mark.parametrize("ref_table", ["pending_actions", "git_pull_requests"])
def test_deciding_a_pending_action_or_pr_card_from_chat_is_refused(db_path, ref_table):
    """These need context (era/kroger/ccxt/git_ops/etc.) this business-tools client
    doesn't have -- refusing beats silently marking the card decided while the Kroger
    cart write or PR merge it stands for never actually happens."""
    client = BusinessClient(db_path, owner_user_id=1, profile=PROFILE)
    item_id = business_db.create_review_item(
        db_path, 1, "Confirm: something", ref_table=ref_table, ref_id=99)

    result = client.call_tool("decide_review_item", {"item_id": item_id, "decision": "approved"})

    assert "error" in result
    assert business_db.get_review_item(db_path, 1, item_id)["status"] == "pending"
