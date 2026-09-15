import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

/**
 * The board's data pulse.
 *
 * Two loops, not one, because the panels have genuinely different metabolisms:
 *
 *  - The SNAPSHOT (presence, stove, tasks, pipelines, events, readouts) is a single
 *    cheap local read, so it can run fast enough to feel live.
 *  - The SLOW set (active work, SSH mesh, finance, crypto) each touch something that
 *    can hang — a TCP probe, a vendor cache — and are polled on their own longer tick
 *    with Promise.allSettled, so one of them being down leaves the other three, and
 *    the whole rest of the board, perfectly alive.
 *
 * Errors are held as data alongside the last good value rather than replacing it. A
 * board that blanks a panel the first time a poll blips is worse than useless — you
 * learn to distrust the whole screen. Panels show their last known numbers and a
 * staleness marker instead.
 */
const SNAPSHOT_MS = 5000
const SLOW_MS = 30000

function useVisible() {
  const [visible, setVisible] = useState(() => !document.hidden)
  useEffect(() => {
    const onChange = () => setVisible(!document.hidden)
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [])
  return visible
}

export function useCommandCenter() {
  const [snapshot, setSnapshot] = useState(null)
  const [snapshotError, setSnapshotError] = useState(null)
  const [lastUpdate, setLastUpdate] = useState(null)

  const [work, setWork] = useState(null)
  const [hosts, setHosts] = useState(null)
  const [finance, setFinance] = useState(null)
  const [netWorth, setNetWorth] = useState(null)
  const [crypto, setCrypto] = useState(null)
  const [schedule, setSchedule] = useState(null)
  const [slowErrors, setSlowErrors] = useState({})

  // Nothing on this board is worth a network request while it's in a background tab —
  // this screen is meant to live on a wall display for days at a time.
  const visible = useVisible()
  const visibleRef = useRef(visible)
  useEffect(() => { visibleRef.current = visible }, [visible])

  const loadSnapshot = useCallback(async () => {
    try {
      const data = await api.commandCenter()
      setSnapshot(data)
      setSnapshotError(null)
      setLastUpdate(new Date())
    } catch (e) {
      setSnapshotError(e.message)
    }
  }, [])

  const loadSlow = useCallback(async () => {
    const [w, h, f, nw, c, s] = await Promise.allSettled([
      api.activeWork(),
      api.sshHostsStatus(),
      Promise.all([api.financeSummary(), api.financeSafeToSpend()]),
      api.financeNetWorth(),
      api.cryptoDashboard(),
      api.scheduleReminders(),
    ])
    const errors = {}
    const take = (result, set, key) => {
      if (result.status === 'fulfilled') set(result.value)
      else errors[key] = result.reason?.message || 'unavailable'
    }
    take(w, (v) => setWork(v.items || []), 'work')
    take(h, (v) => setHosts(v.hosts || []), 'hosts')
    take(f, ([summary, safe]) => setFinance({ ...summary, safe_to_spend: safe?.safe_to_spend ?? null }), 'finance')
    take(nw, (v) => setNetWorth(v || []), 'netWorth')
    take(c, setCrypto, 'crypto')
    take(s, (v) => setSchedule(v || []), 'schedule')
    setSlowErrors(errors)
  }, [])

  useEffect(() => {
    let cancelled = false
    const tick = (fn) => () => { if (!cancelled && visibleRef.current) fn() }

    loadSnapshot()
    loadSlow()
    const fast = setInterval(tick(loadSnapshot), SNAPSHOT_MS)
    const slow = setInterval(tick(loadSlow), SLOW_MS)
    return () => { cancelled = true; clearInterval(fast); clearInterval(slow) }
  }, [loadSnapshot, loadSlow])

  // Coming back to the tab should show current numbers immediately, not whatever was
  // on screen when it was hidden.
  useEffect(() => {
    if (visible) { loadSnapshot(); loadSlow() }
  }, [visible, loadSnapshot, loadSlow])

  return {
    snapshot, snapshotError, lastUpdate, refresh: loadSnapshot,
    work, hosts, finance, netWorth, crypto, schedule, slowErrors,
  }
}
