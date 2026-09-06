import React, { useEffect, useState, useCallback } from 'react';
import { fetchDisputeItems } from '../api/disputeApi';
import DisputeCard from '../components/DisputeCard';
import type { DisputeItem, DisputeState } from '../types';

const STATES: DisputeState[] = ['drafted', 'mailed', 'resolved'];
const STATE_LABELS: Record<DisputeState, string> = {
  drafted: 'Drafted',
  mailed: 'Mailed',
  resolved: 'Resolved',
};

export default function DisputeTrackerPage() {
  const [items, setItems] = useState<DisputeItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await fetchDisputeItems());
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="dispute-tracker-page">
      <h2>Dispute Tracker</h2>
      <p className="hint">
        Each disputed item is tracked separately per credit bureau, through
        three states: drafted â†’ mailed â†’ resolved.
      </p>
      {loading && <p>Loadingâ€¦</p>}
      {error && <p className="error">{error}</p>}
      <div className="dispute-board">
        {STATES.map(state => (
          <div key={state} className="dispute-column">
            <h3>{STATE_LABELS[state]}</h3>
            {items.filter(i => i.state === state).map(item => (
              <DisputeCard key={item.id} item={item} />
            ))}
            {items.filter(i => i.state === state).length === 0 && (
              <p className="empty-state">Nothing here</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
