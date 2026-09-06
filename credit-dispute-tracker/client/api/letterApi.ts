import type { DisputeLetter } from '../types';

const BASE = '/api/dispute-letters';

export async function fetchLetterForDispute(disputeItemId: string): Promise<DisputeLetter | null> {
  const res = await fetch(`${BASE}/by-dispute/${disputeItemId}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Failed to load letter (${res.status})`);
  return (await res.json()).letter;
}

export async function draftLetter(disputeItemId: string, letterText: string, recipient: {
  name: string; addressLine1: string; addressLine2?: string; city: string; state: string; zip: string;
}): Promise<DisputeLetter> {
  const res = await fetch(`${BASE}/draft`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ disputeItemId, letterText, recipient }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Failed to draft letter (${res.status})`);
  return (await res.json()).letter;
}

export async function quoteLetter(letterId: string): Promise<DisputeLetter> {
  const res = await fetch(`${BASE}/${letterId}/quote`, { method: 'POST' });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Failed to get quote (${res.status})`);
  return (await res.json()).letter;
}

/**
 * The one function in the whole frontend allowed to trigger real
 * postage. Only call this from the ConfirmMailModal's confirm button,
 * after the user has seen recipient, letter text, and quoted cost.
 */
export async function confirmAndMailLetter(
  letterId: string,
  confirmedRecipientName: string,
  confirmedQuotedCostCents: number
): Promise<DisputeLetter> {
  const res = await fetch(`${BASE}/${letterId}/confirm-and-mail`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmedRecipientName, confirmedQuotedCostCents, userApproved: true }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Failed to mail letter (${res.status})`);
  return (await res.json()).letter;
}
