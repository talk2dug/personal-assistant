export type Bureau = 'equifax' | 'experian' | 'transunion';
export type DisputeState = 'drafted' | 'mailed' | 'resolved';
export type DisputeReason = 'not_mine' | 'incorrect_balance' | 'paid_in_full' | 'duplicate' | 'other';
export type DisputeResolution = 'removed' | 'updated' | 'verified_accurate' | 'no_response';

export interface Dispute {
  id: string;
  userId: string;
  bureau: Bureau;
  creditorName: string;
  accountReference: string | null;
  itemDescription: string;
  disputeReason: DisputeReason;
  state: DisputeState;
  resolution: DisputeResolution | null;
  resolutionNotes: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface DisputeInput {
  bureau: Bureau;
  creditorName: string;
  accountReference?: string;
  itemDescription: string;
  disputeReason: DisputeReason;
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

export interface RecipientSnapshot {
  bureauName: string;
  addressLines: string[];
}

export interface DisputeLetter {
  id: string;
  disputeId: string;
  letterVersion: number;
  recipientSnapshot: RecipientSnapshot;
  letterBody: string;
  letterstreamMailId: string | null;
  quotedCostCents: number | null;
  quoteCurrency: string;
  contentHash: string | null;
  status: LetterStatus;
  confirmedHash: string | null;
  confirmedByUserAt: string | null;
  authorizedAt: string | null;
  mailedAt: string | null;
  trackingNumber: string | null;
  lastTrackedStatus: string | null;
  lastTrackedAt: string | null;
  createdAt: string;
  updatedAt: string;
}
