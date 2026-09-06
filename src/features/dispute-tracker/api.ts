import { Dispute, DisputeFormValues, DisputeLetter } from './types';

const BASE = '/api/disputes';

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.message || body.error || `Request failed (${res.status})`);
  }
  return res.json();
}

export async function fetchDisputes(): Promise<Dispute[]> {
  const { disputes } = await json<{ disputes: Dispute[] }>(await fetch(BASE));
  return disputes;
}

export async function createDispute(values: DisputeFormValues): Promise<Dispute> {
  const { dispute } = await json<{ dispute: Dispute }>(
    await fetch(BASE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...values,
        accountReference: values.accountReference || undefined,
      }),
    })
  );
  return dispute;
}

export async function resolveDispute(
  id: string,
  resolution: string,
  resolutionNotes?: string
): Promise<Dispute> {
  const { dispute } = await json<{ dispute: Dispute }>(
    await fetch(`${BASE}/${id}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolution, resolutionNotes }),
    })
  );
  return dispute;
}

export async function draftLetter(
  disputeId: string,
  consumer: { fullName: string; addressLines: string[] }
): Promise<DisputeLetter> {
  const { letter } = await json<{ letter: DisputeLetter }>(
    await fetch(`${BASE}/${disputeId}/letters`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ consumer }),
    })
  );
  return letter;
}

export async function confirmLetter(letterId: string): Promise<DisputeLetter> {
  const { letter } = await json<{ letter: DisputeLetter }>(
    await fetch(`${BASE}/letters/${letterId}/confirm`, { method: 'POST' })
  );
  return letter;
}

// The one call in this whole feature that spends real money. Requires the
// letter to already be 'confirmed' server-side, plus this explicit flag.
export async function authorizeLetter(letterId: string): Promise<DisputeLetter> {
  const { letter } = await json<{ letter: DisputeLetter }>(
    await fetch(`${BASE}/letters/${letterId}/authorize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ explicitApproval: true }),
    })
  );
  return letter;
}

export async function trackLetter(letterId: string): Promise<DisputeLetter> {
  const { letter } = await json<{ letter: DisputeLetter }>(
    await fetch(`${BASE}/letters/${letterId}/track`, { method: 'POST' })
  );
  return letter;
}

export async function cancelLetter(letterId: string): Promise<DisputeLetter> {
  const { letter } = await json<{ letter: DisputeLetter }>(
    await fetch(`${BASE}/letters/${letterId}/cancel`, { method: 'POST' })
  );
  return letter;
}
