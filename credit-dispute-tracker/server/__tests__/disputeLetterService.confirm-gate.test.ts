import { describe, it, expect, vi } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'letter-test-'));
process.env.DASHBOARD_DATA_DIR = tmpDir;
process.env.DASHBOARD_DB_PATH = path.join(tmpDir, 'test.db');
process.env.LETTERSTREAM_API_KEY = 'test-key';

vi.mock('../integrations/letterstream/client', () => ({
  letterstream_send_mail: vi.fn(async () => ({
    orderId: 'order-1',
    quotedCostCents: 812,
    currency: 'USD',
    quotedAt: new Date().toISOString(),
  })),
  letterstream_authorize_mail: vi.fn(async () => ({
    orderId: 'order-1',
    authorizedAt: new Date().toISOString(),
    trackingId: 'track-1',
  })),
}));

import { createDisputeItem } from '../models/dispute';
import {
  draftLetter, quoteLetter, confirmAndMailLetter, ConfirmationMismatchError,
} from '../services/disputeLetterService';
import { letterstream_authorize_mail } from '../integrations/letterstream/client';

function makeDraftedAndQuotedLetter(recipientName: string) {
  const item = createDisputeItem({
    bureau: 'equifax', creditorName: 'Acme Card', itemDescription: 'x', disputeReason: 'y',
  });
  const letter = draftLetter({
    disputeItemId: item.id,
    letterText: 'Please investigate...',
    recipient: { name: recipientName, addressLine1: '123 Main St', city: 'Atlanta', state: 'GA', zip: '30309' },
  });
  return { item, letter };
}

describe('confirmAndMailLetter hard gate', () => {
  it('refuses to authorize when confirmed recipient does not match', async () => {
    const { letter } = makeDraftedAndQuotedLetter('Equifax Disputes');
    const quoted = await quoteLetter(letter.id);

    await expect(confirmAndMailLetter(letter.id, {
      confirmedRecipientName: 'Wrong Name',
      confirmedQuotedCostCents: quoted.quotedCostCents!,
      userApproved: true,
    })).rejects.toThrow(ConfirmationMismatchError);

    expect(letterstream_authorize_mail).not.toHaveBeenCalled();
  });

  it('refuses to authorize when confirmed cost does not match', async () => {
    const { letter } = makeDraftedAndQuotedLetter('TransUnion Disputes');
    await quoteLetter(letter.id);

    await expect(confirmAndMailLetter(letter.id, {
      confirmedRecipientName: 'TransUnion Disputes',
      confirmedQuotedCostCents: 1,
      userApproved: true,
    })).rejects.toThrow(ConfirmationMismatchError);

    expect(letterstream_authorize_mail).not.toHaveBeenCalled();
  });

  it('authorizes and moves the dispute to mailed only when confirmation matches exactly', async () => {
    const { letter } = makeDraftedAndQuotedLetter('Equifax Disputes');
    const quoted = await quoteLetter(letter.id);

    const mailed = await confirmAndMailLetter(letter.id, {
      confirmedRecipientName: quoted.recipientName,
      confirmedQuotedCostCents: quoted.quotedCostCents!,
      userApproved: true,
    });

    expect(mailed.authorized).toBe(true);
    expect(mailed.mailStatus).toBe('mailed');
    expect(letterstream_authorize_mail).toHaveBeenCalledTimes(1);
  });

  it('refuses to mail a letter twice', async () => {
    const { letter } = makeDraftedAndQuotedLetter('Equifax Disputes');
    const quoted = await quoteLetter(letter.id);
    const confirmation = {
      confirmedRecipientName: quoted.recipientName,
      confirmedQuotedCostCents: quoted.quotedCostCents!,
      userApproved: true as const,
    };
    await confirmAndMailLetter(letter.id, confirmation);
    await expect(confirmAndMailLetter(letter.id, confirmation)).rejects.toThrow();
  });
});
