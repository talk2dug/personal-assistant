# Phase 5 â€” Credit Score & Dispute Tracker

## Status
Design + implementation for review. Not deployed, not wired into the live app yet.

## Assumptions (please confirm against Phases 1-4)
I could not browse the existing repository in this session (no read/browse tool
available to me here â€” only branch/commit/PR tools). So this feature is built
as a self-contained module on these assumptions, common to the stated stack
(TS/JS, Node, React):

- Backend: Node.js + TypeScript + Express, PostgreSQL via a shared `pg` Pool
  exported as `server/src/db/pool.ts` (`export const pool: Pool`).
- Auth: an existing `requireAuth` middleware at `server/src/middleware/auth.ts`
  that attaches `req.userId`.
- Frontend: React + TypeScript, a fetch-based API layer, React Router for
  page routing.
- New dependencies introduced: `zod` (validation), `recharts` (history chart).
  No existing package.json was touched â€” please add these deps in the
  project's actual manifest.

If any of this diverges from the real stack, the module boundaries (routes,
service, client) are narrow enough that only the adapter edges (db pool,
auth middleware, HTTP client) need to change.

## 1. Credit Score UI (manual entry + history)

No live credit-bureau feed exists, so every score is user-entered. Model:

```
credit_scores
  id            uuid pk
  user_id       text
  bureau        text  -- equifax | experian | transunion | other
  score         int   -- 300-850 validated range
  score_model   text  -- e.g. 'FICO 8', 'VantageScore 3.0' (optional)
  recorded_on   date  -- the date the score reflects, not entry date
  source        text  -- e.g. 'manual', 'bank app export', 'annualcreditreport.com'
  notes         text
  created_at    timestamptz
  updated_at    timestamptz
```

UI: a simple entry form (bureau, score, model, date, source, notes) and a
line chart of score-over-time per bureau, plus a sortable table of raw
entries with edit/delete. Because entries are manual, validation is the main
guardrail (score range, no future `recorded_on`, duplicate-date warning).

## 2. Dispute Tracker data model

Each disputed item is tracked **per credit bureau** (the same underlying
error often has to be disputed separately with each bureau reporting it).

```
disputes
  id                 uuid pk
  user_id            text
  bureau             text  -- equifax | experian | transunion
  creditor_name      text
  account_reference  text  -- e.g. last 4 of account #, optional
  item_description   text
  dispute_reason     text  -- 'not_mine' | 'incorrect_balance' | 'paid_in_full' | 'duplicate' | 'other'
  state              text  -- drafted | mailed | resolved
  resolution         text  -- null | removed | updated | verified_accurate | no_response
  resolution_notes   text
  created_at         timestamptz
  updated_at         timestamptz
```

A dispute can have multiple letters over time (initial + follow-ups), so
the letter/mailing lifecycle lives in its own table rather than flattening
into `disputes`:

```
dispute_letters
  id                     uuid pk
  dispute_id             uuid fk -> disputes
  letter_version         int
  recipient_snapshot     jsonb  -- bureau name/address used at draft time
  letter_body            text
  letterstream_mail_id   text   -- external id from letterstream_send_mail
  quoted_cost_cents      int
  quote_currency         text default 'USD'
  content_hash           text   -- sha256 of {recipient, body, cost} at quote time
  status                 text   -- draft|quoted|confirmed|authorized|mailed|in_transit|delivered|returned|failed|cancelled
  confirmed_hash         text   -- content_hash captured at the moment the user confirmed
  confirmed_by_user_at   timestamptz
  authorized_at          timestamptz
  mailed_at              timestamptz
  tracking_number        text
  last_tracked_status    text
  last_tracked_at        timestamptz
  created_at             timestamptz
  updated_at             timestamptz

dispute_mail_events   -- append-only audit log
  id                 uuid pk
  dispute_letter_id  uuid fk -> dispute_letters
  event_type         text  -- draft_created|quote_received|user_confirmed|authorize_requested|authorized|authorize_failed|tracking_update|cancelled
  event_payload      jsonb
  created_at         timestamptz
```

### Dispute state machine
`drafted -> mailed` only once at least one of its letters actually reaches
`mailed` status (i.e. postage was authorized and LetterStream confirmed it).
`mailed -> resolved` is a manual action by the user recording the bureau's
outcome. `resolved -> drafted` is allowed to support a follow-up round if
the first dispute didn't fully resolve the issue.

## 3. LetterStream tie-in â€” draft, quote, hard confirm, then (and only then) authorize

Three external calls are used, each with a distinct trust level:

| Call | Purpose | Cost/risk |
|---|---|---|
| `letterstream_send_mail` | Create the letter + get a cost quote. Does **not** put anything in the mail. | none |
| `letterstream_authorize_mail` | Commits to real postage/printing. | **real money, irreversible** |
| `letterstream_track_mail` | Poll delivery status of something already mailed. | none |

Flow, enforced server-side (not just in the UI):

1. `POST /api/disputes/:id/letters` builds the letter body from a template,
   snapshots the bureau's mailing address, calls `letterstream_send_mail`,
   and stores the result with `status='quoted'` plus a `content_hash` of
   exactly what was quoted (recipient + body + cost).
2. The UI shows the full recipient, full letter text, and quoted cost in a
   review panel. Nothing is editable in place â€” if the user wants changes,
   a new draft/quote is generated (so the hash always matches what's shown).
3. `POST /letters/:id/confirm` â€” the **hard confirm** step. Requires the
   user to have reviewed the exact content above and click an explicit
   "Approve" action (checkbox + button in the UI, described below). Stores
   `confirmed_hash` and `confirmed_by_user_at`.
4. `POST /letters/:id/authorize` â€” before ever calling
   `letterstream_authorize_mail`, the service:
   - requires `status === 'confirmed'`
   - requires the request body to carry `explicitApproval: true`
     (defense in depth against any accidental/automated call)
   - recomputes the content hash from the stored letter right now and
     rejects with 409 if it no longer matches `confirmed_hash` (i.e.
     anything changed between confirm and authorize forces re-confirmation)
   Only if all three hold does it call `letterstream_authorize_mail`.
5. On success, letter status becomes `authorized` then `mailed`; the parent
   dispute's state flips to `mailed`; every step above is written to
   `dispute_mail_events` for an audit trail.

The UI mirrors this: a "Review & Draft" panel, then a distinctly-styled
red/orange "Authorize real postage ($X.XX)" button that only enables after
the review checkbox is ticked, separate from the neutral "Draft & Quote"
action. There is no code path that reaches `letterstream_authorize_mail`
without a prior, content-matched `confirm` call.

## 4. Mail status tracking

For any letter with status `mailed`/`in_transit`, `POST /letters/:id/track`
calls `letterstream_track_mail` and updates `last_tracked_status` /
`last_tracked_at`, mapping terminal LetterStream statuses to `delivered` or
`returned`. This is read-only against LetterStream â€” no cost, no gate
needed. The dispute UI shows a status badge per mailed letter.

## Open items for review
- Confirm actual DB/auth wiring matches assumptions above.
- Confirm how `letterstream_*` calls are actually invoked from server code
  in this environment (direct HTTP client vs. an internal SDK/tool bridge).
  I've written `server/src/integrations/letterstream/client.ts` as a typed
  interface with a clearly marked TODO at the actual call sites, plus a
  mock implementation for local dev/tests, since I don't have the real
  binding mechanism to inspect here.
- Bureau mailing addresses in `bureauAddresses.ts` are current as of this
  writing but should be verified before any real mailing â€” a visible
  reminder is included in the confirm UI.
