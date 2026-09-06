import React, { useState } from 'react';
import { CreditScoreFormValues, Bureau } from './types';

const BUREAUS: Bureau[] = ['equifax', 'experian', 'transunion', 'other'];

const EMPTY: CreditScoreFormValues = {
  bureau: 'equifax',
  score: '',
  scoreModel: '',
  recordedOn: new Date().toISOString().slice(0, 10),
  source: 'manual',
  notes: '',
};

interface Props {
  onSubmit: (values: CreditScoreFormValues) => Promise<void>;
}

export function ScoreEntryForm({ onSubmit }: Props) {
  const [values, setValues] = useState<CreditScoreFormValues>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const set = <K extends keyof CreditScoreFormValues>(key: K, val: CreditScoreFormValues[K]) =>
    setValues((v) => ({ ...v, [key]: val }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const scoreNum = Number(values.score);
    if (!Number.isInteger(scoreNum) || scoreNum < 300 || scoreNum > 850) {
      setError('Score must be a whole number between 300 and 850.');
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(values);
      setValues(EMPTY);
    } catch (err: any) {
      setError(err.message || 'Failed to save score.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="score-entry-form">
      <label>
        Bureau
        <select value={values.bureau} onChange={(e) => set('bureau', e.target.value as Bureau)}>
          {BUREAUS.map((b) => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
      </label>
      <label>
        Score
        <input
          type="number"
          min={300}
          max={850}
          value={values.score}
          onChange={(e) => set('score', e.target.value)}
          required
        />
      </label>
      <label>
        Model (optional)
        <input
          type="text"
          placeholder="FICO 8, VantageScore 3.0..."
          value={values.scoreModel}
          onChange={(e) => set('scoreModel', e.target.value)}
        />
      </label>
      <label>
        Date
        <input
          type="date"
          value={values.recordedOn}
          max={new Date().toISOString().slice(0, 10)}
          onChange={(e) => set('recordedOn', e.target.value)}
          required
        />
      </label>
      <label>
        Source
        <input
          type="text"
          placeholder="manual, bank app, annualcreditreport.com..."
          value={values.source}
          onChange={(e) => set('source', e.target.value)}
        />
      </label>
      <label>
        Notes
        <textarea value={values.notes} onChange={(e) => set('notes', e.target.value)} />
      </label>
      {error && <p className="form-error">{error}</p>}
      <button type="submit" disabled={submitting}>
        {submitting ? 'Saving...' : 'Add score'}
      </button>
    </form>
  );
}
