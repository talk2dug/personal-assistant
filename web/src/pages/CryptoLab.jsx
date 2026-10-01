import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

// Same recharts + HUD palette as the finance charts, so the modal reads as one app.
const CYAN = '#94bce3'
const AMBER = '#e8a33d'
const GRID = 'rgba(148, 188, 227, 0.12)'
const DIM_TEXT = '#7ea3c4'

function money(v, digits = 2) {
  if (v == null) return '—'
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: digits })
}

function signed(v) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : '−'}${money(Math.abs(v))}`
}

function shortTime(iso) {
  const d = new Date(iso)
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-date">{shortTime(label)}</div>
      {payload.map((p) => (
        <div key={p.dataKey} className="chart-tooltip-value" style={{ color: p.color }}>
          {p.name}: {money(p.value)}
        </div>
      ))}
    </div>
  )
}

/** One book's equity against holding BTC from the same start. */
export function EquityVsHold({ title, points }) {
  if (!points || points.length < 2) {
    return (
      <div className="hud-panel crypto-chart">
        <span className="hud-label">{title}</span>
        <p className="crypto-muted">Not enough history yet — a point is recorded every 5 minutes.</p>
      </div>
    )
  }
  const last = points[points.length - 1]
  const gap = last.hold_equity != null ? last.equity - last.hold_equity : null
  const values = points.flatMap((p) => [p.equity, p.hold_equity]).filter((v) => v != null)
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const pad = Math.max(2, (hi - lo) * 0.15)
  return (
    <div className="hud-panel crypto-chart">
      <div className="crypto-chart-head">
        <span className="hud-label">{title}</span>
        <span className={`crypto-chart-gap ${gap == null ? '' : gap >= 0 ? 'up' : 'down'}`}>
          {money(last.equity)} · {gap == null ? '' : `${signed(gap)} vs holding BTC`}
        </span>
      </div>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={points} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="at" tickFormatter={shortTime} stroke={DIM_TEXT} fontSize={10}
            minTickGap={60} tickLine={false} />
          <YAxis domain={[Math.floor(lo - pad), Math.ceil(hi + pad)]} stroke={DIM_TEXT} fontSize={10}
            tickFormatter={(v) => `$${v}`} width={52} tickLine={false} />
          <Tooltip content={<ChartTooltip />} />
          <Legend wrapperStyle={{ fontSize: 11, color: DIM_TEXT }} />
          <Line type="monotone" dataKey="equity" name="Account" stroke={CYAN} strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="hold_equity" name="Just holding BTC" stroke={AMBER}
            strokeWidth={1.5} strokeDasharray="4 3" dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function reasoning(output) {
  if (!output) return ''
  const cut = output.search(/```(journal|orders)/)
  return (cut > 0 ? output.slice(0, cut) : output).trim()
}

/** The BTC-only, chart-only experiment: its book, its trades, and its latest reasoning. */
export function LabSection({ lab }) {
  if (!lab) return null
  const a = lab.account
  const perf = lab.performance
  const latest = lab.trader?.runs?.[0]
  return (
    <div className="crypto-lab">
      <div className="hud-panel crypto-book">
        <div className="crypto-book-headline">
          <div>
            <span className="hud-label">Equity</span>
            <div className="crypto-equity">{money(a.equity)}</div>
          </div>
          <div className="crypto-lab-vs">
            <span className="hud-label">vs holding BTC</span>
            <div className={a.vs_hold >= 0 ? 'crypto-pos' : 'crypto-neg'}>{signed(a.vs_hold)}</div>
          </div>
        </div>
        <div className="crypto-book-grid">
          <div><span className="hud-label">Cash</span><div>{money(a.cash)}</div></div>
          <div><span className="hud-label">BTC held</span><div>{a.btc.toFixed(6)}</div></div>
          <div><span className="hud-label">BTC value</span><div>{money(a.btc_value)}</div></div>
          <div><span className="hud-label">Unrealised</span>
            <div className={a.unrealized >= 0 ? 'crypto-pos' : 'crypto-neg'}>{signed(a.unrealized)}</div></div>
          <div><span className="hud-label">Realised</span>
            <div className={perf.realized_pnl >= 0 ? 'crypto-pos' : 'crypto-neg'}>{signed(perf.realized_pnl)}</div></div>
          <div><span className="hud-label">Trades</span><div>{perf.buys} buys · {perf.closed_trades} sells</div></div>
          <div><span className="hud-label">Fees paid</span><div>{money(perf.fees_paid)}</div></div>
          <div><span className="hud-label">Exits set</span>
            <div>{a.btc > 0 ? `stop ${a.stop_loss ? money(a.stop_loss, 0) : '—'} · target ${a.take_profit ? money(a.take_profit, 0) : '—'}` : '—'}</div></div>
        </div>
        <p className="crypto-muted">
          Started {shortTime(a.started_at)} with {money(a.starting_cash, 0)} when BTC was {money(a.btc_start_price, 0)}.
          Sees only BTC candles — no news, no other coins, no web.
        </p>
      </div>

      <div className="hud-panel">
        {lab.trades.length === 0 && <p className="crypto-muted">No trades yet.</p>}
        <div className="crypto-trade-log">
          {lab.trades.map((t) => (
            <div key={t.id} className={`crypto-trade-row crypto-trade-${t.side}`}>
              <div className="crypto-trade-top">
                <span className="crypto-trade-badge">{t.side.toUpperCase()}</span>
                <span className="crypto-trade-amount">{t.btc.toFixed(6)} BTC @ {money(t.price, 0)} = {money(t.usd)}</span>
                {t.realized != null && (
                  <span className={t.realized >= 0 ? 'crypto-pos' : 'crypto-neg'}>{signed(t.realized)}</span>
                )}
                {t.trigger !== 'agent' && <span className="crypto-muted">({t.trigger.replace('_', ' ')})</span>}
                <span className="crypto-trade-time">{shortTime(t.at)}</span>
              </div>
              {t.reason && <div className="crypto-trade-reason">{t.reason}</div>}
            </div>
          ))}
        </div>
      </div>

      {latest && (
        <div className="hud-panel crypto-lab-reading">
          <span className="hud-label">Its latest read · {shortTime(latest.started_at)}</span>
          <div className="crypto-lab-text">{reasoning(latest.output)}</div>
        </div>
      )}
    </div>
  )
}
