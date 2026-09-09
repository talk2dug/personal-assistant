# Room presence, identity & open-mic conversation — how it's actually wired

Reference notes for this feature area, written so the shape of it never has to be
rediscovered from scratch. Read core/vision.py's own module docstring first — it states
the three design decisions (motion gates everything, cameras are abstract, identity is a
separate later stage) that everything below assumes.

## Status at the start of this work

core/vision.py and core/detector.py already existed on `main` before this feature was
built: the cameras/vision_events/known_people/unknown_faces schema, the motion gate, the
YOLO11n wrapper targeting the RTX 3060. None of it was wired into main.py, web_main.py,
or any route — it was scaffolding, not a running pipeline. There was no face recognition,
no enrollment flow, and nothing calling any of it.

That schema is treated here as the canonical presence & identity schema and extended
additively (new columns would have needed a migration; new tables/functions didn't). No
separate architecture branch or PR for this area was found to reconcile with at the time
this was built — if one exists, its schema should be compared against `known_people` /
`unknown_faces` / `vision_events` in core/vision.py before either is changed further.

## The pipeline

```
scripts/vision_worker.py          (.venv-vision, RTX 3060 host, standalone process)
        |
        v
core/camera_watch.py              VisionRunner -> one CameraWatcher per camera
        |                          motion gate -> detect -> recognise -> record
        |
        +--> core/detector.py      YOLO11n: is there a person/pet, roughly where
        +--> core/face_recognition.py   insightface on the person crop: whose face
        v
core/vision.py                    the schema: vision_events, known_people, unknown_faces
        |
        v
assistant/web/routes/vision.py    read surface: cameras, presence, events, conversation-mode
```

**Process isolation is load-bearing, not incidental.** torch/ultralytics/insightface are
large, CUDA-specific packages that must never become a dependency of jarvis-core.service
or jarvis-web.service — those run wherever the assistant itself runs, which may not even
have a GPU. core/detector.py and core/face_recognition.py both import their heavy
dependencies lazily (inside `_ensure_loaded`), so importing the *modules* is always safe;
only core/camera_watch.py actually calls that path, and only scripts/vision_worker.py
imports camera_watch.py. requirements-vision.txt is a second, separate requirements file
for exactly this reason — install it into `.venv-vision`, never the shared `.venv`.

**Day-one camera coverage is Touch1 and laptop1.** Touch1 is both the existing Pi voice
terminal (device/device.example.json) and, now, a camera (its uStreamer MJPEG feed,
`kind: "mjpeg"`). laptop1 is the host running the assistant itself, watched by its own
attached webcam (`kind: "local"`, `url` is the OpenCV device index as a string, e.g.
`"0"`). Both are configured in config.json's `cameras` list and seeded into the DB at
startup by main.py/web_main.py (`vision.add_camera` is an upsert, so both processes doing
it is harmless — same pattern as seeding users). Adding camera #3 is a config entry, not
a code change.

## Enrollment: a stranger becomes a known person

The house never guesses a name. A face that doesn't match anyone in `known_people`
becomes an `unknown_faces` row (`upsert_unknown_face`); repeat sightings of the *same*
unresolved stranger bump `seen_count` on that same row rather than creating a new one
(matched by embedding similarity, not by camera — someone walks between rooms). Once
`seen_count` reaches `enroll_after_sightings` (default 3) and the row hasn't been asked
about yet, `CameraWatcher._maybe_ask_to_enroll` puts a card on the Review page via
`business_db.create_review_item`, with `ref_table="unknown_faces"` and the thumbnail as
its one option (so Review.jsx renders a photo even though it's a plain approve/reject,
not a pick-one — see that component's `options.length > 1` vs `options.length >= 1`
distinction).

This reuses the review queue's existing submit/decide shape exactly — no new UI, no new
approval mechanism. The owner types the person's name into the note field that's already
there and clicks Approve. routes/review.py's `decide()`:

1. Refuses to record an "approved" decision on an `unknown_faces` item with no note
   (checked *before* the decision is written, since an item can only be decided once).
2. On approve, calls `vision.enroll_known_person` (creates `known_people`, or adds an
   embedding to an existing person of that name) and `vision.resolve_unknown_face`.
3. On reject, does nothing further — the row stays `asked=1`, so the house won't ask
   about the same face again on its own. (There's no "ask again later" path yet; if the
   owner wants to revisit a rejected stranger, that's currently a direct DB/tool action,
   not a UI affordance. Worth adding if it comes up in practice.)

## Open-mic conversation mode

A voice terminal (device/jarvis_device.py) normally waits for "hey Jarvis". While a
*recognised* known person is in view of that device's camera, it doesn't: the wake-word
branch of its state machine is bypassed and any incoming audio frame starts an utterance
directly. This is a poll, not a push — the device asks
`GET /api/vision/conversation-mode/{device_id}` every `OPEN_MIC_POLL_SEC` (4s), the
server answers from `vision.presence_now()` + `vision.known_people_present()` joined
through `device_camera_map` (which camera watches which device's room — day one, the
identity map, since Touch1 and laptop1 are each their own camera and their own
terminal). An unrecognised person in frame does **not** enable it; a false detection or a
stranger triggering an always-listening terminal is the expensive mistake here, the same
reasoning as detector.py's confidence gating and vision.py's conservative match
threshold.

The device side is one extra branch in the existing state machine, not a second one:
leaving "record" always returns to "wake", and if open-mic is still active the very next
frame re-triggers listening immediately (see the top of `handle()` in jarvis_device.py).
No chime plays per open-mic utterance — a ding on every turn of a continuous conversation
is worse than the thing it would be signalling.

## What's deliberately not in this change

- **Chat-based presence queries** ("who's home right now"). `vision.known_people_present`
  exists and is exposed at `GET /api/vision/presence`, but nothing wires it into
  engine.py's tool-calling loop yet. engine.py is large and under active development
  elsewhere; adding a `vision` tool context there is a clean, small follow-up once this
  lands, following the same `*Context` dataclass + `build_*_context` pattern every other
  integration in core/setup.py already uses.
- **A dedicated Presence page in the web UI.** The task asked specifically for enrollment
  to surface on the existing Review page, which it does. `/api/vision/presence` and
  `/api/vision/events` are there for a future page but nothing in web/src consumes them
  yet.
- **Persistent RTSP/local capture connections.** FrameSource opens and releases per
  snapshot, same as the pre-existing RTSP placeholder — fine at the motion-gated poll
  rate this runs at (`camera_poll_seconds`, default 1.5s), worth revisiting if a camera
  needs tighter latency.
- **Recording.** `cameras.recordable` and `known_people.recording_preference` exist in
  the schema (inherited from the pre-existing design) but nothing in this change writes
  video anywhere — the whole pipeline only ever produces small face-crop thumbnails for
  the enrollment review card.
