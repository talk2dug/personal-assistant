import React, { useEffect, useState, useCallback } from 'react';
import CreditScoreHistoryChart from '../components/CreditScoreHistoryChart';
import CreditScoreEntryForm from '../components/CreditScoreEntryForm';
import { fetchCreditScoreHistory, addCreditScoreEntry } from '../api/creditScoreApi';
import type { CreditScoreEntry, NewCreditScoreEntry } from '../types';

export default function CreditScorePage() {
  const [entries, setEntries] = useState<CreditScoreEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setEntries(await fetchCreditScoreHistory());
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleAdd = async (entry: NewCreditScoreEntry) => {
    const saved = await addCreditScoreEntry(entry);
    setEntries(prev => [...prev, saved].sort((a, b) => a.recordedAt.localeCompare(b.recordedAt)));
  };

  return (
    <div className="credit-score-page">
      <h2>Credit Score</h2>
      <p className="hint">
        Manual tracking only -- no live bureau feed. Log a reading whenever
        you check one, from any bureau or free-score source.
      </p>
      {loading && <p>Loadingâ€¦</p>}
      {error && <p className="error">{error}</p>}
      {!loading && <CreditScoreHistoryChart entries={entries} />}
      <CreditScoreEntryForm onSubmit={handleAdd} />
    </div>
  );
}
