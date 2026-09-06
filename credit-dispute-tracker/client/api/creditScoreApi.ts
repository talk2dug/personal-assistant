import type { CreditScoreEntry, NewCreditScoreEntry, Bureau } from '../types';

const BASE = '/api/credit-score';

export async function fetchCreditScoreHistory(bureau?: Bureau): Promise<CreditScoreEntry[]> {
  const url = bureau ? `${BASE}?bureau=${encodeURIComponent(bureau)}` : BASE;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load credit score history (${res.status})`);
  const data = await res.json();
  return data.entries;
}

export async function addCreditScoreEntry(entry: NewCreditScoreEntry): Promise<CreditScoreEntry> {
  const res = await fetch(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(entry),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Failed to save entry (${res.status})`);
  }
  const data = await res.json();
  return data.entry;
}

export async function deleteCreditScoreEntry(id: string): Promise<void> {
  const res = await fetch(`${BASE}/${id}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 404) {
    throw new Error(`Failed to delete entry (${res.status})`);
  }
}
