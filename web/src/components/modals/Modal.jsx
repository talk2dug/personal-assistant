import { useEffect } from 'react'
import { useJarvis } from '../../context/JarvisContext'
import './modal.css'

/**
 * The one shared modal base for the Command Center's click-to-detail cards (Phase 2)
 * and, later, Jarvis's own pushed content (Phase 3) -- every modal in this app before
 * now hand-rolled its own backdrop/panel classes (ConfirmSendModal, ConfirmMailModal,
 * CameraWindow); this is the first shared one, used by everything new from here on
 * rather than retrofitted onto those three. Escape-to-close, backdrop-click-to-close,
 * and the angled-corner-cut HUD styling are all here once instead of copy-pasted.
 *
 * elevated stacks this modal above the chat text modal (z-index, see modal.css) --
 * only ContentModal (Phase 3, whatever Jarvis just pushed into view) uses it, since
 * that's the one thing the owner needs to actually see even if the chat modal is also
 * open. Every click-opened detail modal (Phase 2) stays at the base layer, below chat.
 *
 * Every modal also carries its own "talk to Jarvis" button in the header (Phase 4):
 * a full-screen backdrop blocks clicks to whatever's behind it, including the footer's
 * own Message button, so without this the owner would have to close whatever they're
 * looking at before they could ask Jarvis about it. This opens the chat modal on top
 * (it stacks above every detail/content modal, see modal.css) without touching onClose,
 * so the thing being discussed stays open underneath.
 */
export default function Modal({ title, onClose, children, wide = false, elevated = false }) {
  const { setModalOpen } = useJarvis()

  useEffect(() => {
    function onKeyDown(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className={`modal-backdrop ${elevated ? 'modal-backdrop-elevated' : ''}`} onClick={onClose}>
      <div
        className={`modal-panel ${wide ? 'modal-panel-wide' : ''}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="modal-header">
          <span className="hud-label">{title}</span>
          <div className="modal-header-actions">
            <button type="button" className="modal-talk" onClick={() => setModalOpen(true)}>
              talk to jarvis
            </button>
            <button type="button" className="modal-close" onClick={onClose}>close</button>
          </div>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}
