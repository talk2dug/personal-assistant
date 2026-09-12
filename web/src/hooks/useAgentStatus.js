import { useEffect, useState } from 'react'
import { api } from '../api'

// Matches the office's own visual states (Agents.jsx) so anywhere this status shows up
// reads the same as the animated view.
export const AGENT_STATUS_LABEL = {
  working: 'working',
  on_gpu: 'on the GPU',
  waiting_gpu: 'waiting for the GPU',
  just_finished: 'just finished',
  failed: 'failed',
  idle: 'idle',
}

const POLL_MS = 4000

/**
 * Polls /api/agents/status on one shared timer.
 *
 * Extracted out of Agents.jsx so other views (the compact status panel on Chat.jsx) can
 * show the same agent list without opening a second, independently-timed poll against
 * the same endpoint — one fetch loop, multiple consumers.
 */
export function useAgentStatus() {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const data = await api.agentStatus()
        if (!cancelled) {
          setStatus(data)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return { status, error }
}
