# Feature 1: camera-based presence and identity

Status: **built on a feature branch, off by default, needs owner testing before merge.**
Nothing here is wired into a running deployment until `vision_enabled` is set to `true`
in `config.json` and at least one camera is registered.

## What this is

Turns on the presence/identity schema that already existed in `assistant/core/vision.py`
(cameras, vision_events, known_people, unknown_faces) but had never been connected to
anything -- no detector, no face matching, no enrollment flow, nothing initialised at
startup. This feature:

1. Runs YOLO11n (`assistant/core/detector.py`, already existed and already defaulted to
   `yolo11n.pt`) to detect people and pets per camera, gated by motion so an empty room
   costs nothing.
2. Runs InsightFace (new: `assistant/core/face_id.py`) to embed any face YOLO's person
   detections imply is probably visible, and matches it against `known_people` by
   cosine similarity (new matching/enrollment functions in `vision.py`).
3. Surfaces a new/unfamiliar face as a Review-page item once it's been seen a few times
   (default 3, `vision_unknown_face_ask_after`) -- never enrolls silently.
4. Gates personal/business tool access on a voice terminal (`/api/devices/{id}/turn`) on
   whether the identified, present person is actually the owner account.

## How it wires into the existing schema

`assistant/core/vision.py` already had the full schema and a docstring explicitly
planning this ("Identity is a separate, later stage... An unknown face becomes a
question for the owner"). This PR:

- Adds two columns via an idempotent migration (`known_people.linked_user_id`,
  `cameras.device_id`) -- both nullable, so existing rows need no backfill.
- Adds identity functions to `vision.py` itself (`enroll_person`, `add_embedding`,
  `match_embedding`, `upsert_unknown_face`, `apply_face_review_decision`,
  `identify_present`, `camera_for_device`/`set_camera_device`) rather than a parallel
  schema, since this *is* the module the codebase already designated for this.
- Adds `assistant/core/face_id.py` (InsightFace) and `assistant/core/vision_runtime.py`
  (the per-camera pipeline that calls detector.py + face_id.py + vision.py's functions
  and creates Review-page items), following the existing lazy-import/isolated-venv
  convention `detector.py` already established for `.venv-vision`.
- Reuses `business_db.review_items`/`review_options` verbatim (the same table Product
  Concepts/Art Briefs/Listings/Posts already use) -- no schema change needed there. An
  unfamiliar face becomes a `kind="other"` review item with `ref_table="unknown_faces"`,
  options for each known person plus "New person", and the thumbnail as every option's
  preview image (served through the *existing*, unmodified `/api/review/media`
  endpoint, since thumbnails are written under `generated_media_path`).
- Extends `assistant/web/routes/review.py`'s existing per-`ref_table` write-through
  pattern (same shape as `product_concepts`/`art_briefs`/`store_listings`/
  `social_posts`) with one more branch for `unknown_faces`, calling
  `vision.apply_face_review_decision`. **No changes to the Review page's React
  component** -- it already renders generic title/summary/options/note/approve/reject,
  which is exactly the submit_for_review shape this needed.

## The enrollment flow (requirement 3)

1. `VisionRuntime` sees a face that doesn't match any `known_people` embedding above
   `vision_face_match_threshold`.
2. It's recorded as an `unknown_faces` sighting (deduped against other recent
   unresolved sightings on the same camera by embedding similarity, so one lingering
   stranger doesn't create dozens of rows).
3. Once seen `vision_unknown_face_ask_after` times, a Review-page item appears:
   *"Unfamiliar face on <camera>"*, with a thumbnail, one option per existing known
   person ("This is Dug"), and a "New person" option.
4. The owner reviews it like anything else in the queue:
   - Approve an existing-person option -> the face's embedding is added to that
     person's gallery (`add_embedding`), so their recognition improves over time.
   - Approve "New person" **with a name typed in the note field** -> a new
     `known_people` row is created (`enroll_person`).
   - Approve "New person" **without** a name -> nothing is enrolled; the item stays
     answerable again rather than guessing a name.
   - Reject/cancel -> dismissed, never asked about that exact face again.
5. `known_people.linked_user_id` is NOT set by this flow (there's no field for it on
   the Review page, deliberately -- linking a face to an actual Jarvis *account*, which
   is what grants sensitive-tool access at a terminal, is a more consequential decision
   than naming a face). **Needs owner follow-up**: today the only way to set
   `linked_user_id` is directly via `vision.py`'s functions (e.g. a one-off script or a
   REPL) or a small addition to `/api/vision/known-people` if the owner wants a web
   control for it. Left out of this PR on purpose rather than guessing the right UX.

## The gating flow (requirement 4)

The only surface in this codebase where "who is physically speaking" and "who Jarvis
answers as" were previously conflated is the voice terminal (`/api/devices/{id}/turn`,
`assistant/web/routes/devices.py`) -- it always ran every message as the single
configured owner, regardless of who was actually talking. `_resolve_speaker` there now:

- Falls back to today's exact behaviour (owner, full access) when vision is disabled,
  or when the specific device has no camera linked to it via `cameras.device_id` --
  so this cannot silently lock a terminal nobody has configured for vision yet.
- Once a camera *is* linked: looks up `vision.identify_present` for that camera. Full
  access is only granted when the identified person's `known_people.linked_user_id`
  matches the owner account. Anyone else -- unresolved face, an identified but unlinked
  household member/guest, or nobody in frame at all -- gets every personal/business/
  financial context (mail, calendar, Era, Home Assistant, the business tools, Kroger,
  CCXT, LetterStream, etc.) stripped from that turn. What's left is still a working
  assistant, just one that can't read out a calendar or a balance to whoever's in the
  room.

The Telegram bot and the logged-in web chat aren't touched by this gate: both already
know who they're talking to (a Telegram chat ID / a session cookie), so there's nothing
camera-based to add there. This PR's identity gate is specific to the one transport that
had no other way to know who was present.

## What still needs the owner

- **A real camera and a real GPU box.** Nothing here has been run against actual
  hardware -- `vision_face_match_threshold` (0.38) is a documented starting point for
  InsightFace's buffalo_l model, not a calibrated number. Expect to tune it (and
  `vision_unknown_face_ask_after`) against real faces/lighting.
- **Installing `.venv-vision`** on the RTX 3060 box with `requirements-vision.txt`
  (torch/ultralytics were already implied by `detector.py`; insightface/onnxruntime-gpu
  are new).
- **Registering at least one camera** (`POST /api/vision/cameras`, or directly via
  `vision.add_camera`) and, if a voice terminal should be gated, linking it with
  `device_id`.
- **Deciding how `linked_user_id` gets set** for the owner's own face -- see above; this
  PR enrolls people but the extra step of marking one of them "this is the account
  holder" needs a decision on where that control should live.
- **Flipping `vision_enabled: true`** in `config.json` once the above is in place.
- **Enabling `vision.py`'s `recording_preference`/`recordable`** logic if the owner
  wants actual video retention rules -- out of scope for this feature (presence/
  identity only, no recording was requested), but the schema already has the columns
  for it.

Does NOT touch `assistant/core/engine.py` (the chat/tool-dispatch core) at all --
conversational tool-calling for vision (e.g. "who's home right now") is a reasonable
follow-up but deliberately left out of this PR rather than hand-editing that file's
dispatch logic without a full, careful read of it.
