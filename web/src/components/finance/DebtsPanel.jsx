import { useCallback, useEffect, useState } from 'react'
import { LineChart, Line, ResponsiveContainer, YAxis } from 'recharts'
import { api } from '../../api'
import './debts.css'

// Rates are the whole point of a payoff effort, so this panel is built around two
// questions: what does he owe, and which of it is bleeding the most per month. The
// suggested orderings are offered as a choice he makes, never as an assigned plan -- he
// was explicit that assigning payoff priorities is a joint, ongoing effort.

const KIND_LABELS = {
  credit_card: 'card', loan: 'loan', student_loan: 'student loan', auto: 'auto',
  mortgage: 'mortgage', medical: 'medical', collections: 'collections', other: 'debt',
}

const STATUS_LABELS = {
  active: 'active', paid_off: 'paid off', in_dispute: 'in dispute', closed: 'closed',
}

function formatMoney(amount) {
  return (amount ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

// A balance is always "as of" something. Showing one without its date would present a
// statement from last March as what he owes today.
function AsOf({ date, source, confirmed }) {
  if (!date) return null
  const origin = confirmed ? 'you told me' : source === 'email' ? 'from a statement email' : source
  return (
    <span className="debt-asof" title={`Recorded ${date} — ${origin}`}>
      as of {date} · {origin}
    </span>
  )
}

function BalanceTrend({ trend }) {
  if (!trend || trend.length < 2) return <span className="debt-trend-empty">—</span>
  const first = trend[0].balance
  const last = trend[trend.length - 1].balance
  const falling = last < first
  return (
    <div className={`debt-trend ${falling ? 'falling' : 'rising'}`}>
      <ResponsiveContainer width="100%" height={34}>
        <LineChart data={trend}>
          <YAxis hide domain={['dataMin', 'dataMax']} />
          <Line
            type="monotone" dataKey="balance" dot={false} strokeWidth={2}
            stroke={falling ? 'var(--cyan-bright)' : 'var(--red)'} isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
      <span className="debt-trend-delta">
        {falling ? '▼' : '▲'} {formatMoney(Math.abs(last - first))} over {trend.length} readings
      </span>
    </div>
  )
}

function DebtRow({ debt, onChange }) {
  const [expanded, setExpanded] = useState(false)
  const [observations, setObservations] = useState(null)
  const [balance, setBalance] = useState('')

  async function toggle() {
    const next = !expanded
    setExpanded(next)
    if (next && observations === null) setObservations(await api.debtObservations(debt.id))
  }

  async function handleRecord(e) {
    e.preventDefault()
    if (balance === '') return
    await api.addDebtObservation(debt.id, { balance: Number(balance) })
    setBalance('')
    setObservations(await api.debtObservations(debt.id))
    onChange?.()
  }

  async function handlePriority(e) {
    const value = e.target.value
    await api.setDebtPriority(debt.id, value === '' ? null : Number(value))
    onChange?.()
  }

  return (
    <div className={`debt-row debt-kind-${debt.kind} ${debt.status !== 'active' ? 'inactive' : ''}`}>
      <div className="debt-row-main">
        <button type="button" className="debt-row-head" onClick={toggle}>
          <span className="debt-creditor">
            {debt.creditor}
            {debt.account_last4 && <span className="debt-acct"> ····{debt.account_last4}</span>}
          </span>
          <span className="debt-kind">{KIND_LABELS[debt.kind] || debt.kind}</span>
          {debt.status !== 'active' && <span className="debt-status">{STATUS_LABELS[debt.status]}</span>}
        </button>

        <div className="debt-figures">
          <div className="debt-figure">
            <span className="debt-figure-label">balance</span>
            <span className="debt-figure-value">
              {debt.current_balance !== null
                ? formatMoney(debt.current_balance)
                : debt.current_balance_text || 'not known'}
            </span>
            <AsOf
              date={debt.current_balance_observed_on}
              source={debt.current_balance_source}
              confirmed={debt.current_balance_confirmed}
            />
          </div>
          <div className="debt-figure">
            <span className="debt-figure-label">rate</span>
            <span className="debt-figure-value">
              {debt.current_apr !== null ? `${debt.current_apr}%` : debt.current_apr_text || '—'}
            </span>
          </div>
          <div className="debt-figure">
            <span className="debt-figure-label">minimum</span>
            <span className="debt-figure-value">
              {debt.current_minimum_payment !== null
                ? formatMoney(debt.current_minimum_payment)
                : debt.current_minimum_payment_text || '—'}
            </span>
          </div>
          <div className="debt-figure debt-figure-cost">
            <span className="debt-figure-label">costs / mo</span>
            <span className="debt-figure-value">
              {debt.estimated_monthly_interest !== null
                ? formatMoney(debt.estimated_monthly_interest)
                : '—'}
            </span>
          </div>
        </div>

        <BalanceTrend trend={debt.balance_trend} />

        <label className="debt-priority">
          <span className="debt-figure-label">your priority</span>
          <select value={debt.priority ?? ''} onChange={handlePriority}>
            <option value="">not set</option>
            {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>
      </div>

      {expanded && (
        <div className="debt-detail">
          <form className="debt-record" onSubmit={handleRecord}>
            <label>
              Record a new balance
              <input
                type="number" step="0.01" placeholder="e.g. 3980.00"
                value={balance} onChange={(e) => setBalance(e.target.value)}
              />
            </label>
            <button type="submit">Add reading</button>
          </form>
          <div className="debt-history">
            <div className="debt-group-label">Where every number came from</div>
            {observations === null && <p className="empty-hint">Loading…</p>}
            {observations?.length === 0 && (
              <p className="empty-hint">No readings recorded yet.</p>
            )}
            {observations?.map((o) => (
              <div key={o.id} className="debt-observation">
                <span className="debt-observation-date">{o.observed_on}</span>
                <span className="debt-observation-balance">
                  {o.balance !== null ? formatMoney(o.balance) : o.balance_text || '—'}
                </span>
                <span className={`debt-observation-source src-${o.source}`}>
                  {o.confirmed ? 'you told me' : o.source === 'email' ? 'statement email' : o.source}
                </span>
                <span className="debt-observation-detail">{o.source_detail || o.notes || ''}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function Proposals({ proposals, onChange }) {
  if (proposals.length === 0) return null
  return (
    <div className="debt-proposals">
      <div className="debt-group-label">
        Found in your mail — not counted until you confirm ({proposals.length})
      </div>
      {proposals.map((p) => (
        <div key={p.id} className="debt-proposal">
          <div className="debt-proposal-what">
            <span className="debt-creditor">
              {p.creditor}
              {p.account_last4 && <span className="debt-acct"> ····{p.account_last4}</span>}
            </span>
            <span className="debt-proposal-figure">
              {p.current_balance !== null
                ? formatMoney(p.current_balance)
                : p.current_balance_text || 'no balance stated'}
            </span>
            <span className="debt-asof">
              {p.observation_count} message{p.observation_count === 1 ? '' : 's'}
              {p.current_balance_observed_on ? ` · latest ${p.current_balance_observed_on}` : ''}
            </span>
          </div>
          <p className="debt-proposal-origin">{p.origin_detail}</p>
          <div className="debt-proposal-actions">
            <button
              type="button" className="debt-confirm"
              onClick={async () => { await api.resolveDebtProposal(p.id, 'confirm'); onChange?.() }}
            >
              This is mine — track it
            </button>
            <button
              type="button"
              onClick={async () => { await api.resolveDebtProposal(p.id, 'dismiss'); onChange?.() }}
            >
              Not mine
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}

function PayoffSuggestions({ orders }) {
  const [strategy, setStrategy] = useState('avalanche')
  const chosen = orders?.[strategy]
  if (!chosen || chosen.order.length === 0) return null
  return (
    <div className="debt-suggestions">
      <div className="debt-group-label">
        A suggested order — yours to accept, change, or ignore
      </div>
      <div className="debt-strategy-toggle">
        {['avalanche', 'snowball'].map((key) => (
          <button
            key={key} type="button"
            className={strategy === key ? 'active' : ''}
            onClick={() => setStrategy(key)}
          >
            {orders[key].label}
          </button>
        ))}
      </div>
      <p className="debt-strategy-why">{chosen.why}</p>
      <ol className="debt-suggested-order">
        {chosen.order.map((d) => (
          <li key={d.debt_id}>
            <span>{d.creditor}</span>
            <span className="debt-suggested-figure">
              {strategy === 'avalanche'
                ? `${d.current_apr}%`
                : formatMoney(d.current_balance)}
            </span>
          </li>
        ))}
      </ol>
      {chosen.unranked.length > 0 && (
        <p className="empty-hint">
          Not ranked, because I don&apos;t have the number yet:{' '}
          {chosen.unranked.map((d) => d.creditor).join(', ')}.
        </p>
      )}
    </div>
  )
}

export default function DebtsPanel() {
  const [debts, setDebts] = useState(null)
  const [proposals, setProposals] = useState([])
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)

  const [reloadToken, setReloadToken] = useState(0)
  // Children signal "I changed something" by bumping this rather than being handed a
  // loader, which keeps all three fetches in one place and makes the cancelled-flag guard
  // cover every path -- a panel unmounted mid-fetch must not write into a dead component.
  const reload = useCallback(() => setReloadToken((n) => n + 1), [])

  useEffect(() => {
    let cancelled = false
    Promise.all([api.listDebts(), api.debtProposals(), api.debtSummary()])
      .then(([debtData, proposalData, summaryData]) => {
        if (cancelled) return
        setDebts(debtData)
        setProposals(proposalData)
        setSummary(summaryData)
        setError(null)
      })
      .catch((e) => {
        if (!cancelled) setError(e.message)
      })
    return () => { cancelled = true }
  }, [reloadToken])

  if (error) return <p className="modal-error">{error}</p>
  if (debts === null || summary === null) return <p className="empty-hint">Loading…</p>

  return (
    <div className="debts-panel">
      <div className="debt-totals">
        <div className="card debt-total-card">
          <div className="card-label">Total owed</div>
          <div className="card-amount">{formatMoney(summary.total_balance)}</div>
          {summary.unknown_balance_count > 0 && (
            <div className="debt-total-caveat">
              at least — {summary.unknown_balance_count} debt
              {summary.unknown_balance_count === 1 ? '' : 's'} with no balance yet
            </div>
          )}
        </div>
        <div className="card debt-total-card">
          <div className="card-label">Minimums / month</div>
          <div className="card-amount">{formatMoney(summary.total_minimum_payment)}</div>
        </div>
        <div className="card debt-total-card debt-total-interest">
          <div className="card-label">Interest / month</div>
          <div className="card-amount">{formatMoney(summary.estimated_monthly_interest)}</div>
          {summary.costliest_debt && (
            <div className="debt-total-caveat">
              worst: {summary.costliest_debt.creditor} at {summary.costliest_debt.current_apr}%
            </div>
          )}
        </div>
      </div>

      <Proposals proposals={proposals} onChange={reload} />

      {debts.length === 0 && (
        <p className="empty-hint">
          No debts tracked yet. Tell Jarvis a balance in chat, or confirm one it finds in your mail.
        </p>
      )}
      {debts.map((d) => <DebtRow key={d.id} debt={d} onChange={reload} />)}

      {summary.unprioritized_count > 0 && debts.length > 0 && (
        <p className="empty-hint">
          {summary.unprioritized_count} of these have no payoff priority set — that one&apos;s yours to decide.
        </p>
      )}

      <PayoffSuggestions orders={summary.suggested_payoff_orders} />
    </div>
  )
}
