import { useState } from 'react'
import { api } from '../../api'

const BUREAUS = [
  { value: 'experian', label: 'Experian' },
  { value: 'equifax', label: 'Equifax' },
  { value: 'transunion', label: 'TransUnion' },
  { value: 'other', label: 'Other (a creditor/furnisher directly)' },
]

/** The same inaccurate item reported by two bureaus is two separate dispute items --
 * each bureau is disputed, mailed, and resolved independently (see personal_db.py). */
export default function DisputeCreateForm({ onCreated }) {
  const [open, setOpen] = useState(false)
  const [bureau, setBureau] = useState('experian')
  const [creditorName, setCreditorName] = useState('')
  const [accountReference, setAccountReference] = useState('')
  const [itemDescription, setItemDescription] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!creditorName.trim() || !itemDescription.trim() || !reason.trim()) {
      setError('Creditor, whatâ€™s wrong, and the reason are all required.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.createDispute({
        bureau, creditor_name: creditorName.trim(), item_description: itemDescription.trim(),
        reason: reason.trim(), account_reference: accountReference.trim() || undefined,
      })
      setCreditorName('')
      setAccountReference('')
      setItemDescription('')
      setReason('')
      setOpen(false)
      onCreated()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="dispute-create">
      <button onClick={() => setOpen((o) => !o)}>{open ? 'Cancel' : '+ Track a new dispute'}</button>
      {open && (
        <form className="dispute-create-form" onSubmit={submit}>
          <select value={bureau} onChange={(e) => setBureau(e.target.value)}>
            {BUREAUS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
          </select>
          <input
            placeholder="Creditor / furnisher name"
            value={creditorName} onChange={(e) => setCreditorName(e.target.value)}
          />
          <input
            placeholder="Account reference (optional)"
            value={accountReference} onChange={(e) => setAccountReference(e.target.value)}
          />
          <textarea
            placeholder="What's being disputed" rows={2}
            value={itemDescription} onChange={(e) => setItemDescription(e.target.value)}
          />
          <textarea
            placeholder="Why it's inaccurate â€” goes into the dispute letter" rows={2}
            value={reason} onChange={(e) => setReason(e.target.value)}
          />
          <button type="submit" disabled={busy}>{busy ? 'Savingâ€¦' : 'Start tracking'}</button>
          {error && <span className="credit-form-error">{error}</span>}
        </form>
      )}
    </div>
  )
}
