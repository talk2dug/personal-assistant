import { randomUUID } from 'crypto';
import { getDb, nowIso } from '../db';
import { getDisputeItem, transitionDisputeState } from '../models/dispute';
import { letterstream_send_mail, letterstream_authorize_mail, letterstream_track_mail } from '../integrations/letterstream/client';
import type { LetterStreamAddress } from '../integrations/letterstream/types';

export interface DraftLetterInput {
  disputeItemId: string;
  letterText: string;
  recipient: LetterStreamAddress;
}

const RETURN_ADDRESS: LetterStreamAddress = {
  // TODO: pull from user profile/settings once that exists elsewhere
  // in the dashboard; hardcoding here would mail from the wrong
  // address for anyone but the original developer.
  name: process.env.DISPUTE_LETTER_SENDER_NAME || 'REPLACE_ME',
  addressLine1: process.env.DISPUTE_LETTER_SENDER_ADDRESS1 || 'REPLACE_ME',
  city: process.env.DISPUTE_LETTER_SENDER_CITY || 'REPLACE_ME',
  state: process.env.DISPUTE_LETTER_SENDER_STATE || 'REPLACE_ME',
  zip: process.env.DISPUTE_LETTER_SENDER_ZIP || 'REPLACE_ME',
};

function rowToLetter(row: any) {
  return {
    id: row.id,
    disputeItemId: row.dispute_item_id,
    letterText: row.letter_text,
    recipientName: row.recipient_name,
    recipientAddressLine1: row.recipient_address_line1,
    recipientAddressLine2: row.recipient_address_line2 ?? undefined,
    recipientCity: row.recipient_city,
    recipientState: row.recipient_state,
    recipientZip: row.recipient_zip,
    letterstreamOrderId: row.letterstream_order_id ?? undefined,
    quotedCostCents: row.quoted_cost_cents ?? undefined,
    quoteCurrency: row.quote_currency ?? undefined,
    quotedAt: row.quoted_at ?? undefined,
    confirmedByUser: !!row.confirmed_by_user,
    confirmedAt: row.confirmed_at ?? undefined,
    authorized: !!row.authorized,
    authorizedAt: row.authorized_at ?? undefined,
    letterstreamTrackingId: row.letterstream_tracking_id ?? undefined,
    mailStatus: row.mail_status,
    mailStatusUpdatedAt: row.mail_status_updated_at ?? undefined,
  };
}

export function getLetterForDispute(disputeItemId: string) {
  const row = getDb().prepare(
    'SELECT * FROM dispute_letters WHERE dispute_item_id = ? ORDER BY created_at DESC LIMIT 1'
  ).get(disputeItemId);
  return row ? rowToLetter(row) : undefined;
}

export function getLetter(id: string) {
  const row = getDb().prepare('SELECT * FROM dispute_letters WHERE id = ?').get(id);
  return row ? rowToLetter(row) : undefined;
}

/**
 * Step 1: draft the letter locally (no external call).
 */
export function draftLetter(input: DraftLetterInput) {
  const item = getDisputeItem(input.disputeItemId);
  if (!item) throw new Error(`Dispute item ${input.disputeItemId} not found`);

  const db = getDb();
  const id = randomUUID();
  const now = nowIso();
  db.prepare(`
    INSERT INTO dispute_letters (
      id, dispute_item_id, letter_text,
      recipient_name, recipient_address_line1, recipient_address_line2,
      recipient_city, recipient_state, recipient_zip,
      mail_status, created_at, updated_at
    ) VALUES (
      @id, @disputeItemId, @letterText,
      @recipientName, @recipientAddressLine1, @recipientAddressLine2,
      @recipientCity, @recipientState, @recipientZip,
      'not_sent', @createdAt, @updatedAt
    )
  `).run({
    id,
    disputeItemId: input.disputeItemId,
    letterText: input.letterText,
    recipientName: input.recipient.name,
    recipientAddressLine1: input.recipient.addressLine1,
    recipientAddressLine2: input.recipient.addressLine2 ?? null,
    recipientCity: input.recipient.city,
    recipientState: input.recipient.state,
    recipientZip: input.recipient.zip,
    createdAt: now,
    updatedAt: now,
  });
  return getLetter(id)!;
}

/**
 * Step 2: get a quote from LetterStream for the drafted letter.
 * Safe to call/re-call -- does not spend money or mail anything.
 */
export async function quoteLetter(letterId: string) {
  const letter = getLetter(letterId);
  if (!letter) throw new Error(`Letter ${letterId} not found`);

  const result = await letterstream_send_mail({
    externalId: letter.id,
    to: {
      name: letter.recipientName,
      addressLine1: letter.recipientAddressLine1,
      addressLine2: letter.recipientAddressLine2,
      city: letter.recipientCity,
      state: letter.recipientState,
      zip: letter.recipientZip,
    },
    from: RETURN_ADDRESS,
    documentText: letter.letterText,
    mailClass: 'certified_return_receipt',
    color: false,
    doubleSided: false,
  });

  const db = getDb();
  db.prepare(`
    UPDATE dispute_letters
    SET letterstream_order_id = @orderId,
        quoted_cost_cents = @quotedCostCents,
        quote_currency = @currency,
        quoted_at = @quotedAt,
        mail_status = 'quoted',
        updated_at = @updatedAt
    WHERE id = @id
  `).run({
    id: letterId,
    orderId: result.orderId,
    quotedCostCents: result.quotedCostCents,
    currency: result.currency,
    quotedAt: result.quotedAt,
    updatedAt: nowIso(),
  });
  return getLetter(letterId)!;
}

export interface MailConfirmation {
  /** Must equal the letter's current recipient name, verbatim -- a lightweight "did you actually read this" check. */
  confirmedRecipientName: string;
  /** Must equal the letter's quoted cost in cents. */
  confirmedQuotedCostCents: number;
  /** Explicit boolean the UI can only set via a checked confirm checkbox -- never defaulted true. */
  userApproved: true;
}

export class ConfirmationMismatchError extends Error {}

/**
 * Step 3 (the hard gate): authorize and actually mail a quoted letter.
 *
 * This is the ONLY function in the codebase allowed to call
 * letterstream_authorize_mail. It refuses to proceed unless the
 * caller supplies a MailConfirmation whose recipient name and quoted
 * cost match exactly what's stored for this letter -- i.e. the UI
 * must show the user the real recipient, letter text, and real quoted
 * cost, and the user must explicitly approve those exact values, not
 * just click a generic "yes" button blind to what changed since the
 * quote.
 *
 * No amount of retry/automation logic should ever construct a
 * MailConfirmation programmatically; it must originate from a user
 * action in the dispute-letters routes.
 */
export async function confirmAndMailLetter(letterId: string, confirmation: MailConfirmation) {
  if (confirmation.userApproved !== true) {
    throw new ConfirmationMismatchError('userApproved must be explicitly true');
  }
  const letter = getLetter(letterId);
  if (!letter) throw new Error(`Letter ${letterId} not found`);
  if (letter.mailStatus !== 'quoted' || !letter.letterstreamOrderId) {
    throw new Error(`Letter ${letterId} has not been quoted yet -- call quoteLetter first`);
  }
  if (letter.authorized) {
    throw new Error(`Letter ${letterId} has already been authorized and mailed -- refusing to mail twice`);
  }
  if (confirmation.confirmedRecipientName !== letter.recipientName) {
    throw new ConfirmationMismatchError(
      `Confirmed recipient "${confirmation.confirmedRecipientName}" does not match letter recipient "${letter.recipientName}"`
    );
  }
  if (confirmation.confirmedQuotedCostCents !== letter.quotedCostCents) {
    throw new ConfirmationMismatchError(
      `Confirmed cost ${confirmation.confirmedQuotedCostCents} does not match quoted cost ${letter.quotedCostCents} -- the quote may have expired, re-quote before mailing`
    );
  }

  const db = getDb();
  const confirmedAt = nowIso();
  db.prepare(`
    UPDATE dispute_letters
    SET confirmed_by_user = 1, confirmed_at = @confirmedAt, updated_at = @updatedAt
    WHERE id = @id
  `).run({ id: letterId, confirmedAt, updatedAt: confirmedAt });

  const result = await letterstream_authorize_mail({ orderId: letter.letterstreamOrderId });

  db.prepare(`
    UPDATE dispute_letters
    SET authorized = 1,
        authorized_at = @authorizedAt,
        letterstream_tracking_id = @trackingId,
        mail_status = 'mailed',
        mail_status_updated_at = @authorizedAt,
        updated_at = @updatedAt
    WHERE id = @id
  `).run({
    id: letterId,
    authorizedAt: result.authorizedAt,
    trackingId: result.trackingId,
    updatedAt: nowIso(),
  });

  // Only now does the dispute item itself move to "mailed" -- this is
  // the single place that transition happens, guaranteeing it can
  // never occur without a real LetterStream authorization behind it.
  transitionDisputeState(letter.disputeItemId, 'mailed');

  return getLetter(letterId)!;
}

/**
 * Step 4: refreshes mail status for a letter that has already been
 * authorized, via letterstream_track_mail. Read-only against
 * LetterStream -- never touches money or authorization state.
 */
export async function refreshMailStatus(letterId: string) {
  const letter = getLetter(letterId);
  if (!letter) throw new Error(`Letter ${letterId} not found`);
  if (!letter.authorized || !letter.letterstreamOrderId) {
    throw new Error(`Letter ${letterId} has not been mailed yet -- nothing to track`);
  }
  const status = await letterstream_track_mail(letter.letterstreamOrderId);

  const db = getDb();
  const at = nowIso();
  db.prepare(`
    UPDATE dispute_letters
    SET mail_status = @status, mail_status_updated_at = @at, updated_at = @at
    WHERE id = @id
  `).run({ id: letterId, status: status.status, at });

  return getLetter(letterId)!;
}
