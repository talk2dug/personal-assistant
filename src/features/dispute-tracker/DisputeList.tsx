import React from 'react';
import { Dispute } from './types';

interface Props {
  disputes: Dispute[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function DisputeList({ disputes, selectedId, onSelect }: Props) {
  const byBureau = disputes.reduce<Record<string, Dispute[]>>((acc, d) => {
    (acc[d.bureau] ??= []).push(d);
    return acc;
  }, {});

  return (
    <div className="dispute-list">
      {Object.entries(byBureau).map(([bureau, items]) => (
        <div key={bureau}>
          <h3>{bureau}</h3>
          <ul>
            {items.map((d) => (
              <li key={d.id}>
                <button
                  className={d.id === selectedId ? 'selected' : ''}
                  onClick={() => onSelect(d.id)}
                >
                  {d.creditorName} â€” {d.state}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
