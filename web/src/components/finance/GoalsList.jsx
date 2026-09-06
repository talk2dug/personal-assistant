import { useState } from 'react'
import { api } from '../../api'

function formatMoney(amount) {
  return (amount ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

export default function GoalsList({ goals, onChange }) {
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [targetAmount, setTargetAmount] = useState('')
  const [targetDate, setTargetDate] = useState('')

  async function handleCreate(e) {
    e.preventDefault()
    if (!name.trim() || !targetAmount) return
    await api.createGoal({
      name: name.trim(),
      target_amount: Number(targetAmount),
      target_date: targetDate || null,
    })
    setName('')
    setTargetAmount('')
    setTargetDate('')
    setShowForm(false)
    onChange()
  }

  async function handleDelete(id) {
    await api.deleteGoal(id)
    onChange()
  }

  return (
    <div className="goals-list">
      <div className="goals-header">
        <h3>Savings goals</h3>
        <button onClick={() => setShowForm((s) => !s)}>{showForm ? 'Cancel' : '+ Add goal'}</button>
      </div>

      {showForm && (
        <form className="goal-form" onSubmit={handleCreate}>
          <input placeholder="What are you saving for?" value={name} onChange={(e) => setName(e.target.value)} />
          <input
            type="number"
            placeholder="Target amount"
            value={targetAmount}
            onChange={(e) => setTargetAmount(e.target.value)}
          />
          <input type="date" value={targetDate} onChange={(e) => setTargetDate(e.target.value)} />
          <button type="submit">Save</button>
        </form>
      )}

      {goals.length === 0 && !showForm && <p className="empty-hint">No goals yet.</p>}

      <ul>
        {goals.map((g) => (
          <li key={g.id} className="goal-row">
            <div>
              <div className="goal-name">{g.name}</div>
              <div className="goal-meta">
                {formatMoney(g.target_amount)}
                {g.target_date && ` by ${g.target_date}`}
                {g.reachable_on && (
                  <span className="goal-reachable"> — reachable by {g.reachable_on}</span>
                )}
                {!g.reachable_on && <span className="goal-unreachable"> — not reachable in projected horizon</span>}
              </div>
            </div>
            <button className="goal-delete" onClick={() => handleDelete(g.id)}>
              ✕
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
