import { useEffect, useState } from 'react'
import { api } from '../../api'
import { fmtUsd } from '../../lib/format'
import Modal from './Modal'

export default function FinanceModal({ onClose }) {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.financeSummary().then(setSummary).catch((e) => setError(e.message))
  }, [])

  const upcoming = (summary?.recurring_charges || [])
    .filter((c) => c.next_expected_date)
    .sort((a, b) => new Date(a.next_expected_date) - new Date(b.next_expected_date))
    .slice(0, 6)

  return (
    <Modal title="Finance" onClose={onClose}>
      {error && <p className="modal-error">{error}</p>}
      {!error && !summary && <p className="modal-empty">Loading…</p>}
      {!error && summary && (
        <>
          <div className="modal-section">
            <div className="modal-section-title">Total balance</div>
            <div className="dash-card-figure">{fmtUsd(summary.total_balance)}</div>
          </div>

          <div className="modal-section">
            <div className="modal-section-title">Accounts ({summary.accounts.length})</div>
            {summary.accounts.length === 0 && <p className="modal-empty">No accounts connected.</p>}
            {summary.accounts.length > 0 && (
              <ul className="modal-row-list">
                {summary.accounts.map((a) => (
                  <li key={a.account_key} className="modal-row">
                    <span className="modal-row-label">{a.name}</span>
                    <span className="modal-row-value">{fmtUsd(a.balance)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="modal-section">
            <div className="modal-section-title">Upcoming recurring charges</div>
            {upcoming.length === 0 && <p className="modal-empty">Nothing on the calendar.</p>}
            {upcoming.length > 0 && (
              <ul className="modal-row-list">
                {upcoming.map((c) => (
                  <li key={c.charge_key} className="modal-row">
                    <span className="modal-row-label">{c.description}</span>
                    <span className="modal-row-value">
                      {c.direction === 'debit' ? '-' : '+'}{fmtUsd(Math.abs(c.amount))} · {c.next_expected_date}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </Modal>
  )
}
