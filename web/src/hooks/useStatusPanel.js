import { useEffect, useState } from 'react'
import { api } from '../api'
import { AGENT_STATUS_LABEL, useAgentStatus } from './useAgentStatus'

// Crypto trades happen on a several-minute cadence (see Crypto.jsx) and reminders/review
// items move slower still, so one shared 15s tick for this trio is plenty fresh for a
// glance-only summary — the agent roster keeps its own faster 4s tick via useAgentStatus.
const POLL_MS = 15000

/** due_at is a server timestamp; dayOffset 0 = today, 1 = tomorrow, compared in the
 * viewer's local time — same as how Schedule.jsx formats these for display. */
function isLocalDay(iso, dayOffset) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return false
  const target = new Date()
  target.setDate(target.getDate() + dayOffset)
  return (
    d.getFullYear() === target.getFullYear() &&
    d.getMonth() === target.getMonth() &&
    d.getDate() === target.getDate()
  )
}

function summarizeSchedule(reminders) {
  const list = reminders || []
  const byDue = (a, b) => new Date(a.due_at) - new Date(b.due_at)
  return {
    today: list.filter((r) => isLocalDay(r.due_at, 0)).sort(byDue),
    tomorrow: list.filter((r) => isLocalDay(r.due_at, 1)).sort(byDue),
  }
}

/**
 * Combined, condensed data source for the Chat page's compact status panel: the agent
 * roster, the crypto desk's top-line numbers only, today/tomorrow's schedule, and the
 * review queue count.
 *
 * Deliberately not a generic "fetch everything" hook — it summarizes as it goes (e.g.
 * schedule reminders are bucketed into today/tomorrow here, not left as a flat list) so
 * the component stays dumb and small. Agents reuse the office's existing poll via
 * useAgentStatus; crypto/schedule/review share one lightweight poll since the panel
 * only ever needs their top-line shape, not the full per-page detail.
 */
export function useStatusPanel() {
  const { status: agentStatus, error: agentsError } = useAgentStatus()
  const [book, setBook] = useState(null)
  const [cryptoTraders, setCryptoTraders] = useState(0)
  const [cryptoError, setCryptoError] = useState(null)
  const [schedule, setSchedule] = useState(null)
  const [scheduleError, setScheduleError] = useState(null)
  const [pendingReview, setPendingReview] = useState(0)
  const [reviewError, setReviewError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function poll() {
      const [c, s, r] = await Promise.allSettled([
        api.cryptoDashboard(),
        api.scheduleReminders(),
        api.reviewItems('pending'),
      ])
      if (cancelled) return

      if (c.status === 'fulfilled') {
        setBook(c.value.book || null)
        setCryptoTraders(c.value.traders?.length || 0)
        setCryptoError(null)
      } else {
        setCryptoError(c.reason?.message || 'failed to load')
      }

      if (s.status === 'fulfilled') {
        setSchedule(summarizeSchedule(s.value))
        setScheduleError(null)
      } else {
        setScheduleError(s.reason?.message || 'failed to load')
      }

      if (r.status === 'fulfilled') {
        setPendingReview(r.value.pending ?? r.value.items?.length ?? 0)
        setReviewError(null)
      } else {
        setReviewError(r.reason?.message || 'failed to load')
      }
    }
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const agents = (agentStatus?.agents || []).map((a) => ({
    key: a.key,
    title: a.title,
    label: AGENT_STATUS_LABEL[a.status] || a.status,
  }))

  return {
    agents, agentsError,
    book, cryptoTraders, cryptoError,
    schedule, scheduleError,
    pendingReview, reviewError,
  }
}
