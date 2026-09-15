import { TINT, fmtUsd } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

function fmtPrice(v) {
  if (v === null || v === undefined) return '—'
  const abs = Math.abs(v)
  if (abs >= 1) return fmtUsd(v, 2)
  if (abs === 0) return '$0'
  return `$${v.toPrecision(3)}`
}

/**
 * The paper-trading desk: equity against the starting stake, and every open position
 * marked to the live price cache.
 *
 * A position whose quote has gone stale is valued at cost by paper_trading.portfolio
 * rather than at zero, and shows its price as '—' here — the desk never books a loss
 * just because a feed stopped answering.
 */
export default function CryptoDesk({ crypto, error, onOpen }) {
  const book = crypto?.book
  const positions = (book?.positions || []).slice(0, 4)
  const up = (book?.total_return ?? 0) >= 0

  return (
    <Panel
      title="Crypto desk"
      meta={book?.win_rate_pct != null ? `paper · win ${book.win_rate_pct}%` : 'paper book'}
      onOpen={onOpen}
    >
      {error && <PanelError>desk unavailable — {error}</PanelError>}
      {!error && !crypto && <PanelEmpty>Loading…</PanelEmpty>}
      {!error && crypto && !book && <PanelEmpty>Nobody on the crypto desk.</PanelEmpty>}

      {!error && book && (
        <>
          <div className="cc-figure-row">
            <div className="cc-figure-stack">
              <span className="cc-figure">{fmtUsd(book.equity, 2)}</span>
              <span className="cc-delta" style={{ color: up ? TINT.ok : TINT.warn }}>
                {up ? '+' : ''}{book.total_return_pct}% total return
              </span>
            </div>
            <div className="cc-figure-side">
              <span className="cc-subtle">{fmtUsd(book.cash, 0)} cash</span>
              <span className="cc-subtle">{book.closed_trades ?? 0} closed</span>
            </div>
          </div>

          {positions.length === 0 ? (
            <p className="cc-empty is-inline">Flat — no open positions.</p>
          ) : (
            <div className="cc-positions">
              {positions.map((p) => (
                <div className="cc-position" key={p.code}>
                  <span className="cc-pos-code">{p.code}</span>
                  <span className="cc-pos-qty">{p.qty}</span>
                  <span className="cc-pos-price">{fmtPrice(p.price)}</span>
                  <span
                    className="cc-pos-pct"
                    style={{ color: p.unrealized_pct >= 0 ? TINT.ok : TINT.warn }}
                  >
                    {p.unrealized_pct >= 0 ? '+' : ''}{p.unrealized_pct}%
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Panel>
  )
}
