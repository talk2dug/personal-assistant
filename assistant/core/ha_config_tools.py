"""Chat tool schemas for changing Home Assistant's configuration -- scenes and
automations. The implementation is ha_config.py; the gate is engine.py.

Split from engine.py's HOME_ASSISTANT_TOOLS on purpose: the local fast path
(local_fast_path.py) is handed HOME_ASSISTANT_TOOLS only, and a one-shot local model
must never be the thing that writes an automation.
"""
import re

HA_CONFIG_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_scene",
            "description": (
                "Save the CURRENT state of some lights as a named Home Assistant scene, e.g. "
                "'take the current bedroom lights and call it Bed TV Time' -> "
                "save_scene(name='Bed TV Time', room='bedroom'). It reads the real brightness "
                "and colour of each light right now, so never describe levels yourself, and "
                "never turn lights on or off first unless he asked. Pass `room` for a whole "
                "room's lights (kitchen, living room, bedroom), or `entity_ids` for specific "
                "lights, switches or fans. The scene is saved permanently and appears as "
                "scene.<name>; he activates it later with call_service(domain='scene', "
                "service='turn_on', entity_id='scene.<name>'). If a scene with that name "
                "exists, this returns exists=true: ask whether to overwrite it, then call again "
                "with replace=true. Tell him which lights were captured and at what levels, and "
                "mention any that were skipped as unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The scene name exactly as he said it, e.g. 'Bed TV Time'."},
                    "room": {"type": "string", "description": "Room whose lights to capture: kitchen, living room or bedroom."},
                    "entity_ids": {"type": "array", "items": {"type": "string"},
                                   "description": "Specific entities to capture instead of (or when there's no) room."},
                    "replace": {"type": "boolean", "description": "Overwrite an existing scene of the same name. Only after he says yes."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scenes",
            "description": "List the saved Home Assistant scenes and which entities each one sets.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_scene",
            "description": "Delete a saved scene. Stages for his confirmation before anything is deleted.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string", "description": "e.g. scene.bed_tv_time"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": (
                "Create (or, with automation_id or replace=true, update) a Home Assistant "
                "automation: something that runs on its own when a trigger fires and its "
                "conditions hold. It is checked first (every entity and service must exist), "
                "then staged for his explicit yes, because it will act unattended. Before "
                "calling, get the real entity ids you need: list_entities, get_presence (his "
                "person entity), list_scenes. Use the current HA format. Triggers, e.g. "
                "{'trigger':'sun','event':'sunset','offset':'-00:30:00'} (30 min BEFORE "
                "sunset), {'trigger':'numeric_state','entity_id':'sun.sun','attribute':"
                "'elevation','below':4} (as the sun starts going down), {'trigger':'time',"
                "'at':'22:30:00'}, {'trigger':'state','entity_id':'person.x','to':'home'} "
                "(he arrives). Conditions, e.g. {'condition':'state','entity_id':'person.x',"
                "'state':'home'} (only when he's home), {'condition':'sun','after':'sunset'}, "
                "{'condition':'time','after':'18:00:00','before':'23:00:00'}. Actions, e.g. "
                "{'action':'light.turn_on','target':{'entity_id':[...]},'data':{'brightness_pct':"
                "60,'transition':900}} (fade up over 15 min; transition is seconds), "
                "{'action':'scene.turn_on','target':{'entity_id':'scene.bed_tv_time'}}. Think "
                "about the gaps he'd hit: 'lights on at sunset only when I'm home' also needs "
                "an arrival trigger (plus a sun condition) or it does nothing on days he gets "
                "home after dark. Propose that rather than silently adding it. After it's "
                "staged, explain it in plain words (when, only if, what) and ask yes or no."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string", "description": "Short human name, e.g. 'Sunset lights when home'."},
                    "description": {"type": "string", "description": "One sentence on what it does and why."},
                    "triggers": {"type": "array", "items": {"type": "object"}},
                    "conditions": {"type": "array", "items": {"type": "object"}},
                    "actions": {"type": "array", "items": {"type": "object"}},
                    "mode": {"type": "string", "enum": ["single", "restart", "queued", "parallel"]},
                    "automation_id": {"type": "string", "description": "Existing automation's id, to edit it in place."},
                    "replace": {"type": "boolean", "description": "Overwrite an existing automation with the same alias."},
                },
                "required": ["alias", "triggers", "actions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": (
                "List the Home Assistant automations with their triggers, conditions and actions, "
                "whether each is enabled, and when it last ran. Use it before editing one, and for "
                "'what automations do I have'. Enable or disable one with call_service(domain="
                "'automation', service='turn_on'/'turn_off')."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_automation",
            "description": "Delete an automation. Stages for his confirmation before anything is deleted.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string", "description": "e.g. automation.sunset_lights_when_home"}},
                "required": ["entity_id"],
            },
        },
    },
]

HA_CONFIG_TOOL_NAMES = {t["function"]["name"] for t in HA_CONFIG_TOOLS}

# Written to run unattended, or destroys something he built: always his explicit yes.
HA_CONFIG_SENSITIVE = {"create_automation", "delete_automation", "delete_scene"}

HA_CONFIG_KEYWORDS = [
    "scene", "automation", "automate", "routine", "sunset", "sunrise", "sun goes down",
    "sun sets", "when i get home", "when i'm home", "when im home", "every night", "every morning",
]

# A request to SAVE or SCHEDULE something rather than do it now. The local fast path
# declines these: its one-shot model would read "take the current bedroom lights and call
# it Bed TV Time" as "turn on the bedroom lights".
CONFIG_INTENT_RE = re.compile(
    r"\b(scene|automation|automate|routine|call it|name it|save (it|this|the|these)|"
    r"remember (this|these)|whenever|every (day|night|morning|evening)|"
    r"sunset|sunrise|sun (goes|starts|sets)|when (i|i'm|im) (get|home|arrive|leave))\b",
    re.I,
)
