import { useEffect } from 'react'
import './modal.css'

/**
 * The one shared modal base for the Command Center's click-to-detail cards (Phase 2)
 * and, later, Jarvis's own pushed content (Phase 3) -- every modal in this app before
 * now hand-rolled its own backdrop/panel classes (ConfirmSendModal, ConfirmMailModal,
 * CameraWindow); this is the first shared one, used by everything new from here on
 * rather than retrofitted onto those three. Escape-to-close, backdrop-click-to-close,
 * and the angled-corner-cut HUD styling are all here once instead of copy-pasted.
 */
export default function Modal({ title, onClose, children, wide = false }) {
  useEffect(() => {
    function onKeyDown(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className={`modal-panel ${wide ? 'modal-panel-wide' : ''}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="modal-header">
          <span className="hud-label">{title}</span>
          <button type="button" className="modal-close" onClick={onClose}>close</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}
