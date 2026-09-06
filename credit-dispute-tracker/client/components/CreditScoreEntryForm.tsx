import React, { useState } from 'react';
import type { Bureau, NewCreditScoreEntry } from '../types';

interface Props {
  onSubmit: (entry: NewCreditScoreEntry) => Promise<void>;
}

const BUREAUS: Bureau[] = ['equifax', 'experian', 'transunion', 'other'];

export default function CreditScoreEntryForm({ onSubmit }: Props) {
  const [recordedAt, setRecordedAt] = useState(() => new Date().toISOString().slice(0, 10));
  const [score, setScore] = useState('');
  const [bureau, setBureau] = useState<Bureau>('experian');
  const [source, setSource] = useState('');
  const [notes, setNotes] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const numericScore = Number(score);
    if (!Number.isInteger(numericScore) || numericScore < 300 || numericScore > 900) {
      setError('Enter a whole-number score between 300 and 900.');
      return;
    }
    setSaving(true);
    try {
      await onSubmit({ recordedAt, score: numericScore, bureau, source: source || undefined, notes: notes || undefined });
      setScore('');
      setSource('');
      setNotes('');
    } catch (err: any) {
      setError(err.message || 'Failed to save entry');
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="credit-score-entry-form">
      <h3>Add a score reading</h3>
      <p className="hint">
        There's no live bureau feed wired up -- enter whatever you see when you
        check a score (bureau site, card issuer, annual pull, etc).
      </p>
      <label>
        Date
        <input type="date" value={recordedAt} onChange={e => setRecordedAt(e.target.value)} required />
      </label>
      <label>
        Score
        <input type="number" min={300} max={900} value={score} onChange={e => setScore(e.target.value)} required />
      </label>
      <label>
        Bureau
        <select value={bureau} onChange={e => setBureau(e.target.value as Bureau)}>
          {BUREAUS.map(b => <option key={b} value={b}>{b}</option>)}
        </select>
      </label>
      <label>
        Source (optional)
        <input type="text" placeholder="e.g. Chase Credit Journey" value={source} onChange={e => setSource(e.target.value)} />
      </label>
      <label>
        Notes (optional)
        <textarea value={notes} onChange={e => setNotes(e.target.value)} />
      </label>
      {error && <p className="error">{error}</p>}
      <button type="submit" disabled={saving}>{saving ? 'Savingâ€¦' : 'Add entry'}</button>
    </form>
  );
}
