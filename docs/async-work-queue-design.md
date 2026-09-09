# Async work queue for employee/agent assignments

## Status (2026-09-09)

Shipped for the on-demand path (§10 steps 2-3, compressed into one change rather than a
dual-write migration): `assistant/core/work_queue.py` adds a `work_queue` table plus a
single background worker thread, started in `main.py` once the Telegram notifier exists.
`assign_work`'s tool handler (`business_tools.py`) now validates the employee
synchronously (a bad key or a paused employee is the caller's mistake, worth surfacing in
the same turn) then enqueues and returns immediately; the worker claims the row, runs
`staff.assign()` exactly as before, and notifies the owner with the deliverable (or the
failure) as a follow-up message once it actually finishes. Poll interval is 12s, matching
§7's "10-15s"; per-job timeout reuses the already-shipped `staff_assignment_timeout_seconds`
knob (see §9's own §1 correction below) rather than a new schema column.

**Update**: §8's cadence-tick flip has since shipped too. `staff.run_due` now takes an
optional `work_queue`; when given (the normal case — `scheduler.py`'s `_staff_tick` passes
`business.work_queue`), each due employee is enqueued as `kind='cadence'` instead of run
inline, and `run_due` returns immediately per employee rather than blocking the tick.
`due_for_cadence` has no visibility into the queue, so `run_due` calls `work_queue.
has_pending(employee_key)` before enqueueing — without it, an assignment that outlives one
5-minute tick (routine for real dev-team work) would get piled on again every tick until
the first copy drains. The alert-policy/cooldown decision that used to run inline right
after `assign()` returned is now `staff.handle_cadence_outcome`, called once by the worker
after the async job actually finishes, with the identical logic (and identical message
formatting, via the new shared `staff.format_alert_text`) as before — the two paths
(work_queue given vs. not, the latter kept as a fallback and still exercised by every
pre-existing `run_due` test) were verified to produce the same alert decisions given the
same inputs. `main.py` builds a `cadence_notify` closure the same shape as `scheduler.py`'s
`_staff_alert` and passes it to `start_worker` alongside the plain on-demand `notify`.

**Update**: §10 step 5 (split the timeout budget) has since shipped too. `claude_timeout_seconds`
— which bounds only `converse()`/`chat()`, the owner's own live turn — is now **90s**, down
from 300s, in `assistant/config.py`'s dataclass default, `load_config`'s fallback,
`config.json`, and `config.example.json`. `research()`/`engineer()`/`assign()` already
override the client's timeout per-call with their own much larger budget
(`staff_assignment_timeout_seconds`, 10800s), so nothing background-facing shares this
number — it was safe to tighten in isolation. All 925 tests pass unmodified.

Still not built: the process-group kill and stream-json heartbeat instrumentation in §6/§9
(exit_code, heartbeat_at) — real value for diagnosing a *hang* specifically, but
`staff.assign()`'s own broad try/except already turns a timeout into a clean `failed` row
today, which is what the owner-facing incident actually needed fixed.

**Correction to §1**: the design doc's own read of the numbers was slightly off, not the
diagnosis. `assign()`'s subprocess timeout was already a separately-configurable
`staff_assignment_timeout_seconds` (default 10800s) by the time this was implemented, not
the global `claude_timeout_seconds` (300s) — a same-day fix (commit `8aabc98`) had already
split them, for the identical reason this doc gives in §1. Both fixes were made in the
same local session and one was pushed to a stale local `main` that never reached origin;
recovered and pushed alongside this change. The root cause and its consequence
("orchestrator dies, the side effect keeps running unwatched") were exactly right —
`claude_timeout_seconds` (bounding the owner's live chat turn) still fires long before a
real dev-team assignment finishes, which is precisely what this queue decouples.

---

Why: the synchronous `assign_work` call (and the `staff_cadence` background tick that
calls the same path) has been observed to time out — specifically the developer
assignment for the git "checkout existing branch" fix, task 9 on Jarvis Platform
Engineering — while the underlying work may still be completing. This proposes
replacing the synchronous wait with a durable queue + short-interval poller, and lays
out how to actually tell apart *why* a given call timed out instead of just tolerating
it.

## 0. What I read, and what I could not

Verified directly from the repo (`scheduler.py`, `claude_cli.py`, `config.py`,
`web/routes/agents.py`, `agents.py`, `personal_agents.py`, and the header of
`staff.py`):

- The employee/agent invocation ultimately bottoms out in `ClaudeCLIClient`
  (`assistant/core/claude_cli.py`), which shells out to the `claude` CLI via
  `subprocess.run(command, ..., timeout=self.timeout)` and blocks the calling thread
  until it returns, raises, or the timeout fires.
- `self.timeout` comes from one global config knob, `claude_timeout_seconds`, default
  **300 seconds**, set once when the client is constructed (`config.py`). There is no
  per-task-type override for `converse()` (the tool-calling path). `research()` is the
  only call site that overrides it per-call (600–900s for background research agents).
- The background employee run loop is an APScheduler job, `staff_cadence`
  (`scheduler.py`), ticking every 5 minutes, calling `staff.due_for_cadence` then
  `staff.run_due`, guarded against exceptions, with `max_instances=3` and
  `misfire_grace_time=300`. Whatever `run_due` does per employee, it does it
  synchronously inside that tick, on the same blocking-subprocess primitive above —
  the same pattern used identically in `agents.run_market_agent` /
  `run_trend_agent` / `run_research_queue` and `personal_agents.run_personal_research_queue`
  (start a run row → one blocking LLM call → write the result to the row → return).
- `web/routes/agents.py` confirms the shape of the work-tracking table hired staff
  already write to (`staff_work`, read via `staff.recent_work`): at minimum a `key`,
  an `assignment` text, a `status` (`running` / `delivered` / `failed` seen directly;
  the docstrings imply more), `started_at`, `finished_at`.
- `claude_cli.py`'s `converse()` (used for anything that needs Jarvis's own MCP tools)
  reaches those tools through a spawned MCP-bridge subprocess that forwards each tool
  call over HTTP to the **always-on** Jarvis web process (`claude_tools_url`, default
  `http://127.0.0.1:8080/api/tools/call`). That web process is a separate long-lived
  service from the short-lived CLI subprocess `claude_cli.py` spawns and kills.

**Could not verify** (tool output for these files exceeded what I could retrieve this
session — `staff.py` is ~53KB and `engine.py`/`business_tools.py` are 70–80KB, all past
the size this session's file-read tool would return in full): the actual body of
`assign_work`, `run_due`, and the capability-tier → tool-grant mapping that decides
whether a hired "developer" employee's LLM call is agentic (`converse()`, real MCP
tool access up to its tier) or a tools-free one-shot call (`research()`/`chat()`-style,
text-only output for the owner to review, matching `staff.py`'s own docstring that
"employees produce, they do not act"). This matters for part of the root-cause
analysis below, and a developer picking this up should confirm it by actually reading
`staff.py::assign_work` before implementing — the queue design itself does not depend
on the answer, but the process-management fix in §3 does.

## 1. Root cause of the observed timeout (task 9)

Three candidate explanations were named in the assignment; here is how to tell them
apart, and which one the evidence actually points to.

| Candidate | Evidence for/against |
|---|---|
| **(a) Client-side call timeout** | **Most likely.** `claude_cli.py` enforces a single global 300s wall-clock budget on the whole synchronous call via `subprocess.run(timeout=...)`, with no override for `converse()`. A "fix an existing-branch checkout bug" task plausibly needs several tool round-trips (list files, read file, write/push a branch, maybe more) plus model reasoning between them — 5 minutes is a tight budget for that *class* of task, and the failure is deterministic given that budget, not a bug in the usual sense. |
| **(b) Actual hang in execution** | Can't be fully ruled out without more instrumentation (see §3.3), but there's no evidence of one, and (a) alone is sufficient to explain the symptom without needing an infinite hang — the process was very likely still doing real, bounded work when it was killed at 300s. |
| **(c) Dropped response** | Unlikely as the *primary* cause. A hard kill via `subprocess.run(timeout=...)` on `TimeoutExpired` doesn't typically leave a parseable-but-wrong JSON on stdout for `claude_cli.py` to mis-handle; it correctly surfaces as "the request took longer than 300s." A parse failure would show up as a *different*, distinguishable error ("could not parse the CLI's response"), not a timeout. |

**Why "the work may still complete" is plausible even though (a) is the cause:**
`subprocess.run`'s timeout kills only the *immediate* child process/handle it holds.
If that child (the `claude` CLI, or the MCP-bridge subprocess it spawned) has itself
fanned out further work — in particular, if a tool call had already been dispatched
over HTTP to the always-on web process before the 300s mark — that web process keeps
running the real action to completion regardless of whether the CLI orchestrator that
asked for it is still alive to receive the reply. Jarvis reports "timed out" because
the *orchestrating* call died; the *side effect* it triggered may land seconds or
minutes later with nothing watching for it. This is the textbook failure mode of
"kill the parent, not the process tree," and it is fixable independently of the queue
work below (§3.3).

**Bottom line:** this was very likely a real, reproducible client-side timeout caused
by applying one 300-second budget, tuned for a quick chat reply, to a structurally
longer class of task (unattended developer work). It is not evidence of a flaky or
hanging agent, and it doesn't need to be tolerated — it needs a bigger, separately
configurable budget for background/assignment work, decoupled entirely from the
synchronous request path that produced the symptom.

## 2. Proposed architecture

Replace "call the agent and block until it returns" with three independent pieces
that only interact through a durable table:

```
  chat / staff_cadence tick               background worker                  poller (existing scheduler)
  ------------------------               ------------------                 ---------------------------
  assign_work(key, assignment)  ---->    claim oldest 'queued' row  ---->    every 10-15s:
    INSERT staff_work                     UPDATE status='running'             SELECT rows where status in
    status='queued'                       spawn claude CLI (async,             (done, failed, timeout)
    return immediately                    own process group)                  AND NOT delivered
    ("I've handed this to Dana,                                               for each: notify owner /
     I'll update you")                    on exit: UPDATE row with            advance linked task or PR /
                                           status, result, exit_code,          mark delivered_at
                                           finished_at
```

The key change from today: **nothing that talks to the owner or to chat ever waits on
the CLI subprocess.** The subprocess's only job is to eventually write one row. Every
consumer of that row (chat replies, the pixel-office UI, the daily digest, task/PR
advancement) reads it back out on its own schedule.

## 3. Storage backend: a SQLite table, not Redis

Evaluated against the stack as it actually exists (single user, single box, already
SQLite via `sqlite3` everywhere, APScheduler in-process, no message broker anywhere in
the codebase today):

| | SQLite table (recommended) | Redis (queue/streams) | Postgres |
|---|---|---|---|
| New infra to run/monitor/back up | None — same `jarvis.db` already opened everywhere | A new service, its own process supervision, its own "is it up" failure mode | A new service, same operational cost as Redis |
| Durability across a restart | Free — it's a file on disk, already how every other run-history table in this codebase works (`agent_runs`, `business_research`, `staff_work` itself) | Only with AOF/RDB tuned correctly; default configs are lossy | Free |
| Throughput this system needs | A handful of assignments a day, poll every 10–15s — nowhere near a SQLite table's ceiling | Massive headroom this system will never use | Massive headroom this system will never use |
| Atomic "claim a job" | `UPDATE ... WHERE id=? AND status='queued'` + check `rowcount`; SQLite's single-writer lock makes this trivially race-free for the concurrency levels here | Native (`BLPOP`/streams), better fit at high concurrency | `SELECT ... FOR UPDATE SKIP LOCKED`, better fit at high concurrency |
| Fits existing code | Identical shape to every other table in `staff.py`/`business_db.py`/`db.py` — same connection pattern, same migration story | Introduces a second data model and a second backup story for state that's otherwise all in one file | Already used for other things in this stack? (not observed — the codebase is SQLite-only today) |

**Recommendation:** extend the existing `staff_work` table (it is already, functionally,
an un-queued queue: one row per assignment, a status column, timestamps) rather than
stand up a new backend. Redis buys latency and throughput this system has no use for,
at the cost of a new thing that can silently die on a personal box nobody is paging for.
If usage ever grows into genuinely concurrent, high-frequency job processing, the
natural next step is Postgres with `SKIP LOCKED` — not Redis — because the rest of the
stack would presumably have moved there too. To keep that door open cheaply: put the
enqueue/claim/complete/fail operations behind a small `work_queue.py` module with a
plain function API, so a future backend swap is a one-file change, not an
application-wide rewrite.

**One concrete implementation note:** enable WAL mode (`PRAGMA journal_mode=WAL`) on
`jarvis.db` if not already on — it lets the poller's reads and the worker's writes
proceed without blocking each other, which matters once both are ticking independently
rather than sharing one call stack.

## 4. Schema

Extend `staff_work` (or, if the existing table's columns turn out to be more
constrained than the header comment in `staff.py` suggested, create a new
`work_queue` table with a foreign key to `staff_work` for the human-readable
assignment metadata — a developer should check the live column list before choosing
between "extend" and "new table next to it"):

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `employee_key` | TEXT | matches `staff.key` (or `AGENT_ROSTER` key for the built-in agents) |
| `assignment_id` | TEXT/INTEGER, nullable | back-reference to whatever this work is *for* — a task id, a PR number, an eng-board ticket. Lets the poller advance the right downstream object without re-parsing free text. |
| `kind` | TEXT | `on_demand` \| `cadence` \| `research` — distinguishes a chat-triggered assignment from a scheduled cadence run for reporting/debugging |
| `status` | TEXT | `queued` \| `running` \| `done` \| `failed` \| `timeout` |
| `payload` | TEXT (JSON) | the assignment/prompt text and any structured input |
| `result` | TEXT (JSON), nullable | the finished output, or the last partial fragment captured before a timeout/kill |
| `error` | TEXT, nullable | exception message or CLI stderr, separate from `result` so success and failure don't share a column meaning |
| `exit_code` | INTEGER, nullable | the subprocess's actual return code, when we got one — critical for telling "process finished but produced garbage" from "process never finished" (§5) |
| `created_at` | TEXT (ISO) | enqueue time |
| `started_at` | TEXT (ISO), nullable | when a worker claimed it |
| `heartbeat_at` | TEXT (ISO), nullable | last sign of life from the worker (see §5) |
| `timeout_at` | TEXT (ISO) | `started_at` + this job's budget, computed at claim time — not the global 300s, a per-kind budget (§6) |
| `finished_at` | TEXT (ISO), nullable | terminal timestamp, any terminal status |
| `delivered_at` | TEXT (ISO), nullable | set by the poller once it has surfaced this row to the owner/downstream system — this is what makes polling idempotent |
| `attempt` | INTEGER default 1 | for an eventual retry policy; not required for v1 |

Status transitions are one-directional and each has exactly one writer:
`queued → running` (worker claims), `running → done|failed` (worker finishes),
`running → timeout` (watchdog only, never the worker — see §5), any terminal status
`→ delivered_at set` (poller only, never changes `status` itself).

## 5. Producer side: `assign_work` becomes non-blocking

```python
def assign_work(db_path, employee_key, assignment_text, assignment_id=None, kind="on_demand"):
    work_id = work_queue.enqueue(db_path, employee_key, assignment_text, assignment_id, kind)
    return {"ok": True, "work_id": work_id,
            "message": f"Queued for {employee_key}. I'll let you know when it's done."}
```

This is the one behavior change the owner will actually notice in chat: instead of
Jarvis going quiet for up to 5 minutes and then reporting success/timeout in the same
turn, he gets an immediate acknowledgement, and the real result arrives as a follow-up
message once the poller sees it — the same pattern `request_research` /
`request_personal_research` already use today for research, just applied to staff work
too.

## 6. Worker

A single long-lived loop (its own thread, started alongside the scheduler, not another
APScheduler interval job — it needs to hold a live subprocess handle across ticks,
which a stateless interval callback doesn't fit well):

1. Poll for the oldest `status='queued'` row (`ORDER BY created_at LIMIT 1`), claim it
   atomically (`UPDATE ... WHERE id=? AND status='queued'`, check `rowcount==1`).
2. Compute `timeout_at` for this row from a **per-kind** budget, not the global chat
   timeout: e.g. `on_demand`/developer-style work gets 30–45 minutes, quick
   classification-style calls keep something close to today's 300s. This directly
   fixes the root cause in §1 — background work stops sharing a budget with
   interactive chat.
3. Spawn the CLI **asynchronously** (`asyncio.create_subprocess_exec` or a `Popen` the
   worker polls, not a blocking `subprocess.run`), with `start_new_session=True` (POSIX)
   so the whole process group can be killed together, not just the immediate child —
   this is the fix for the "work may still complete after we gave up on it" half of
   the root cause.
4. Prefer `--output-format stream-json` over the current `json` mode so the worker can
   read output incrementally and update `heartbeat_at` on every turn, rather than only
   finding out anything happened when the whole process exits. This is what makes a
   "still working, on turn 6 of a long task" distinguishable from "nothing has happened
   in 10 minutes."
5. On exit: write `result`, `exit_code`, `finished_at`, `status='done'` or `'failed'`.
6. On an internal exception before the subprocess even starts: `status='failed'`,
   `error` set, immediately — don't let a bug in the worker itself masquerade as a
   timeout.

## 7. Poller (10–15s, on the existing scheduler)

A small addition to `scheduler.py`, same shape as every other job already there:

```python
scheduler.add_job(
    _guarded_simple("work_queue_poll", lambda: work_queue.deliver_finished(db_path, notify)),
    "interval", seconds=12, id="work_queue_poll",
)
```

`deliver_finished` selects rows where `status IN ('done','failed','timeout') AND
delivered_at IS NULL`, and for each: notifies the owner (reusing the existing
`notify(chat_id, text)` callback every other job already uses), advances whatever
`assignment_id` points to (marks a task done, comments on a PR, whatever the caller
registered), and sets `delivered_at`. Nothing here talks to the CLI or the worker — it
only ever reads rows the worker already finished writing, which is what makes this
safe to run on a short interval indefinitely.

A **separate** watchdog check runs in the same tick (or its own, it's cheap either
way): rows with `status='running' AND timeout_at < now()` get marked `status='timeout'`
(never touched by the worker itself, so a slow-but-alive worker can't race the
watchdog into double-writing the same row) and are surfaced to the owner explicitly as
*stuck*, distinct from a normal failure: "Developer task 9 has been running for 42
minutes with no update — it may be stuck," rather than silently hanging forever or
silently disappearing.

## 8. Interaction with existing cadences

- **On-demand (chat)**: `assign_work` enqueues and returns immediately (§6). No change
  to *what* gets asked for, only that the answer comes back as a follow-up rather than
  in the same turn.
- **Interval/daily/weekly (`staff_cadence`, every 5 min via APScheduler)**: today
  `run_due` loops over due employees and blocks on each one in turn, which is exactly
  why `max_instances=3`/`coalesce=True` exists — it's a workaround for one slow
  employee blocking the tick, not a fix. Under this design, `run_due` changes from
  "run each due employee and wait" to "enqueue a `kind='cadence'` row for each due
  employee and move on" — the tick becomes as fast as N inserts, and the single worker
  loop (§6) is what actually processes them, at whatever pace the CLI can sustain.
  `max_instances`/`coalesce` on the scheduler job stop being load-bearing once the tick
  itself can't hang.
- **Business agents** (`market_agent`, `trend_agent`, research queues, creative
  pipeline): same treatment is optional but recommended for consistency — they already
  journal to `agent_runs` the same way `staff_work` does, so the same `work_queue`
  pattern (or a thin adapter over the existing table) applies without a schema change
  in spirit, just a second producer.
- **Priority**: for a single-user system doing a handful of jobs a day, plain FIFO with
  one worker is sufficient. If on-demand requests ever need to jump ahead of a backlog
  of cadence jobs, add a `priority` column and order by `(priority, created_at)` — not
  needed for v1.

## 9. Diagnosing a timeout going forward (not just tolerating it)

With the schema above, a stuck/finished-late job is no longer a mystery:

1. **`exit_code IS NOT NULL`** but `status='timeout'`: the process actually finished
   (with some exit code) after the watchdog gave up on it — pure race, budget was too
   tight, no process-management bug. Fix: raise the per-kind budget.
2. **`exit_code IS NULL`** at watchdog time, and the OS-level PID (logged at claim
   time) is confirmed gone (check on the host): the process died silently without our
   worker's kill — a real crash, worth its own alert distinct from a timeout.
3. **`exit_code IS NULL`**, PID still alive, `heartbeat_at` advancing: genuinely still
   working — this is (b) from §1, a real long-running task, not a hang. Don't kill it;
   let the per-kind budget run out normally or extend it.
4. **`exit_code IS NULL`**, PID still alive, `heartbeat_at` **not** advancing past
   several stream-json turns' worth of time: a genuine hang — now distinguishable from
   "just slow," and worth escalating differently (e.g. flag the specific tool call it
   was last waiting on, from the last streamed event).
5. Cross-reference the web process's own `/api/tools/call` request log (add a request
   id if one doesn't exist) against `work_queue` rows for agentic assignments: this is
   how you confirm or rule out §1's "the side effect landed after we gave up" theory
   for any specific incident, rather than assuming it.

## 10. Migration plan

1. **Instrument first, change nothing.** Add `heartbeat_at`/exit-code capture and
   switch to `stream-json` under the *current* synchronous call, purely for logging.
   This turns the next timeout into a diagnosable incident before any behavior changes
   and de-risks the rest of the migration.
2. **Add the table + poller, dual-write.** Add the new columns / table and the 10–15s
   poller job, but keep `assign_work` synchronous: it writes `status='running'` at
   start and `done`/`failed` at the end of the same call it already makes. No visible
   behavior change; this only proves the schema and poller work against real data
   before anything depends on them.
3. **Flip the producer.** `assign_work` switches to enqueue-and-return (§6); the
   dedicated worker loop (§7) becomes the only thing that actually invokes the CLI for
   staff assignments. Chat replies change from "waits, then reports" to "acknowledges,
   then a follow-up message."
4. **Flip the cadence tick.** `run_due` switches from "run and block per employee" to
   "enqueue per due employee" (§8). Remove `max_instances=3`/`coalesce` once the tick
   can no longer hang — they were compensating for the thing this removes.
5. **Split the timeout budget.** Cut `claude_timeout_seconds` for the interactive chat
   path (the only place a human is actually waiting) down to something tight — 60–90s
   — and give the background worker its own, much larger, per-kind budget (§6, §step
   2). Today these are wrongly the same number.
6. **(Optional, same pattern)** apply steps 2–4 to the business/personal research
   queues if their current blocking behavior on the scheduler thread ever becomes a
   problem in practice — not urgent, since those already run in dedicated interval
   jobs with generous per-call timeouts (600–900s) rather than the tight chat default.

Each step is independently shippable and reversible (dual-write in step 2 means step 3
can be rolled back to "synchronous, but now logged" without a data migration).

## 11. Open questions for whoever implements this

- Read `staff.py::assign_work` and `run_due` directly (this session's tooling couldn't
  pull the full file) to confirm the exact current signature and whether developer
  assignments already go through `converse()` (agentic, real tool access up to the
  employee's capability tier) or a tools-free `research()`/`chat()` call. It changes
  whether §3.3/§9's process-group and tool-call-log cross-referencing work applies, or
  whether "the work may still complete" is instead purely an OS-level orphaned-process
  question with no HTTP side channel involved.
- Confirm `staff_work`'s actual current columns before deciding "extend it" vs "new
  table beside it" in §4.
- Decide the per-kind timeout defaults in §6 with the owner — 30–45 minutes for
  developer-style work is a reasonable starting guess, not a measured number.
