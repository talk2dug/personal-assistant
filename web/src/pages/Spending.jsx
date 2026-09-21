import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './spending.css'

/**
 * The checking account, sorted into what is required and what is not.
 *
 * Merchant-first on purpose. The account holds 845 transactions across 96 merchants, so
 * the list leads with merchants ordered by what they cost: settle the top ten and most of
 * the money is decided. A per-transaction view opens underneath for the merchants that are
 * genuinely both things — an Amazon order can be a fridge part or a whim, and only he
 * knows which. A decision made on a single item is never overwritten by the merchant rule.
 *
 * The unreviewed total is always on screen. A half-sorted account that shows only what has
 * been classified reads as a much cheaper life than it is, which would make the budget
 * built from it wrong in the one direction that matters.
 */

const CHOICES = [
  { key: 'required', label: 'Required', hint: 'rent, utilities, insurance — the floor' },
  { key: 'extra', label: 'Extra', hint: 'could stop tomorrow' },
  { key: 'transfer', label: 'Transfer', hint: 'your own money moving — not spending' },
  { key: 'income', label: 'Income', hint: 'money in' },
]

const money = (n) => `$${Math.abs(Number(n || 0)).toLocaleString(undefined, {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
})}`

function Choice({ value, onPick, busy }) {
  return (
    <div className="sp-choice">
      {CHOICES.map((c) => (
        <button
          key={c.key}
          type="button"
          title={c.hint}
          disabled={busy}
          className={`sp-pill p-${c.key} ${value === c.key ? 'is-on' : ''}`}
          onClick={() => onPick(c.key)}
        >
          {c.label}
        </button>
      ))}
    </div>
  )
}

function Transactions({ merchant, onChanged }) {
  const [rows, setRows] = useState(null)
  const [busy, setBusy] = useState(null)

  const load = useCallback(async () => {
    const data = await api.spendingTransactions({ merchant, limit: 300 })
    setRows(data.transactions)
  }, [merchant])

  useEffect(() => { load() }, [load])

  async function pick(row, necessity) {
    setBusy(row.era_id)
    try {
      await api.classifyTransaction(row.era_id, necessity)
      await load()
      onChanged?.()
    } finally {
      setBusy(null)
    }
  }

  if (!rows) return <p className="sp-dim">Loading…</p>
  return (
    <div className="sp-txns">
      <p className="sp-dim">
        {rows.length} item{rows.length === 1 ? '' : 's'} — changing one here overrides the
        merchant rule for that item only, permanently.
      </p>
      <ul>
        {rows.map((t) => (
          <li key={t.era_id} className={t.necessity_source === 'manual' ? 'is-manual' : ''}>
            <span className="sp-date">{String(t.txn_date || '').slice(5)}</span>
            <span className="sp-amt">{t.is_outflow ? '-' : '+'}{money(t.amount)}</span>
            <span className="sp-desc" title={t.original_description || ''}>
              {t.description || t.merchant}
              {t.era_category && <em> · {t.era_category}</em>}
            </span>
            <Choice value={t.necessity} busy={busy === t.era_id}
                    onPick={(n) => pick(t, n)} />
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function Spending() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [openMerchant, setOpen] = useState(null)
  const [onlyTodo, setOnlyTodo] = useState(true)
  const [busy, setBusy] = useState(null)
  const [syncing, setSyncing] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api.spending({ unreviewed: onlyTodo }))
      setError(null)
    } catch (err) {
      setError(err?.message || 'could not load spending')
    }
  }, [onlyTodo])

  useEffect(() => { load() }, [load])

  async function pickMerchant(merchant, necessity) {
    setBusy(merchant)
    try {
      await api.classifyMerchant(merchant, necessity)
      await load()
    } finally {
      setBusy(null)
    }
  }

  async function sync() {
    setSyncing(true)
    try {
      await api.syncSpending()
      await load()
    } catch (err) {
      setError(err?.message || 'sync failed')
    } finally {
      setSyncing(false)
    }
  }

  if (error) return <p className="sp-error">{error}</p>
  if (!data) return <p className="sp-dim">Loading…</p>

  const { summary, merchants } = data
  const reviewed = summary.reviewed_share
  const left = merchants.filter((m) => m.necessity === 'unreviewed').length

  return (
    <div className="sp">
      <div className="sp-summary">
        <div className="sp-stat">
          <span className="v req">{money(summary.required)}</span>
          <span className="l">required</span>
        </div>
        <div className="sp-stat">
          <span className="v ext">{money(summary.extra)}</span>
          <span className="l">extra</span>
        </div>
        <div className="sp-stat">
          <span className="v todo">{money(summary.unreviewed)}</span>
          <span className="l">not sorted yet</span>
        </div>
        <div className="sp-stat">
          <span className="v">{money(summary.income)}</span>
          <span className="l">income in</span>
        </div>
        <div className="sp-meta">
          {summary.from} → {summary.to}
          {reviewed != null && <> · {Math.round(reviewed * 100)}% of spend sorted</>}
          <button type="button" className="sp-sync" onClick={sync} disabled={syncing}>
            {syncing ? 'Syncing…' : 'Sync from bank'}
          </button>
        </div>
      </div>

      {/* Deliberately stated rather than implied: a split computed from a third of the
          account is not a budget, and the number that says so belongs next to it. */}
      {reviewed != null && reviewed < 0.9 && (
        <p className="sp-warn">
          {money(summary.unreviewed)} is still unsorted, so the required/extra split above
          only covers {Math.round(reviewed * 100)}% of what left the account. Sort the
          biggest merchants first — the top ten usually settle most of it.
        </p>
      )}

      <div className="sp-head">
        <h3>{onlyTodo ? `${left} merchants left to sort` : `${merchants.length} merchants`}</h3>
        <button type="button" className="sp-toggle" onClick={() => setOnlyTodo(!onlyTodo)}>
          {onlyTodo ? 'Show all' : 'Show only unsorted'}
        </button>
      </div>

      <ul className="sp-merchants">
        {merchants.map((m) => (
          <li key={m.merchant_key} className={`n-${m.necessity}`}>
            <div className="sp-m-row">
              <button type="button" className="sp-m-name"
                      onClick={() => setOpen(openMerchant === m.merchant ? null : m.merchant)}>
                {m.merchant}
                <em>{m.txns} item{m.txns === 1 ? '' : 's'}</em>
              </button>
              <span className="sp-m-spent">{money(m.spent)}</span>
              <Choice value={m.necessity} busy={busy === m.merchant}
                      onPick={(n) => pickMerchant(m.merchant, n)} />
            </div>
            {openMerchant === m.merchant && (
              <Transactions merchant={m.merchant} onChanged={load} />
            )}
          </li>
        ))}
      </ul>

      {merchants.length === 0 && (
        <p className="sp-dim">
          Nothing left to sort. Switch to &ldquo;Show all&rdquo; to revisit anything.
        </p>
      )}
    </div>
  )
}
