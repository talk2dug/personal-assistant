import { useEffect, useState } from 'react'
import { api } from '../../api'

const CADENCE_OPTIONS = [
  { value: 'monthly_on_day', label: 'Same day every month (exact)' },
  { value: 'monthly_on_last_day', label: 'Last day of every month' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'biweekly', label: 'Every 2 weeks' },
  { value: 'semimonthly', label: 'Twice a month (~15 days apart)' },
  { value: 'monthly', label: 'Monthly (~30 days, approximate)' },
  { value: 'quarterly', label: 'Quarterly' },
  { value: 'semiannual', label: 'Every 6 months' },
  { value: 'yearly', label: 'Yearly' },
]

function formatMoney(amount) {
  return (amount ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

export default function RecurringCharges({ onChange }) {
  const [era, setEra] = useState([])
  const [manual, setManual] = useState([])
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({
    description: '', amount: '', direction: 'expense', cadence: 'monthly_on_day', next_expected_date: '',
  })

  async function load() {
    const data = await api.listRecurring()
    setEra(data.era)
    setManual(data.manual)
  }

  useEffect(() => {
    load()
  }, [])

  async function handleToggleExcluded(charge) {
    await api.setEraRecurringExcluded(charge.charge_key, !charge.excluded)
    await load()
    onChange?.()
  }

  async function handleDeleteManual(id) {
    await api.deleteManualRecurring(id)
    await load()
    onChange?.()
  }

  async function handleAdd(e) {
    e.preventDefault()
    if (!form.description.trim() || !form.amount || !form.next_expected_date) return
    await api.createManualRecurring({
      ...form,
      description: form.description.trim(),
      amount: Number(form.amount),
    })
    setForm({ description: '', amount: '', direction: 'expense', cadence: 'monthly_on_day', next_expected_date: '' })
    setShowForm(false)
    await load()
    onChange?.()
  }

  const cadenceLabel = (c) => CADENCE_OPTIONS.find((o) => o.value === c)?.label || c

  return (
    <div className="recurring-widget">
      <div className="recurring-header">
        <span />
        <button onClick={() => setShowForm((s) => !s)}>{showForm ? 'Cancel' : '+ Add bill/income'}</button>
      </div>

      {showForm && (
        <form className="recurring-form" onSubmit={handleAdd}>
          <input
            placeholder="Description (e.g. Rent)"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
          <div className="recurring-form-row">
            <select value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value })}>
              <option value="expense">Expense</option>
              <option value="income">Income</option>
            </select>
            <input
              type="number"
              placeholder="Amount"
              value={form.amount}
              onChange={(e) => setForm({ ...form, amount: e.target.value })}
            />
          </div>
          <select value={form.cadence} onChange={(e) => setForm({ ...form, cadence: e.target.value })}>
            {CADENCE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <label className="recurring-date-label">
            {form.cadence === 'monthly_on_day'
              ? 'Any date on the target day (e.g. the 15th)'
              : 'Next occurrence date'}
            <input
              type="date"
              value={form.next_expected_date}
              onChange={(e) => setForm({ ...form, next_expected_date: e.target.value })}
            />
          </label>
          <button type="submit">Save</button>
        </form>
      )}

      <div className="recurring-list">
        <div className="recurring-group-label">Detected by Era</div>
        {era.length === 0 && <p className="empty-hint">Nothing detected yet.</p>}
        {era.map((c) => (
          <div key={c.charge_key} className={`recurring-row ${c.excluded ? 'excluded' : ''}`}>
            <div>
              <span className="recurring-desc">{c.description}</span>
              <span className="recurring-meta">
                {' '}
                — {cadenceLabel(c.cadence)}, next {c.next_expected_date}
              </span>
            </div>
            <div className="recurring-row-right">
              <span className={c.direction === 'income' ? 'amount-income' : 'amount-expense'}>
                {c.direction === 'income' ? '+' : '-'}
                {formatMoney(c.amount)}
              </span>
              <button onClick={() => handleToggleExcluded(c)}>
                {c.excluded ? 'include' : 'exclude'}
              </button>
            </div>
          </div>
        ))}

        <div className="recurring-group-label">Added manually</div>
        {manual.length === 0 && <p className="empty-hint">None yet.</p>}
        {manual.map((c) => (
          <div key={c.id} className="recurring-row">
            <div>
              <span className="recurring-desc">{c.description}</span>
              <span className="recurring-meta">
                {' '}
                — {cadenceLabel(c.cadence)}, next {c.next_expected_date}
              </span>
            </div>
            <div className="recurring-row-right">
              <span className={c.direction === 'income' ? 'amount-income' : 'amount-expense'}>
                {c.direction === 'income' ? '+' : '-'}
                {formatMoney(c.amount)}
              </span>
              <button onClick={() => handleDeleteManual(c.id)}>remove</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
