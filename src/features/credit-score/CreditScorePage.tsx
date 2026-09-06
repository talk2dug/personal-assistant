import React, { useEffect, useState, useCallback } from 'react';
import { ScoreEntryForm } from './ScoreEntryForm';
import { ScoreHistoryChart } from './ScoreHistoryChart';
import { fetchCreditScores, createCreditScore, deleteCreditScore } from './api';
import { CreditScoreEntry, CreditScoreFormValues } from './types';

export function CreditScorePage() {
  const [entries, setEntries] = useState<CreditScoreEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setEntries(await fetchCreditScores());
    } catch (err: any) {
      setError(err.message || 'Failed to load scores.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleSubmit = async (values: CreditScoreFormValues) => {
    await createCreditScore(values);
    await load();
  };

  const handleDelete = async (id: string) => {
    await deleteCreditScore(id);
    await load();
  };

  return (
    <section className="credit-score-page">
      <h1>Credit Score</h1>
      <p className="muted">
        Manually tracked â€” there's no live bureau feed. Enter scores as you check them.
      </p>

      <ScoreEntryForm onSubmit={handleSubmit} />

      {loading && <p>Loading...</p>}
      {error && <p className="form-error">{error}</p>}

      <ScoreHistoryChart entries={entries} />

      <table className="score-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Bureau</th>
            <th>Score</th>
            <th>Model</th>
            <th>Source</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {entries
            .slice()
            .reverse()
            .map((e) => (
              <tr key={e.id}>
                <td>{e.recordedOn}</td>
                <td>{e.bureau}</td>
                <td>{e.score}</td>
                <td>{e.scoreModel || 'â€”'}</td>
                <td>{e.source}</td>
                <td>
                  <button onClick={() => handleDelete(e.id)}>Delete</button>
                </td>
              </tr>
            ))}
        </tbody>
      </table>
    </section>
  );
}
