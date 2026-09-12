import { Link } from 'react-router-dom'
import { useStatusPanel } from '../hooks/useStatusPanel'
import './StatusPanel.css'

/**
 * Compact, text-only status summary for the Chat/orb page: one line per agent, the
 * crypto desk's top-line numbers (nothing trade-level), a condensed today/tomorrow
 * schedule, and a pending-review count linking through to the full queue.
 *
 * Deliberately dumb — all state lives in useStatusPanel. Each section below is a small,
 * self-contained function so the layout can be re-shaped (grid vs. row, reordered,
 * collapsed) without touching the data side once the dashboard layout spec lands.
 */

function fmtUsd(v) {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

function fmtReminder(r) {
  const time = new Date(r.due_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  return `${time} · ${r.text}`
}

function DaySummary({ label, items }) {
  if (!items || items.length === 0) {
    return <div className="status-line status-muted">{label}: nothing scheduled</div>
  }
  const shown = items.slice(0, 2)
  const rest = items.length - shown.length
  return (
    <div className="status-line">
      <strong>{label}:</strong> {shown.map(fmtReminder).join('; ')}
      {rest > 0 && ` · +${rest} more`}
    </div>
  )
}

function AgentsSection({ agents, error }) {
  return (
    <section className="status-section">
      <span className="hud-label">Agents</span>
      {error && <div className="status-line status-error">Unavailable — {error}</div>}
      {!error && agents.length === 0 && <div className="status-line status-muted">No agents on staff.</div>}
      {!error && agents.map((a) => (
        <div key={a.key} className="status-line">{a.title} — {a.label}</div>
      ))}
    </section>
  )
}

function CryptoSection({ book, hasTraders, error }) {
  return (
    <section className="status-section">
      <span className="hud-label">Crypto</span>
      {error && <div className="status-line status-error">Unavailable — {error}</div>}
      {!error && !book && (
        <div className="status-line status-muted">
          {hasTraders ? 'No paper book yet.' : 'No one on the crypto desk.'}
        </div>
      )}
      {!error && book && (
        <div className="status-line">
          {fmtUsd(book.equity)}{' · '}
          <span className={book.total_return >= 0 ? 'status-pos' : 'status-neg'}>
            {book.total_return_pct > 0 ? '+' : ''}{book.total_return_pct}%
          </span>
          {' · win '}
          {book.win_rate_pct === null || book.win_rate_pct === undefined ? '—' : `${book.win_rate_pct}%`}
        </div>
      )}
    </section>
  )
}

function ScheduleSection({ schedule, error }) {
  return (
    <section className="status-section">
      <span className="hud-label">Schedule</span>
      {error && <div className="status-line status-error">Unavailable — {error}</div>}
      {!error && schedule && (
        <>
          <DaySummary label="Today" items={schedule.today} />
          <DaySummary label="Tomorrow" items={schedule.tomorrow} />
        </>
      )}
    </section>
  )
}

function ReviewSection({ pending, error }) {
  return (
    <section className="status-section">
      <span className="hud-label">Review</span>
      {error && <div className="status-line status-error">Unavailable — {error}</div>}
      {!error && (
        <Link to="/review" className="status-line status-link">
          {pending > 0 ? `${pending} pending` : 'Nothing pending'}
        </Link>
      )}
    </section>
  )
}

export default function StatusPanel() {
  const {
    agents, agentsError,
    book, cryptoTraders, cryptoError,
    schedule, scheduleError,
    pendingReview, reviewError,
  } = useStatusPanel()

  return (
    <div className="status-panel">
      <AgentsSection agents={agents} error={agentsError} />
      <CryptoSection book={book} hasTraders={cryptoTraders > 0} error={cryptoError} />
      <ScheduleSection schedule={schedule} error={scheduleError} />
      <ReviewSection pending={pendingReview} error={reviewError} />
    </div>
  )
}
