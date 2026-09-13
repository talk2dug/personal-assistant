import { useEffect, useState } from 'react'
import { api } from '../../api'
import { OptionPreview } from '../../pages/Review'
import Modal from './Modal'

/**
 * What Jarvis pulls into view mid-conversation -- the frontend half of show_content
 * (engine.py) via the same one-shot pending-content handoff show_camera already uses
 * (see JarvisContext.jsx's activeContent, set from sendToJarvis's response). Two kinds:
 * 'text' needs nothing further (title+body arrive with the tool call already); 'review_item'
 * fetches the real item (so this always shows current, real data, never something staged
 * days ago) and reuses Review.jsx's own OptionPreview for image/video rendering -- one
 * image pipeline, not two. This is a glance, not the decision UI: no approve/reject
 * here, just "look at what I'm talking about" -- the owner still decides on /review.
 */
export default function ContentModal({ content, onClose }) {
  const [item, setItem] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (content.kind !== 'review_item') return
    api.reviewItem(content.review_item_id).then(setItem).catch((e) => setError(e.message))
  }, [content])

  if (content.kind === 'text') {
    return (
      <Modal title={content.title} onClose={onClose} elevated>
        <pre className="modal-text-body">{content.body}</pre>
      </Modal>
    )
  }

  return (
    <Modal title={item?.title || 'Review item'} onClose={onClose} wide elevated>
      {error && <p className="modal-error">{error}</p>}
      {!error && !item && <p className="modal-empty">Loading…</p>}
      {!error && item && (
        <>
          {item.summary && <p className="modal-content-summary">{item.summary}</p>}
          {item.options.length === 0 && item.detail && (
            <pre className="modal-text-body">{item.detail}</pre>
          )}
          {item.options.length > 0 && (
            <div className="modal-content-gallery">
              {item.options.map((o) => (
                <div key={o.id} className="modal-content-gallery-item">
                  <OptionPreview option={o} />
                  <div className="modal-content-gallery-label">{o.label}</div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Modal>
  )
}
