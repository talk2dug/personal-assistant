import React, { useMemo } from 'react';
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
} from 'recharts';
import type { CreditScoreEntry, Bureau } from '../types';

interface Props {
  entries: CreditScoreEntry[];
}

const BUREAU_COLORS: Record<Bureau, string> = {
  equifax: '#c0392b',
  experian: '#2980b9',
  transunion: '#27ae60',
  other: '#8e44ad',
};

/**
 * Pivots flat entries (one row per reading) into one row per date with
 * a column per bureau, which is what recharts wants for multi-line
 * charts.
 */
function pivotByDate(entries: CreditScoreEntry[]) {
  const byDate = new Map<string, any>();
  for (const e of entries) {
    const row = byDate.get(e.recordedAt) || { recordedAt: e.recordedAt };
    row[e.bureau] = e.score;
    byDate.set(e.recordedAt, row);
  }
  return Array.from(byDate.values()).sort((a, b) => a.recordedAt.localeCompare(b.recordedAt));
}

export default function CreditScoreHistoryChart({ entries }: Props) {
  const data = useMemo(() => pivotByDate(entries), [entries]);
  const bureausPresent = useMemo(
    () => Array.from(new Set(entries.map(e => e.bureau))) as Bureau[],
    [entries]
  );

  if (entries.length === 0) {
    return <p className="empty-state">No score history yet -- add your first reading below.</p>;
  }

  return (
    <ResponsiveContainer width="100%" height={320}>
      <LineChart data={data} margin={{ top: 8, right: 24, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="recordedAt" />
        <YAxis domain={[300, 900]} />
        <Tooltip />
        <Legend />
        {bureausPresent.map(b => (
          <Line
            key={b}
            type="monotone"
            dataKey={b}
            name={b}
            stroke={BUREAU_COLORS[b]}
            connectNulls
            dot={{ r: 3 }}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
