import JarvisDock from './components/JarvisDock'
import { AuthProvider, useAuth } from './context/AuthContext'
import { JarvisProvider } from './context/JarvisContext'
import CommandCenter from './pages/CommandCenter'
import Device from './pages/Device'
import Login from './pages/Login'

/**
 * The app shell.
 *
 * There is no nav and no router left here. The Command Center is the application: every
 * other section opens as a modal over it (see sections.js and SectionModal), so the
 * board keeps polling and the conversation keeps its place while you look at Finance or
 * the inbox. What used to be twelve routes is now one screen with twelve doors.
 *
 * JarvisDock still mounts, but only for the off-screen media elements and the pushed-
 * content/camera windows it owns — its floating status pill is suppressed, because the
 * board already shows Jarvis's state at 212px in the middle of the screen.
 */
function Gate() {
  const { user } = useAuth()

  // A voice terminal renders before any auth check: it sits on a shelf with nobody to
  // sign it in, and authenticates with the device key already in its URL.
  if (window.location.pathname === '/device') return <Device />
  if (user === undefined) return <div className="loading-screen">Loading…</div>
  if (user === null) return <Login />

  // The Jarvis session is created only once signed in — it fetches history and holds
  // the mic, neither of which makes sense on the login screen.
  return (
    <JarvisProvider>
      <div className="app-shell is-single-page">
        <main className="app-main">
          <CommandCenter />
        </main>
        <JarvisDock compact />
      </div>
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
