import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import Media from './Media'
import './inventory.css'

// Category colours carry meaning across the whole page: a folder's chip, a drive's bar and
// the where-things-live table all use the same one. File types borrow the colour of the
// category they most often belong to, so a mostly-blue type bar next to a Photos chip
// reads as agreement at a glance. Amber is deliberately absent: on this board amber
// means "wants Jack", and a category is not a warning.
const CATEGORY_COLOR = {
  side_hustle: '#e07bd6', photos: '#5cb8ff', home_video: '#8d87ff', music: '#4fd1a1',
  projects: '#d4d95f', documents: '#c9d6e3', software: '#ff8f73', system_junk: '#7c8a99',
  unsorted: '#475566',
}
const TYPE_COLOR = {
  image: '#5cb8ff', video: '#8d87ff', audio: '#4fd1a1', design: '#e07bd6', model3d: '#a9ad4a',
  document: '#c9d6e3', code: '#d4d95f', archive: '#a58b6f', software: '#ff8f73',
  system: '#7c8a99', other: '#3a4652',
}
const TYPE_LABEL = {
  image: 'images', video: 'video', audio: 'audio', design: 'design files', model3d: '3D models',
  document: 'documents', code: 'code', archive: 'archives', software: 'installers',
  system: 'system files', other: 'other',
}
const POLL_MS = 30000

function bytes(n) {
  let v = Number(n || 0)
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return i === 0 ? `${v} B` : `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

function count(n) {
  return Number(n || 0).toLocaleString()
}

function years(oldest, newest) {
  if (!oldest || !newest) return '—'
  const a = new Date(oldest * 1000).getFullYear()
  const b = new Date(newest * 1000).getFullYear()
  return a === b ? `${a}` : `${a}–${b}`
}

function ago(iso) {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

/** A proportional bar over {key: bytes}. Segments in `order`, anything tiny still visible. */
function StackBar({ parts, colors, order, labels, height = 8, title }) {
  const total = Object.values(parts || {}).reduce((a, b) => a + (b || 0), 0)
  if (!total) return <div className="inv-bar inv-bar-empty" style={{ height }} />
  const keys = (order || Object.keys(parts)).filter((k) => parts[k] > 0)
  return (
    <div className="inv-bar" style={{ height }} title={title}>
      {keys.map((k) => (
        <span
          key={k}
          style={{ flexGrow: parts[k], background: colors[k] || '#3a4652' }}
          title={`${labels?.[k] || k}: ${bytes(parts[k])} (${Math.round((parts[k] / total) * 100)}%)`}
        />
      ))}
    </div>
  )
}

function CategoryChip({ category, source, labels }) {
  return (
    <span
      className={`inv-chip inv-chip-${source || 'suggested'}`}
      style={{ '--cat': CATEGORY_COLOR[category] || '#475566' }}
      title={source === 'tag' ? 'You tagged this folder'
        : source === 'inherited_tag' ? 'Inherited from a folder you tagged above'
          : 'Suggested by Jarvis — tag it to confirm or change'}
    >
      {labels[category] || category}
    </span>
  )
}

function TagSelect({ id, ownTag, categories, onTag, busy }) {
  return (
    <select
      id={`inv-tag-${id}`}
      className="inv-select"
      value=""
      disabled={busy}
      aria-label="Tag this folder"
      onChange={(e) => {
        const v = e.target.value
        if (v) onTag(id, v === '__clear' ? null : v)
      }}
    >
      <option value="">{ownTag ? 'Change tag…' : 'Tag as…'}</option>
      {categories.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
      {ownTag && <option value="__clear">Clear my tag</option>}
    </select>
  )
}

// ------------------------------------------------------------------- overview

function Totals({ data, labels }) {
  const totals = data.category_totals || {}
  const all = Object.values(totals).reduce((a, b) => a + b, 0)
  const order = data.categories.map((c) => c.key)
  return (
    <section>
      <h3>Everything, by where it belongs</h3>
      <StackBar parts={totals} colors={CATEGORY_COLOR} order={order} labels={labels} height={14} />
      <div className="inv-legend">
        {order.filter((k) => totals[k]).map((k) => (
          <span key={k} className="inv-legend-item">
            <i style={{ background: CATEGORY_COLOR[k] }} />
            {labels[k]} <b>{bytes(totals[k])}</b>
            <em>{Math.round((totals[k] / all) * 100)}%</em>
          </span>
        ))}
      </div>
      <p className="inv-muted">
        {bytes(all)} across {data.volumes.filter((v) => v.current_scan_id).length} scanned drives.
        Solid chips are folders you've tagged; dashed ones are Jarvis's suggestions.
        {data.tags ? ` ${data.tags} folder${data.tags === 1 ? '' : 's'} tagged so far.` : ''}
      </p>
    </section>
  )
}

function volumeState(v) {
  if (v.scan_status === 'running') return { tone: 'live', text: 'scanning now' }
  if (!v.online) return { tone: 'off', text: `offline · last seen ${ago(v.last_seen_at)}` }
  if (v.scan_status === 'failed') return { tone: 'warn', text: `scan failed: ${v.scan_error || 'unknown'}` }
  if (!v.current_scan_id) return { tone: 'off', text: 'not scanned yet' }
  return { tone: 'ok', text: `scanned ${ago(v.scanned_at)}` }
}

function Drives({ data, labels, onOpen }) {
  const byHost = useMemo(() => {
    const m = new Map()
    for (const v of data.volumes) {
      if (!m.has(v.host)) m.set(v.host, [])
      m.get(v.host).push(v)
    }
    return [...m.entries()]
  }, [data.volumes])
  const unreachable = data.hosts.filter((h) => !h.reachable)
  const order = data.categories.map((c) => c.key)

  return (
    <section>
      <h3>Drives</h3>
      <div className="inv-hosts">
        {byHost.map(([host, vols]) => (
          <div key={host} className="inv-host">
            <div className="inv-host-name">{host}</div>
            {vols.map((v) => {
              const st = volumeState(v)
              const clickable = !!v.root_id
              return (
                <button
                  key={v.id} type="button" className="inv-drive" disabled={!clickable}
                  onClick={() => clickable && onOpen(v.root_id)}
                >
                  <div className="inv-drive-top">
                    <span className="inv-drive-mount">{v.mount}</span>
                    <span className="inv-drive-label">{v.label || ''}</span>
                    {v.is_system ? <span className="inv-tag-mini">system</span> : null}
                  </div>
                  <StackBar parts={v.by_category} colors={CATEGORY_COLOR} order={order} labels={labels} />
                  <div className="inv-drive-meta">
                    <span>{bytes(v.used_bytes)} used of {bytes(v.size_bytes)}</span>
                    <span className={`inv-state inv-state-${st.tone}`}>{st.text}</span>
                  </div>
                </button>
              )
            })}
          </div>
        ))}
      </div>
      {unreachable.length > 0 && (
        <p className="inv-muted">
          Couldn't reach {unreachable.map((h) => h.name).join(', ')} on the last scan. Their drives
          aren't included.
        </p>
      )}
    </section>
  )
}

function WhereThingsLive({ data, labels, onOpen }) {
  const scanned = data.volumes.filter((v) => v.current_scan_id)
  const cats = data.categories.map((c) => c.key).filter((k) => data.category_totals?.[k])
  if (!scanned.length || !cats.length) return null
  const max = Math.max(...scanned.flatMap((v) => cats.map((k) => v.by_category[k] || 0)))
  return (
    <section>
      <h3>Where each category lives</h3>
      <div className="inv-table-wrap">
        <table className="inv-table inv-matrix">
          <thead>
            <tr>
              <th>Drive</th>
              {cats.map((k) => (
                <th key={k} className="inv-num"><i className="inv-dot" style={{ background: CATEGORY_COLOR[k] }} />{labels[k]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {scanned.map((v) => (
              <tr key={v.id}>
                <td>
                  <button type="button" className="inv-link" onClick={() => onOpen(v.root_id)}>
                    {v.host} {v.mount}
                  </button>
                  <span className="inv-faint"> {v.label}</span>
                </td>
                {cats.map((k) => {
                  const b = v.by_category[k] || 0
                  const alpha = b ? 0.12 + 0.55 * Math.sqrt(b / max) : 0
                  return (
                    <td key={k} className="inv-num inv-mono"
                        style={{ background: b ? `color-mix(in srgb, ${CATEGORY_COLOR[k]} ${Math.round(alpha * 100)}%, transparent)` : undefined }}>
                      {b ? bytes(b) : ''}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="inv-muted">Darker cells hold more. A category spread across many drives is a candidate to consolidate.</p>
    </section>
  )
}

// --------------------------------------------------------------------- folder

function FolderView({ dirId, categories, labels, onOpen, onBack }) {
  const [f, setF] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [showFiles, setShowFiles] = useState(false)

  const load = useCallback(() => api.inventoryFolder(dirId)
    .then((d) => { setF(d); setError(null) })
    .catch((e) => setError(e.message || String(e))), [dirId])

  // Keyed by dirId in the parent, so each folder mounts fresh: nothing to reset here.
  useEffect(() => { load() }, [load])

  const tag = async (id, category) => {
    setBusy(true)
    try {
      await api.inventoryTag(id, category)
      await load()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  if (error && !f) return <p className="inv-muted">Couldn't open that folder: {error}</p>
  if (!f) return <p className="inv-muted">Opening…</p>

  const typeOrder = Object.keys(TYPE_COLOR)
  const biggest = f.children[0]?.total_bytes || 1

  return (
    <div className="inv-folder">
      <div className="inv-crumbs">
        <button type="button" className="inv-link" onClick={onBack}>All drives</button>
        <span className="inv-sep">/</span>
        <span className="inv-faint">{f.volume.host} {f.volume.mount}</span>
        {f.breadcrumb.map((c, i) => (
          <span key={c.id}>
            <span className="inv-sep">/</span>
            {i === f.breadcrumb.length - 1
              ? <b>{c.name}</b>
              : <button type="button" className="inv-link" onClick={() => onOpen(c.id)}>{c.name}</button>}
          </span>
        ))}
      </div>

      <div className="inv-folder-head">
        <div className="inv-folder-title">
          <h3 className="inv-plain">{f.name}</h3>
          <CategoryChip category={f.category} source={f.category_source} labels={labels} />
          <TagSelect id={f.id} ownTag={f.own_tag} categories={categories} onTag={tag} busy={busy} />
        </div>
        <p className="inv-reason">
          {f.category_source === 'tag' && 'You tagged this folder. Everything inside inherits it unless tagged otherwise.'}
          {f.category_source === 'inherited_tag' && 'Inherited from a folder you tagged higher up.'}
          {f.category_source === 'suggested' && <>Suggested: {f.suggested_reason}.</>}
          {f.collapsed && ' Counted but not itemised: it is tool or operating-system data.'}
        </p>
        <div className="inv-stats">
          <span><b>{bytes(f.total_bytes)}</b> total</span>
          <span><b>{count(f.total_files)}</b> files</span>
          <span><b>{count(f.total_dirs)}</b> folders</span>
          <span><b>{years(f.oldest, f.newest)}</b> modified</span>
        </div>
        <StackBar parts={f.type_bytes} colors={TYPE_COLOR} order={typeOrder} labels={TYPE_LABEL} height={12} />
        <div className="inv-legend">
          {typeOrder.filter((t) => f.type_bytes[t]).map((t) => (
            <span key={t} className="inv-legend-item">
              <i style={{ background: TYPE_COLOR[t] }} />{TYPE_LABEL[t]} <b>{bytes(f.type_bytes[t])}</b>
            </span>
          ))}
        </div>
        {f.top_exts.length > 0 && (
          <p className="inv-muted inv-mono">
            {f.top_exts.map(([ext, n, b]) => `.${ext} ${count(n)} (${bytes(b)})`).join(' · ')}
          </p>
        )}
      </div>

      {f.children.length > 0 && (
        <div className="inv-table-wrap">
          <table className="inv-table">
            <thead>
              <tr>
                <th>Folder</th><th className="inv-size-col">Size</th><th className="inv-num">Files</th>
                <th className="inv-types-col">What's in it</th><th>Years</th><th>Category</th><th />
              </tr>
            </thead>
            <tbody>
              {f.children.map((c) => (
                <tr key={c.id}>
                  <td>
                    {c.collapsed || (c.total_dirs === 0 && c.total_files === 0)
                      ? <span className="inv-leaf">{c.name}</span>
                      : <button type="button" className="inv-link inv-name" onClick={() => onOpen(c.id)}>{c.name}</button>}
                    {c.collapsed && <span className="inv-tag-mini">not itemised</span>}
                  </td>
                  <td className="inv-size-col">
                    <div className="inv-size">
                      <span className="inv-mono">{bytes(c.total_bytes)}</span>
                      <span className="inv-size-bar"><span style={{ width: `${Math.max(1, (c.total_bytes / biggest) * 100)}%` }} /></span>
                    </div>
                  </td>
                  <td className="inv-num inv-mono">{count(c.total_files)}</td>
                  <td className="inv-types-col">
                    <StackBar parts={c.type_bytes} colors={TYPE_COLOR} order={typeOrder} labels={TYPE_LABEL} />
                  </td>
                  <td className="inv-mono inv-faint">{years(c.oldest, c.newest)}</td>
                  <td title={c.category_source === 'suggested' ? c.suggested_reason : undefined}>
                    <CategoryChip category={c.category} source={c.category_source} labels={labels} />
                  </td>
                  <td>
                    <TagSelect id={c.id} ownTag={c.category_source === 'tag'}
                               categories={categories} onTag={tag} busy={busy} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {f.direct_files > 0 && (
        <div className="inv-files">
          <button type="button" className="inv-link" onClick={() => setShowFiles((s) => !s)}>
            {showFiles ? 'Hide' : 'Show'} the {count(f.direct_files)} file{f.direct_files === 1 ? '' : 's'} directly in this folder
            {f.direct_files > f.files_shown ? ` (largest ${f.files_shown})` : ''} · {bytes(f.direct_bytes)}
          </button>
          {showFiles && (
            <div className="inv-table-wrap inv-files-list">
              <table className="inv-table">
                <thead><tr><th>File</th><th className="inv-num">Size</th><th>Type</th><th>Modified</th></tr></thead>
                <tbody>
                  {f.files.map((x) => (
                    <tr key={x.name}>
                      <td>{x.name}</td>
                      <td className="inv-num inv-mono">{bytes(x.size_bytes)}</td>
                      <td><i className="inv-dot" style={{ background: TYPE_COLOR[x.ftype] }} />{TYPE_LABEL[x.ftype]}</td>
                      <td className="inv-mono inv-faint">{x.mtime ? new Date(x.mtime * 1000).toLocaleDateString() : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Search({ labels, onOpen }) {
  const [q, setQ] = useState('')
  const [found, setFound] = useState({ q: '', results: [] })
  const active = q.trim().length >= 2
  useEffect(() => {
    if (!active) return undefined
    const t = setTimeout(() => api.inventorySearch(q)
      .then((d) => setFound({ q, results: d.results }))
      .catch(() => setFound({ q, results: [] })), 250)
    return () => clearTimeout(t)
  }, [q, active])
  // Only show results that belong to what's typed now, never a stale earlier query's.
  const results = active && found.q === q ? found.results : null
  return (
    <div className="inv-search-wrap">
      <input
        id="inv-search" type="search" className="inv-search" value={q}
        placeholder="Find a folder on any drive…" onChange={(e) => setQ(e.target.value)}
        aria-label="Find a folder by name"
      />
      {results && (
        <div className="inv-results">
          {results.length === 0 && <div className="inv-muted">No folder by that name.</div>}
          {results.map((r) => (
            <button key={r.id} type="button" className="inv-result" onClick={() => { setQ(''); onOpen(r.id) }}>
              <span className="inv-result-name">{r.name}</span>
              <span className="inv-faint inv-mono">{r.host} {r.volume_mount}{r.path ? `/${r.path}` : ''}</span>
              <span className="inv-mono">{bytes(r.total_bytes)}</span>
              <CategoryChip category={r.category} source={r.category_source} labels={labels} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * Media & drives — every folder on every drive in the house, sized, broken down by what
 * it holds, and sorted into where it belongs. The old media import queue is the second tab.
 */
export default function Inventory() {
  const [tab, setTab] = useState('inventory')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [dirId, setDirId] = useState(null)

  const load = useCallback(() => api.inventoryOverview()
    .then((d) => { setData(d); setError(null) })
    .catch((e) => setError(e.message || String(e))), [])

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  const labels = useMemo(() => Object.fromEntries((data?.categories || []).map((c) => [c.key, c.label])), [data])
  const scanning = data?.volumes.some((v) => v.scan_status === 'running')

  return (
    <div className="inv-page">
      <div className="inv-header">
        <h2>Media &amp; drives</h2>
        <div className="inv-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={tab === 'inventory'} onClick={() => setTab('inventory')}>Drive inventory</button>
          <button type="button" role="tab" aria-selected={tab === 'import'} onClick={() => setTab('import')}>Old import queue</button>
        </div>
      </div>

      {tab === 'import' && <Media />}

      {tab === 'inventory' && (
        <>
          {!data && <p className="inv-muted">{error ? `Couldn't load the inventory: ${error}` : 'Loading…'}</p>}
          {data && (
            <>
              <div className="inv-toolbar">
                <Search labels={labels} onOpen={setDirId} />
                <span className={`inv-state ${scanning ? 'inv-state-live' : 'inv-state-ok'}`}>
                  {scanning ? 'Scan in progress — totals fill in as each drive finishes' : 'Rescan: python scripts/inventory_drives.py'}
                </span>
              </div>
              {data.volumes.length === 0 && (
                <p className="inv-muted">No drives inventoried yet. Run <span className="inv-mono">python scripts/inventory_drives.py</span> on jarvisbox.</p>
              )}
              {dirId
                ? <FolderView key={dirId} dirId={dirId} categories={data.categories} labels={labels}
                              onOpen={setDirId} onBack={() => { setDirId(null); load() }} />
                : (
                  <>
                    <Totals data={data} labels={labels} />
                    <Drives data={data} labels={labels} onOpen={setDirId} />
                    <WhereThingsLive data={data} labels={labels} onOpen={setDirId} />
                  </>
                )}
            </>
          )}
        </>
      )}
    </div>
  )
}
