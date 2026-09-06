"""Read/control access to Home Assistant via its REST API and a long-lived access
token. Exposes call_tool(name, arguments) matching the shape Era/phone/mail/Obsidian
already use so it plugs into the same dispatch machinery in engine.py.
"""
import logging

import httpx

logger = logging.getLogger(__name__)


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str, default_notify_target: str | None = None):
        self.base_url = base_url.rstrip("/")
        # Which phone to push to when a caller doesn't name one.
        self.default_notify_target = default_notify_target
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def list_entities(self, domain: str | None = None) -> dict:
        resp = httpx.get(f"{self.base_url}/api/states", headers=self._headers, timeout=15.0)
        resp.raise_for_status()
        entities = resp.json()
        if domain:
            entities = [e for e in entities if e["entity_id"].startswith(f"{domain}.")]
        return {
            "entities": [
                {
                    "entity_id": e["entity_id"],
                    "name": e.get("attributes", {}).get("friendly_name", e["entity_id"]),
                    "state": e["state"],
                }
                for e in entities
            ]
        }

    def get_entity_state(self, entity_id: str) -> dict:
        resp = httpx.get(f"{self.base_url}/api/states/{entity_id}", headers=self._headers, timeout=15.0)
        if resp.status_code == 404:
            return {"error": f"no entity '{entity_id}'"}
        resp.raise_for_status()
        data = resp.json()
        return {"entity_id": data["entity_id"], "state": data["state"], "attributes": data.get("attributes", {})}

    def get_weather(self, entity_id: str | None = None, forecast_type: str = "daily") -> dict:
        """Current conditions plus a forecast, in one call.

        Weather is split awkwardly in modern Home Assistant: current conditions live in
        the entity's attributes, but multi-day/hourly forecasts moved out of attributes
        into a `weather.get_forecasts` service call that only returns data when asked
        with ?return_response. Rather than expect the model to know that (and to know
        the local entity id), this finds the weather entity itself and returns both
        halves together.
        """
        if entity_id is None:
            candidates = [
                e["entity_id"] for e in self.list_entities("weather")["entities"]
            ]
            if not candidates:
                return {"error": "no weather entity is configured in Home Assistant"}
            entity_id = candidates[0]

        current = self.get_entity_state(entity_id)
        if "error" in current:
            return current
        attributes = current.get("attributes", {})

        forecast = []
        try:
            resp = httpx.post(
                f"{self.base_url}/api/services/weather/get_forecasts?return_response",
                headers=self._headers,
                json={"entity_id": entity_id, "type": forecast_type},
                timeout=20.0,
            )
            resp.raise_for_status()
            body = resp.json()
            forecast = (body.get("service_response", body).get(entity_id) or {}).get("forecast", [])
        except Exception as e:
            # Current conditions are still worth returning on their own â€” a forecast
            # failure shouldn't turn "it's 93 degrees" into an error.
            forecast = [{"error": f"forecast unavailable: {e}"}]

        return {
            "entity_id": entity_id,
            "condition": current.get("state"),
            "temperature": attributes.get("temperature"),
            "temperature_unit": attributes.get("temperature_unit"),
            "humidity": attributes.get("humidity"),
            "wind_speed": attributes.get("wind_speed"),
            "wind_speed_unit": attributes.get("wind_speed_unit"),
            "uv_index": attributes.get("uv_index"),
            "pressure": attributes.get("pressure"),
            "forecast_type": forecast_type,
            # Trimmed: the hourly forecast runs 48 entries, which is a lot of tokens for
            # a question usually answered by the next day or two.
            "forecast": forecast[:12],
        }

    def get_travel_time(self) -> dict:
        """Live drive times for the user's saved routes.

        Home Assistant's Waze Travel Time integration creates one sensor per configured
        route, whose state is the current traffic-aware duration in minutes and whose
        attributes carry the route description and distance. Discovering them by
        device_class/attribute shape rather than a hardcoded entity id means routes the
        user adds later are picked up with no code change here.
        """
        resp = httpx.get(f"{self.base_url}/api/states", headers=self._headers, timeout=15.0)
        resp.raise_for_status()

        routes = []
        for entity in resp.json():
            entity_id = entity["entity_id"]
            attributes = entity.get("attributes", {})
            if not entity_id.startswith("sensor."):
                continue
            # Waze/Google Travel Time sensors are minute-valued and carry a route string.
            is_travel_sensor = (
                "duration" in attributes
                or "route" in attributes
                or "travel_time" in entity_id
                or attributes.get("attribution", "").lower().find("waze") != -1
            )
            if not is_travel_sensor:
                continue
            routes.append({
                "entity_id": entity_id,
                "name": attributes.get("friendly_name", entity_id),
                "minutes": entity["state"],
                "unit": attributes.get("unit_of_measurement", "min"),
                "route": attributes.get("route"),
                "distance": attributes.get("distance"),
            })

        if not routes:
            return {
                "routes": [],
                "note": (
                    "No travel-time routes are configured in Home Assistant yet. The user needs "
                    "to add the Waze Travel Time integration there and define a route (origin and "
                    "destination) before drive times are available. Say this plainly rather than "
                    "estimating a drive time yourself."
                ),
            }
        return {"routes": routes}

    def notify(
        self, message: str, title: str | None = None, target: str | None = None,
        actions: list[dict] | None = None,
    ) -> dict:
        """Sends a push notification to the user's phone via HA's mobile app integration.

        Not expressible through call_service: notify services take no entity_id, and
        passing one is a 400 from Home Assistant (verified against the real instance).
        `actions` become tappable buttons whose taps come back to /notification-action,
        which is what lets a staged lock be approved from the lock screen.
        """
        service = (target or self.default_notify_target or "notify").removeprefix("notify.")
        payload: dict = {"message": message}
        # The mobile_app config entry can be unloaded and reloaded (watched HA do exactly
        # that at 1:17am, logging "Config entry was never loaded!"), which briefly takes
        # the device's notify service with it. Rather than fail the notification, fall
        # back to another mobile_app target if the configured one has gone.
        available = self.notify_targets()
        if available and f"notify.{service}" not in available:
            alternative = next((t for t in available if "mobile_app" in t), None)
            if alternative:
                logger.warning("notify.%s is not registered; using %s instead", service, alternative)
                service = alternative.removeprefix("notify.")
            else:
                return {"error": f"notify.{service} is not registered in Home Assistant "
                                 f"(available: {', '.join(available)})"}
        if title:
            payload["title"] = title
        if actions:
            payload["data"] = {"actions": actions}
        resp = httpx.post(
            f"{self.base_url}/api/services/notify/{service}",
            headers=self._headers, json=payload, timeout=20.0,
        )
        if resp.status_code >= 400:
            return {"error": f"notify failed ({resp.status_code}): {resp.text[:200]}"}
        return {"ok": True, "target": f"notify.{service}", "message": message}

    def notify_targets(self) -> list[str]:
        try:
            services = httpx.get(f"{self.base_url}/api/services", headers=self._headers, timeout=20.0).json()
        except Exception:
            return []
        block = next((s for s in services if s.get("domain") == "notify"), None)
        return sorted(f"notify.{n}" for n in (block or {}).get("services", {}))

    def request_location_update(self, target: str | None = None) -> dict:
        """Wakes the companion app and makes it report its position now.

        This is the answer to iOS suspending the app. `request_location_update` is a
        silent command notification — no banner, no sound — that the companion app
        handles by taking a fresh fix and pushing it to HA. It means Jarvis can get a
        position on its own schedule instead of waiting for iOS to decide to send one,
        which otherwise only happens on a zone crossing or ~500m of movement.

        It is still a push, so it costs a little battery; callers should ask when they
        need an answer, not continuously.
        """
        service = (target or self.default_notify_target or "notify").removeprefix("notify.")
        try:
            resp = httpx.post(
                f"{self.base_url}/api/services/notify/{service}",
                headers=self._headers, json={"message": "request_location_update"}, timeout=20.0,
            )
            if resp.status_code >= 400:
                return {"error": f"location request failed ({resp.status_code}): {resp.text[:160]}"}
            return {"ok": True, "target": f"notify.{service}"}
        except Exception as e:
            return {"error": str(e)}

    def location_now(self, wait_seconds: float = 6.0, poll_interval: float = 1.0) -> dict:
        """Forces a fresh fix and waits briefly for it to arrive.

        Compares against the reading taken before the request so a *stale* cached
        position isn't mistaken for a fresh one — the phone may have been reporting the
        same coordinates for hours, and 'it didn't change' has to be distinguishable from
        'it answered'.
        """
        import time

        before = self.location()
        before_key = (before.get("latitude"), before.get("longitude"), before.get("accuracy_m"))

        request = self.request_location_update()
        if request.get("error"):
            return {**before, "refresh": request}

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(poll_interval)
            current = self.location()
            if current.get("latitude") is None:
                continue
            key = (current.get("latitude"), current.get("longitude"), current.get("accuracy_m"))
            if before.get("latitude") is None or key != before_key:
                return {**current, "refresh": {"ok": True, "updated": True}}

        final = self.location()
        return {**final, "refresh": {
            "ok": True, "updated": False,
            "note": ("asked the phone for a fresh position but it hasn't answered yet — "
                     "the reading may be stale or the app may be offline"),
        }}

    def location(self) -> dict:
        """Raw GPS for the user, from whichever entity actually carries coordinates.

        Prefers a `person` entity (HA merges several trackers into one) and falls back to
        the most accurate `device_tracker`. Returns None coordinates rather than raising
        when nothing reports GPS — a phone with location permission off is a normal state,
        not an error.
        """
        try:
            resp = httpx.get(f"{self.base_url}/api/states", headers=self._headers, timeout=15.0)
            resp.raise_for_status()
            states = resp.json()
        except Exception as e:
            return {"error": str(e), "latitude": None, "longitude": None}

        def extract(entity):
            a = entity.get("attributes", {})
            if a.get("latitude") is None or a.get("longitude") is None:
                return None
            return {
                "entity_id": entity["entity_id"],
                "zone": entity["state"],
                "latitude": a["latitude"],
                "longitude": a["longitude"],
                "accuracy_m": a.get("gps_accuracy"),
                "speed": a.get("speed"),
                "altitude": a.get("altitude"),
                "battery": a.get("battery_level"),
                "source": a.get("source_type"),
            }

        people = [extract(s) for s in states if s["entity_id"].startswith("person.")]
        trackers = [extract(s) for s in states if s["entity_id"].startswith("device_tracker.")]
        candidates = [c for c in people if c] or [c for c in trackers if c]
        if not candidates:
            return {"latitude": None, "longitude": None,
                    "error": "no entity in Home Assistant is reporting GPS coordinates"}
        # Best fix wins; unknown accuracy sorts last rather than being trusted.
        candidates.sort(key=lambda c: c["accuracy_m"] if c["accuracy_m"] is not None else 1e9)
        return candidates[0]

    def presence(self) -> dict:
        """Where the user is, from HA's person/device_tracker entities.

        Returned as a small summary rather than raw entities so callers can route on it
        without each one re-deciding what "away" means.
        """
        try:
            resp = httpx.get(f"{self.base_url}/api/states", headers=self._headers, timeout=15.0)
            resp.raise_for_status()
            states = resp.json()
        except Exception as e:
            return {"error": str(e), "home": None}
        people = [
            {"entity_id": s["entity_id"],
             "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
             "state": s["state"]}
            for s in states if s["entity_id"].startswith("person.")
        ]
        trackers = [
            {"entity_id": s["entity_id"], "state": s["state"]}
            for s in states if s["entity_id"].startswith("device_tracker.")
        ]
        # "home" is HA's own zone name; a named zone like "work" counts as away. But
        # "unknown"/"unavailable" is NOT away — it means the phone dropped off HA, which
        # happens routinely (sleep, wifi drop, app killed). Treating that as away made
        # the notification policy start pushing to a phone that had just gone offline,
        # and would have fired "you left home" routines every time it slept.
        def usable(entries):
            return [e for e in entries if e["state"] not in ("unknown", "unavailable", "")]

        home = None
        known_people, known_trackers = usable(people), usable(trackers)
        if known_people:
            home = any(p["state"] == "home" for p in known_people)
        elif known_trackers:
            home = any(t["state"] == "home" for t in known_trackers)
        return {"home": home, "people": people, "device_trackers": trackers,
                "tracking_available": bool(known_people or known_trackers)}

    def call_service(self, domain: str, service: str, entity_id: str | None = None,
                     data: dict | None = None) -> dict:
        # entity_id is omitted when absent: several domains (notify, script with no
        # target, tts) reject a payload that carries one.
        payload = {**(data or {})}
        if entity_id:
            payload["entity_id"] = entity_id
        resp = httpx.post(
            f"{self.base_url}/api/services/{domain}/{service}", headers=self._headers, json=payload, timeout=15.0
        )
        resp.raise_for_status()
        return {"ok": True, "domain": domain, "service": service, "entity_id": entity_id, "result": resp.json()}

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "list_entities":
            return self.list_entities(arguments.get("domain"))
        if name == "get_entity_state":
            return self.get_entity_state(arguments["entity_id"])
        if name == "get_weather":
            return self.get_weather(arguments.get("entity_id"), arguments.get("forecast_type", "daily"))
        if name == "get_travel_time":
            return self.get_travel_time()
        if name == "call_service":
            return self.call_service(
                arguments["domain"], arguments["service"], arguments.get("entity_id"), arguments.get("data")
            )
        if name == "notify_me":
            return self.notify(
                arguments["message"], arguments.get("title"),
                arguments.get("target"), arguments.get("actions"),
            )
        if name == "get_presence":
            return self.presence()
        return {"error": f"unknown home assistant tool {name}"}


