# Email API â€” Integration Notes (Phase 4)

This router assumes an existing mail-tools module at
`server/src/tools/mailTools.ts` exporting:

```
listEmails({ folder, pageToken?, maxResults? }) -> { messages, nextPageToken }
searchEmails({ query, maxResults? })            -> { messages, nextPageToken }
readEmail({ id })                               -> full message object
sendEmail({ to, cc?, bcc?, subject, body })     -> send result
```

These map directly to the four mail tools named in the Phase 4 spec
(list_emails, search_emails, read_email, send_email). I did not have read
access to the actual repository in this session, so:

- If the real module lives at a different path, or the functions/args are
  named differently, update the single
  `import { ... } from '../tools/mailTools'` line at the top of `email.ts`
  - nothing else in this file needs to change.
- Wire this router into the main app with one line, e.g.:
  ```
  import emailRouter from './routes/email';
  app.use('/api/email', emailRouter);
  ```
  I deliberately did not add this line myself, since I couldn't see (and
  didn't want to risk overwriting) the actual app entry file.

## Security-relevant behavior for review

- `POST /send` hard-rejects any request without `confirmed: true` in the
  body. The frontend only sets that flag after the user has seen the
  literal to/cc/subject/body in `ConfirmSendDialog` and clicked "Send".
  This is a belt-and-suspenders check â€” the real confirmation UX lives in
  the client; this is just a server-side guard against something calling
  the endpoint directly (e.g. a script, or a future feature) and skipping
  the human-in-the-loop step.
- No draft is auto-saved or auto-sent anywhere in this flow; sending is
  always a two-step, user-initiated action.
- This router does no auth/authorization itself â€” it assumes the app's
  existing auth middleware is applied ahead of it (same as other routes).
