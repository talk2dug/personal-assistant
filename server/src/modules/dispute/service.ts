import { randomUUID } from 'crypto';
import { pool } from '../../db/pool';
import { Dispute, DisputeInput, DisputeState, DisputeResolution } from './types';
import { assertCanTransitionDispute } from './stateMachine';

function rowToDispute(row: any): Dispute {
  return {
    id: row.id,
    userId: row.user_id,
    bureau: row.bureau,
    creditorName: row.creditor_name,
    accountReference: row.account_reference,
    itemDescription: row.item_description,
    disputeReason: row.dispute_reason,
    state: row.state,
    resolution: row.resolution,
    resolutionNotes: row.resolution_notes,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export async function listDisputes(
  userId: string,
  filter: { bureau?: string; state?: string } = {}
): Promise<Dispute[]> {
  const params: any[] = [userId];
  let where = 'user_id = $1';
  if (filter.bureau) {
    params.push(filter.bureau);
    where += ` AND bureau = $${params.length}`;
  }
  if (filter.state) {
    params.push(filter.state);
    where += ` AND state = $${params.length}`;
  }
  const { rows } = await pool.query(
    `SELECT * FROM disputes WHERE ${where} ORDER BY created_at DESC`,
    params
  );
  return rows.map(rowToDispute);
}

export async function getDispute(userId: string, id: string): Promise<Dispute | null> {
  const { rows } = await pool.query(`SELECT * FROM disputes WHERE id = $1 AND user_id = $2`, [
    id,
    userId,
  ]);
  return rows[0] ? rowToDispute(rows[0]) : null;
}

export async function createDispute(userId: string, input: DisputeInput): Promise<Dispute> {
  const id = randomUUID();
  const { rows } = await pool.query(
    `INSERT INTO disputes
       (id, user_id, bureau, creditor_name, account_reference, item_description, dispute_reason)
     VALUES ($1,$2,$3,$4,$5,$6,$7)
     RETURNING *`,
    [
      id,
      userId,
      input.bureau,
      input.creditorName,
      input.accountReference ?? null,
      input.itemDescription,
      input.disputeReason,
    ]
  );
  return rowToDispute(rows[0]);
}

/**
 * Explicit, validated state transition. Callers outside this module
 * (e.g. the mailing service marking a dispute 'mailed', or the resolve
 * endpoint) must go through this rather than writing `state` directly.
 */
export async function transitionDisputeState(
  userId: string,
  id: string,
  to: DisputeState,
  resolution?: { resolution: DisputeResolution; resolutionNotes?: string }
): Promise<Dispute | null> {
  const current = await getDispute(userId, id);
  if (!current) return null;
  assertCanTransitionDispute(current.state, to);

  const { rows } = await pool.query(
    `UPDATE disputes SET
       state = $3,
       resolution = COALESCE($4, resolution),
       resolution_notes = COALESCE($5, resolution_notes),
       updated_at = now()
     WHERE id = $1 AND user_id = $2 AND state = $6
     RETURNING *`,
    [
      id,
      userId,
      to,
      resolution?.resolution ?? null,
      resolution?.resolutionNotes ?? null,
      current.state, // optimistic concurrency guard
    ]
  );
  return rows[0] ? rowToDispute(rows[0]) : null;
}

export async function deleteDispute(userId: string, id: string): Promise<boolean> {
  const { rowCount } = await pool.query(`DELETE FROM disputes WHERE id = $1 AND user_id = $2`, [
    id,
    userId,
  ]);
  return (rowCount ?? 0) > 0;
}
