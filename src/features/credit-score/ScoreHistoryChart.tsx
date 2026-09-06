import React from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'; // new dependency, see docs/phase5-credit-dispute-tracker.md
import { CreditScoreEntry, Bureau } from './types';

interface Props {
  entries: CreditScoreEntry[];
}

const COLORS: Record<Bureau, string> = {
  equifax: '#c0392b',
  experian: '#2980b9',
  transunion: '#27ae60',
  other: '#7f8c8d',
};

// Reshape [{bureau, score, recordedOn}] into one row per date with a column
// per bureau, which is what recharts wants for multi-line series.
function toChartRows(entries: CreditScoreEntry[]) {
  const byDate = new Map<string, any>();
  for (const e of entries) {
    const row = byDate.get(e.recordedOn) ?? { recordedOn: e.recordedOn };
    row[e.bureau] = e.score;
    byDate.set(e.recordedOn, row);
  }
  return Array.from(byDate.values()).sort((a, b) => a.recordedOn.localeCompare(b.recordedOn));
}

export function ScoreHistoryChart({ entries }: Props) {
  if (entries.length === 0) {
    return <p>No score history yet â€” add your first entry above.</p>;
  }
  const data = toChartRows(entries);
  const bureausPresent = Array.from(new Set(entries.map((e) => e.bureau)));

  return (
    <ResponsiveContainer width="100%" height={320}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="recordedOn" />
        <YAxis domain={[300, 850]} />
        <Tooltip />
        <Legend />
        {bureausPresent.map((b) => (
          <Line key={b} type="monotone" dataKey={b} stroke={COLORS[b]} connectNulls />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
