export type Bureau = 'equifax' | 'experian' | 'transunion' | 'other';
export type DisputeBureau = 'equifax' | 'experian' | 'transunion';
export type DisputeState = 'drafted' | 'mailed' | 'resolved';
export type ResolutionOutcome = 'removed' | 'updated' | 'verified' | 'no_change';
export type MailStatus = 'not_sent' | 'quoted' | 'mailed' | 'in_transit' | 'delivered' | 'returned' | 'failed';

export interface CreditScoreEntry {
  id: string;
  recordedAt: string;
  score: number;
  bureau: Bureau;
  source?: string;
  notes?: string;
}

export interface NewCreditScoreEntry {
  recordedAt: string;
  score: number;
  bureau: Bureau;
  source?: string;
  notes?: string;
}

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

export interface DisputeLetter {
  id: string;
  disputeItemId: string;
  letterText: string;
  recipientName: string;
  recipientAddressLine1: string;
  recipientAddressLine2?: string;
  recipientCity: string;
  recipientState: string;
  recipientZip: string;
  letterstreamOrderId?: string;
  quotedCostCents?: number;
  quoteCurrency?: string;
  quotedAt?: string;
  confirmedByUser: boolean;
  confirmedAt?: string;
  authorized: boolean;
  authorizedAt?: string;
  letterstreamTrackingId?: string;
  mailStatus: MailStatus;
  mailStatusUpdatedAt?: string;
}
