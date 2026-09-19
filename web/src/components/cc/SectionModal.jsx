import { useEffect } from 'react'
import { useJarvis } from '../../context/JarvisContext'
import { SECTION_BY_KEY } from '../../sections'

/**
 * A whole section of the app, opened over the board.
 *
 * This is what replaced routing. Each section is the same page component it always was
 * — Finance, Kitchen, Email — mounted inside a full-bleed blueprint frame instead of a
 * route. Mounting rather than navigating is the point: the board keeps running
 * underneath, the conversation keeps its place, and closing puts you back exactly where
 * you were instead of on a fresh render of the dashboard.
 *
 * Two things every section modal carries, both learned the hard way:
 *
 *  - Its own "talk to Jarvis" button. A full-screen overlay swallows clicks to the
 *    console underneath it, so without this you would have to close what you are
 *    looking at before you could ask about it.
 *  - viewing_context. Whatever is open is described to Jarvis in one plain sentence
 *    (see sections.js), so "what am I looking at?" has a real answer without you
 *    having to narrate your own screen.
 */
export default function SectionModal({ sectionKey, onClose }) {
  const { setModalOpen, setViewingContext } = useJarvis()
  const section = SECTION_BY_KEY[sectionKey]

  useEffect(() => {
    if (!section) return undefined
    setViewingContext(section.context || `the ${section.label} section`)
    return () => setViewingContext(null)
  }, [section, setViewingContext])

  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  if (!section) return null
  const Body = section.element

  return (
    <div className="cc-section-backdrop" onClick={onClose}>
      <div
        className={`blueprint cc-section ${section.wide ? 'is-wide' : ''}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={section.label}
      >
        <i className="corner tl" /><i className="corner tr" />
        <i className="corner bl" /><i className="corner br" />

        <header className="cc-section-head">
          <span className="cc-section-title">{section.label}</span>
          <div className="cc-section-actions">
            <button type="button" className="cc-btn" onClick={() => setModalOpen(true)}>
              ASK JARVIS
            </button>
            <button type="button" className="cc-btn" onClick={onClose}>CLOSE ✕</button>
          </div>
        </header>

        <div className="cc-section-body">
          <Body />
        </div>
      </div>
    </div>
  )
}
