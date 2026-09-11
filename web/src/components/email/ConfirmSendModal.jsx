import { useState } from 'react'
import { api } from '../../api'

/**
 * THE send gate. This is the only component in the frontend that can call api.sendEmail,
 * which is the only frontend call that reaches routes/email.py's POST /send -- the one
 * route that actually calls send_email (a real message, to a real address, that cannot
 * be recalled). ComposePanel builds this freely; nothing is sent until this modal's own
 * button is clicked, showing the exact to/subject/body one more time first.
 */
export default function ConfirmSendModal({ draft, onClose, onSent }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleSend() {
    setBusy(true)
    setError(null)
    try {
      await api.sendEmail(draft.to, draft.subject, draft.body)
      onSent()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="confirm-send-overlay" role="dialog" aria-modal="true">
      <div className="confirm-send-modal">
        <h3>Send this email?</h3>
        <p className="confirm-send-warning">
          This sends a real message right now. It cannot be recalled once sent.
        </p>

        <div className="confirm-send-block">
          <span className="hud-label">To</span>
          <div>{draft.to}</div>
        </div>
        <div className="confirm-send-block">
          <span className="hud-label">Subject</span>
          <div>{draft.subject}</div>
        </div>
        <div className="confirm-send-block">
          <span className="hud-label">Body</span>
          <pre className="confirm-send-body-text">{draft.body}</pre>
        </div>

        {error && <p className="email-form-error">{error}</p>}

        <div className="confirm-send-actions">
          <button className="confirm-send-cancel" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="confirm-send-go" onClick={handleSend} disabled={busy}>
            {busy ? 'Sending…' : 'Yes, send it'}
          </button>
        </div>
      </div>
    </div>
  )
}
