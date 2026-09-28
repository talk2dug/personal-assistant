"""Changing Home Assistant's own configuration: saving scenes and creating automations.

Everything else Jarvis does in HA is a momentary *action* (turn a light on). This is
different in kind -- it writes something that persists and, for automations, runs later
with nobody watching. So:

- Scenes are written through HA's config API (`/api/config/scene/config/<id>`, the same
  endpoint the scene editor uses), which lands them in scenes.yaml. They survive an HA
  restart, unlike `scene.create`, whose scenes vanish on reboot -- a named scene Jack
  built by hand and then lost on the next update would be worse than none.
- A scene is captured from the lights' LIVE state, not described by the model. "Take the
  current bedroom lights and call it Bed TV Time" means exactly what is lit right now, and
  reading it back from HA is the only way to get the real brightness/colour values.
- Automations are validated here BEFORE they are staged for confirmation: every entity id
  must exist and every action must be a real service. Asking Jack to approve an
  automation that references a light that isn't there wastes his yes and fails silently at
  sunset. HA itself re-validates the structure when it is written.

Saving a scene changes nothing in the house and is ungated. Creating an automation and
deleting either kind go through engine.py's pending-confirmation gate.
"""
import re
import time

import httpx

from .home_state import ROOMS

# What a scene may capture. Deliberately excludes lock/cover/alarm: a scene is replayed
# with one tap and no confirmation, so it must never carry a sensitive domain.
SCENE_DOMAINS = {"light", "switch", "fan", "input_boolean"}

# The colour attribute HA needs to reproduce each colour mode.
_COLOR_ATTR = {
    "color_temp": "color_temp_kelvin",
    "hs": "hs_color",
    "rgb": "rgb_color",
    "rgbw": "rgbw_color",
    "rgbww": "rgbww_color",
    "xy": "xy_color",
}

_UNUSABLE = ("unavailable", "unknown", "")
# Fallback when the caller doesn't pass config's ha_sensitive_domains.
DEFAULT_SENSITIVE_DOMAINS = {"lock", "cover", "alarm_control_panel"}
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class HAConfigError(Exception):
    pass


def slugify(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (name or "").lower())).strip("_")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def room_lights(room: str) -> list[str]:
    """The lights home_state.py assigns to a room ('bedroom', 'Living Room', 'livingroom')."""
    key = _norm(room)
    for r in ROOMS:
        if key in (_norm(r["key"]), _norm(r["name"])):
            return [e for e in r["activity"] if e.startswith("light.")]
    known = ", ".join(r["name"] for r in ROOMS)
    raise HAConfigError(f"unknown room {room!r}; rooms are: {known}")


def _state_for_scene(state: dict) -> dict:
    """One entity's live state, reduced to what HA needs to reproduce it."""
    if state["state"] != "on":
        return {"state": "off"}
    a = state.get("attributes", {})
    out = {"state": "on"}
    if a.get("brightness") is not None:
        out["brightness"] = a["brightness"]
    mode = a.get("color_mode")
    attr = _COLOR_ATTR.get(mode)
    if attr and a.get(attr) is not None:
        out["color_mode"] = mode
        out[attr] = a[attr]
    return out


def _describe(entity_id: str, s: dict) -> str:
    name = entity_id.split(".", 1)[1]
    if s["state"] != "on":
        return f"{name} off"
    parts = [name, "on"]
    if "brightness" in s:
        parts.append(f"{round(s['brightness'] / 255 * 100)}%")
    if "color_temp_kelvin" in s:
        parts.append(f"{s['color_temp_kelvin']}K")
    elif "hs_color" in s:
        parts.append(f"hue {round(s['hs_color'][0])}")
    return " ".join(parts)


def _find_by_name(states: list[dict], domain: str, name: str) -> dict | None:
    """An existing scene/automation whose friendly name matches, ignoring case/punctuation."""
    want = _norm(name)
    for s in states:
        if s["entity_id"].startswith(f"{domain}.") and _norm(s.get("attributes", {}).get("friendly_name")) == want:
            return s
    return None


def _wait_for_entity(client, domain: str, config_id: str, timeout: float = 6.0) -> dict | None:
    """HA reloads the domain after a config write; poll until the new entity shows up."""
    deadline = time.monotonic() + timeout
    while True:
        for s in client.raw_states(max_age_seconds=0):
            if s["entity_id"].startswith(f"{domain}.") and s.get("attributes", {}).get("id") == config_id:
                return s
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


# ---------------------------------------------------------------------------- scenes

def save_scene(client, name: str, room: str | None = None, entity_ids: list[str] | None = None,
               replace: bool = False) -> dict:
    name = (name or "").strip()
    if not name:
        raise HAConfigError("a scene needs a name")
    targets = list(entity_ids or []) or (room_lights(room) if room else [])
    if not targets:
        raise HAConfigError("say which room, or give the entity ids to capture")
    bad = [e for e in targets if e.split(".", 1)[0] not in SCENE_DOMAINS]
    if bad:
        raise HAConfigError(f"scenes can only capture {sorted(SCENE_DOMAINS)}; refused: {bad}")

    states = client.raw_states(max_age_seconds=0)
    by_id = {s["entity_id"]: s for s in states}

    existing = _find_by_name(states, "scene", name)
    if existing and not replace:
        return {
            "ok": False, "exists": True, "entity_id": existing["entity_id"],
            "message": (f"A scene called {name!r} already exists. Ask whether to overwrite it "
                        "with the current lights, then call again with replace=true."),
        }
    config_id = (existing or {}).get("attributes", {}).get("id") or f"jarvis_{slugify(name)}"

    captured, skipped, missing = {}, [], []
    for eid in targets:
        s = by_id.get(eid)
        if s is None:
            missing.append(eid)
        elif s["state"] in _UNUSABLE:
            skipped.append(eid)
        else:
            captured[eid] = _state_for_scene(s)
    if not captured:
        raise HAConfigError(f"none of those lights are reporting right now (unavailable: {skipped or missing})")

    resp = httpx.post(f"{client.base_url}/api/config/scene/config/{config_id}", headers=client._headers,
                      json={"id": config_id, "name": name, "entities": captured}, timeout=15.0)
    if resp.status_code >= 400:
        raise HAConfigError(f"Home Assistant refused the scene: {resp.status_code} {resp.text[:300]}")

    entity = _wait_for_entity(client, "scene", config_id)
    return {
        "ok": True,
        "entity_id": entity["entity_id"] if entity else None,
        "name": name,
        "replaced": bool(existing),
        "captured": [_describe(e, s) for e, s in captured.items()],
        "skipped_unavailable": skipped,
        "not_found": missing,
        "note": None if entity else "Saved, but HA hasn't loaded it yet. It should appear within a minute.",
    }


def list_scenes(client) -> dict:
    scenes = []
    for s in client.raw_states(max_age_seconds=0):
        if not s["entity_id"].startswith("scene."):
            continue
        a = s.get("attributes", {})
        scenes.append({
            "entity_id": s["entity_id"], "name": a.get("friendly_name"),
            "entities": a.get("entity_id", []), "editable": bool(a.get("id")),
        })
    return {"scenes": scenes}


def delete_scene(client, entity_id: str) -> dict:
    s = next((x for x in client.raw_states(max_age_seconds=0) if x["entity_id"] == entity_id), None)
    if s is None:
        raise HAConfigError(f"no scene {entity_id}")
    config_id = s.get("attributes", {}).get("id")
    if not config_id:
        raise HAConfigError(f"{entity_id} isn't defined in scenes.yaml, so it can't be deleted from here")
    resp = httpx.delete(f"{client.base_url}/api/config/scene/config/{config_id}", headers=client._headers, timeout=15.0)
    if resp.status_code >= 400:
        raise HAConfigError(f"Home Assistant refused: {resp.status_code} {resp.text[:300]}")
    return {"ok": True, "deleted": entity_id}


# ----------------------------------------------------------------------- automations

def _normalize_automation(args: dict) -> dict:
    """Accept the older 'platform'/'service' key spellings the model may reach for, and
    emit the current ones HA 2024.10+ writes ('trigger', 'action')."""
    def fix_trigger(t):
        t = dict(t)
        if "platform" in t and "trigger" not in t:
            t["trigger"] = t.pop("platform")
        return t

    def fix_action(a):
        a = dict(a)
        if "service" in a and "action" not in a:
            a["action"] = a.pop("service")
        for key in ("then", "else", "sequence"):
            if isinstance(a.get(key), list):
                a[key] = [fix_action(x) for x in a[key]]
        if isinstance(a.get("choose"), list):
            a["choose"] = [{**c, "sequence": [fix_action(x) for x in c.get("sequence", [])]} for c in a["choose"]]
        return a

    return {
        "alias": (args.get("alias") or "").strip(),
        "description": args.get("description") or "",
        "mode": args.get("mode") or "single",
        "triggers": [fix_trigger(t) for t in args.get("triggers") or []],
        "conditions": list(args.get("conditions") or []),
        "actions": [fix_action(a) for a in args.get("actions") or []],
    }


def _walk(node, entity_ids: set, services: set):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "entity_id":
                for e in ([v] if isinstance(v, str) else v if isinstance(v, list) else []):
                    if isinstance(e, str) and "{{" not in e and e not in ("all", "none"):
                        entity_ids.add(e)
            elif k == "action" and isinstance(v, str) and "{{" not in v:
                services.add(v)
            else:
                _walk(v, entity_ids, services)
    elif isinstance(node, list):
        for x in node:
            _walk(x, entity_ids, services)


def validate_automation(client, args: dict, sensitive_domains=None) -> dict:
    """Returns {"ok": bool, "errors": [...], "automation": normalized, "sensitive": [...]}.
    Cheap enough to run before staging: one /api/states read and one /api/services read."""
    auto = _normalize_automation(args)
    errors = []
    if not auto["alias"]:
        errors.append("give it a name (alias)")
    if not auto["triggers"]:
        errors.append("it needs at least one trigger")
    if not auto["actions"]:
        errors.append("it needs at least one action")
    for t in auto["triggers"]:
        if not isinstance(t, dict) or "trigger" not in t:
            errors.append(f"trigger without a type: {t}")

    entity_ids, services = set(), set()
    _walk([auto["triggers"], auto["conditions"], auto["actions"]], entity_ids, services)

    known = {s["entity_id"] for s in client.raw_states(max_age_seconds=0)}
    for e in sorted(entity_ids):
        if not _ENTITY_RE.match(e) or e not in known:
            errors.append(f"no such entity: {e}")

    try:
        resp = httpx.get(f"{client.base_url}/api/services", headers=client._headers, timeout=15.0)
        resp.raise_for_status()
        real = {f"{d['domain']}.{svc}" for d in resp.json() for svc in d.get("services", {})}
        for svc in sorted(services):
            if svc not in real:
                errors.append(f"no such service: {svc}")
    except httpx.HTTPError:
        pass  # can't list services; HA still validates on write

    sensitive_domains = set(sensitive_domains or DEFAULT_SENSITIVE_DOMAINS)
    sensitive = sorted({s for s in services if s.split(".", 1)[0] in sensitive_domains})
    return {"ok": not errors, "errors": errors, "automation": auto, "sensitive": sensitive}


def create_automation(client, args: dict) -> dict:
    check = validate_automation(client, args)
    if not check["ok"]:
        raise HAConfigError("; ".join(check["errors"]))
    auto = check["automation"]

    states = client.raw_states(max_age_seconds=0)
    config_id = args.get("automation_id")
    existing = None
    if not config_id:
        existing = _find_by_name(states, "automation", auto["alias"])
        if existing and not args.get("replace"):
            raise HAConfigError(f"an automation called {auto['alias']!r} already exists "
                                f"({existing['entity_id']}); pass replace=true to overwrite it")
        config_id = (existing or {}).get("attributes", {}).get("id") or f"jarvis_{slugify(auto['alias'])}"

    body = {"id": config_id, **auto}
    resp = httpx.post(f"{client.base_url}/api/config/automation/config/{config_id}",
                      headers=client._headers, json=body, timeout=15.0)
    if resp.status_code >= 400:
        raise HAConfigError(f"Home Assistant rejected the automation: {resp.status_code} {resp.text[:400]}")
    entity = _wait_for_entity(client, "automation", config_id)
    return {
        "ok": True, "id": config_id, "alias": auto["alias"],
        "entity_id": entity["entity_id"] if entity else None,
        "state": entity["state"] if entity else None,
        "replaced": bool(existing or args.get("automation_id")),
    }


def list_automations(client) -> dict:
    out = []
    for s in client.raw_states(max_age_seconds=0):
        if not s["entity_id"].startswith("automation."):
            continue
        a = s.get("attributes", {})
        item = {"entity_id": s["entity_id"], "name": a.get("friendly_name"), "enabled": s["state"] == "on",
                "last_triggered": a.get("last_triggered"), "id": a.get("id")}
        if a.get("id"):
            try:
                r = httpx.get(f"{client.base_url}/api/config/automation/config/{a['id']}",
                              headers=client._headers, timeout=10.0)
                if r.status_code == 200:
                    cfg = r.json()
                    item.update({k: cfg.get(k) for k in ("description", "triggers", "conditions", "actions", "mode")})
            except httpx.HTTPError:
                pass
        out.append(item)
    return {"automations": out}


def delete_automation(client, entity_id: str) -> dict:
    s = next((x for x in client.raw_states(max_age_seconds=0) if x["entity_id"] == entity_id), None)
    if s is None:
        raise HAConfigError(f"no automation {entity_id}")
    config_id = s.get("attributes", {}).get("id")
    if not config_id:
        raise HAConfigError(f"{entity_id} isn't defined in automations.yaml, so it can't be deleted from here")
    resp = httpx.delete(f"{client.base_url}/api/config/automation/config/{config_id}",
                        headers=client._headers, timeout=15.0)
    if resp.status_code >= 400:
        raise HAConfigError(f"Home Assistant refused: {resp.status_code} {resp.text[:300]}")
    return {"ok": True, "deleted": entity_id}


def call_tool(client, name: str, arguments: dict) -> dict:
    try:
        if name == "save_scene":
            return save_scene(client, arguments.get("name"), arguments.get("room"),
                              arguments.get("entity_ids"), bool(arguments.get("replace")))
        if name == "list_scenes":
            return list_scenes(client)
        if name == "delete_scene":
            return delete_scene(client, arguments["entity_id"])
        if name == "create_automation":
            return create_automation(client, arguments)
        if name == "list_automations":
            return list_automations(client)
        if name == "delete_automation":
            return delete_automation(client, arguments["entity_id"])
    except HAConfigError as e:
        return {"error": str(e)}
    return {"error": f"unknown home assistant config tool {name}"}
