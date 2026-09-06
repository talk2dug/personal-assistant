"""Chat-facing tools for the owner's own life — separate from business_tools.py the same way
personal_db.py is separate from business_db.py: personal to-dos, personal projects, and
errands he's delegated ("find me a doctor") have nothing to do with the print business.

Nothing here is gated behind pending_actions, for the same reason as business_tools.py:
these write only to Jarvis's own database, spend no money, and touch nothing physical.
"""
from . import personal_db

PERSONAL_TOOLS = [
    {"type": "function", "function": {
        "name": "list_personal_projects",
        "description": "List the owner's personal projects (things he's building or working toward, not business).",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_personal_project",
        "description": "Start tracking a new personal project. Use when he describes something he's personally taking on.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "goal": {"type": "string", "description": "What finishing this looks like."},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "update_personal_project",
        "description": "Change a personal project's name, goal, or status.",
        "parameters": {"type": "object", "properties": {
            "project_id": {"type": "integer"},
            "name": {"type": "string"},
            "goal": {"type": "string"},
            "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
        }, "required": ["project_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_personal_tasks",
        "description": "List the owner's personal to-dos, optionally filtered by status or project.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "doing", "done", "dropped"]},
            "project_id": {"type": "integer"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_personal_task",
        "description": (
            "Add a personal to-do — errands, chores, appointments to book, anything for his "
            "own life rather than the business (that's create_task). Create these proactively "
            "when he mentions something he needs to do."
        ),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "project_id": {"type": "integer", "description": "Optional personal project to file it under."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "due_at": {"type": "string", "description": "Optional local ISO 8601 date/time."},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "update_personal_task",
        "description": "Update a personal task — mark it done, change priority, move it, reword it.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "text": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "doing", "done", "dropped"]},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "project_id": {"type": "integer"},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "request_personal_research",
        "description": (
            "Queue a personal errand for the background agent, which searches the web and "
            "writes back a practical answer. Use this whenever he asks you to find or look "
            "into something for him personally that deserves real digging — a doctor or "
            "dentist near him, a service provider, comparing options, how to handle "
            "something. Tell him you've put it in hand and will report back; do not answer "
            "from memory when this is the right call."
        ),
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "Short label, e.g. 'Find a dentist'."},
            "question": {"type": "string", "description": "The specific thing to find out."},
            "project_id": {"type": "integer"},
        }, "required": ["topic"]},
    }},
    {"type": "function", "function": {
        "name": "list_personal_research",
        "description": "Recent personal errands and their findings, including anything still queued.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Default 10."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "update_pantry_status",
        "description": (
            "Set what's on hand for a pantry/grocery item — 'have', 'low', or 'out'. Use "
            "this whenever he mentions running low on or out of something ('I'm about out "
            "of milk', 'we're low on eggs'), or confirms he just bought something ('have'). "
            "This is a simple status, not a quantity — never ask him for a count."
        ),
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string"},
            "status": {"type": "string", "enum": ["have", "low", "out"]},
            "notes": {"type": "string"},
        }, "required": ["item", "status"]},
    }},
    {"type": "function", "function": {
        "name": "list_pantry",
        "description": "What's on hand, optionally filtered to what's low or out.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["have", "low", "out"]},
        }, "required": []},
    }},
]

PERSONAL_SYSTEM_NOTE = (
    " You also keep track of the owner's own personal life, separate from the business: his "
    "personal projects, his to-do list, and errands he's asked you to look into. Treat that as "
    "a standing responsibility. When he mentions something he's personally working on, record "
    "it with create_personal_project; when he mentions something he needs to do, record it with "
    "create_personal_task rather than replying with encouragement and letting it evaporate. When "
    "he asks you to find or look into something personal that deserves real digging — a doctor, "
    "a service, comparing options — queue it with request_personal_research rather than "
    "answering from memory: it runs a real web search in the background and reports back, so "
    "say you'll look into it rather than pretending you already know. Never confuse this with "
    "the business tools (create_project/create_task/request_research) — those are for the "
    "print business, these are for him."
    " You also track what's in his kitchen with update_pantry_status/list_pantry — a simple "
    "have/low/out status per item, not a quantity. Whenever he says he's running low on or "
    "out of something, or that he just bought something, update it yourself immediately "
    "rather than just acknowledging it in conversation."
)


class PersonalClient:
    """Executes the personal tools. Same call_tool shape as every other integration."""

    def __init__(self, db_path: str, owner_user_id: int):
        self.db_path = db_path
        self.owner_user_id = owner_user_id

    def call_tool(self, name: str, arguments: dict) -> dict:
        db_path, owner = self.db_path, self.owner_user_id

        if name == "list_personal_projects":
            return {"projects": personal_db.list_projects(db_path, owner, arguments.get("status"))}
        if name == "create_personal_project":
            pid = personal_db.create_project(db_path, owner, arguments["name"], arguments.get("goal"))
            return {"ok": True, "project_id": pid}
        if name == "update_personal_project":
            ok = personal_db.update_project(
                db_path, owner, arguments["project_id"],
                name=arguments.get("name"), goal=arguments.get("goal"), status=arguments.get("status"))
            return {"ok": ok}

        if name == "list_personal_tasks":
            return {"tasks": personal_db.list_tasks(
                db_path, owner, arguments.get("status"), arguments.get("project_id"))}
        if name == "create_personal_task":
            tid = personal_db.create_task(
                db_path, owner, arguments["text"], arguments.get("project_id"),
                arguments.get("priority", "normal"), arguments.get("due_at"))
            return {"ok": True, "task_id": tid}
        if name == "update_personal_task":
            ok = personal_db.update_task(
                db_path, owner, arguments["task_id"], text=arguments.get("text"),
                status=arguments.get("status"), priority=arguments.get("priority"),
                project_id=arguments.get("project_id"))
            return {"ok": ok}

        if name == "request_personal_research":
            rid = personal_db.create_research(
                db_path, owner, arguments["topic"], arguments.get("question"), arguments.get("project_id"))
            return {
                "ok": True, "research_id": rid,
                "message": (
                    "Queued for the background research agent. Tell him you've put it in hand "
                    "and will report back — you do not have findings yet."
                ),
            }
        if name == "list_personal_research":
            return {"research": personal_db.list_research(db_path, owner, arguments.get("limit", 10))}

        if name == "update_pantry_status":
            item_id = personal_db.upsert_pantry_item(
                db_path, owner, arguments["item"], arguments["status"], arguments.get("notes"))
            return {"ok": True, "item_id": item_id}
        if name == "list_pantry":
            return {"pantry": personal_db.list_pantry(db_path, owner, arguments.get("status"))}

        return {"error": f"unknown personal tool {name}"}
