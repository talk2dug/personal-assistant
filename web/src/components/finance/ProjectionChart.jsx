import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

const CYAN = '#22e8ff'
const CYAN_BRIGHT = '#a9f8ff'
const AMBER = '#ffb020'
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

export default function ProjectionChart({ series, goals }) {
  if (!series.length) return null

  return (
    <div className="projection-chart">
      <ResponsiveContainer width="100%" height={280}>
        <AreaChart data={series} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="balanceFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={CYAN} stopOpacity={0.4} />
              <stop offset="95%" stopColor={CYAN} stopOpacity={0} />
            </linearGradient>
            <filter id="lineGlow" x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="3" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
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
            dataKey="balance"
            stroke={CYAN_BRIGHT}
            fill="url(#balanceFill)"
            strokeWidth={2}
            filter="url(#lineGlow)"
          />
          {goals
            .filter((g) => g.reachable_on)
            .map((g) => (
              <ReferenceLine
                key={g.id}
                x={g.reachable_on}
                stroke={AMBER}
                strokeDasharray="3 4"
                label={{
                  value: g.name.toUpperCase(),
                  position: 'top',
                  fontSize: 10,
                  fill: AMBER,
                  fontFamily: 'Rajdhani',
                  fontWeight: 700,
                }}
              />
            ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
