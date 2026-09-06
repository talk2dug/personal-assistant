import type {
  LetterStreamSendMailRequest, LetterStreamSendMailResult,
  LetterStreamAuthorizeMailRequest, LetterStreamAuthorizeMailResult,
  LetterStreamTrackMailResult,
} from './types';

const API_BASE = process.env.LETTERSTREAM_API_BASE || 'https://api.letterstream.com/v1';
const API_KEY = process.env.LETTERSTREAM_API_KEY;

function requireApiKey() {
  if (!API_KEY) {
    throw new Error(
      'LETTERSTREAM_API_KEY is not set. Refusing to call LetterStream without credentials configured.'
    );
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  requireApiKey();
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${API_KEY}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`LetterStream API error ${res.status}: ${detail}`);
  }
  return res.json();
}

/**
 * Drafts + quotes a letter with LetterStream. Safe to call freely --
 * per LetterStream's model it creates a job and returns pricing but
 * does not spend money or mail anything.
 */
export async function letterstream_send_mail(
  req: LetterStreamSendMailRequest
): Promise<LetterStreamSendMailResult> {
  return postJson<LetterStreamSendMailResult>('/mail/send', req);
}

/**
 * Actually authorizes production + postage for a previously-quoted
 * order -- this is the step that spends real money and puts a real
 * letter in the mail.
 *
 * CALLERS MUST NOT invoke this directly from a route handler. Go
 * through disputeLetterService.confirmAndMailLetter, which enforces
 * that a human has explicitly confirmed recipient, letter text, and
 * quoted cost first.
 */
export async function letterstream_authorize_mail(
  req: LetterStreamAuthorizeMailRequest
): Promise<LetterStreamAuthorizeMailResult> {
  return postJson<LetterStreamAuthorizeMailResult>('/mail/authorize', req);
}

/**
 * Polls current production/delivery status for a mailed letter.
 */
export async function letterstream_track_mail(
  orderId: string
): Promise<LetterStreamTrackMailResult> {
  requireApiKey();
  const res = await fetch(`${API_BASE}/mail/track/${encodeURIComponent(orderId)}`, {
    headers: { Authorization: `Bearer ${API_KEY}` },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`LetterStream API error ${res.status}: ${detail}`);
  }
  return res.json();
}
