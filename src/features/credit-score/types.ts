export type Bureau = 'equifax' | 'experian' | 'transunion' | 'other';

export interface CreditScoreEntry {
  id: string;
  bureau: Bureau;
  score: number;
  scoreModel: string | null;
  recordedOn: string;
  source: string;
  notes: string | null;
}

export interface CreditScoreFormValues {
  bureau: Bureau;
  score: string; // string in the form, parsed before submit
  scoreModel: string;
  recordedOn: string;
  source: string;
  notes: string;
}
