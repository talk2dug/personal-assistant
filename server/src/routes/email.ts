import { Router } from 'express';
// Assumed existing mail-tools module wrapping the list_emails / search_emails /
// read_email / send_email tools. If the real module lives elsewhere or has a
// different export shape, this is the only import that needs to change - see
// EMAIL_INTEGRATION.md in this directory.
import { listEmails, searchEmails, readEmail, sendEmail } from '../tools/mailTools';

const router = Router();

// GET /api/email/inbox?folder=inbox&pageToken=...&maxResults=...
router.get('/inbox', async (req, res) => {
  try {
    const { folder = 'inbox', pageToken, maxResults } = req.query;
    const result = await listEmails({
      folder: String(folder),
      pageToken: pageToken ? String(pageToken) : undefined,
      maxResults: maxResults ? Number(maxResults) : 25,
    });
    res.json(result);
  } catch (err) {
    console.error('[email:inbox] failed', err);
    res.status(502).json({ error: 'Failed to load inbox', detail: (err as Error).message });
  }
});

// GET /api/email/search?q=...&maxResults=...
router.get('/search', async (req, res) => {
  const q = String(req.query.q || '').trim();
  if (!q) {
    return res.status(400).json({ error: 'Missing query parameter q' });
  }
  try {
    const result = await searchEmails({
      query: q,
      maxResults: req.query.maxResults ? Number(req.query.maxResults) : 25,
    });
    res.json(result);
  } catch (err) {
    console.error('[email:search] failed', err);
    res.status(502).json({ error: 'Search failed', detail: (err as Error).message });
  }
});

// GET /api/email/message/:id
router.get('/message/:id', async (req, res) => {
  try {
    const message = await readEmail({ id: req.params.id });
    res.json(message);
  } catch (err) {
    console.error('[email:read] failed', err);
    res.status(502).json({ error: 'Failed to read message', detail: (err as Error).message });
  }
});

// POST /api/email/send
// Body: { to: string[], cc?: string[], bcc?: string[], subject: string, body: string, confirmed: true }
// `confirmed` must be true - the frontend only sets it after the user has
// reviewed the exact to/cc/subject/body in ConfirmSendDialog and clicked Send.
router.post('/send', async (req, res) => {
  const { to, cc, bcc, subject, body, confirmed } = req.body || {};

  if (!confirmed) {
    return res.status(400).json({
      error: 'Send not confirmed. Set confirmed:true only after showing the user the exact recipient/subject/body.',
    });
  }
  if (!Array.isArray(to) || to.length === 0) {
    return res.status(400).json({ error: 'At least one recipient (to) is required' });
  }
  if (!subject || !String(subject).trim()) {
    return res.status(400).json({ error: 'Subject is required' });
  }

  try {
    const result = await sendEmail({ to, cc, bcc, subject, body });
    res.json({ ok: true, result });
  } catch (err) {
    console.error('[email:send] failed', err);
    res.status(502).json({ error: 'Failed to send email', detail: (err as Error).message });
  }
});

export default router;
