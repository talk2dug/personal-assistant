import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api'

/**
 * The debt facts that need a human before anything can be planned against them.
 *
 * Shown ONE card at a time, deliberately. Fifteen items in a list is a list he doesn't
 * start — his words about the review queue were "I'm drowning", and the fix that worked
 * there was grouping, not more rows. Here the fix is a queue depth and a single card:
 * you can always see how many are left, but you are only ever asked one question.
 *
 * The three problems are kept apart because they need different actions. "Are these seven
 * rows one debt?" is answered by picking one and pressing a button. "What does Wells Fargo
 * actually say you owe?" is a phone call. Mixing them into one "needs attention" pile
 * makes the easy ones invisible behind the hard ones.
 */

function money(value) {
  if (value == null) return '—'
  return `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function age(days) {
  if (days == null) return 'date unknown'
  if (days < 60) return `${days}d ago`
  if (days < 365) return `${Math.round(days / 30)} months ago`
  return `${(days / 365).toFixed(1)} years ago`
}

/** Same account number, several creditor names: one debt as it was sold on. */
function DuplicateCard({ item, onDone, busy, setBusy }) {
  const [keepId, setKeepId] = useState(item.rows[0]?.id ?? null)

  async function merge() {
    setBusy(true)
    try {
      await api.mergeDebts(keepId, item.rows.map((r) => r.id),
                           `Confirmed one obligation on account ...${item.account_last4}`)
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <p className="fin-rec-ask">
        These <b>{item.rows.length} rows</b> share account <b>…{item.account_last4}</b>.
        That usually means one debt, filed again each time it changed hands.
        Which row is the real one?
      </p>

      <div className="fin-rec-rows">
        {item.rows.map((row) => (
          <label key={row.id} className={`fin-rec-row ${keepId === row.id ? 'is-picked' : ''}`}>
            <input type="radio" name="keep" checked={keepId === row.id}
                   onChange={() => setKeepId(row.id)} />
            <span className="fin-rec-row-name">{row.creditor}</span>
            <span className="fin-rec-row-bal">{money(row.balance)}</span>
            <span className="fin-rec-row-age">{age(row.age_days)}</span>
          </label>
        ))}
      </div>

      {item.distinct_balances.length > 1 && (
        <p className="fin-rec-warn">
          These rows disagree on the balance ({item.distinct_balances.map(money).join(' · ')}).
          Pick the one you believe; you can correct the number afterwards.
        </p>
      )}

      <div className="fin-rec-actions">
        <button type="button" className="fin-btn-primary" disabled={busy || keepId == null}
                onClick={merge}>
          {busy ? 'Merging…' : 'These are one debt'}
        </button>
        <button type="button" className="fin-btn-quiet" disabled={busy} onClick={onDone}>
          They're separate — skip
        </button>
      </div>
    </>
  )
}

/** A balance old enough that it is history, and one nobody has ever stated as a number. */
function BalanceCard({ item, onDone, busy, setBusy }) {
  const [amount, setAmount] = useState('')
  const unknown = item.kind_of_problem === 'unknown'

  async function record() {
    const value = Number(amount)
    if (!Number.isFinite(value) || value < 0) return
    setBusy(true)
    try {
      await api.addDebtObservation(item.id, {
        observed_on: new Date().toISOString().slice(0, 10),
        balance: value,
        balance_text: `$${value.toFixed(2)}`,
        source: 'chat',
        confirmed: true,
        notes: 'Confirmed by Jack from the finance dashboard',
      })
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <p className="fin-rec-ask">
        {unknown ? (
          <>No balance has <b>ever</b> been recorded for <b>{item.creditor}</b>. Until there
          is a number, it can't be part of any payoff plan.</>
        ) : (
          <><b>{item.creditor}</b> was last seen at <b>{money(item.balance)}</b>, but that
          was <b>{age(item.age_days)}</b>. That's a memory, not a balance.</>
        )}
      </p>
      <p className="fin-rec-hint">
        Check a statement, your creditor's app, or annualcreditreport.com — then put the
        real number in. Leave it for now if you'd rather chase it later.
      </p>

      <div className="fin-rec-actions">
        <div className="fin-rec-input">
          <span>$</span>
          <input type="number" inputMode="decimal" step="0.01" min="0" value={amount}
                 placeholder="current balance"
                 onChange={(e) => setAmount(e.target.value)} />
        </div>
        <button type="button" className="fin-btn-primary" disabled={busy || amount === ''}
                onClick={record}>
          {busy ? 'Saving…' : 'Record it'}
        </button>
        <button type="button" className="fin-btn-quiet" disabled={busy} onClick={onDone}>
          Not now
        </button>
      </div>
    </>
  )
}

export default function ReconcileQueue({ onChange }) {
  const [data, setData] = useState(null)
  const [skipped, setSkipped] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    api.financeReconcile().then(setData).catch((e) => setError(e.message))
  }, [])

  useEffect(() => { load() }, [load])

  if (error) return <p className="cc-panel-err">{error}</p>
  if (!data) return <p className="cc-empty">Checking…</p>

  // One flat queue, biggest money first within each kind. Duplicates lead because they
  // are the only ones answerable right now, without a phone call.
  const items = [
    ...data.duplicates.map((d) => ({ ...d, _key: `dup-${d.account_last4}` })),
    ...data.stale.map((d) => ({ ...d, _key: `stale-${d.id}` })),
    ...data.unknown.map((d) => ({ ...d, _key: `unknown-${d.id}` })),
  ].filter((item) => !skipped.includes(item._key))

  if (items.length === 0) {
    return (
      <div className="fin-rec fin-rec-clear">
        <span className="fin-rec-tick">✓</span>
        <div>
          <b>Nothing waiting on you.</b>
          <p>Every tracked debt is confirmed as one obligation with a balance seen this year.</p>
        </div>
      </div>
    )
  }

  const item = items[0]
  const done = () => { setSkipped((s) => [...s, item._key]); load(); onChange?.() }

  return (
    <div className="fin-rec">
      <header className="fin-rec-head">
        <div>
          <span className="fin-rec-count">{items.length}</span>
          <span className="fin-rec-label">
            {items.length === 1 ? 'thing needs you' : 'things need you'}
          </span>
        </div>
        {data.phantom_total > 0 && (
          <span className="fin-rec-phantom">
            {money(data.phantom_total)} of your debt total isn't real yet
          </span>
        )}
      </header>

      <div className="fin-rec-card">
        <div className="fin-rec-kind">
          {item.kind === 'duplicate' ? 'Same account, filed more than once'
            : item.kind_of_problem === 'unknown' ? 'Balance never recorded'
            : 'Balance out of date'}
          {item.at_stake > 0 && <span className="fin-rec-stake">{money(item.at_stake)}</span>}
        </div>

        {item.kind === 'duplicate'
          ? <DuplicateCard item={item} onDone={done} busy={busy} setBusy={setBusy} />
          : <BalanceCard item={item} onDone={done} busy={busy} setBusy={setBusy} />}
      </div>

      <footer className="fin-rec-foot">
        {data.totals.row_count} rows · {data.totals.obligation_count} real obligations ·
        {' '}<b>{money(data.totals.plannable)}</b> confirmed this year
      </footer>
    </div>
  )
}
