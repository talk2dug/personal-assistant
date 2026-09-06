import { randomUUID } from 'crypto';
import { getDb, nowIso } from '../db';

export type DisputeBureau = 'equifax' | 'experian' | 'transunion';
export type DisputeState = 'drafted' | 'mailed' | 'resolved';
export type ResolutionOutcome = 'removed' | 'updated' | 'verified' | 'no_change';

export interface DisputeItem {
  id: string;
  bureau: DisputeBureau;
  creditorName: string;
  accountNumberLast4?: string;
  itemDescription: string;
  disputeReason: string;
  state: DisputeState;
  resolutionOutcome?: ResolutionOutcome;
  resolutionNotes?: string;
  createdAt: string;
  updatedAt: string;
}

export interface NewDisputeItem {
  bureau: DisputeBureau;
  creditorName: string;
  accountNumberLast4?: string;
  itemDescription: string;
  disputeReason: string;
}

/**
 * Allowed forward transitions for a dispute item's lifecycle.
 * Each disputed item is tracked per bureau (a report error at two
 * bureaus is two separate dispute_items rows, each with its own
 * letter/mailing/resolution).
 *
 *   drafted -> mailed -> resolved
 *
 * "mailed" is only reachable in practice once a letter has actually
 * been authorized through LetterStream (see disputeLetterService) --
 * this model itself doesn't know about LetterStream, but the routes
 * layer never exposes a generic "set to mailed" path; only the letter
 * service calls transitionDisputeState(..., 'mailed').
 */
const ALLOWED_TRANSITIONS: Record<DisputeState, DisputeState[]> = {
  drafted: ['mailed'],
  mailed: ['resolved'],
  resolved: [],
};

function rowToItem(row: any): DisputeItem {
  return {
    id: row.id,
    bureau: row.bureau,
    creditorName: row.creditor_name,
    accountNumberLast4: row.account_number_last4 ?? undefined,
    itemDescription: row.item_description,
    disputeReason: row.dispute_reason,
    state: row.state,
    resolutionOutcome: row.resolution_outcome ?? undefined,
    resolutionNotes: row.resolution_notes ?? undefined,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export function createDisputeItem(input: NewDisputeItem): DisputeItem {
  const db = getDb();
  const id = randomUUID();
  const now = nowIso();
  db.prepare(`
    INSERT INTO dispute_items
      (id, bureau, creditor_name, account_number_last4, item_description, dispute_reason, state, created_at, updated_at)
    VALUES
      (@id, @bureau, @creditorName, @accountNumberLast4, @itemDescription, @disputeReason, 'drafted', @createdAt, @updatedAt)
  `).run({
    id,
    bureau: input.bureau,
    creditorName: input.creditorName,
    accountNumberLast4: input.accountNumberLast4 ?? null,
    itemDescription: input.itemDescription,
    disputeReason: input.disputeReason,
    createdAt: now,
    updatedAt: now,
  });
  return getDisputeItem(id)!;
}

export function getDisputeItem(id: string): DisputeItem | undefined {
  const row = getDb().prepare('SELECT * FROM dispute_items WHERE id = ?').get(id);
  return row ? rowToItem(row) : undefined;
}

export function listDisputeItems(filter?: { bureau?: DisputeBureau; state?: DisputeState }): DisputeItem[] {
  const db = getDb();
  const clauses: string[] = [];
  const params: any = {};
  if (filter?.bureau) { clauses.push('bureau = @bureau'); params.bureau = filter.bureau; }
  if (filter?.state) { clauses.push('state = @state'); params.state = filter.state; }
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = db.prepare(`SELECT * FROM dispute_items ${where} ORDER BY created_at DESC`).all(params);
  return rows.map(rowToItem);
}

export class InvalidTransitionError extends Error {}

/**
 * Moves a dispute item to a new state, enforcing the
 * drafted -> mailed -> resolved order. Throws InvalidTransitionError
 * on any attempt to skip a step or go backwards.
 */
export function transitionDisputeState(
  id: string,
  toState: DisputeState,
  extra?: { resolutionOutcome?: ResolutionOutcome; resolutionNotes?: string }
): DisputeItem {
  const item = getDisputeItem(id);
  if (!item) throw new Error(`Dispute item ${id} not found`);

  const allowed = ALLOWED_TRANSITIONS[item.state];
  if (!allowed.includes(toState)) {
    throw new InvalidTransitionError(
      `Cannot move dispute ${id} from '${item.state}' to '${toState}'. Allowed: ${allowed.join(', ') || '(none -- terminal state)'}`
    );
  }
  if (toState === 'resolved' && !extra?.resolutionOutcome) {
    throw new Error('resolutionOutcome is required when resolving a dispute');
  }

  const db = getDb();
  db.prepare(`
    UPDATE dispute_items
    SET state = @state,
        resolution_outcome = @resolutionOutcome,
        resolution_notes = @resolutionNotes,
        updated_at = @updatedAt
    WHERE id = @id
  `).run({
    id,
    state: toState,
    resolutionOutcome: extra?.resolutionOutcome ?? null,
    resolutionNotes: extra?.resolutionNotes ?? null,
    updatedAt: nowIso(),
  });
  return getDisputeItem(id)!;
}
