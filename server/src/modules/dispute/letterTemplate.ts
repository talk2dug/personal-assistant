import { Dispute } from './types';
import { BUREAU_ADDRESSES } from './bureauAddresses';

export interface ConsumerProfile {
  fullName: string;
  addressLines: string[];
}

const REASON_TEXT: Record<Dispute['disputeReason'], string> = {
  not_mine: 'This account/item does not belong to me and I have never had a relationship with this creditor.',
  incorrect_balance: 'The balance reported for this item is incorrect.',
  paid_in_full: 'This item was paid in full and should be reported as such.',
  duplicate: 'This item is a duplicate of another item already reporting on my file.',
  other: 'This item is inaccurate and should be corrected or removed.',
};

/**
 * Builds the plain-text body of a dispute letter. Kept deliberately simple
 * and literal (no hidden formatting) since this text is exactly what gets
 * shown to the user for the hard-confirm review before anything is mailed.
 */
export function buildDisputeLetterBody(dispute: Dispute, consumer: ConsumerProfile): string {
  const bureau = BUREAU_ADDRESSES[dispute.bureau];
  const today = new Date().toISOString().slice(0, 10);

  return [
    consumer.fullName,
    ...consumer.addressLines,
    '',
    today,
    '',
    bureau.name,
    ...bureau.addressLines,
    '',
    'Re: Request for Investigation of Inaccurate Information (Fair Credit Reporting Act, 15 U.S.C. Â§ 1681i)',
    '',
    `To whom it may concern,`,
    '',
    `I am writing to dispute the following item reported by ${dispute.creditorName}` +
      (dispute.accountReference ? ` (account reference: ${dispute.accountReference})` : '') +
      ':',
    '',
    dispute.itemDescription,
    '',
    REASON_TEXT[dispute.disputeReason],
    '',
    'Under the Fair Credit Reporting Act, I am requesting that you investigate this matter and correct or remove the inaccurate information. Please send me the results of your investigation and an updated copy of my credit report if changes are made.',
    '',
    'Thank you for your attention to this matter.',
    '',
    'Sincerely,',
    consumer.fullName,
  ].join('\n');
}
