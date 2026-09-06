import { useEffect, useState } from 'react'
import { api } from '../../api'

function formatMoney(amount) {
  return (amount ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

export default function SpendingByCategory() {
  const [period, setPeriod] = useState('this_month')
  const [categories, setCategories] = useState([])
  const [showForm, setShowForm] = useState(false)
  const [selectedCategory, setSelectedCategory] = useState('')
  const [limit, setLimit] = useState('')

  async function load() {
    const data = await api.financeSpending(period)
    setCategories(data.categories)
  }

  useEffect(() => {
    load()
  }, [period])

  async function handleAddBudget(e) {
    e.preventDefault()
    const cat = categories.find((c) => c.category_key === selectedCategory)
    if (!cat || !limit) return
    await api.createBudget({
      category_key: cat.category_key,
      category_label: cat.label,
      monthly_limit: Number(limit),
    })
    setSelectedCategory('')
    setLimit('')
    setShowForm(false)
    load()
  }

  async function handleRemoveBudget(categoryKey) {
    const budgets = await api.listBudgets()
    const budget = budgets.find((b) => b.category_key === categoryKey)
    if (budget) {
      await api.deleteBudget(budget.id)
      load()
    }
  }

  const unbudgeted = categories.filter((c) => c.budget_limit == null)

  return (
    <div className="spending-widget">
      <div className="spending-header">
        <div className="period-toggle">
          <button className={period === 'this_month' ? 'active' : ''} onClick={() => setPeriod('this_month')}>
            This month
          </button>
          <button className={period === 'last_30_days' ? 'active' : ''} onClick={() => setPeriod('last_30_days')}>
            Last 30 days
          </button>
        </div>
        <button onClick={() => setShowForm((s) => !s)}>{showForm ? 'Cancel' : '+ Set budget'}</button>
      </div>

      {showForm && (
        <form className="budget-form" onSubmit={handleAddBudget}>
          <select value={selectedCategory} onChange={(e) => setSelectedCategory(e.target.value)}>
            <option value="">Choose a category…</option>
            {unbudgeted.map((c) => (
              <option key={c.category_key} value={c.category_key}>
                {c.label}
              </option>
            ))}
          </select>
          <input
            type="number"
            placeholder="Monthly limit"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
          <button type="submit">Save</button>
        </form>
      )}

      {categories.length === 0 && <p className="empty-hint">No spending data cached yet.</p>}

      <ul className="category-list">
        {categories.map((c) => {
          const pct = c.budget_limit ? Math.min(100, (c.amount / c.budget_limit) * 100) : null
          const over = c.budget_limit != null && c.amount > c.budget_limit
          return (
            <li key={c.category_key} className="category-row">
              <div className="category-row-top">
                <span className="category-label">{c.label}</span>
                <span className="category-amount">
                  {formatMoney(c.amount)}
                  {c.budget_limit != null && <span className="category-limit"> / {formatMoney(c.budget_limit)}</span>}
                </span>
              </div>
              {c.budget_limit != null ? (
                <div className="budget-bar-track">
                  <div
                    className={`budget-bar-fill ${over ? 'over' : ''}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              ) : (
                c.percent_of_total != null && (
                  <div className="category-percent">{c.percent_of_total.toFixed(0)}% of spending</div>
                )
              )}
              {c.budget_limit != null && (
                <button className="budget-remove" onClick={() => handleRemoveBudget(c.category_key)}>
                  remove budget
                </button>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
