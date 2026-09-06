# Phase 4 â€” Email Page: implementation notes for reviewers

Branch: `feature/phase4-email-page`

## What's here

- **Frontend** (`client/src/pages/Email/`): inbox list, thread/read view,
  and a compose modal, all wired to a small `api.ts` fetch client.
- **Backend** (`server/src/routes/email.ts`): four routes proxying to the
  existing mail tools (`list_emails`, `search_emails`, `read_email`,
  `send_email`).
- **The confirm-before-send requirement**: `ComposeModal` is a two-step
  flow. Step 1 is a normal edit form. Step 2 (`ConfirmSendDialog`) is a
  read-only, verbatim rendering of To/Cc/Subject/Body. `sendEmailConfirmed()`
  (the only function that calls `POST /api/email/send`) is invoked from
  exactly one place: the "Send" button inside `ConfirmSendDialog`. The
  server route also rejects any send request that doesn't carry
  `confirmed: true`, as a backstop against something bypassing the UI.
- Tests cover both the UI confirm-gate (send is never called before the
  user reviews and clicks Send, and is never called after Cancel/Back) and
  the backend routes (correct mail tool called with correct args; send
  rejected without confirmation).

## Important limitation in this session

I did not have read/browse access to the actual repository â€” only tools to
create a branch and push full file contents to new paths. That means:

- I could **not** inspect Phases 1-3 to match existing folder structure,
  component conventions, routing setup, or the real shape of the mail
  tools module.
- Every new file above lives at a path that shouldn't collide with
  anything existing (`client/src/pages/Email/**`,
  `server/src/routes/email.ts`, `server/src/routes/__tests__/email.ts`).
  I deliberately did **not** touch `app.tsx`/router config, `package.json`,
  or any file I couldn't see, to avoid silently clobbering something.
- Two integration points need a human (or a follow-up commit once the
  real structure is visible) to finish wiring:
  1. Mount `EmailPage` in the app's router/nav â€” see
     `client/src/pages/Email/INTEGRATION.md`.
  2. Point `server/src/routes/email.ts`'s single import at the real
     mail-tools module if the path or function signatures differ â€” see
     `server/src/routes/EMAIL_INTEGRATION.md`.
- I assumed a React + TypeScript client and an Express + TypeScript
  server, and Jest/RTL/supertest for tests, as the most likely stack given
  the role description. If Phases 1-3 used something else (Next.js, a
  different backend framework, a different test runner), the code will
  need adjusting to fit â€” the logic and behavior (routes, confirm-gate,
  error handling) should carry over with mostly mechanical changes.
- **I have not run or built any of this code** â€” there's no execution
  environment available to me here. It's written carefully and I'm
  confident in the logic, but it has not been compiled or test-run, and
  that should happen before merge (CI on the PR, plus a manual pass by
  whoever has the real repo checked out).

## Suggested review order

1. `server/src/routes/EMAIL_INTEGRATION.md` â€” confirm/fix the mailTools
   import.
2. `server/src/routes/email.ts` + its tests.
3. `client/src/pages/Email/components/ComposeModal.tsx` +
   `ConfirmSendDialog.tsx` â€” this is the safety-critical part of the
   phase.
4. `client/src/pages/Email/INTEGRATION.md` â€” wire into the real app shell.
