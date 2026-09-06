import { randomUUID } from 'crypto';
import { getDb, nowIso } from '../db';

export type Bureau = 'equifax' | 'experian' | 'transunion' | 'other';

export interface CreditScoreEntry {
  id: string;
  recordedAt: string;
  score: number;
  bureau: Bureau;
  source?: string;
  notes?: string;
  createdAt: string;
  updatedAt: string;
}

export interface NewCreditScoreEntry {
  recordedAt: string;
  score: number;
  bureau: Bureau;
  source?: string;
  notes?: string;
}

function rowToEntry(row: any): CreditScoreEntry {
  return {
    id: row.id,
    recordedAt: row.recorded_at,
    score: row.score,
    bureau: row.bureau,
    source: row.source ?? undefined,
    notes: row.notes ?? undefined,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export function createCreditScoreEntry(input: NewCreditScoreEntry): CreditScoreEntry {
  if (input.score < 300 || input.score > 900) {
    throw new Error('Score must be between 300 and 900');
  }
  const db = getDb();
  const id = randomUUID();
  const now = nowIso();
  db.prepare(`
    INSERT INTO credit_score_entries (id, recorded_at, score, bureau, source, notes, created_at, updated_at)
    VALUES (@id, @recordedAt, @score, @bureau, @source, @notes, @createdAt, @updatedAt)
  `).run({
    id,
    recordedAt: input.recordedAt,
    score: input.score,
    bureau: input.bureau,
    source: input.source ?? null,
    notes: input.notes ?? null,
    createdAt: now,
    updatedAt: now,
  });
  return getCreditScoreEntry(id)!;
}

export function getCreditScoreEntry(id: string): CreditScoreEntry | undefined {
  const row = getDb().prepare('SELECT * FROM credit_score_entries WHERE id = ?').get(id);
  return row ? rowToEntry(row) : undefined;
}

export function listCreditScoreHistory(bureau?: Bureau): CreditScoreEntry[] {
  const db = getDb();
  const rows = bureau
    ? db.prepare('SELECT * FROM credit_score_entries WHERE bureau = ? ORDER BY recorded_at ASC').all(bureau)
    : db.prepare('SELECT * FROM credit_score_entries ORDER BY recorded_at ASC').all();
  return rows.map(rowToEntry);
}

export function deleteCreditScoreEntry(id: string): boolean {
  const result = getDb().prepare('DELETE FROM credit_score_entries WHERE id = ?').run(id);
  return result.changes > 0;
}
