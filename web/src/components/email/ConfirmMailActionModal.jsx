import { useState } from 'react'
import { api } from '../../api'

/**
 * The are-you-sure step in front of archive/delete. Neither call is destructive on the
 * server -- archive_message/delete_message are always copy-then-expunge into the
 * account's real Archive/Trash folder, never a bare EXPUNGE (see mail_client.py) -- but
 * a message disappearing from the inbox with no warning still feels destructive, so this
 * mirrors ConfirmSendModal's pattern rather than letting either action fire straight off
 * a row's icon button. Lighter than credit's ConfirmMailModal (no typed confirmation
 * word): that gate exists because LetterStream spends real, non-refundable postage,
 * which has no equivalent here.
 */
export default function ConfirmMailActionModal({ action, message, onClose, onDone }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const isDelete = action === 'delete'

  async function handleConfirm() {
    setBusy(true)
    setError(null)
    try {
      if (isDelete) await api.deleteEmail(message.uid, message.folder)
      else await api.archiveEmail(message.uid, message.folder)
      onDone()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="confirm-send-overlay" role="dialog" aria-modal="true">
      <div className="confirm-send-modal">
        <h3>{isDelete ? 'Delete this email?' : 'Archive this email?'}</h3>
        <p className="confirm-send-warning">
          {isDelete
            ? "This moves the message to Trash right away. It's not a permanent, " +
              'unrecoverable wipe, but it will leave your inbox immediately.'
            : "This moves the message out of your inbox into Archive. It's not deleted " +
              '-- you can still find it there any time.'}
        </p>

        <div className="confirm-send-block">
          <span className="hud-label">Subject</span>
          <div>{message.subject || '(no subject)'}</div>
        </div>
        <div className="confirm-send-block">
          <span className="hud-label">From</span>
          <div>{message.from}</div>
        </div>

        {error && <p className="email-form-error">{error}</p>}

        <div className="confirm-send-actions">
          <button className="confirm-send-cancel" onClick={onClose} disabled={busy}>Cancel</button>
          <button
            className={`confirm-send-go ${isDelete ? 'confirm-send-danger' : ''}`}
            onClick={handleConfirm}
            disabled={busy}
          >
            {busy
              ? (isDelete ? 'Deleting…' : 'Archiving…')
              : (isDelete ? 'Yes, delete it' : 'Yes, archive it')}
          </button>
        </div>
      </div>
    </div>
  )
}
