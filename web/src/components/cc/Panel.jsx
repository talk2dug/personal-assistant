/**
 * The board's one panel shape: a blueprint frame with registration marks at its
 * corners, a condensed uppercase title, and an optional right-hand meta value.
 *
 * Every tile on the Command Center is one of these, including the ones that open a
 * modal. `onOpen` is what makes a panel a door: it turns the whole frame into a button
 * (keyboard included) and adds the corner chevron, so "this drills down" is a visual
 * property of the panel rather than something you have to discover by clicking.
 */
export default function Panel({
  title, meta, metaTint, onOpen, openLabel, className = '', children, grow = false,
}) {
  const interactive = typeof onOpen === 'function'

  const handleKey = (e) => {
    if (!interactive) return
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onOpen()
    }
  }

  return (
    <section
      className={`blueprint cc-panel ${interactive ? 'is-open' : ''} ${grow ? 'is-grow' : ''} ${className}`}
      onClick={interactive ? onOpen : undefined}
      onKeyDown={handleKey}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-label={interactive ? (openLabel || `Open ${title}`) : undefined}
    >
      <i className="corner tl" /><i className="corner tr" />
      <i className="corner bl" /><i className="corner br" />

      {title && (
        <header className="cc-panel-head">
          <span className="cc-panel-title">{title}</span>
          {meta != null && (
            <span className="cc-panel-meta" style={metaTint ? { color: metaTint } : undefined}>
              {meta}
            </span>
          )}
          {interactive && <span className="cc-panel-open" aria-hidden="true">▸</span>}
        </header>
      )}
      {children}
    </section>
  )
}

/** A panel that has nothing to show yet — an integration that isn't wired up, or a
 *  table with no rows. Says which, rather than rendering a convincing zero. */
export function PanelEmpty({ children }) {
  return <p className="cc-empty">{children}</p>
}

/** A panel whose data source failed. Deliberately quiet: one dim amber line, never a
 *  red block — a scraper being down is not an emergency and must not look like one. */
export function PanelError({ children }) {
  return <p className="cc-panel-err">{children}</p>
}
