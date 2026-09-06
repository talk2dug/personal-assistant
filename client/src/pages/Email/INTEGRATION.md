# Email Page â€” Integration Notes (Phase 4)

This directory is self-contained and does not modify any existing routing
or app-shell files, because I didn't have read access to the current repo
layout in this session. To mount it:

1. Import and render `EmailPage` wherever the dashboard's other pages
   (Phases 1-3) are routed, e.g.:
   ```tsx
   import { EmailPage } from './pages/Email/EmailPage';
   ...
   <Route path="/email" element={<EmailPage />} />
   ```
   (adjust to whatever router the dashboard actually uses â€” React Router,
   Next.js pages, etc.)
2. Add a nav link to `/email` in the existing sidebar/nav component.
3. The page calls the backend at `/api/email/*` (see
   `server/src/routes/email.ts` and its own integration notes). If the
   dashboard's API is mounted at a different base path or behind a proxy,
   update the `BASE` constant in `api.ts`.
4. Styling: `EmailPage.css` is plain CSS with BEM-ish class names and no
   framework dependency, so it works regardless of whether the rest of the
   app uses Tailwind, CSS modules, etc. Feel free to restyle to match the
   existing design system.

No existing files were modified or overwritten by this change.

## Out of scope for this phase

- Attachments (viewing or sending)
- Multiple inbox folders / labels beyond a single `folder` query param
- Pagination UI (the API supports `nextPageToken` but the list view doesn't
  yet page through it)
- Draft auto-save

These weren't in the Phase 4 spec; flagging them in case they should be
queued for a follow-up phase.
