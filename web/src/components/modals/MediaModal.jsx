import { useEffect, useState } from 'react'
import { api } from '../../api'
import { fmtBytes } from '../../lib/format'
import Modal from './Modal'

export default function MediaModal({ onClose }) {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.mediaSummary().then(setSummary).catch((e) => setError(e.message))
  }, [])

  return (
    <Modal title="Media Catalogue" onClose={onClose}>
      {error && <p className="modal-error">{error}</p>}
      {!error && !summary && <p className="modal-empty">Loading…</p>}
      {!error && summary && (
        <>
          <div className="modal-section">
            <div className="modal-section-title">Total</div>
            <div className="dash-card-figure">{fmtBytes(summary.total_bytes)}</div>
            <div className="dash-card-sub">{summary.total_files.toLocaleString()} files</div>
          </div>

          <div className="modal-section">
            <div className="modal-section-title">By kind</div>
            {(summary.by_kind || []).length === 0 && <p className="modal-empty">Nothing catalogued yet.</p>}
            {(summary.by_kind || []).length > 0 && (
              <ul className="modal-row-list">
                {summary.by_kind.map((k) => (
                  <li key={k.kind} className="modal-row">
                    <span className="modal-row-label">{k.kind}</span>
                    <span className="modal-row-value">{k.files.toLocaleString()} files · {fmtBytes(k.bytes)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="modal-section">
            <div className="modal-section-title">By volume ({(summary.by_volume || []).length})</div>
            {(summary.by_volume || []).length === 0 && <p className="modal-empty">No volumes registered.</p>}
            {(summary.by_volume || []).length > 0 && (
              <ul className="modal-row-list">
                {summary.by_volume.map((v) => (
                  <li key={v.id} className="modal-row">
                    <span className="modal-row-label">{v.label || v.mountpoint} ({v.hostname})</span>
                    <span className="modal-row-value">{v.files.toLocaleString()} files · {fmtBytes(v.bytes)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </Modal>
  )
}
