import { randomUUID } from 'crypto';
import { pool } from '../../db/pool'; // ASSUMPTION: shared pg Pool, see docs/phase5-credit-dispute-tracker.md
import { CreditScoreEntry, CreditScoreInput } from './types';

function rowToEntry(row: any): CreditScoreEntry {
  return {
    id: row.id,
    userId: row.user_id,
    bureau: row.bureau,
    score: row.score,
    scoreModel: row.score_model,
    recordedOn: row.recorded_on,
    source: row.source,
    notes: row.notes,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export async function listCreditScores(
  userId: string,
  filter: { bureau?: string } = {}
): Promise<CreditScoreEntry[]> {
  const params: any[] = [userId];
  let where = 'user_id = $1';
  if (filter.bureau) {
    params.push(filter.bureau);
    where += ` AND bureau = $${params.length}`;
  }
  const { rows } = await pool.query(
    `SELECT * FROM credit_scores WHERE ${where} ORDER BY recorded_on ASC`,
    params
  );
  return rows.map(rowToEntry);
}

export async function createCreditScore(
  userId: string,
  input: CreditScoreInput
): Promise<CreditScoreEntry> {
  const id = randomUUID();
  const { rows } = await pool.query(
    `INSERT INTO credit_scores
       (id, user_id, bureau, score, score_model, recorded_on, source, notes)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
     RETURNING *`,
    [
      id,
      userId,
      input.bureau,
      input.score,
      input.scoreModel ?? null,
      input.recordedOn,
      input.source ?? 'manual',
      input.notes ?? null,
    ]
  );
  return rowToEntry(rows[0]);
}

export async function updateCreditScore(
  userId: string,
  id: string,
  input: Partial<CreditScoreInput>
): Promise<CreditScoreEntry | null> {
  const { rows } = await pool.query(
    `UPDATE credit_scores SET
       bureau = COALESCE($3, bureau),
       score = COALESCE($4, score),
       score_model = COALESCE($5, score_model),
       recorded_on = COALESCE($6, recorded_on),
       source = COALESCE($7, source),
       notes = COALESCE($8, notes),
       updated_at = now()
     WHERE id = $1 AND user_id = $2
     RETURNING *`,
    [
      id,
      userId,
      input.bureau ?? null,
      input.score ?? null,
      input.scoreModel ?? null,
      input.recordedOn ?? null,
      input.source ?? null,
      input.notes ?? null,
    ]
  );
  return rows[0] ? rowToEntry(rows[0]) : null;
}

export async function deleteCreditScore(userId: string, id: string): Promise<boolean> {
  const { rowCount } = await pool.query(
    `DELETE FROM credit_scores WHERE id = $1 AND user_id = $2`,
    [id, userId]
  );
  return (rowCount ?? 0) > 0;
}
