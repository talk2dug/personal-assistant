import type { DisputeItem, NewDisputeItem, DisputeBureau, DisputeState, ResolutionOutcome } from '../types';

const BASE = '/api/disputes';

export async function fetchDisputeItems(filter?: { bureau?: DisputeBureau; state?: DisputeState }): Promise<DisputeItem[]> {
  const params = new URLSearchParams();
  if (filter?.bureau) params.set('bureau', filter.bureau);
  if (filter?.state) params.set('state', filter.state);
  const qs = params.toString();
  const res = await fetch(qs ? `${BASE}?${qs}` : BASE);
  if (!res.ok) throw new Error(`Failed to load disputes (${res.status})`);
  return (await res.json()).items;
}

export async function fetchDisputeItem(id: string): Promise<DisputeItem> {
  const res = await fetch(`${BASE}/${id}`);
  if (!res.ok) throw new Error(`Failed to load dispute (${res.status})`);
  return (await res.json()).item;
}

export async function createDisputeItem(input: NewDisputeItem): Promise<DisputeItem> {
  const res = await fetch(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Failed to create dispute (${res.status})`);
  return (await res.json()).item;
}

export async function resolveDisputeItem(id: string, resolutionOutcome: ResolutionOutcome, resolutionNotes?: string): Promise<DisputeItem> {
  const res = await fetch(`${BASE}/${id}/transition`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ toState: 'resolved', resolutionOutcome, resolutionNotes }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Failed to resolve dispute (${res.status})`);
  return (await res.json()).item;
}
