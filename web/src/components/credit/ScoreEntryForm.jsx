import { useState } from 'react'
import { api } from '../../api'

const BUREAUS = [
  { value: 'experian', label: 'Experian' },
  { value: 'equifax', label: 'Equifax' },
  { value: 'transunion', label: 'TransUnion' },
  { value: 'other', label: 'Other' },
]

function today() {
  return new Date().toISOString().slice(0, 10)
}

/** Manual entry only -- there is no live bureau feed (see personal_db.py's schema
 * comment). One row per check, so ScoreHistoryChart can plot a real trend. */
export default function ScoreEntryForm({ onAdded }) {
  const [bureau, setBureau] = useState('experian')
  const [score, setScore] = useState('')
  const [recordedOn, setRecordedOn] = useState(today())
  const [source, setSource] = useState('')
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    const n = Number(score)
    if (!Number.isInteger(n) || n < 300 || n > 850) {
      setError('Score must be a whole number between 300 and 850.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.addCreditScore({
        bureau, score: n, recorded_on: recordedOn || undefined,
        source: source.trim() || undefined, notes: notes.trim() || undefined,
      })
      setScore('')
      setSource('')
      setNotes('')
      onAdded()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="credit-score-form" onSubmit={submit}>
      <select value={bureau} onChange={(e) => setBureau(e.target.value)}>
        {BUREAUS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
      </select>
      <input
        type="number" min="300" max="850" placeholder="Score"
        value={score} onChange={(e) => setScore(e.target.value)}
      />
      <input type="date" value={recordedOn} onChange={(e) => setRecordedOn(e.target.value)} />
      <input placeholder="Source (optional)" value={source} onChange={(e) => setSource(e.target.value)} />
      <input placeholder="Notes (optional)" value={notes} onChange={(e) => setNotes(e.target.value)} />
      <button type="submit" disabled={busy}>{busy ? 'Savingâ€¦' : 'Add score'}</button>
      {error && <span className="credit-form-error">{error}</span>}
    </form>
  )
}
