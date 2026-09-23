import { useEffect, useState } from 'react'
import { useAuth } from '../../context/AuthContext'
import { clockSuffix, clockTime } from '../../lib/cc'
import { SECTIONS } from '../../sections'

/**
 * The board's header: identity, weather, clock, the section launcher, and the count of
 * things waiting on Jack.
 *
 * The launcher is what replaced the nav sidebar when this became a single page. It is
 * deliberately a menu rather than a row of twelve tabs: on a board this dense, twelve
 * permanent labels along the top would compete with the panels for attention, and the
 * panels are the thing you are meant to be reading. Everything in the menu is also
 * reachable by clicking the panel or readout it belongs to — the menu is the
 * completeness guarantee, not the primary route.
 */
export default function Header({ weather, pendingReview, needsYou = 0, onOpenSection, lastUpdate, stale }) {
  const { user, logout } = useAuth()
  const [now, setNow] = useState(() => new Date())
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  // Clicking anywhere else closes the launcher — a menu that only closes via its own
  // button is a menu you end up fighting.
  useEffect(() => {
    if (!menuOpen) return
    const close = () => setMenuOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [menuOpen])

  // /api/weather returns a flat payload: temperature at the top level, forecast as a
  // sibling list. Entries that failed to resolve carry an `error` and no temperature.
  const nowTemp = weather?.temperature
  const days = (weather?.forecast || [])
    .filter((f) => !f.error && f.temperature != null)
    .slice(0, 5)

  return (
    <header className="cc-header">
      <div className="cc-brand">
        <span className="blueprint cc-brand-mark">
          <i className="corner tl" /><i className="corner tr" />
          <i className="corner bl" /><i className="corner br" />
          J
        </span>
        <span className="cc-brand-text">
          <span className="cc-brand-name">J A R V I S</span>
          <span className="cc-brand-sub">Command Center · {user?.display_name || 'operator'}</span>
        </span>
      </div>

      <div className="cc-header-mid">
        <span
          className={`blueprint cc-status-pill ${stale ? 'is-stale' : ''}`}
          title={lastUpdate ? `last refreshed ${lastUpdate.toLocaleTimeString()}` : 'never refreshed'}
        >
          <i className="corner tl" /><i className="corner tr" />
          <i className="corner bl" /><i className="corner br" />
          <span className="cc-pip is-pulse" />
          {stale ? 'RECONNECTING' : 'ALL SYSTEMS NOMINAL'}
        </span>

        {nowTemp != null && (
          <div className="cc-weather">
            <span className="cc-weather-now">{Math.round(nowTemp)}°</span>
            {days.map((d, i) => (
              <span className="cc-weather-day" key={i}>
                <span className="cc-weather-dow">
                  {new Date(d.datetime).toLocaleDateString(undefined, { weekday: 'narrow' })}
                </span>
                {Math.round(d.temperature)}°
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="cc-header-right">
        <div className="cc-clock">
          <span className="cc-clock-time">
            {clockTime(now)}<span className="cc-clock-sec">{clockSuffix(now)}</span>
          </span>
          <span className="cc-clock-date">
            {now.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
          </span>
        </div>

        <div className="cc-launcher" onClick={(e) => e.stopPropagation()}>
          <button type="button" className="cc-btn" onClick={() => setMenuOpen((v) => !v)} aria-expanded={menuOpen}>
            SECTIONS ▾
          </button>
          {menuOpen && (
            <div className="blueprint cc-launcher-menu">
              <i className="corner tl" /><i className="corner tr" />
              <i className="corner bl" /><i className="corner br" />
              {SECTIONS.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => { setMenuOpen(false); onOpenSection(s.key) }}
                >
                  {s.label}
                  {s.key === 'review' && pendingReview > 0 && (
                    <span className="cc-launcher-badge">{pendingReview}</span>
                  )}
                  {/* Same badge as the review queue, for the same reason: something is
                      stopped and waiting on him, and he should not have to open it to
                      find that out. */}
                  {s.key === 'needs' && needsYou > 0 && (
                    <span className="cc-launcher-badge">{needsYou}</span>
                  )}
                </button>
              ))}
              <button type="button" className="cc-launcher-out" onClick={logout}>Sign out</button>
            </div>
          )}
        </div>

        <button
          type="button"
          className={`blueprint cc-pending ${pendingReview > 0 ? 'is-hot' : ''}`}
          onClick={() => onOpenSection('review')}
        >
          <i className="corner tl" /><i className="corner tr" />
          <i className="corner bl" /><i className="corner br" />
          <span className="cc-pending-count">{pendingReview}</span>
          <span className="cc-pending-label">awaiting your call</span>
        </button>
      </div>
    </header>
  )
}
