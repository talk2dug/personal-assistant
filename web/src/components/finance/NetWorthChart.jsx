import { useEffect, useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '../../api'

// Same charting approach as ProjectionChart.jsx (recharts AreaChart, same HUD styling)
// rather than introducing a second charting library for one more chart.
const CYAN = '#22e8ff'
const CYAN_BRIGHT = '#a9f8ff'
const GRID = 'rgba(34, 232, 255, 0.12)'
const DIM_TEXT = '#5f8a94'

function formatMoney(amount) {
  return (amount ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

function HudTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-date">{label}</div>
      <div className="chart-tooltip-value">{formatMoney(payload[0].value)}</div>
    </div>
  )
}

export default function NetWorthChart() {
  const [history, setHistory] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.financeNetWorth().then(setHistory).catch((e) => setError(e.message))
  }, [])

  if (error) return <p className="modal-error">{error}</p>
  if (!history) return <p className="empty-hint">Loading…</p>
  if (history.length < 2) {
    return (
      <p className="empty-hint">
        Not enough history yet — one snapshot is recorded per day as the finance cache refreshes.
      </p>
    )
  }

  return (
    <div className="projection-chart net-worth-chart">
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={history} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="netWorthFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={CYAN} stopOpacity={0.4} />
              <stop offset="95%" stopColor={CYAN} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="2 6" stroke={GRID} />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: DIM_TEXT, fontFamily: 'JetBrains Mono' }}
            minTickGap={40}
            axisLine={{ stroke: GRID }}
            tickLine={false}
          />
          <YAxis
            tickFormatter={formatMoney}
            width={72}
            tick={{ fontSize: 10, fill: DIM_TEXT, fontFamily: 'JetBrains Mono' }}
            axisLine={{ stroke: GRID }}
            tickLine={false}
          />
          <Tooltip content={<HudTooltip />} />
          <Area
            type="monotone"
            dataKey="net_worth"
            stroke={CYAN_BRIGHT}
            fill="url(#netWorthFill)"
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
