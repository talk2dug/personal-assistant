# Room Presence, Identity & Open-Mic Conversation — architecture

What this project answers: an open-mic voice terminal (no login, anyone in earshot can
trigger it) must not hand over personal or financial information to whoever happens to be
standing near it, and when more than one terminal hears the same wake word, only the one
the owner is actually near should answer.

Three pieces, each already covered by its own module docstring in detail — this is the
map connecting them, plus the tradeoffs made along the way.

## 1. Schema (assistant/core/vision.py, assistant/core/db.py)

`cameras`, `vision_events`, `known_people`, `unknown_faces` already existed before this
work — the schema was there, but only `cameras` and `vision_events` had any functions
behind them. This project wired up the rest:

- **`known_people`** gained `access_level` (`owner` | `household` | `guest`). Being
  *recognised* (a face matches a known_people row) and being *authorized* for
  personal/financial info are different questions on purpose — a recognised guest still
  defaults to `guest` and gets nothing sensitive until the owner explicitly changes that
  with `set_person_access_level`.
- **`terminal_cameras`** is new: `device_id -> camera_key`. A voice terminal is not a
  camera, and not every terminal has one — day one, only `touch1` and `laptop1` do. A
  terminal absent from this table simply has no way to confirm anyone's identity, which
  is the correct (fail-closed) default, not an error state.
- **`users.role`** (db.py) gained `'guest'`, requiring a table rebuild since SQLite can't
  ALTER a CHECK constraint in place. Guests are synthetic, per-device, identity-less rows
  (`presence.guest_user_id`) so an unconfirmed turn's conversation history never mixes
  with the owner's real account.

## 2. Data flow: camera -> identity -> gating

```
RTSP/MJPEG frame (Touch1's uStreamer feed, laptop1's own webcam)
        |  assistant/core/vision.py: FrameSource.snapshot()
        v
MotionGate.update()  --- no motion --> nothing happens, cheaply, forever
        | motion
        v
detector.Detector.detect()  (YOLO11n, RTX 3060, .venv-vision)
        | person detection(s)
        v
identity.FaceEmbedder.embed()  (InsightFace buffalo_l, same GPU/venv)
        | face embedding
        v
identity.match_known_person()  vs. vision.list_known_people()'s stored samples
        |                                   |
        | match >= threshold                | no match
        v                                   v
vision_events: kind='identified',   vision_events: kind='unknown_person'
  person_key=<key>                    + unknown_faces row queued for "who is this?"
```

All of the above runs in **assistant/vision_main.py**, a separate process under its own
`.venv-vision` (torch/ultralytics/insightface never become a dependency of the Telegram
or web processes — same reasoning detector.py already documented for itself). It shares
`jarvis.db` (WAL) with jarvis-core and jarvis-web exactly the way the GPU bridge does.

The read side is a single function, `vision.identity_on_camera(camera_key)`: the
known_people row for whoever that camera's *most recent* identified/unknown_person/
cleared event says is there, within a configurable window (default 45s). "Most recent",
not "seen at all recently" — a stranger walking in immediately invalidates a stale
"identified" reading rather than waiting out the whole window.

## 3. Gating (assistant/core/presence.py)

`presence.evaluate(device_id)` is the one decision point:

1. Does this terminal have a camera at all (`terminal_cameras`)? No -> not confirmed.
2. Does that camera currently show someone identified (`vision.identity_on_camera`)?
   No -> not confirmed.
3. Is their `access_level` in the configured authorized set (default `{"owner"}`)?
   No -> not confirmed.

`presence.gate_contexts()` then removes era/personal/mail/business/ccxt/kroger/
letterstream from the turn entirely when not confirmed — `handle_message` receives
`None` for those, the same mechanism `assistant/web/routes/chat.py` already uses for
`is_owner` gating on the logged-in web UI, just driven by camera identity instead of a
password. This is wired into `assistant/web/routes/devices.py`'s `/turn` endpoint, which
is the one place in the codebase an unauthenticated person can start a conversation at
all.

**Deliberately fails closed.** Every other optional integration in this codebase (phone,
Home Assistant, mail...) fails *open* on error — an outage only disables a feature, never
blocks the assistant (see `setup.py`). Identity is the one exception: camera unreachable,
no face matched, access level too low, or simply an empty room all collapse to the same
"not confirmed" answer, because an inconvenienced owner costs nothing and a leaked bank
balance can't be undone.

**Not scoped in this pass, flagged for the owner's explicit call:**
- `phone`, `calendar`, `home_assistant`, `obsidian`, `airbnb`, `ticketmaster` are left
  ungated for day one. HA's lock/alarm/cover actions already require an explicit "yes"
  via the existing `pending_actions` confirmation gate regardless of who's asking, which
  is a different (weaker) protection than presence gating -- worth deciding whether that's
  enough, given the open-mic scenario means literally anyone in earshot can attempt it.
- Only `known_people.access_level == 'owner'` unlocks anything sensitive by default.
  `household` exists in the schema for a partner-type case but isn't granted anything
  extra yet — that's a policy call (`presence_authorized_access_levels` in config), not a
  code change, whenever the owner wants to make it.
- A second person visible alongside a confirmed owner (e.g. a guest standing next to him)
  is not currently treated any differently — the gate only checks *the most recent*
  identified signal per camera. If the owner wants "don't read out my balance if someone
  else is in frame too" that's a real, addressable refinement to `identity_on_camera`,
  just not one this scope asked for.

## 4. Wake-word arbitration (assistant/core/wake_arbitration.py, device/jarvis_device.py)

Every terminal (touch1, laptop1, jarvisaudio1, jarvisaudio2, jarvisbox) runs its own
`openWakeWord` model against its own microphone continuously — streaming raw audio from
every room to a central server just to decide who should answer is a much bigger
privacy/bandwidth trade than the actual problem (adjacent rooms both hearing "hey
Jarvis") deserves.

On a wake-word hit, a terminal now calls `POST /api/devices/{id}/wake_claim` with its
confidence score *before* chiming or recording, and blocks briefly (default 400ms)
waiting to see if a louder claim comes in from elsewhere. Confidence score stands in for
physical proximity because it's the one signal every terminal produces uniformly, with or
without camera coverage — camera coverage is two terminals deep on day one; wake-word
terminals are more than that already. Camera-confirmed presence could sharpen this later
(e.g. as a tiebreaker when scores are close) but wasn't necessary to hit "only the
terminal you're near answers" for the terminals that exist today.

Arbitration **fails open** — the opposite posture from presence gating. If the server or
the arbitration call itself is unreachable, the terminal proceeds unarbitrated. Worst
case, two terminals answer the same utterance once in a while; that's a far smaller
problem for a voice assistant than a wake word that silently does nothing because a
network call failed.

## What a future terminal/camera needs

1. Add a row to `vision_cameras` in config (key/name/url/kind/location).
2. Add `"<device_id>": "<camera_key>"` to `terminal_cameras` in config.
3. Restart jarvis-web and jarvis-vision. No code change, no migration.

A voice-only terminal with no camera needs nothing beyond being deployed — it participates
in wake arbitration automatically (any terminal calling `/wake_claim` is included) and
simply never has a confirmed identity of its own, which is the safe default.
