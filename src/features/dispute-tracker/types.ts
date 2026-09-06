export type Bureau = 'equifax' | 'experian' | 'transunion';
export type DisputeState = 'drafted' | 'mailed' | 'resolved';
export type DisputeReason = 'not_mine' | 'incorrect_balance' | 'paid_in_full' | 'duplicate' | 'other';
export type DisputeResolution = 'removed' | 'updated' | 'verified_accurate' | 'no_response';

export interface Dispute {
  id: string;
  bureau: Bureau;
  creditorName: string;
  accountReference: string | null;
  itemDescription: string;
  disputeReason: DisputeReason;
  state: DisputeState;
  resolution: DisputeResolution | null;
  resolutionNotes: string | null;
  createdAt: string;
}

export type LetterStatus =
  | 'draft'
  | 'quoted'
  | 'confirmed'
  | 'authorized'
  | 'mailed'
  | 'in_transit'
  | 'delivered'
  | 'returned'
  | 'failed'
  | 'cancelled';

export interface DisputeLetter {
  id: string;
  disputeId: string;
  letterVersion: number;
  recipientSnapshot: { bureauName: string; addressLines: string[] };
  letterBody: string;
  quotedCostCents: number | null;
  quoteCurrency: string;
  status: LetterStatus;
  trackingNumber: string | null;
  lastTrackedStatus: string | null;
}

export interface DisputeFormValues {
  bureau: Bureau;
  creditorName: string;
  accountReference: string;
  itemDescription: string;
  disputeReason: DisputeReason;
}
