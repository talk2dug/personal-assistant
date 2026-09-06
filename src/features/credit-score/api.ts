import { CreditScoreEntry, CreditScoreFormValues } from './types';

const BASE = '/api/credit-scores';

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Request failed (${res.status})`);
  }
  return res.json();
}

export async function fetchCreditScores(bureau?: string): Promise<CreditScoreEntry[]> {
  const url = bureau ? `${BASE}?bureau=${encodeURIComponent(bureau)}` : BASE;
  const { entries } = await json<{ entries: CreditScoreEntry[] }>(await fetch(url));
  return entries;
}

export async function createCreditScore(values: CreditScoreFormValues): Promise<CreditScoreEntry> {
  const { entry } = await json<{ entry: CreditScoreEntry }>(
    await fetch(BASE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bureau: values.bureau,
        score: Number(values.score),
        scoreModel: values.scoreModel || undefined,
        recordedOn: values.recordedOn,
        source: values.source || undefined,
        notes: values.notes || undefined,
      }),
    })
  );
  return entry;
}

export async function deleteCreditScore(id: string): Promise<void> {
  await fetch(`${BASE}/${id}`, { method: 'DELETE' });
}
