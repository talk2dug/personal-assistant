import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

// Trades happen on a 5-15 minute employee cadence, so sub-second push would be theater —
// this just needs to feel current when the tab is open, same reasoning as the office's
// own poll (Agents.jsx, 4s). Slower here since there is far more to fetch per tick.
const POLL_MS = 12000

function fmtUsd(v, opts = {}) {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', ...opts })
}

function fmtPrice(v) {
  if (v === null || v === undefined) return '—'
  const a = Math.abs(v)
  if (a >= 1) return fmtUsd(v)
  if (a >= 0.01) return `$${v.toFixed(4)}`
  if (a === 0) return '$0'
  return `$${v.toPrecision(3)}`
}

function timeAgo(iso) {
  if (!iso) return 'never'
  const ms = Date.now() - new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z').getTime()
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

function pctClass(v) {
  if (v > 0) return 'crypto-pos'
  if (v < 0) return 'crypto-neg'
  return ''
}

/** Pulls the reasoning prose out of a run's output, dropping the JSON verdict line and
 * the ledger's own execution report — both are shown elsewhere as structured data, so
 * repeating them in the free-text reasoning block would just be noise twice over. */
function reasoningText(output) {
  if (!output) return ''
  return output
    .split('--- EXECUTION REPORT')[0]
    .split(/\n\{"alert":/)[0]
    .trim()
}

function FeedStatus({ status }) {
  if (!status) return null
  const stale = status.stale
  return (
    <div className={`crypto-feed-pill ${stale ? 'crypto-feed-stale' : 'crypto-feed-live'}`}>
      <span className="crypto-feed-dot" />
      {stale ? 'FEED STALE' : 'FEED LIVE'}
      <span className="crypto-feed-detail">
        {status.coins_tracked} coins · {status.seconds_since_poll}s old · {status.credits_remaining} credits left
      </span>
    </div>
  )
}

function BookSummary({ book }) {
  if (!book) {
    return (
      <div className="hud-panel crypto-book-empty">
        <span className="hud-label">Paper Book</span>
        <p>No paper trading account yet — nothing has been granted the ledger.</p>
      </div>
    )
  }
  const up = book.total_return >= 0
  return (
    <div className="hud-panel crypto-book">
      <div className="crypto-book-headline">
        <div>
          <span className="hud-label">Equity</span>
          <div className="crypto-equity">{fmtUsd(book.equity)}</div>
        </div>
        <div className={`crypto-return ${pctClass(book.total_return)}`}>
          {up ? '▲' : '▼'} {fmtUsd(Math.abs(book.total_return))} ({book.total_return_pct > 0 ? '+' : ''}
          {book.total_return_pct}%)
        </div>
      </div>
      <div className="crypto-book-grid">
        <div><span className="hud-label">Cash</span><div>{fmtUsd(book.cash)}</div></div>
        <div><span className="hud-label">Holdings</span><div>{fmtUsd(book.holdings_value)}</div></div>
        <div><span className="hud-label">Realised</span>
          <div className={pctClass(book.realized_pnl)}>{fmtUsd(book.realized_pnl)}</div></div>
        <div><span className="hud-label">Unrealised</span>
          <div className={pctClass(book.unrealized_pnl)}>{fmtUsd(book.unrealized_pnl)}</div></div>
        <div><span className="hud-label">Win rate</span>
          <div>{book.win_rate_pct === null ? '—' : `${book.win_rate_pct}%`} ({book.closed_trades} closed)</div></div>
        <div><span className="hud-label">Fees paid</span><div>{fmtUsd(book.fees_paid)}</div></div>
      </div>

      {book.positions.length > 0 && (
        <table className="crypto-positions">
          <thead>
            <tr><th>Coin</th><th>Qty</th><th>Avg cost</th><th>Now</th><th>P&amp;L</th></tr>
          </thead>
          <tbody>
            {book.positions.map((p) => (
              <tr key={p.code}>
                <td className="crypto-code">{p.code}</td>
                <td>{p.qty.toLocaleString('en-US', { maximumFractionDigits: 6 })}</td>
                <td>{fmtPrice(p.avg_cost)}</td>
                <td>{fmtPrice(p.price)}</td>
                <td className={pctClass(p.unrealized)}>
                  {fmtUsd(p.unrealized, { signDisplay: 'exceptZero' })} ({p.unrealized_pct > 0 ? '+' : ''}
                  {p.unrealized_pct}%)
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {book.positions.length === 0 && <p className="crypto-muted">Fully in cash — no open positions.</p>}
      {book.unpriced.length > 0 && (
        <p className="crypto-muted">Not priceable (left the tracked set): {book.unpriced.join(', ')}</p>
      )}
    </div>
  )
}

function TradeLog({ trades, rejections }) {
  const rows = [
    ...trades.map((t) => ({ ...t, kind: 'fill' })),
    ...rejections.map((r) => ({ ...r, kind: 'rejected' })),
  ].sort((a, b) => new Date(b.at) - new Date(a.at))

  if (rows.length === 0) {
    return <p className="crypto-muted">No trades or orders yet.</p>
  }

  return (
    <div className="crypto-trade-log">
      {rows.map((r, i) => (
        <div key={i} className={`crypto-trade-row crypto-trade-${r.kind} ${r.kind === 'fill' ? r.side : ''}`}>
          <div className="crypto-trade-top">
            <span className="crypto-trade-badge">
              {r.kind === 'rejected' ? 'REJECTED' : r.side?.toUpperCase()}
            </span>
            <span className="crypto-code">{r.code}</span>
            {r.kind === 'fill' && (
              <span className="crypto-trade-amount">
                {r.qty.toLocaleString('en-US', { maximumFractionDigits: 6 })} @ {fmtPrice(r.price)}
                {' = '}{fmtUsd(r.gross)}
                {r.realized !== null && r.realized !== undefined && (
                  <span className={pctClass(r.realized)}> ({fmtUsd(r.realized, { signDisplay: 'exceptZero' })})</span>
                )}
              </span>
            )}
            <span className="crypto-trade-time">{timeAgo(r.at)}</span>
          </div>
          <div className="crypto-trade-reason">
            {r.kind === 'rejected' ? r.reason : (r.reason || <em>no reason given</em>)}
          </div>
        </div>
      ))}
    </div>
  )
}

function EmployeeCard({ person }) {
  const [expanded, setExpanded] = useState(0)
  if (!person) return null
  const latest = person.runs[0]
  return (
    <div className="hud-panel crypto-employee">
      <div className="crypto-employee-header">
        <div>
          <div className="crypto-employee-title">{person.title}</div>
          <div className="hud-label crypto-employee-meta">
            {person.status} · every {person.interval_minutes}m
            {latest && ` · last run ${timeAgo(latest.finished_at || latest.started_at)}`}
          </div>
        </div>
      </div>
      {person.standing_assignment && (
        <p className="crypto-standing">{person.standing_assignment}</p>
      )}
      {person.runs.length === 0 && <p className="crypto-muted">No runs yet.</p>}
      <div className="crypto-run-list">
        {person.runs.map((run, i) => (
          <div key={run.id} className="crypto-run">
            <button className="crypto-run-toggle" onClick={() => setExpanded(expanded === i ? -1 : i)}>
              <span className={`crypto-run-status crypto-run-${run.status}`} />
              {timeAgo(run.finished_at || run.started_at)}
              <span className="crypto-run-expand">{expanded === i ? '▾' : '▸'}</span>
            </button>
            {expanded === i && (
              <div className="crypto-run-body">
                {run.status === 'failed'
                  ? <p className="crypto-neg">{run.error}</p>
                  : <pre>{reasoningText(run.output) || '(no reasoning captured)'}</pre>}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Crypto() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const poll = () =>
      api.cryptoDashboard()
        .then((d) => { if (mounted.current) { setData(d); setError(null) } })
        .catch((e) => { if (mounted.current) setError(e.message) })
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => { mounted.current = false; clearInterval(id) }
  }, [])

  if (error && !data) return <div className="crypto-page loading">{error}</div>
  if (!data) return <div className="crypto-page loading">Loading…</div>

  const noStaff = data.analysts.length === 0 && data.traders.length === 0

  return (
    <div className="crypto-page">
      <div className="crypto-page-header">
        <h2>Crypto Desk</h2>
        <FeedStatus status={data.feed_status} />
      </div>

      {noStaff && (
        <p className="crypto-muted">
          No one is on the crypto desk yet — hire a research analyst or trader with the market feed
          granted to see them here.
        </p>
      )}

      {data.traders.length > 0 && (
        <section>
          <h3>Paper Book</h3>
          <BookSummary book={data.book} />
        </section>
      )}

      {data.traders.length > 0 && (
        <section>
          <h3>Trade Log</h3>
          <div className="hud-panel">
            <TradeLog trades={data.trades} rejections={data.rejections} />
          </div>
        </section>
      )}

      {(data.analysts.length > 0 || data.traders.length > 0) && (
        <section>
          <h3>The Desk</h3>
          <div className="crypto-employee-grid">
            {[...data.analysts, ...data.traders].map((p) => <EmployeeCard key={p.key} person={p} />)}
          </div>
        </section>
      )}
    </div>
  )
}
