import ActiveWorkPanel from '../components/dashboard/ActiveWorkPanel'

/**
 * Landing page for the whole app (see sections.js). Deliberately thin: Active Work is
 * the first panel here (personal-dashboard task 17). Further panels -- a schedule
 * strip, a finance snapshot -- are separate later tasks and slot in below the same way
 * Finance.jsx composes its sub-components, rather than this file growing into a
 * monolith that owns everyone else's data-fetching too.
 */
export default function Dashboard() {
  return (
    <div className="dashboard-page">
      <header className="dashboard-header">
        <h1>Dashboard</h1>
        <p className="dashboard-sub">Everything in flight, at a glance.</p>
      </header>

      <ActiveWorkPanel />
    </div>
  )
}
