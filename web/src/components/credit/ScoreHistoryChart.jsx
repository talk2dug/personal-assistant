import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

// Same HUD palette as finance/ProjectionChart.jsx, one distinct color per bureau so
// scores are never blended or averaged across bureaus (they're genuinely different
// numbers from different sources -- see personal_db.py's schema comment).
const BUREAU_COLORS = {
  experian: '#22e8ff',
  equifax: '#ffb020',
  transunion: '#ff5da2',
  other: '#9aa0a6',
}
const BUREAU_LABEL = { experian: 'Experian', equifax: 'Equifax', transunion: 'TransUnion', other: 'Other' }
const GRID = 'rgba(34, 232, 255, 0.12)'
const DIM_TEXT = '#5f8a94'

/** One row per distinct recorded_on date, one column per bureau -- a bureau with no
 * entry on a given date is simply absent from that row (not zero, not interpolated by
 * us); connectNulls on <Line> draws the trend across those gaps. */
function buildSeries(entries) {
  const byDate = new Map()
  for (const e of entries) {
    const row = byDate.get(e.recorded_on) || { date: e.recorded_on }
    row[e.bureau] = e.score
    byDate.set(e.recorded_on, row)
  }
  return [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
}

function HudTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-date">{label}</div>
      {payload.map((p) => (
        <div key={p.dataKey} className="chart-tooltip-value" style={{ color: p.color }}>
          {BUREAU_LABEL[p.dataKey] || p.dataKey}: {p.value}
        </div>
      ))}
    </div>
  )
}

export default function ScoreHistoryChart({ entries }) {
  const data = buildSeries(entries)
  const bureausPresent = [...new Set(entries.map((e) => e.bureau))]

  if (data.length === 0) return <p className="empty-hint">Nothing to chart yet.</p>

  return (
    <div className="credit-score-chart">
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="2 6" stroke={GRID} />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: DIM_TEXT, fontFamily: 'JetBrains Mono' }}
            minTickGap={30}
            axisLine={{ stroke: GRID }}
            tickLine={false}
          />
          <YAxis
            domain={[300, 850]}
            tick={{ fontSize: 10, fill: DIM_TEXT, fontFamily: 'JetBrains Mono' }}
            axisLine={{ stroke: GRID }}
            tickLine={false}
            width={40}
          />
          <Tooltip content={<HudTooltip />} />
          <Legend formatter={(v) => BUREAU_LABEL[v] || v} wrapperStyle={{ fontSize: 11 }} />
          {bureausPresent.map((b) => (
            <Line
              key={b}
              type="monotone"
              dataKey={b}
              stroke={BUREAU_COLORS[b] || '#ffffff'}
              strokeWidth={2}
              dot={{ r: 3 }}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
