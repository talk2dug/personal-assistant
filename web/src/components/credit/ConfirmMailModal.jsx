import { useState } from 'react'
import { api } from '../../api'

function formatCost(cost) {
  if (cost === null || cost === undefined || cost === '') return '—'
  return /^\d+(\.\d+)?$/.test(String(cost)) ? `$${cost}` : cost
}

/**
 * THE hard confirm gate. This is the only component in the whole frontend that can
 * call api.mailDisputeLetter, which is the only frontend call that reaches
 * credit.py's POST /letters/:id/mail -- the one route that actually calls
 * letterstream_authorize_mail (real postage, a real letter that cannot be recalled).
 *
 * Deliberately more friction than a normal confirm dialog: two separate checkboxes
 * (reviewed the specifics / understand the consequence) plus typing the literal word
 * MAIL, on top of showing the exact recipient, the full letter text, and the quoted
 * cost. quoted_cost is echoed back to the server exactly as it was fetched here, so a
 * stale quote (e.g. someone re-drafted in another tab) is rejected server-side too.
 */
export default function ConfirmMailModal({ letter, onClose, onMailed }) {
  const [ackReviewed, setAckReviewed] = useState(false)
  const [ackMoney, setAckMoney] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const canConfirm = ackReviewed && ackMoney && confirmText.trim() === 'MAIL' && !busy

  async function handleMail() {
    if (!canConfirm) return
    setBusy(true)
    setError(null)
    try {
      await api.mailDisputeLetter(letter.id, letter.quoted_cost)
      onMailed()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="confirm-mail-overlay" role="dialog" aria-modal="true">
      <div className="confirm-mail-modal">
        <h3>Confirm before this is actually mailed</h3>
        <p className="confirm-mail-warning">
          This releases real postage and puts a real, physical letter in the mail. It
          cannot be recalled once sent. Read everything below before confirming.
        </p>

        <div className="confirm-mail-block">
          <span className="hud-label">Recipient</span>
          <div className="confirm-mail-recipient">
            <div>{letter.recipient_name}</div>
            <div>
              {letter.recipient_address}
              {letter.recipient_address_2 ? `, ${letter.recipient_address_2}` : ''}
            </div>
            <div>{letter.recipient_city}, {letter.recipient_state} {letter.recipient_zip}</div>
          </div>
        </div>

        <div className="confirm-mail-block">
          <span className="hud-label">Mail type</span>
          <div>{letter.mail_type}</div>
        </div>

        <div className="confirm-mail-block">
          <span className="hud-label">Quoted cost</span>
          <div className="confirm-mail-cost">{formatCost(letter.quoted_cost)}</div>
        </div>

        <div className="confirm-mail-block">
          <span className="hud-label">Letter text</span>
          <pre className="confirm-mail-letter-text">{letter.letter_text}</pre>
        </div>

        <label className="confirm-mail-check">
          <input type="checkbox" checked={ackReviewed} onChange={(e) => setAckReviewed(e.target.checked)} />
          I have reviewed the recipient and letter text above and they are correct.
        </label>
        <label className="confirm-mail-check">
          <input type="checkbox" checked={ackMoney} onChange={(e) => setAckMoney(e.target.checked)} />
          I understand this spends real money and mails a physical letter that cannot be recalled.
        </label>
        <label className="confirm-mail-type-label">
          Type MAIL to enable the button:
          <input
            className="confirm-mail-type-input"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder="MAIL"
          />
        </label>

        {error && <p className="credit-form-error">{error}</p>}

        <div className="confirm-mail-actions">
          <button className="confirm-mail-cancel" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="confirm-mail-go" onClick={handleMail} disabled={!canConfirm}>
            {busy ? 'Mailing…' : 'Mail this letter now'}
          </button>
        </div>
      </div>
    </div>
  )
}
