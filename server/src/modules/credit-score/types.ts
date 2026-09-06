export type Bureau = 'equifax' | 'experian' | 'transunion' | 'other';

export interface CreditScoreEntry {
  id: string;
  userId: string;
  bureau: Bureau;
  score: number;
  scoreModel: string | null;
  recordedOn: string; // ISO date (yyyy-mm-dd)
  source: string;
  notes: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface CreditScoreInput {
  bureau: Bureau;
  score: number;
  scoreModel?: string;
  recordedOn: string;
  source?: string;
  notes?: string;
}
