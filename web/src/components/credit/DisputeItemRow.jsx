import { useState } from 'react'
import { api } from '../../api'
import ConfirmMailModal from './ConfirmMailModal'

const BUREAU_LABEL = { experian: 'Experian', equifax: 'Equifax', transunion: 'TransUnion', other: 'Other' }
const MAIL_TYPES = [
  { value: 'certified', label: 'Certified mail (proof of mailing)' },
  { value: 'firstclass', label: 'First class' },
  { value: 'certnoerr', label: 'Certified, no return receipt' },
]

function today() {
  return new Date().toISOString().slice(0, 10)
}

function defaultLetterText(item) {
  return `Date: ${today()}

To Whom It May Concern,

I am writing to dispute the following item on my credit report:

Creditor: ${item.creditor_name}
Account reference: ${item.account_reference || 'N/A'}
Item: ${item.item_description}

Reason for dispute: ${item.reason}

Under the Fair Credit Reporting Act, I request that you investigate this item and
correct or remove it if it cannot be verified as accurate.

Sincerely,
`
}

function formatCost(cost) {
  if (cost === null || cost === undefined || cost === '') return '—'
  return /^\d+(\.\d+)?$/.test(String(cost)) ? `$${cost}` : cost
}

function formatWhen(iso) {
  if (!iso) return null
  try {
    return new Date(iso).toLocaleString('en-US', {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

/** Draft & QUOTE only -- this always hits letterstream_send_mail (preauth), never
 * letterstream_authorize_mail. Safe to submit freely, same as the chat tool
 * draft_dispute_letter (see personal_tools.py). Nothing is mailed or charged here. */
function DraftLetterForm({ item, onDrafted }) {
  const [mailType, setMailType] = useState('certified')
  const [letterText, setLetterText] = useState(() => defaultLetterText(item))
  const [useCustomRecipient, setUseCustomRecipient] = useState(item.bureau === 'other')
  const [recipientName, setRecipientName] = useState('')
  const [recipientAddress, setRecipientAddress] = useState('')
  const [recipientAddress2, setRecipientAddress2] = useState('')
  const [recipientCity, setRecipientCity] = useState('')
  const [recipientState, setRecipientState] = useState('')
  const [recipientZip, setRecipientZip] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!letterText.trim()) {
      setError('Letter text is required.')
      return
    }
    if (item.bureau === 'other' && (
      !recipientName.trim() || !recipientAddress.trim() ||
      !recipientCity.trim() || !recipientState.trim() || !recipientZip.trim()
    )) {
      setError('This dispute has no standard bureau address on file — full recipient details are required.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.draftDisputeLetter(item.id, {
        letter_text: letterText.trim(),
        mail_type: mailType,
        recipient_name: recipientName.trim(),
        recipient_address: recipientAddress.trim(),
        recipient_address_2: recipientAddress2.trim(),
        recipient_city: recipientCity.trim(),
        recipient_state: recipientState.trim(),
        recipient_zip: recipientZip.trim(),
      })
      onDrafted()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="dispute-draft-form" onSubmit={submit}>
      <label className="hud-label">Letter text</label>
      <textarea rows={10} value={letterText} onChange={(e) => setLetterText(e.target.value)} />

      <label className="hud-label">Mail type</label>
      <select value={mailType} onChange={(e) => setMailType(e.target.value)}>
        {MAIL_TYPES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
      </select>

      {item.bureau !== 'other' && (
        <label className="dispute-recipient-toggle">
          <input
            type="checkbox" checked={useCustomRecipient}
            onChange={(e) => setUseCustomRecipient(e.target.checked)}
          />
          Use a different recipient than {BUREAU_LABEL[item.bureau]}'s standard dispute address
        </label>
      )}

      {(useCustomRecipient || item.bureau === 'other') && (
        <div className="dispute-recipient-fields">
          <input placeholder="Recipient name" value={recipientName} onChange={(e) => setRecipientName(e.target.value)} />
          <input placeholder="Address" value={recipientAddress} onChange={(e) => setRecipientAddress(e.target.value)} />
          <input
            placeholder="Address line 2 (optional)"
            value={recipientAddress2} onChange={(e) => setRecipientAddress2(e.target.value)}
          />
          <input placeholder="City" value={recipientCity} onChange={(e) => setRecipientCity(e.target.value)} />
          <input placeholder="State" value={recipientState} onChange={(e) => setRecipientState(e.target.value)} />
          <input placeholder="Zip" value={recipientZip} onChange={(e) => setRecipientZip(e.target.value)} />
        </div>
      )}

      <button type="submit" disabled={busy}>{busy ? 'Getting quote…' : 'Get quote from LetterStream'}</button>
      {error && <p className="credit-form-error">{error}</p>}
    </form>
  )
}

function LetterRow({ letter, onOpenMail, onTrack }) {
  return (
    <div className={`dispute-letter-row status-${letter.status}`}>
      <div className="dispute-letter-main">
        <div className="dispute-letter-recipient">{letter.recipient_name}</div>
        <div className="dispute-letter-address">
          {letter.recipient_address}{letter.recipient_address_2 ? `, ${letter.recipient_address_2}` : ''},{' '}
          {letter.recipient_city}, {letter.recipient_state} {letter.recipient_zip}
        </div>
        <div className="dispute-letter-meta">
          {letter.mail_type} · quoted {formatCost(letter.quoted_cost)} · {formatWhen(letter.quoted_at)}
          {letter.tracking_number && ` · tracking ${letter.tracking_number}`}
        </div>
      </div>
      <div className="dispute-letter-actions">
        <span className={`dispute-letter-badge status-${letter.status}`}>{letter.status}</span>
        {letter.status === 'quoted' && (
          <button className="dispute-letter-mail-btn" onClick={() => onOpenMail(letter)}>Review &amp; mail</button>
        )}
        {letter.status === 'mailed' && (
          <button onClick={() => onTrack(letter)}>Refresh status</button>
        )}
      </div>
    </div>
  )
}

/** One dispute item's whole lifecycle: drafted -> mailed -> resolved. 'mailed' is never
 * set directly from this component -- it only ever happens as a side effect of a real
 * LetterStream authorization succeeding (see credit.py's mail_letter route), which is
 * why there's no "mark mailed" button here, only "Mark resolved" once it already is. */
export default function DisputeItemRow({ item, onChange }) {
  const [expanded, setExpanded] = useState(false)
  const [letters, setLetters] = useState(null)
  const [showDraftForm, setShowDraftForm] = useState(false)
  const [mailingLetter, setMailingLetter] = useState(null)
  const [showResolve, setShowResolve] = useState(false)
  const [resolution, setResolution] = useState('')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(null)

  async function loadLetters() {
    setLetters(await api.disputeLetters(item.id))
  }

  async function toggleExpand() {
    const next = !expanded
    setExpanded(next)
    if (next && letters === null) await loadLetters()
  }

  async function afterDraft() {
    setShowDraftForm(false)
    await loadLetters()
  }

  async function afterMailed() {
    setMailingLetter(null)
    await loadLetters()
    onChange()
  }

  async function track(letter) {
    setActionError(null)
    try {
      await api.trackDisputeLetter(letter.id)
      await loadLetters()
    } catch (err) {
      setActionError(err.message)
    }
  }

  async function resolve(e) {
    e.preventDefault()
    if (!resolution.trim()) return
    setBusy(true)
    setActionError(null)
    try {
      await api.updateDispute(item.id, { status: 'resolved', resolution: resolution.trim() })
      setShowResolve(false)
      setResolution('')
      onChange()
    } catch (err) {
      setActionError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`dispute-item-row status-${item.status}`}>
      <button className="dispute-item-header" onClick={toggleExpand}>
        <span className={`dispute-item-badge status-${item.status}`}>{item.status}</span>
        <span className="dispute-item-bureau">{BUREAU_LABEL[item.bureau] || item.bureau}</span>
        <span className="dispute-item-creditor">{item.creditor_name}</span>
        <span className="dispute-item-desc">{item.item_description}</span>
        <span className="dispute-item-expand">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="dispute-item-body">
          <p className="dispute-item-reason"><strong>Reason:</strong> {item.reason}</p>
          {item.account_reference && <p className="dispute-item-account">Account ref: {item.account_reference}</p>}
          {item.resolution && <p className="dispute-item-resolution"><strong>Resolution:</strong> {item.resolution}</p>}

          <div className="dispute-letters">
            <div className="dispute-letters-header">
              <span className="hud-label">Letters</span>
              <button onClick={() => setShowDraftForm((s) => !s)}>
                {showDraftForm ? 'Cancel' : '+ Draft a letter'}
              </button>
            </div>

            {showDraftForm && <DraftLetterForm item={item} onDrafted={afterDraft} />}

            {letters === null && <p className="empty-hint">Loading…</p>}
            {letters && letters.length === 0 && !showDraftForm && (
              <p className="empty-hint">No letters drafted yet.</p>
            )}
            {letters && letters.map((l) => (
              <LetterRow key={l.id} letter={l} onOpenMail={setMailingLetter} onTrack={track} />
            ))}
          </div>

          {actionError && <p className="credit-form-error">{actionError}</p>}

          {item.status === 'mailed' && !showResolve && (
            <button className="dispute-resolve-btn" onClick={() => setShowResolve(true)}>Mark resolved</button>
          )}
          {showResolve && (
            <form className="dispute-resolve-form" onSubmit={resolve}>
              <textarea
                rows={2}
                placeholder="What did the bureau actually do? (removed, updated, verified, no change...)"
                value={resolution} onChange={(e) => setResolution(e.target.value)}
              />
              <div>
                <button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save resolution'}</button>
                <button type="button" onClick={() => setShowResolve(false)}>Cancel</button>
              </div>
            </form>
          )}
        </div>
      )}

      {mailingLetter && (
        <ConfirmMailModal letter={mailingLetter} onClose={() => setMailingLetter(null)} onMailed={afterMailed} />
      )}
    </div>
  )
}
