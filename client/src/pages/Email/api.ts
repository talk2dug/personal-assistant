import { EmailSummary, EmailMessage, ComposeDraft } from './types';

// Base path for the Email API. If the dashboard mounts its API elsewhere
// or behind a different prefix, update this one constant.
const BASE = '/api/email';

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.error || body.detail || detail;
    } catch {
      // response wasn't JSON - fall back to statusText
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export interface InboxPage {
  messages: EmailSummary[];
  nextPageToken?: string | null;
}

export async function fetchInbox(opts: { folder?: string; pageToken?: string } = {}): Promise<InboxPage> {
  const params = new URLSearchParams();
  if (opts.folder) params.set('folder', opts.folder);
  if (opts.pageToken) params.set('pageToken', opts.pageToken);
  const res = await fetch(`${BASE}/inbox?${params.toString()}`);
  return handle<InboxPage>(res);
}

export async function searchInbox(query: string): Promise<InboxPage> {
  const params = new URLSearchParams({ q: query });
  const res = await fetch(`${BASE}/search?${params.toString()}`);
  return handle<InboxPage>(res);
}

export async function fetchMessage(id: string): Promise<EmailMessage> {
  const res = await fetch(`${BASE}/message/${encodeURIComponent(id)}`);
  return handle<EmailMessage>(res);
}

/**
 * Sends an email. This must only ever be called after the user has seen the
 * exact to/cc/subject/body in the confirmation dialog and explicitly clicked
 * Send - see ConfirmSendDialog + ComposeModal. The `confirmed: true` flag is
 * also re-checked server-side (see server/src/routes/email.ts).
 */
export async function sendEmailConfirmed(draft: ComposeDraft): Promise<{ ok: true }> {
  const res = await fetch(`${BASE}/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...draft, confirmed: true }),
  });
  return handle<{ ok: true }>(res);
}
