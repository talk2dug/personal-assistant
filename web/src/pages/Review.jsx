import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

/**
 * The review surface: a stack of everything the team is waiting on a decision for, and
 * a large preview of whichever one you're looking at.
 *
 * Two shapes of decision share one screen. An item with no options is a straight
 * approve/reject. An item with two or more is a pick-one — the options become selectable
 * cards and the big preview shows whichever is selected, so choosing between three
 * artwork treatments means actually looking at them side by side rather than reading
 * filenames.
 */

const KIND_LABEL = {
  concept: 'Product concept', art: 'Artwork', listing: 'Listing',
  post: 'Social post', media: 'Media', research: 'Research', other: 'Item',
}

const IMAGE_RE = /\.(png|jpe?g|gif|webp|avif)$/i
const VIDEO_RE = /\.(mp4|webm|mov|m4v)$/i

function OptionPreview({ option }) {
  if (!option) return <div className="review-preview-empty">Nothing selected.</div>
  const path = option.media_path || ''
  const src = `/api/review/media/${option.id}`

  if (IMAGE_RE.test(path)) {
    return <img className="review-preview-media" src={src} alt={option.label} />
  }
  if (VIDEO_RE.test(path)) {
    return <video className="review-preview-media" src={src} controls loop muted playsInline />
  }
  if (option.body) {
    return <pre className="review-preview-text">{option.body}</pre>
  }
  return <div className="review-preview-empty">{option.description || 'No preview for this option.'}</div>
}

export default function Review() {
  const [items, setItems] = useState([])
  const [historyItems, setHistoryItems] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [chosenOption, setChosenOption] = useState(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [showDecided, setShowDecided] = useState(false)

  const load = useCallback(async () => {
    try {
      const [pending, decided] = await Promise.all([
        api.reviewItems('pending'),
        api.reviewItems('approved'),
      ])
      setItems(pending.items)
      setHistoryItems(decided.items)
      setError(null)
      // Keep the current selection if it's still pending; otherwise fall to the top of
      // the stack, so deciding an item advances you rather than dumping you nowhere.
      setSelectedId((prev) => {
        if (prev && pending.items.some((i) => i.id === prev)) return prev
        return pending.items[0]?.id ?? null
      })
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 10000)
    return () => clearInterval(id)
  }, [load])

  const shown = showDecided ? historyItems : items
  const selected = shown.find((i) => i.id === selectedId) || shown[0] || null
  const options = selected?.options || []
  const isChoice = options.length > 1

  // Default the selection to the first option (or whichever was previously chosen).
  useEffect(() => {
    if (!selected) return setChosenOption(null)
    const previouslyChosen = options.find((o) => o.chosen)
    setChosenOption(previouslyChosen?.id ?? options[0]?.id ?? null)
    setNote('')
  }, [selected?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  async function decide(decision) {
    if (!selected || busy) return
    setBusy(true)
    try {
      await api.decideReview(selected.id, {
        decision,
        option_id: isChoice ? chosenOption : null,
        note: note.trim() || null,
      })
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const previewOption = options.find((o) => o.id === chosenOption) || options[0] || null

  return (
    <div className="review-page">
      <header className="review-header">
        <div>
          <h1>Review</h1>
          <p className="review-sub">
            {items.length
              ? `${items.length} waiting on you`
              : 'Nothing waiting — the team will stack things here.'}
          </p>
        </div>
        <div className="review-tabs">
          <button className={!showDecided ? 'active' : ''} onClick={() => setShowDecided(false)}>
            Pending {items.length > 0 && <span className="review-count">{items.length}</span>}
          </button>
          <button className={showDecided ? 'active' : ''} onClick={() => setShowDecided(true)}>
            Approved
          </button>
        </div>
      </header>

      {error && <div className="review-error">Couldn’t load the queue: {error}</div>}

      <div className="review-body">
        <aside className="review-stack">
          {shown.length === 0 && <p className="review-empty">Nothing here.</p>}
          {shown.map((item) => (
            <button
              key={item.id}
              className={`review-card ${item.id === selected?.id ? 'selected' : ''} priority-${item.priority}`}
              onClick={() => setSelectedId(item.id)}
            >
              <span className="review-card-kind">{KIND_LABEL[item.kind] || item.kind}</span>
              <span className="review-card-title">{item.title}</span>
              <span className="review-card-meta">
                {item.source_agent || 'jarvis'}
                {item.options.length > 1 && ` · ${item.options.length} options`}
              </span>
            </button>
          ))}
        </aside>

        <section className="review-main">
          {!selected ? (
            <div className="review-preview-empty">Select something from the stack.</div>
          ) : (
            <>
              <div className="review-title-row">
                <div>
                  <span className="hud-label">{KIND_LABEL[selected.kind] || selected.kind}</span>
                  <h2>{selected.title}</h2>
                </div>
                {selected.status !== 'pending' && (
                  <span className={`review-badge ${selected.status}`}>{selected.status}</span>
                )}
              </div>

              {selected.summary && <p className="review-summary">{selected.summary}</p>}

              <div className="review-preview">
                {previewOption ? (
                  <OptionPreview option={previewOption} />
                ) : selected.detail ? (
                  <pre className="review-preview-text">{selected.detail}</pre>
                ) : (
                  <div className="review-preview-empty">No preview attached.</div>
                )}
              </div>

              {isChoice && (
                <div className="review-options">
                  {options.map((o) => (
                    <button
                      key={o.id}
                      className={`review-option ${o.id === chosenOption ? 'selected' : ''}`}
                      onClick={() => setChosenOption(o.id)}
                    >
                      {IMAGE_RE.test(o.media_path || '') && (
                        <img src={`/api/review/media/${o.id}`} alt={o.label} />
                      )}
                      <span className="review-option-label">{o.label}</span>
                      {o.description && <span className="review-option-desc">{o.description}</span>}
                      {o.chosen === 1 && <span className="review-option-chosen">chosen</span>}
                    </button>
                  ))}
                </div>
              )}

              {selected.detail && previewOption && (
                <pre className="review-detail">{selected.detail}</pre>
              )}

              {selected.status === 'pending' ? (
                <div className="review-actions">
                  <input
                    className="review-note"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Note (optional) — why, or what to change"
                  />
                  <button className="review-reject" disabled={busy} onClick={() => decide('rejected')}>
                    Reject
                  </button>
                  <button className="review-approve" disabled={busy} onClick={() => decide('approved')}>
                    {isChoice ? 'Approve selected' : 'Approve'}
                  </button>
                </div>
              ) : (
                selected.decision_note && (
                  <p className="review-decision-note">Your note: {selected.decision_note}</p>
                )
              )}
            </>
          )}
        </section>
      </div>
    </div>
  )
}
