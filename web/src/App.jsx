import { useEffect, useState } from 'react'
import { Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api'
import JarvisDock from './components/JarvisDock'
import { AuthProvider, useAuth } from './context/AuthContext'
import { JarvisProvider, useJarvis } from './context/JarvisContext'
import Device from './pages/Device'
import Login from './pages/Login'
import { sections } from './sections'

/** The Command Center already has its own voice bar and text-chat trigger built in, so
 *  the floating dock (which exists to reach Jarvis from every *other* page) would just
 *  duplicate it here. */
const ORB_PATH = '/dashboard'

function NavState() {
  const { mode, recording } = useJarvis()
  return (
    <div className={`nav-jarvis mode-${mode}`}>
      <span className="nav-jarvis-pip" />
      <span>{recording ? 'listening' : mode}</span>
    </div>
  )
}

/** Pending approvals, polled in the shell so the badge is visible from any page —
 *  work waiting on you shouldn't only be discoverable by visiting the Review tab. */
function usePendingReviews() {
  const [pending, setPending] = useState(0)
  const location = useLocation()
  useEffect(() => {
    let cancelled = false
    const poll = () =>
      api.reviewItems('pending')
        .then((d) => !cancelled && setPending(d.pending ?? 0))
        .catch(() => {})
    poll()
    const id = setInterval(poll, 15000)
    return () => { cancelled = true; clearInterval(id) }
  }, [location.pathname])
  return pending
}

function Shell() {
  const { user, logout } = useAuth()
  const location = useLocation()
  const onOrb = location.pathname === ORB_PATH
  const pendingReviews = usePendingReviews()
  const [menuOpen, setMenuOpen] = useState(false)

  // Close the mobile menu whenever the route changes, so navigating away never leaves
  // the overlay stuck open on top of the next page.
  useEffect(() => {
    setMenuOpen(false)
  }, [location.pathname])

  return (
    <div className="app-shell">
      <nav className={`app-nav${menuOpen ? ' nav-open' : ''}`}>
        <div className="app-title">
          <span className="app-title-badge">J</span>
          Jarvis
        </div>
        <button
          type="button"
          className="nav-toggle"
          aria-label="Toggle navigation menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((open) => !open)}
        >
          <span className="nav-toggle-bar" />
          <span className="nav-toggle-bar" />
          <span className="nav-toggle-bar" />
        </button>
        {sections.map((s) => (
          <NavLink
            key={s.path}
            to={s.path}
            className={({ isActive }) => (isActive ? 'active' : '')}
            onClick={() => setMenuOpen(false)}
          >
            {s.label}
            {s.path === '/review' && pendingReviews > 0 && (
              <span className="nav-badge">{pendingReviews}</span>
            )}
          </NavLink>
        ))}
        <div className="nav-spacer" />
        <NavState />
        <div className="nav-user">{user.display_name}</div>
        <button className="nav-logout" onClick={logout}>
          Sign out
        </button>
      </nav>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<Navigate to={sections[0].path} replace />} />
          {sections.map((s) => (
            <Route key={s.path} path={s.path} element={<s.element />} />
          ))}
        </Routes>
      </main>
      <JarvisDock compact={onOrb} />
    </div>
  )
}

function Gate() {
  const { user } = useAuth()
  // A voice terminal renders before any auth check: it sits on a shelf with nobody to
  // sign it in, and authenticates with the device key already in its URL.
  if (window.location.pathname === '/device') return <Device />
  if (user === undefined) return <div className="loading-screen">Loading…</div>
  if (user === null) return <Login />
  // The Jarvis session is created only once signed in — it fetches history and holds the
  // mic, neither of which makes sense on the login screen.
  return (
    <JarvisProvider>
      <Shell />
    </JarvisProvider>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  )
}
