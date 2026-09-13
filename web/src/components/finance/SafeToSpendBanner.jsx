import { useState } from 'react'
import { api } from '../../api'
import { fmtUsd } from '../../lib/format'

// The pay-period-aware answer to "what can I actually spend right now" -- the lowest
// point the projected balance will hit before the next payday, minus a safety buffer he
// can adjust here or by telling Jarvis (set_safety_buffer) -- this never picks the buffer
// for him, it just displays and lets him edit whatever it's currently set to (default $0).
// data comes straight from finance.safe_to_spend's shape, embedded in /api/finance/projection.
export default function SafeToSpendBanner({ data, onChange }) {
  const [editing, setEditing] = useState(false)
  const [bufferInput, setBufferInput] = useState('')

  function startEditing() {
    setBufferInput(String(data?.safety_buffer ?? 0))
    setEditing(true)
  }

  async function saveBuffer(e) {
    e.preventDefault()
    const value = Number(bufferInput)
    if (Number.isNaN(value) || value < 0) return
    await api.setFinanceSafetyBuffer(value)
    setEditing(false)
    onChange?.()
  }

  if (!data) return null

  return (
    <div className={`safe-to-spend-banner${data.safe_to_spend != null && data.safe_to_spend < 0 ? ' safe-to-spend-negative' : ''}`}>
      {data.safe_to_spend == null ? (
        <>
          <span className="safe-to-spend-label">Safe to spend</span>
          <span className="safe-to-spend-message">{data.message || 'Not enough pay-period data yet.'}</span>
        </>
      ) : (
        <>
          <span className="safe-to-spend-label">Safe to spend until {data.payday}</span>
          <span className="safe-to-spend-amount">{fmtUsd(data.safe_to_spend)}</span>
        </>
      )}

      {!editing && (
        <button type="button" className="safe-to-spend-buffer-toggle" onClick={startEditing}>
          buffer: {fmtUsd(data.safety_buffer ?? 0)}
        </button>
      )}
      {editing && (
        <form className="safe-to-spend-buffer-form" onSubmit={saveBuffer}>
          <input
            type="number"
            min="0"
            step="1"
            value={bufferInput}
            onChange={(e) => setBufferInput(e.target.value)}
            autoFocus
          />
          <button type="submit">Save</button>
          <button type="button" onClick={() => setEditing(false)}>Cancel</button>
        </form>
      )}
    </div>
  )
}
