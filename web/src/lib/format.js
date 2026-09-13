// Small formatting helpers shared across the Command Center's dashboard cards. Existing
// pages (Media.jsx, StatusPanel.jsx) each already have their own local copy of one of
// these -- not touched here to avoid unrelated churn on working code; new cards use this
// shared version instead of adding a fourth copy.

export function fmtBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`
}

export function fmtUsd(v) {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}
