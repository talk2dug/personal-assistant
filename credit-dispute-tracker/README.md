# Credit Score & Dispute Tracker (Phase 5)

Self-contained feature module. See `DESIGN.md` for the full design writeup, data model, and the LetterStream safety gate.

## What's here
```
credit-dispute-tracker/
  server/
    db.ts                              sqlite connection + schema
    models/creditScore.ts              manual score entries
    models/dispute.ts                  dispute items + state machine
    services/disputeLetterService.ts   draft/quote/confirm-mail/track orchestration
    integrations/letterstream/         LetterStream API client (send/authorize/track)
    routes/                            Express routers
    __tests__/                         unit tests for the state machine + confirm gate
  client/
    types.ts
    api/                                fetch wrappers
    components/
    pages/CreditScorePage.tsx
    pages/DisputeTrackerPage.tsx
    pages/DisputeDetailPage.tsx
    routes.example.tsx                 example react-router wiring
```
(Built up across the commits in this branch -- see commit history for the sub-tasks.)

## New npm dependencies (add to the app's existing package.json -- not modified here since I can't see its current contents)
- `express` (if not already present)
- `better-sqlite3`
- `react-router-dom` (if not already present)
- `recharts`
- Dev/test: `vitest` (or adapt `server/__tests__` to whatever test runner the repo already uses)

## Environment variables
| Var | Purpose |
|---|---|
| `LETTERSTREAM_API_KEY` | required to call LetterStream at all; calls fail fast without it |
| `LETTERSTREAM_API_BASE` | override API base URL, default `https://api.letterstream.com/v1` |
| `DISPUTE_LETTER_SENDER_NAME` / `_ADDRESS1` / `_CITY` / `_STATE` / `_ZIP` | return address on outgoing dispute letters -- placeholders (`REPLACE_ME`) until set |
| `DASHBOARD_DATA_DIR` / `DASHBOARD_DB_PATH` | override where the sqlite file lives |

## Safety note
`letterstream_authorize_mail` (real postage, real money) is only reachable through `POST /api/dispute-letters/:id/confirm-and-mail`, which requires the exact current recipient name + quoted cost to be echoed back plus `userApproved: true`. Nothing else in this module calls it. See DESIGN.md section 3 for the full flow.

## Status
All 4 scope items from Phase 5 are implemented at the module level. Not yet wired into the live dashboard app or tested against a real LetterStream account/credentials -- see the integration checklist in DESIGN.md.
