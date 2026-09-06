# Phase 5 -- Credit Score & Dispute Tracker: Design

## Scope
1. Credit score UI -- manual entry, history-over-time
2. Dispute tracker data model -- per-bureau, drafted/mailed/resolved
3. LetterStream tie-in -- draft & quote, hard confirm gate before authorize
4. Mail status tracking for anything actually mailed

## Assumptions (flagging these since I don't have read access to the rest of the repo)
- This is built as a self-contained feature module (`credit-dispute-tracker/`) meant to be wired into the existing dashboard's backend and frontend. I could not inspect prior phases' file layout, DB connection, auth, or router conventions, so I couldn't match them exactly -- see "Integration checklist" below.
- Backend: Node + Express + TypeScript, SQLite via `better-sqlite3` (boring, zero-ops, fits a single-user personal dashboard). If the app already has a shared DB layer, swap it in -- this module's schema is additive and namespaced in its own tables.
- Frontend: React + TypeScript, `react-router-dom` for page routing, `recharts` for the score history chart.
- LetterStream: I don't have account credentials or logged-in API docs (their public docs are inside the customer account). `server/integrations/letterstream/` wraps three calls named to match the ones specified in scope (`letterstream_send_mail`, `letterstream_authorize_mail`, `letterstream_track_mail`) against a best-effort REST shape based on LetterStream's published product description. Before go-live: check the account's actual API docs and adjust `types.ts` / `client.ts` -- everything else in the app depends only on the exported types from `types.ts`, so a schema mismatch stays contained to those two files.

## 1. Credit score UI
- No live bureau feed exists, so every row is user-typed: date, score (300-900), bureau (Equifax/Experian/TransUnion/other), optional source ("Chase Credit Journey", "annual pull", etc.) and notes.
- History view is a multi-line chart (one line per bureau) over time, so trends per bureau stay visible even if the user only checks one bureau at a time -- scores are never averaged/reconciled across bureaus since real scores differ.

## 2. Dispute tracker data model
- `dispute_items`: one row per disputed item **per credit bureau** -- e.g. the same collection account showing up wrong at both Experian and TransUnion is two separate rows, because each bureau's dispute process, mailing, and resolution are independent.
- Each item has: bureau, creditor name, optional account last-4, item description, dispute reason, `state`, and (once resolved) an outcome (`removed` / `updated` / `verified` / `no_change`) plus notes.
- State machine is linear and one-way: `drafted -> mailed -> resolved`. Enforced server-side in `models/dispute.ts` (`transitionDisputeState`), not just in the UI -- any attempt to skip or reverse a step throws `InvalidTransitionError`.
- Critically: the `drafted -> mailed` transition is **never** triggered by a generic "update state" call from the UI. It only happens as a side effect inside `disputeLetterService.confirmAndMailLetter`, after LetterStream has actually confirmed authorization -- so the tracker can never show "mailed" for something that wasn't really mailed.
- `dispute_letters`: one row per letter attempt for a dispute item (text, recipient address, LetterStream order id, quote, confirmation flags, authorization flags, tracking id, mail status). Kept separate from `dispute_items` so re-drafting or re-quoting a letter doesn't mutate the dispute item itself.

## 3. LetterStream tie-in
Flow, matching the three specified calls:

1. **Draft** (`draftLetter`) -- pure local write. User writes/edits letter text and the bureau's mailing address. No external call, nothing to confirm yet.
2. **Quote** (`quoteLetter` -> `letterstream_send_mail`) -- sends the drafted letter to LetterStream to create a job and get real pricing. Per LetterStream's model this does not spend money or produce anything -- safe to call and re-call (e.g. if a quote goes stale).
3. **Hard confirm gate -> Mail** (`confirmAndMailLetter` -> `letterstream_authorize_mail`) -- the *only* place in the codebase allowed to call `letterstream_authorize_mail`. It:
   - Requires `userApproved: true` in the request body (never defaulted, never inferable -- must come from a checked checkbox in the UI).
   - Requires the caller to echo back the recipient name and quoted cost **exactly as currently stored** for that letter; any mismatch (stale quote, wrong recipient, tampering) throws `ConfirmationMismatchError` and aborts before ever touching LetterStream.
   - Refuses to run twice against an already-authorized letter.
   - Only after LetterStream returns a successful authorization does the dispute item transition to `mailed`.
   - The frontend (`ConfirmMailModal`) shows the real recipient address, full letter text, and quoted dollar cost, and requires two checkboxes *and* typing "MAIL" before the button is enabled -- deliberately more friction than a normal confirm dialog, because this step spends real money and puts physical mail containing dispute details in transit.
4. **Track** (`refreshMailStatus` -> `letterstream_track_mail`) -- read-only polling of a mailed letter's production/delivery status; never touches money or authorization state.

No automated job, retry loop, or scheduled task in this codebase calls `letterstream_authorize_mail`. It is reachable only via one route (`POST /api/dispute-letters/:id/confirm-and-mail`) that requires the human-confirmation payload described above.

## 4. Mail status tracking
- `dispute_letters.mail_status` moves `not_sent -> quoted -> mailed -> in_transit -> delivered` (or `returned` / `failed`), updated by `refreshMailStatus`.
- UI: `MailStatusBadge` + a manual "Refresh status" button on the dispute detail page. No background polling job is included -- add a scheduled job calling `refreshMailStatus` for all `authorized` letters if/when the dashboard has a task scheduler; left out here rather than guessing at the app's existing job infrastructure.

## Integration checklist for whoever wires this into the live app
- [ ] Mount `credit-dispute-tracker/server/routes/index.ts` under `/api` in the main Express app.
- [ ] Point `server/db.ts` at the app's existing SQLite connection if one already exists, instead of opening a second file.
- [ ] Add npm deps: `express`, `better-sqlite3`, `react-router-dom`, `recharts` (see README).
- [ ] Set env vars: `LETTERSTREAM_API_KEY`, optionally `LETTERSTREAM_API_BASE`, and `DISPUTE_LETTER_SENDER_*` (return address -- currently placeholders; mailing fails loudly without them).
- [ ] Add nav entry + routes for `CreditScorePage`, `DisputeTrackerPage`, `DisputeDetailPage` (see `client/routes.example.tsx`).
- [ ] Verify LetterStream request/response field names against the actual account API docs and adjust `server/integrations/letterstream/{types,client}.ts` if they differ from the assumed shape.
- [ ] Wrap dispute letter routes with whatever auth middleware the rest of the dashboard uses -- this module doesn't add its own since I don't know the app's auth pattern.
