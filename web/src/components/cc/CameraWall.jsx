import { useEffect, useState } from 'react'

/**
 * Every room at once.
 *
 * Each feed is a plain <img> pointed at the authenticated MJPEG proxy — the browser
 * handles the multipart stream natively, which is why this needs no player and no
 * websocket. Streams are only mounted while the wall is open: four simultaneous MJPEG
 * connections is real bandwidth off the terminals, and leaving them running behind a
 * closed modal would quietly saturate the Pis.
 *
 * A feed that fails swaps itself for a retry button rather than an empty frame, since
 * the usual cause is a terminal that rebooted and will be back in a moment.
 */
function Feed({ camera }) {
  const [broken, setBroken] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => setBroken(false), [camera.key, attempt])

  return (
    <figure className="cc-cam">
      <figcaption className="cc-cam-label">
        {camera.name}
        {!camera.enabled && <span className="cc-cam-off">disabled</span>}
      </figcaption>
      {broken ? (
        <button type="button" className="cc-cam-broken" onClick={() => setAttempt((n) => n + 1)}>
          Feed unavailable — retry
        </button>
      ) : (
        <img
          key={`${camera.key}-${attempt}`}
          src={`/api/cameras/${encodeURIComponent(camera.key)}/stream`}
          alt={`${camera.name} camera`}
          onError={() => setBroken(true)}
        />
      )}
    </figure>
  )
}

export default function CameraWall({ cameras, onClose }) {
  const list = (cameras || []).filter((c) => c.enabled)

  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="cc-section-backdrop" onClick={onClose}>
      <div className="blueprint cc-section is-wide" onClick={(e) => e.stopPropagation()}
           role="dialog" aria-modal="true" aria-label="Cameras">
        <i className="corner tl" /><i className="corner tr" />
        <i className="corner bl" /><i className="corner br" />

        <header className="cc-section-head">
          <span className="cc-section-title">Cameras</span>
          <div className="cc-section-actions">
            <button type="button" className="cc-btn" onClick={onClose}>CLOSE ✕</button>
          </div>
        </header>

        <div className="cc-section-body">
          {list.length === 0 ? (
            <p className="cc-empty">No cameras enabled.</p>
          ) : (
            <div className="cc-cam-wall">
              {list.map((camera) => <Feed key={camera.key} camera={camera} />)}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
