export type Bureau = 'equifax' | 'experian' | 'transunion' | 'other';

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
