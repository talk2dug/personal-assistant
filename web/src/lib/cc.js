/**
 * Shared vocabulary for the Command Center board.
 *
 * TINT is the board's entire colour language. Four steel values plus one amber: the
 * board is deliberately near-monochrome so that amber means exactly one thing --
 * something wants Jack -- and reads instantly against everything else. The orb is the
 * only element allowed to leave this palette (see Orb.jsx), because the orb is the one
 * thing whose colour carries state rather than status.
 */
export const TINT = {
  ok: '#b5d9fd',
  base: '#94bce3',
  dim: '#7ea3c4',
  warn: '#e8a33d',
  idle: '#5d82a4',
  text: '#c9dcec',
  bright: '#e6f1fb',
  faint: '#6d90b0',
}

export function tintOf(key) {
  return TINT[key] || TINT.base
}

/** 4:07 style, in the viewer's own timezone. */
export function clockTime(date) {
  return `${date.getHours() % 12 || 12}:${String(date.getMinutes()).padStart(2, '0')}`
}

export function clockSuffix(date) {
  return `:${String(date.getSeconds()).padStart(2, '0')} ${date.getHours() < 12 ? 'AM' : 'PM'}`
}

export function stamp(date) {
  return [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':')
}

/** "14:32" for an ISO timestamp, local time. Event log / schedule rows. */
export function hhmm(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--:--'
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/**
 * How a due date reads on a task chip: "2d late", "today", "Thu", "Oct 4".
 *
 * Deliberately relative rather than absolute for anything inside a week. The whole
 * point of the Tasks panel is to answer "what wants me now" at a glance, and a date
 * like "2026-09-18" makes you do arithmetic before you can answer that.
 */
export function dueLabel(iso, now = new Date()) {
  if (!iso) return null
  const due = new Date(iso)
  if (Number.isNaN(due.getTime())) return null

  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const days = Math.round((startOfDay(due) - startOfDay(now)) / 86400000)

  if (days < 0) return days === -1 ? '1d late' : `${-days}d late`
  if (days === 0) return 'today'
  if (days === 1) return 'tomorrow'
  if (days < 7) return due.toLocaleDateString(undefined, { weekday: 'short' })
  return due.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** Which bucket a due date lands in, recomputed in the VIEWER's timezone.
 *  The server sends its own bucket too (UTC days); this is the one that gets shown. */
export function dueBucket(iso, now = new Date()) {
  if (!iso) return 'someday'
  const due = new Date(iso)
  if (Number.isNaN(due.getTime())) return 'someday'
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const days = Math.round((startOfDay(due) - startOfDay(now)) / 86400000)
  if (days < 0) return 'overdue'
  if (days === 0) return 'today'
  if (days < 7) return 'week'
  return 'later'
}

export const TASK_BUCKETS = [
  { key: 'overdue', label: 'Overdue', tint: TINT.warn },
  { key: 'today', label: 'Today', tint: TINT.ok },
  { key: 'week', label: 'This week', tint: TINT.base },
  { key: 'later', label: 'Later', tint: TINT.dim },
  { key: 'someday', label: 'No date', tint: TINT.idle },
]

export function fmtUsd(v, frac = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toLocaleString('en-US', {
    style: 'currency', currency: 'USD',
    maximumFractionDigits: frac, minimumFractionDigits: frac,
  })
}

export function fmtNum(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toLocaleString('en-US')
}

/**
 * An SVG path through a series of numbers, scaled to fit a w×h box.
 *
 * Returns null for a series too short to draw, so callers can render nothing rather
 * than an "M NaN NaN" path — which browsers silently drop, leaving a chart that looks
 * empty for no discoverable reason.
 */
export function sparkPath(values, w, h, pad = 4) {
  const vals = (values || []).filter((v) => typeof v === 'number' && !Number.isNaN(v))
  if (vals.length < 2) return null
  const lo = Math.min(...vals)
  const hi = Math.max(...vals)
  const span = hi - lo || 1
  const x = (i) => (i / (vals.length - 1)) * w
  const y = (v) => h - pad - ((v - lo) / span) * (h - pad * 2)
  return vals.reduce(
    (d, v, i) => d + `${i ? ' L' : 'M'} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`,
    '',
  )
}
