# Room Presence, Identity & Open-Mic Conversation — Phase 1 Design

**Phase 1 scope only: camera-based room presence and identity gating.**
Not in scope here: continuous open-mic conversation mode, multi-terminal
wake-word arbitration, cross-room conversation handoff. Those are later
phases of the same project and are not designed in this document.

Status: design proposal, no implementation code written yet. Several open
questions below need an explicit owner decision before a development team
should start building against this.


## 1. Why this document exists, and what's already there

Before designing anything, I read the real repository rather than assuming
a stack. That surfaced two things that materially change the shape of this
phase:

**A presence/identity schema and detection pipeline already exist, but are
completely unwired.**

- `assistant/core/vision.py` defines `cameras`, `vision_events`,
 `known_people` (with a JSON `embeddings` column, explicitly "more than
 one because a face at the door in daylight and the same face indoors at
 night are far apart in embedding space"), and `unknown_faces` (faces seen
 but not yet named — intended to become a "who is this?" question for the
 owner). It also has a `MotionGate` (frame-differencing, so a detector
 never runs continuously against a static scene) and a `FrameSource`
 abstraction that already anticipates both MJPEG (uStreamer on the Pi
 terminals) and RTSP cameras.
- `assistant/core/detector.py` wraps YOLO for person/pet detection, with a
 lazy `torch` import so the assistant's main runtime doesn't depend on it.
- Neither module is imported anywhere. Not in `assistant/config.py`, not in
 `main.py` / `web_main.py`, no route in `assistant/web/routes/`, no entry
 in `requirements.txt` (no opencv/ultralytics/torch), no camera reference
 in `device/jarvis_device.py` or `device/device.example.json`. This is
 well-written scaffolding, not a running feature. Nothing in Phase 1
 needs to redesign this schema — it should be reused as-is.
- **There is no face-identity code at all.** No embedding model, no
 matching logic, no enrollment implementation — only the schema that
 anticipates them. This is the actual gap Phase 1 needs to fill.

**There is no camera hardware currently registered anywhere.**
Checked the real SSH host list: `homeassistant, jarvisaudio1, jarvisbox,
laptop1, pi5nas002, simrig, touch1`. `touch1` is the one voice-terminal Pi
visible in the code (`device.example.json`'s example device id), but
`jarvis_device.py` never touches a camera or runs uStreamer — the MJPEG
assumption in `vision.py`'s docstring is not deployed anywhere yet.

**A real, pre-existing privacy gap, independent of cameras.**
`assistant/web/routes/devices.py`'s `/turn` handler (the endpoint every
voice-terminal exchange goes through) hardcodes `_owner_user_id(request)`
for every single request — it never asks who is actually speaking.
Meanwhile `assistant/core/db.py` already has a private/shared scoping model
(`reminders.scope IN ('private','shared')`) and a `users` table with
`owner`/`partner` roles, used correctly by the web UI (session-based login,
`assistant/web/auth.py`) and Telegram (1:1 chat_id per user). Voice
terminals are the one surface with no identity signal at all today: anyone
standing near a kiosk is answered as if they were the owner, including
private-scoped data. Closing this is the actual point of "identity
gating," and it's worth being explicit that the gap exists regardless of
whether a camera ever gets installed.

**An unresolved compute-placement inconsistency.**
`assistant/core/gpu_bridge.py` already routes a `"vision"` task type to
`simrig` running `qwen3-vl:8b` (a VLM) for image-understanding work — but
notes it's a *shared, reservable* resource that gets yanked when the owner
is gaming or racing. `detector.py`'s own docstring, by contrast, assumes
running YOLO locally on "the laptop's RTX 3060" via a separate
`.venv-vision`. Neither is wired up, and they don't agree with each other.
Phase 1 needs one answer, not two half-built ones (see §4).


## 2. What Phase 1 actually needs to do

Given a room with a camera: detect that someone is present, determine (with
a confidence threshold) *who*, and use that identity to decide what a voice
terminal in that room is allowed to say out loud — falling back to a safe,
non-private default when identity isn't confidently known.


## 3. Proposed architecture

### Components

| Component | Status | Role |
|---|---|---|
| `assistant/core/vision.py` | **exists, reuse as-is** | camera/event/known-people schema, motion gate, frame sourcing |
| `assistant/core/detector.py` | **exists, reuse as-is** | person/pet detection (YOLO) |
| `assistant/core/face_id.py` | **new** | face embedding + matching against `known_people.embeddings` |
| `PresenceWatcher` | **new** | background loop wiring motion gate → detector → face_id → `vision.record_event` |
| `assistant/web/routes/devices.py` | **changed** | replace hardcoded owner assumption with a resolved identity |
| config: `presence_identity_enabled`, camera list, device→room map | **new, off by default** | same convention as the existing `business` block — absent/false means zero behavior change |

### `face_id.py`

A narrow interface, deliberately mirroring the discipline already used in
`detector.py` (lazy imports, one class, swappable backend):

```
embed(face_crop: np.ndarray) -> np.ndarray
match(vector, known_people) -> (person_key, score) | None
```

**Why embedding + cosine-similarity matching, not a VLM prompt.** The
codebase already has a working pattern for VLM-based image understanding
(`gpu_bridge`'s `"vision"` task, `qwen3-vl:8b` on `simrig`). It would be
tempting to reuse it for identity too, but a VLM asked "does this face
match person X" is slow, non-deterministic, expensive on a shared and
sometimes-unavailable GPU, and the wrong tool for a security-relevant
yes/no decision. A small embedding model plus cosine similarity against
stored per-person vectors is the boring, proven, cheap, deterministic
choice — and it's exactly what `known_people.embeddings` (plural, per
person, explicitly for lighting/angle variation) was already designed for.

### `PresenceWatcher`

Same shape as the existing `gpu_bridge` worker thread / `scheduler.py`
pattern already used in this codebase. Per enabled camera: motion gate →
if motion, run the detector → if a person, crop the face region → embed →
match against `known_people` → `vision.record_event(...)`. Unknown faces go
to `unknown_faces`, feeding the "who is this?" enrollment flow the schema
already anticipates (owner confirms via chat — never inferred).

Motion-gating, not continuous inference, keeps this cheap enough to run
without contending with `simrig`'s gaming/racing reservation — reinforcing
the recommendation in §4 to keep this off `simrig` entirely.

### Identity resolution in `devices.py`

Replace the hardcoded `_owner_user_id(request)` in `turn()` with an
`identify_speaker(device_id)` resolver:

1. Look up the room mapped to this `device_id` (new config).
2. Call `vision.presence_now()` for that room's camera(s).
3. Apply an explicit policy (this is §5, question 4 — needs your
 sign-off): a single, confidently-identified known person → that user's
 id, scoped exactly as today. Zero or multiple people, or confidence below
 threshold → **fail closed**: answer as an unauthenticated guest, shared
 information only, no private reminders/finance/etc. This is a *stricter*
 default than what exists today (which silently assumes owner always).

Telegram and the web UI are untouched — they already carry explicit
identity (chat_id, session cookie) and are out of scope for this gap.


## 4. First implementation slice — small, testable, low-risk

Scoped deliberately to require **no camera hardware** and **no config
flip**, so it can be built, tested, and merged without changing what the
live system does today:

1. `face_id.py`: embedding + matching logic only, unit-tested against a
 couple of small bundled fixture face crops (same-person and
 different-person cases, threshold behavior at the boundary).
2. Enrollment/lookup helper functions operating on `known_people` /
 `unknown_faces` — pure functions, tested against a throwaway sqlite file
 created in the test, no shared state touched.
3. `identify_speaker(device_id)` added to `devices.py`, gated behind
 `presence_identity_enabled` (default `false`). When off — which is every
 existing deployment until someone deliberately changes config — `turn()`
 behaves exactly as it does today. This is a genuine no-op on `main`.

**Rollback story:** because the change is additive and flag-gated, rollback
is either "leave the flag off" or a plain branch/PR revert. No schema
migration risk — `vision.py`'s tables are already `CREATE TABLE IF NOT
EXISTS` and currently unused in production, so there's no data to lose or
migrate backward.

**Explicitly not in this slice:** any actual camera wiring, uStreamer
install, `PresenceWatcher` deployment, or flag flip to `true`. Those follow
once §5's open questions are answered and real camera hardware exists to
test against — proposed as a follow-up ops plan, not bundled into this PR.


## 5. Open questions — need an explicit owner decision before code gets written

1. **Camera hardware.** Nothing is registered or confirmed anywhere in the
 repo or host list. Is the plan a Pi camera module physically attached to
 `touch1` (running uStreamer, matching `vision.py`'s existing docstring
 assumption), a USB webcam on an existing box, or a standalone RTSP
 camera (e.g. a Reolink/similar)? This decides which `FrameSource` path
 gets built out first and whether uStreamer needs installing anywhere.
2. **Where identity matching computes.** My recommendation is local/CPU —
 cheap, no GPU contention, doesn't depend on `simrig` being free of a
 gaming session — versus running it on `simrig` (contends with the
 existing reservation system) versus `laptop1` (per `detector.py`'s
 stale docstring). This needs a decision so the venv/host story for
 `face_id.py` and `detector.py` is settled together, not left
 inconsistent the way it is today.
3. **Embedding model/library choice.** Nothing is installed yet — no
 `insightface` / `face_recognition` / `dlib` / `onnxruntime` in
 `requirements.txt`. Leaning toward a small ONNX face-embedding model that
 runs on CPU without a CUDA dependency, keeping it out of the
 `.venv-vision` chain `detector.py` already needs — but this is a real
 pick with license and install-footprint tradeoffs I want sign-off on
 before it's built into a schema-adjacent module.
4. **Ambiguous-identity policy.** Confirming the intent: when 0, 2+, or
 unrecognized people are in a room, the terminal answers as an
 unauthenticated guest (shared information only), rather than silently
 defaulting to the owner as it does today. This is a stricter/safer
 behavior change from current. Please confirm — note it also means that
 if recognition briefly fails, the owner himself gets guest treatment
 until re-identified, which is a real usability tradeoff against the
 privacy gain.
5. **Enrollment consent scope.** Confirming only the owner and partner
 (plus explicitly invited guests) get enrolled, and enrollment stays
 owner-approved through the existing "who is this?" chat flow rather than
 automatic. This is biometric data, even inside a private home — worth
 being deliberate about who ends up in `known_people` and how those
 embeddings are backed up/protected.


## 6. Risks

- **False-accept** (identity matching says "owner" when it's someone else)
 is the expensive failure mode — it grants private information to the
 wrong person. Mitigate with a conservative similarity threshold, fail-closed
 default (§3), and full audit trail via the existing `vision_events` table
 so every identification decision is reviewable after the fact.
- **False-reject** (owner not recognized) just falls back to guest/shared-only
 treatment — annoying, not unsafe, by construction of the fail-closed policy.
- **Scope creep into Phase 2–4 features** during implementation — the
 slice in §4 is intentionally narrow; open-mic mode, wake-word arbitration,
 and handoff should stay out of any PR that implements this phase.
