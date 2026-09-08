# Watchdog System — Making Jarvis Proactive

Status: **Phases 1-5 built and live** (quieted alert flooding; task/Review/GitHub watchdogs; diff-scope safety check; auto-merge governance). Phases 6-7 (local SSH/MCP-install access, proactive-contact tie-together) remain. See personal project 15, "Jarvis Operations: Watchdog & Dev-Team Autonomy".

## 1. The problem, stated precisely

Jarvis today has no standing presence except two systemd services that are already
always-on: `jarvis-core.service` (Telegram bot + an APScheduler `BackgroundScheduler`
in `assistant/core/scheduler.py`) and `jarvis-web.service` (FastAPI + dashboard). So the
claim "Jarvis has no background process" is half true — there **is** a process and a
clock (`poll_interval_seconds`, `location_poll_seconds`, `caldav_sync_interval_seconds`,
`research_queue_interval_minutes`, etc. are all live jobs on that scheduler right now).
What's missing is that this clock only watches a few things: due reminders, owner GPS,
Apple Calendar, Era's cache, the business agent cadence, and personal research. It does
**not** watch:

- The Review queue (`business_db` review items, surfaced at `assistant/web/routes/review.py`)
- Personal task due-dates (`personal_tasks.due_at` in `assistant/core/personal_db.py` —
  the column exists, nothing reads it against "now")
- Anything on GitHub (no GitHub integration exists anywhere in this codebase today)

So this isn't "give Jarvis a pulse" — it's "extend the pulse he already has to cover the
sources that currently have none." That reframing matters for scope and risk: most of
this is additive work inside a proven pattern, not new infrastructure.

## 2. Architecture options

### Option A — Extend the existing scheduler (polling, in-process)

Add new jobs to `scheduler.start()` the same shape as `_tick` (reminders) or
`_location_tick` (routines): a function on an `interval` trigger that reads a table,
diffs against "already handled," and acts.

**Requires:** nothing new. No new hosting, no new process, no new auth. Just new
functions in `personal_db.py`/`business_db.py` (e.g. `due_tasks()`, analogous to the
existing `db.due_reminders()`) and a new `scheduler.add_job(...)` call.

**Change-detection strategy:** exactly the pattern already used twice in this codebase:
a nullable `*_at` marker column (`sent_at` on reminders, `last_fired_at`/`done_at` on
routines) that the poll queries against and then stamps. Idempotent by construction —
a tick that fires twice before the stamp lands is the only failure mode, and the
existing code already guards for it (see the comment in `_location_tick` about marking
a routine fired *before* running it, precisely to stop a slow/failing run from being
retried into a repeat-fire storm).

**Latency:** bounded by poll interval. Reminders poll every 30s today; Review/task
polling at 30–60s is indistinguishable from instant to a human.

**Cost:** negligible — a SQLite `SELECT ... WHERE due_at <= ?` every N seconds on a
database already this small.

**Failure modes:**
- *Missed events*: only if the service is down across the whole window, and it
  self-heals on restart because the query is `<= now()`, not `== now()` — anything
  overdue is caught on the next tick after boot. This is the same reasoning already
  written into the research-queue job comment ("a few restarts in an afternoon could
  starve it indefinitely").
- *Duplicate triggers*: possible if a job dies after acting but before writing the
  marker. Mitigated by marking-before-acting for anything whose action itself might be
  slow (matches the routine pattern), not after.
- *Backlog on restart*: not really a failure mode here — a diff query naturally
  processes everything overdue in one tick; there's no queue to drain.

This is the boring, proven option, and it's a straight extension of code that already
works in production for reminders and location routines.

### Option B — Webhooks / push triggers

Two very different cases hide under this label and they should be treated separately:

**B1: State Jarvis itself writes (Review approvals).** When the owner approves a Review
item, the write happens in `review.py`'s `decide()` handler, in Jarvis's own process.
There is nothing to "detect" here — Jarvis already knows the instant it happens, because
it's the one doing it. The right move is not polling *or* a webhook, it's an **in-process
event hook**: after `business_db.decide_review_item(...)` succeeds, call directly into
the same `handle_message`/`notify` path a routine uses. Zero latency, zero new infra,
zero new failure modes (it's a synchronous function call already inside a try/except
web handler).

**B2: State an external system writes (GitHub PR/CI status).** Jarvis does not control
the write path, so this is a genuine webhook case. It **requires**:
- A new FastAPI route (`assistant/web/routes/github.py`), same shape as the existing
  `/api/notification-action` bearer-token pattern in `notifications.py`, but verified
  with GitHub's HMAC webhook signature instead of a static bearer token.
- **A publicly reachable HTTPS endpoint.** This is the real cost, and it doesn't exist
  today: per `deploy/tailscaled-jarvis.service` and `config.example.json`, Jarvis runs
  on a Tailscale-private tailnet plus LAN, with no public ingress. GitHub's webhook
  servers are on the open internet and cannot reach a Tailscale-only host. Options to
  close that gap: **Tailscale Funnel** (lowest lift, since Jarvis already has its own
  tailnet identity — exposes one path publicly without a new box), or a small external
  relay (Cloudflare Tunnel / a cheap VPS forwarding over Tailscale to the Pi). Either
  way, this is new attack surface that doesn't exist in the current deployment and
  needs its own review, separate from the watchdog logic itself.

**Latency:** near-instant (seconds) for B2, and truly instant for B1.

**Failure modes (B2 specifically):** GitHub retries failed deliveries for a limited
window, then drops them — an outage longer than that window loses the event
permanently unless something backstops it. Duplicate delivery is common and expected
(GitHub's own docs say so) and must be de-duped on the `X-GitHub-Delivery` id. None of
this applies to B1 since it's not "delivery" at all, just a function call.

### Option C — Hybrid (recommended)

- Internal state Jarvis owns (Review decisions, task due-dates, reminders): **event
  hook where Jarvis is the writer (B1), polling where it's a due-date the clock has to
  notice (A).** No new infrastructure either way.
- External state Jarvis doesn't own (GitHub): **poll GitHub's REST API** on the existing
  scheduler rather than standing up public webhook ingress. A `github_client.py`
  alongside the other API clients (`market_data.py`, `home_assistant_client.py` are the
  templates), authenticated with a PAT or GitHub App token stored in config the same
  way `era_api_key` is, polling tracked PRs/repos every 2–5 minutes and diffing against
  a last-seen-state cache (status, mergeable, checks conclusion) keyed by PR number.
  GitHub's REST rate limit (5,000 req/hr authenticated) is far more than this scale
  needs. This gets 90% of the value of a webhook with none of the public-ingress cost
  or new attack surface, and matches every other integration pattern already in this
  codebase. Revisit true webhooks only if a specific workflow genuinely needs
  sub-minute latency on a PR event — at that point Tailscale Funnel is the concrete
  next step, not a redesign.

## 3. Handing a detected event to Jarvis

The codebase already has the right shape for this, twice over:

- **Routines** (`db.due_routines` → `handle_message(db_path, llm, owner_id, routine["prompt"], ...)`
  → `notify(chat_id, reply)`): a location transition is detected, a stored prompt is run
  through Jarvis's normal reasoning/tool loop, and the reply is pushed to Telegram.
- **HA notification actions** (`notifications.py`'s `JARVIS_ASK`/`REPLY` branch): an
  external trigger (a phone notification tap) is turned into free text and run through
  the exact same `handle_message` entrypoint a typed chat message uses.

The watchdog should do nothing new here — it should be a third caller of the same two
primitives:

- **Events that need judgement** (a PR's CI failed and someone should look at why; a
  Review item sat pending for 3 days) → synthesize a short natural-language prompt
  describing what changed ("PR #142 'Add print-station port' just failed CI on the lint
  step — want me to look at the log?") and run it through `handle_message`, then
  `notify()` the reply. This lets Jarvis actually reason about the event and decide
  whether/how to act, using his normal tool-calling, not a special watchdog code path.
- **Events that are purely mechanical** (a task's due-date arrived; a reminder is due)
  → skip the LLM round-trip entirely and `notify()` directly, exactly as
  `due_reminders()` does today. No reason to spend a model call telling the owner a
  task is due when the due-date table already says so.

One entrypoint, one notify path, three callers (chat, routines, watchdog). No second
"automation language" to build or maintain — which is the same argument already made
in `db.py`'s comment on why routines run a prompt instead of inventing one.

## 4. The safety boundary — non-negotiable

This is the part I want to be explicit and conservative about, because it's the one
way this feature could go wrong.

The codebase already has a confirmation gate that is deliberately singular:
`pending_actions` + `_resolve_pending_action` in `assistant/core/engine.py`, driven off
config-listed sensitive-tool names (`era_sensitive_tools`, `phone_sensitive_tools`,
`mail_sensitive_tools`, `ha_sensitive_domains` in `config.example.json`). Whenever the
model tries to call one of those tools, the call is staged into `pending_actions` with
status `awaiting_confirmation` instead of executing, and only resolves on an explicit
`yes`/`no` — typed in chat, or tapped on a phone notification button. `notifications.py`
is explicit about why there's only one resolution path: *"Deliberately the same
function a typed reply goes through, so a phone approval can't become a second, weaker
path around the gate."*

Because the watchdog's only way to act is to call `handle_message` — the same
entrypoint chat and routines already use — it inherits this gate automatically. A
watchdog-synthesized prompt that leads the model to attempt a sensitive tool call
still lands in `pending_actions` and still waits for the owner, with no code path
available for it to mark its own trigger as the owner's confirmation. Concretely, this
means:

- **The watchdog never gets a shortcut.** It is not given, and should not be given, any
  ability to write `status = 'confirmed'` into `pending_actions`. That write only ever
  happens from `_resolve_pending_action`, called from a real chat reply or a real
  notification-button tap — both owner-initiated, both requiring the owner to be the
  one who acted at that moment, not to have merely approved a *category* of things
  earlier.
- **New sensitive-action categories the watchdog will newly surface must be added to
  the existing sensitive-tool config lists before this ships**, not left implicit.
  GitHub PR merges are the clear one: whatever tool ends up doing a merge needs to sit
  in an equivalent `github_sensitive_tools` list (mirroring `era_sensitive_tools` etc.)
  from day one, so "Jarvis noticed the PR is green" can never become "Jarvis merged the
  PR" without the owner explicitly saying yes in the moment. The same applies to
  anything the watchdog might reach that grants elevated capability, moves money, or
  releases outbound communication — the existing gate already covers `send_sms`,
  `send_email`, and Era's account-management tools; it needs to be checked against the
  new surfaces the watchdog opens up, not assumed to cover them by default.
- **Note on scope of what I verified:** I confirmed this gate's existence and its
  single-path design from `notifications.py`, `db.py`'s `pending_actions` schema, and
  the sensitive-tool lists in `config.example.json`. `engine.py` (where the gate is
  actually enforced around tool dispatch) is too large to have read in full here — I'd
  read it completely before writing the code that wires the watchdog into it, to
  confirm the enforcement really is centralized per-tool-name and not something that
  could be bypassed by a code path that doesn't go through the normal dispatcher.

## 5. Recommended MVP

**Scope:** Review queue (event hook, not polling — it's free) + personal task
due-dates (polling, identical shape to the existing reminders job). Explicitly
**excludes** GitHub for the first cut.

Why this slice: both pieces reuse patterns that already work in production
(`due_reminders`/`_tick` for tasks; the write-through pattern already in `review.py`
for Review), touch no new infrastructure, and cover the two sources the owner
specifically flagged as painful ("a Review item being approved" and "a task coming
due"). GitHub is excluded because it's the one piece that isn't a small extension — it
needs a new client, new credentials, and (if ever pushed to webhooks) a public-ingress
decision that deserves its own conversation.

**Rough build estimate:**
- Review-approval event hook (in-process, `review.py` + notification path): ~0.5 day
- Task due-date polling job (`personal_db.due_tasks()`/mark-notified + scheduler job,
  mirroring `db.due_reminders()`): ~1 day
- Testing (restart-recovery/no-duplicate-fire, timezone edge cases, owner-only scoping): ~0.5–1 day
- **MVP total: ~2–3 days.**

**Phase 2 (separate proposal once MVP is live):** GitHub PR/CI polling via a new
`github_client.py` and scheduler job, ~2–3 days including the sensitive-tool-list work
for merges. **Phase 3 (only if latency requires it):** true GitHub webhooks, which is
really "stand up Tailscale Funnel and prove it's safe" as a prerequisite — a separate,
larger piece of work I'd want its own sign-off on given it changes the network exposure
of the Pi.

## Open questions for the owner

1. Is 30–60s latency on Review/task detection acceptable, or is there a specific case
   that needs faster?
2. For GitHub: is polling-latency (2–5 min) acceptable indefinitely, or is there a
   known workflow (e.g. wanting to be pinged the second CI goes red before a meeting)
   that would justify paying for public webhook ingress sooner?
3. Confirm which GitHub actions beyond "merge" should be treated as sensitive
   (e.g. should Jarvis be allowed to auto-comment or auto-label without confirmation,
   while merge and branch-deletion always pause)?


## Implementation status (2026-09-08)

This design shipped, in order: the notification-flooding fix (Phase 1), the Review/task watchdog MVP recommended above (Phase 2), the GitHub PR/CI watchdog this doc scoped as "Phase 2" in its own recommended-MVP section (Phase 3 here), a new diff-scope safety check not originally scoped in this doc at all -- added because this project hit three real instances of `main` silently losing shipped code, which made "trust green CI alone" for auto-merge insufficient (Phase 4) -- and the auto-merge governance change to `git_merge_pr` itself, gated on both (Phase 5). This PR is itself a live test of that last piece.
