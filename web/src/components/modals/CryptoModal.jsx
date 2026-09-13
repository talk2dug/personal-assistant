import { useEffect, useState } from 'react'
import { api } from '../../api'
import { fmtUsd } from '../../lib/format'
import Modal from './Modal'

function fmtPrice(v) {
  if (v === null || v === undefined) return '—'
  const a = Math.abs(v)
  if (a >= 1) return fmtUsd(v)
  if (a >= 0.01) return `$${v.toFixed(4)}`
  if (a === 0) return '$0'
  return `$${v.toPrecision(3)}`
}

export default function CryptoModal({ onClose }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.cryptoDashboard().then(setData).catch((e) => setError(e.message))
  }, [])

  const book = data?.book
  const recentTrades = (data?.trades || []).slice(0, 6)

  return (
    <Modal title="Crypto Desk" onClose={onClose} wide>
      {error && <p className="modal-error">{error}</p>}
      {!error && !data && <p className="modal-empty">Loading…</p>}
      {!error && data && !book && <p className="modal-empty">No one on the crypto desk.</p>}
      {!error && book && (
        <>
          <div className="modal-section">
            <div className="modal-section-title">Book</div>
            <ul className="modal-row-list">
              <li className="modal-row">
                <span className="modal-row-label">Equity</span>
                <span className="modal-row-value">{fmtUsd(book.equity)}</span>
              </li>
              <li className="modal-row">
                <span className="modal-row-label">Total return</span>
                <span className="modal-row-value">
                  {fmtUsd(book.total_return)} ({book.total_return_pct > 0 ? '+' : ''}{book.total_return_pct}%)
                </span>
              </li>
              <li className="modal-row">
                <span className="modal-row-label">Cash / Holdings</span>
                <span className="modal-row-value">{fmtUsd(book.cash)} / {fmtUsd(book.holdings_value)}</span>
              </li>
              <li className="modal-row">
                <span className="modal-row-label">Win rate</span>
                <span className="modal-row-value">
                  {book.win_rate_pct === null || book.win_rate_pct === undefined ? '—' : `${book.win_rate_pct}%`}
                  {' '}({book.closed_trades} closed)
                </span>
              </li>
            </ul>
          </div>

          <div className="modal-section">
            <div className="modal-section-title">Open positions ({book.positions.length})</div>
            {book.positions.length === 0 && <p className="modal-empty">Fully in cash.</p>}
            {book.positions.length > 0 && (
              <ul className="modal-row-list">
                {book.positions.map((p) => (
                  <li key={p.code} className="modal-row">
                    <span className="modal-row-label">
                      {p.code} · {p.qty.toLocaleString('en-US', { maximumFractionDigits: 6 })}
                    </span>
                    <span className="modal-row-value">
                      {fmtPrice(p.price)} · {p.unrealized_pct > 0 ? '+' : ''}{p.unrealized_pct}%
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="modal-section">
            <div className="modal-section-title">Recent trades</div>
            {recentTrades.length === 0 && <p className="modal-empty">No trades yet.</p>}
            {recentTrades.length > 0 && (
              <ul className="modal-row-list">
                {recentTrades.map((t, i) => (
                  <li key={i} className="modal-row">
                    <span className="modal-row-label">{t.side} {t.code}</span>
                    <span className="modal-row-value">{fmtPrice(t.price)}</span>
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
