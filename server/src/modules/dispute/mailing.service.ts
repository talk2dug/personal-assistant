import { randomUUID, createHash } from 'crypto';
import { pool } from '../../db/pool';
import { LetterStreamClient } from '../../integrations/letterstream/types';
import { DisputeLetter, RecipientSnapshot } from './types';
import { getDispute, transitionDisputeState } from './service';
import { buildDisputeLetterBody, ConsumerProfile } from './letterTemplate';
import { BUREAU_ADDRESSES } from './bureauAddresses';

function contentHash(recipient: RecipientSnapshot, body: string, costCents: number | null): string {
  return createHash('sha256')
    .update(JSON.stringify({ recipient, body, costCents }))
    .digest('hex');
}

function rowToLetter(row: any): DisputeLetter {
  return {
    id: row.id,
    disputeId: row.dispute_id,
    letterVersion: row.letter_version,
    recipientSnapshot: row.recipient_snapshot,
    letterBody: row.letter_body,
    letterstreamMailId: row.letterstream_mail_id,
    quotedCostCents: row.quoted_cost_cents,
    quoteCurrency: row.quote_currency,
    contentHash: row.content_hash,
    status: row.status,
    confirmedHash: row.confirmed_hash,
    confirmedByUserAt: row.confirmed_by_user_at,
    authorizedAt: row.authorized_at,
    mailedAt: row.mailed_at,
    trackingNumber: row.tracking_number,
    lastTrackedStatus: row.last_tracked_status,
    lastTrackedAt: row.last_tracked_at,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

async function logEvent(letterId: string, eventType: string, payload: unknown) {
  await pool.query(
    `INSERT INTO dispute_mail_events (id, dispute_letter_id, event_type, event_payload)
     VALUES ($1,$2,$3,$4)`,
    [randomUUID(), letterId, eventType, JSON.stringify(payload ?? {})]
  );
}

export class DisputeAlreadyMailedError extends Error {}
export class LetterNotConfirmedError extends Error {}
export class LetterContentChangedSinceConfirmError extends Error {}
export class InvalidLetterStateError extends Error {}

export class MailingService {
  constructor(private readonly letterstream: LetterStreamClient) {}

  /** Step 1: build the letter, get a quote. Does NOT mail anything. */
  async draftLetter(
    userId: string,
    disputeId: string,
    consumer: ConsumerProfile
  ): Promise<DisputeLetter> {
    const dispute = await getDispute(userId, disputeId);
    if (!dispute) throw new Error('dispute not found');

    const bureau = BUREAU_ADDRESSES[dispute.bureau];
    const recipient: RecipientSnapshot = { bureauName: bureau.name, addressLines: bureau.addressLines };
    const body = buildDisputeLetterBody(dispute, consumer);

    const { rows: versionRows } = await pool.query(
      `SELECT COALESCE(MAX(letter_version), 0) + 1 AS next_version
       FROM dispute_letters WHERE dispute_id = $1`,
      [disputeId]
    );
    const nextVersion = versionRows[0].next_version;

    const id = randomUUID();
    const idempotencyKey = id; // stable per-draft key, safe to retry sendMail with

    const { rows } = await pool.query(
      `INSERT INTO dispute_letters
         (id, dispute_id, letter_version, recipient_snapshot, letter_body, status)
       VALUES ($1,$2,$3,$4,$5,'draft')
       RETURNING *`,
      [id, disputeId, nextVersion, JSON.stringify(recipient), body]
    );
    await logEvent(id, 'draft_created', { disputeId, letterVersion: nextVersion });

    const quote = await this.letterstream.sendMail({
      recipient: { name: recipient.bureauName, addressLines: recipient.addressLines },
      senderName: consumer.fullName,
      senderAddressLines: consumer.addressLines,
      body,
      idempotencyKey,
    });

    const hash = contentHash(recipient, body, quote.quotedCostCents);
    const { rows: updated } = await pool.query(
      `UPDATE dispute_letters SET
         letterstream_mail_id = $2,
         quoted_cost_cents = $3,
         quote_currency = $4,
         content_hash = $5,
         status = 'quoted',
         updated_at = now()
       WHERE id = $1
       RETURNING *`,
      [id, quote.mailId, quote.quotedCostCents, quote.currency, hash]
    );
    await logEvent(id, 'quote_received', { mailId: quote.mailId, quotedCostCents: quote.quotedCostCents });

    return rowToLetter(updated[0]);
  }

  /**
   * Step 2: the hard confirm. The caller (API layer / UI) is responsible
   * for having actually shown the user the recipient, full letter text, and
   * quoted cost before this is called. This just records that confirmation
   * happened and pins the exact content that was approved.
   */
  async confirmLetter(letterId: string): Promise<DisputeLetter> {
    const letter = await this.getLetterOrThrow(letterId);
    if (letter.status === 'confirmed') return letter; // idempotent
    if (letter.status !== 'quoted') {
      throw new InvalidLetterStateError(
        `Cannot confirm a letter in status '${letter.status}' (expected 'quoted')`
      );
    }
    const recipient = letter.recipientSnapshot;
    const hash = contentHash(recipient, letter.letterBody, letter.quotedCostCents);

    const { rows } = await pool.query(
      `UPDATE dispute_letters SET
         status = 'confirmed',
         confirmed_hash = $2,
         confirmed_by_user_at = now(),
         updated_at = now()
       WHERE id = $1
       RETURNING *`,
      [letterId, hash]
    );
    await logEvent(letterId, 'user_confirmed', { hash });
    return rowToLetter(rows[0]);
  }

  /**
   * Step 3: authorize real postage. This is the ONLY path in the whole
   * feature that reaches letterstream_authorize_mail, and it is gated by:
   *   1. letter.status === 'confirmed'
   *   2. explicitApproval === true on the request
   *   3. the letter's content hash right now still equals confirmed_hash
   *      (nothing changed since the user reviewed it)
   * Any failure here throws instead of calling the provider.
   */
  async authorizeLetter(letterId: string, explicitApproval: true): Promise<DisputeLetter> {
    if (explicitApproval !== true) {
      throw new LetterNotConfirmedError('explicitApproval must be true to authorize real postage');
    }
    const letter = await this.getLetterOrThrow(letterId);
    if (letter.status !== 'confirmed') {
      throw new LetterNotConfirmedError(
        `Letter must be 'confirmed' before authorizing (currently '${letter.status}')`
      );
    }
    const currentHash = contentHash(letter.recipientSnapshot, letter.letterBody, letter.quotedCostCents);
    if (currentHash !== letter.confirmedHash) {
      throw new LetterContentChangedSinceConfirmError(
        'Letter content changed since it was confirmed -- re-confirm before authorizing'
      );
    }
    if (!letter.letterstreamMailId) {
      throw new InvalidLetterStateError('Letter has no LetterStream mail id to authorize');
    }

    await logEvent(letterId, 'authorize_requested', { mailId: letter.letterstreamMailId });

    let result;
    try {
      result = await this.letterstream.authorizeMail({
        mailId: letter.letterstreamMailId,
        idempotencyKey: letterId,
      });
    } catch (err: any) {
      await logEvent(letterId, 'authorize_failed', { message: err?.message });
      await pool.query(`UPDATE dispute_letters SET status = 'failed', updated_at = now() WHERE id = $1`, [
        letterId,
      ]);
      throw err;
    }

    const { rows } = await pool.query(
      `UPDATE dispute_letters SET
         status = 'mailed',
         authorized_at = $2,
         mailed_at = $2,
         tracking_number = $3,
         updated_at = now()
       WHERE id = $1
       RETURNING *`,
      [letterId, result.authorizedAt, result.trackingNumber]
    );
    await logEvent(letterId, 'authorized', { trackingNumber: result.trackingNumber });

    const updatedLetter = rowToLetter(rows[0]);
    // Reflect on the parent dispute: it's now been mailed to that bureau.
    const dispute = await getDisputeForLetter(updatedLetter.disputeId);
    if (dispute && dispute.state === 'drafted') {
      await transitionDisputeState(dispute.userId, dispute.id, 'mailed');
    }
    return updatedLetter;
  }

  /** Read-only. No cost, no gate needed beyond "it's actually been mailed". */
  async trackLetter(letterId: string): Promise<DisputeLetter> {
    const letter = await this.getLetterOrThrow(letterId);
    if (!['mailed', 'in_transit', 'delivered', 'returned'].includes(letter.status)) {
      throw new InvalidLetterStateError(`Cannot track a letter in status '${letter.status}'`);
    }
    if (!letter.letterstreamMailId) {
      throw new InvalidLetterStateError('Letter has no LetterStream mail id to track');
    }

    const tracked = await this.letterstream.trackMail({ mailId: letter.letterstreamMailId });
    const mappedStatus = mapProviderStatus(tracked.status);

    const { rows } = await pool.query(
      `UPDATE dispute_letters SET
         status = $2,
         tracking_number = COALESCE($3, tracking_number),
         last_tracked_status = $4,
         last_tracked_at = now(),
         updated_at = now()
       WHERE id = $1
       RETURNING *`,
      [letterId, mappedStatus, tracked.trackingNumber, tracked.status]
    );
    await logEvent(letterId, 'tracking_update', { rawStatus: tracked.status });
    return rowToLetter(rows[0]);
  }

  /** Only allowed before real postage is committed. */
  async cancelLetter(letterId: string): Promise<DisputeLetter> {
    const letter = await this.getLetterOrThrow(letterId);
    if (!['draft', 'quoted', 'confirmed'].includes(letter.status)) {
      throw new InvalidLetterStateError(`Cannot cancel a letter in status '${letter.status}'`);
    }
    const { rows } = await pool.query(
      `UPDATE dispute_letters SET status = 'cancelled', updated_at = now() WHERE id = $1 RETURNING *`,
      [letterId]
    );
    await logEvent(letterId, 'cancelled', {});
    return rowToLetter(rows[0]);
  }

  private async getLetterOrThrow(letterId: string): Promise<DisputeLetter> {
    const { rows } = await pool.query(`SELECT * FROM dispute_letters WHERE id = $1`, [letterId]);
    if (!rows[0]) throw new Error('dispute letter not found');
    return rowToLetter(rows[0]);
  }
}

async function getDisputeForLetter(disputeId: string) {
  const { rows } = await pool.query(`SELECT * FROM disputes WHERE id = $1`, [disputeId]);
  if (!rows[0]) return null;
  return {
    id: rows[0].id,
    userId: rows[0].user_id,
    state: rows[0].state as 'drafted' | 'mailed' | 'resolved',
  };
}

function mapProviderStatus(raw: string): string {
  const lowered = raw.toLowerCase();
  if (lowered.includes('deliver')) return 'delivered';
  if (lowered.includes('return') || lowered.includes('undeliverable')) return 'returned';
  if (lowered.includes('transit')) return 'in_transit';
  return 'mailed';
}
