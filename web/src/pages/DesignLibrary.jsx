import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './design-library.css'

/**
 * The Design Library: every asset the print business makes and sells things from, in one
 * browser -- Designs and Local imports are real data today; the other seven categories are
 * a real, empty shelf waiting for a scanner, an upload, or a generation pass to fill them.
 *
 * Two backends behind one UI. 'design' and every category below are library_assets.py's
 * shared table, reached through /api/library/*. 'local_import' is untouched media_scan.py
 * data reached through the existing /api/media/* routes -- the drive-scan triage tool
 * this page does NOT replace, just gives a seat at the same table.
 */

const CATEGORY_META = {
  design: { label: 'Designs', tag: 'PNG' },
  decal_icon: { label: 'Decal icons', tag: 'SVG' },
  cut_file: { label: 'Cut files', tag: 'S3' },
  local_import: { label: 'Local imports', tag: 'IMG' },
  footage: { label: 'Footage', tag: 'MP4' },
  mockup: { label: 'Mockups', tag: 'JPG' },
  background: { label: 'Backgrounds', tag: 'BG' },
  human_model: { label: 'Human models', tag: 'MDL' },
  room: { label: 'Rooms', tag: 'ROOM' },
  stl_model: { label: 'STL models', tag: 'STL' },
}

const CATEGORY_ORDER = Object.keys(CATEGORY_META)

const LIBRARY_STATUSES = ['available', 'used', 'retired']
const IMPORT_DECISIONS = ['undecided', 'import', 'skip', 'imported']

function fmtBytes(n) {
  if (!n) return null
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`
}

/** Whether a browser can plausibly render this as an <img> -- everything else (STL, audio,
 * cut-file binaries) still gets a real file at its media URL, it just isn't a preview. */
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'])
function looksLikeImage(path) {
  const ext = (path || '').split('.').pop()?.toLowerCase()
  return IMAGE_EXTENSIONS.has(ext || '')
}

/** Normalizes a raw row (library_assets row, design_assets row, or a media_scan folder)
 * into the one shape the grid/list/inspector all read from. Nothing else in this file
 * branches on category shape again after this. */
function normalize(category, row) {
  if (category === 'local_import') {
    return {
      id: row.id,
      title: row.dir_path === '.' ? '(root of drive)' : row.dir_path,
      meta: `${row.file_count} files · ${fmtBytes(row.total_bytes) || '0 B'}`,
      status: row.decision,
      sub: `${row.hostname || row.address || ''} · ${row.label || row.mountpoint || ''}`,
      previewUrl: null,
      properties: [],
      raw: row,
    }
  }
  const metaBits = []
  if (row.width && row.height) metaBits.push(`${row.width}×${row.height}`)
  if (row.bytes) metaBits.push(fmtBytes(row.bytes))

  // Properties shown one-per-line in the inspector -- every real column this row
  // actually carries, not just the ones that happened to exist when this was first
  // written. design_assets and library_assets each have their own extra fields; only
  // show a property when the row actually has a value for it.
  const properties = []
  const add = (label, value) => { if (value != null && value !== '') properties.push({ label, value }) }
  add('Source', row.source)
  add('Format', row.file_format)
  if (row.is_vector != null) add('Vector', row.is_vector ? 'yes' : 'no')
  if (row.print_ready != null) add('Print-ready', row.print_ready ? 'yes' : (row.print_ready_note || 'no'))
  add('Tag status', row.tag_status)
  add('External ID', row.external_id)
  add('Times used', row.times_used)
  add('Added', (row.added_at || '').slice(0, 10))
  if (row.metadata) {
    for (const [k, v] of Object.entries(row.metadata)) add(k, v)
  }

  return {
    id: row.id,
    title: row.title || '(untitled)',
    meta: metaBits.join(' · ') || '—',
    status: row.status,
    sub: row.note || '',
    previewUrl: looksLikeImage(row.path)
      ? `/api/library/assets/${category}/${row.id}/media`
      : null,
    properties,
    raw: row,
  }
}

function CategoryRail({ counts, category, onPick }) {
  return (
    <nav className="dl-rail hud-panel">
      {CATEGORY_ORDER.map((key) => (
        <button
          type="button" key={key}
          className={`dl-rail-item ${category === key ? 'is-active' : ''}`}
          onClick={() => onPick(key)}
        >
          <span className="dl-rail-name">{CATEGORY_META[key].label}</span>
          <span className="dl-rail-count">{counts[key] ?? '—'}</span>
        </button>
      ))}
    </nav>
  )
}

function Inspector({ item, category, busy, onDecide, onGenerateMockup }) {
  if (!item) {
    return (
      <div className="dl-inspector hud-panel">
        <div className="dl-inspector-empty">Select an asset to see its details here.</div>
      </div>
    )
  }
  const statuses = category === 'local_import' ? IMPORT_DECISIONS : LIBRARY_STATUSES
  return (
    <div className="dl-inspector hud-panel">
      <div className="dl-inspector-top">
        <span className="dl-inspector-kind">{CATEGORY_META[category].label}</span>
      </div>
      {item.previewUrl && (
        <img className="dl-inspector-img" src={item.previewUrl} alt={item.title}
             onError={(e) => { e.target.style.display = 'none' }} />
      )}
      <h2 className="dl-inspector-title">{item.title}</h2>
      <div className="dl-inspector-meta">{item.meta}</div>
      {item.sub && <div className="dl-inspector-sub">{item.sub}</div>}

      {item.properties.length > 0 && (
        <div className="dl-inspector-properties">
          <span className="dl-inspector-label">Properties</span>
          {item.properties.map((p) => (
            <div key={p.label} className="dl-inspector-property">
              <span>{p.label}</span><span>{String(p.value)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="dl-inspector-status">
        <span className="dl-inspector-label">Status</span>
        <div className="dl-inspector-status-btns">
          {statuses.map((s) => (
            <button
              key={s} type="button" disabled={busy}
              className={`chip ${item.status === s ? 'is-on' : ''}`}
              onClick={() => onDecide([item.id], s)}
            >{s}</button>
          ))}
        </div>
      </div>

      {category === 'design' && (
        <div className="dl-inspector-actions">
          <button type="button" disabled={busy} onClick={() => onGenerateMockup(item.id)}>
            Generate mockup
          </button>
          <div className="dl-inspector-hint">
            Runs the existing GPU render path with a plain prompt -- not the Gemini
            apparel-compositing pipeline (that needs a real human-model photo library
            that doesn't exist yet; see docs/mockup-system.md).
          </div>
        </div>
      )}
    </div>
  )
}

export default function DesignLibrary() {
  const [category, setCategory] = useState('design')
  const [view, setView] = useState('grid')
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [counts, setCounts] = useState({})
  const [items, setItems] = useState([])
  const [selected, setSelected] = useState(() => new Set())
  const [selectedId, setSelectedId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const isImport = category === 'local_import'

  const loadCounts = useCallback(() => {
    // Two independent requests resolving in either order -- both merge functionally so
    // neither can clobber the other's key.
    api.libraryCategories().then((r) => setCounts((c) => ({ ...c, ...r.counts }))).catch(() => {})
    api.mediaSummary().then((s) => setCounts((c) => ({ ...c, local_import: s.total_files || 0 })))
      .catch(() => {})
  }, [])

  const loadItems = useCallback(() => {
    setLoading(true)
    setError(null)
    const request = isImport
      ? api.mediaFolders({ decision: statusFilter, limit: 500 }).then((r) => r.folders)
      : api.libraryAssets({ category, status: statusFilter, search, limit: 300 }).then((r) => r.assets)
    request
      .then((rows) => setItems(rows.map((r) => normalize(category, r))))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [category, statusFilter, search, isImport])

  useEffect(() => { loadCounts() }, [loadCounts])
  useEffect(() => { loadItems() }, [loadItems])
  useEffect(() => { setSelected(new Set()); setSelectedId(null); setStatusFilter('') }, [category])

  // library/design filter by search server-side already; local_import doesn't take one.
  const filtered = items

  const toggle = (id) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    setSelected((prev) => (prev.size === filtered.length ? new Set() : new Set(filtered.map((f) => f.id))))
  }

  const decide = async (ids, status) => {
    setBusy(true)
    try {
      if (isImport) {
        if (ids.length === 1) await api.decideMediaFolder(ids[0], status)
        else await api.decideMediaFoldersBulk(ids, status)
      } else {
        if (ids.length === 1) await api.decideLibraryAsset(ids[0], category, status)
        else await api.decideLibraryAssetsBulk(ids, category, status)
      }
      setItems((prev) => prev.map((it) => (ids.includes(it.id) ? { ...it, status } : it)))
      setSelected(new Set())
      loadCounts()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const generateMockup = async (designAssetId) => {
    setBusy(true)
    setError(null)
    try {
      await api.generateMockup(designAssetId)
      loadCounts()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const selectedItem = filtered.find((it) => it.id === selectedId) || null
  const statuses = isImport ? IMPORT_DECISIONS : LIBRARY_STATUSES

  return (
    <div className="dl-page">
      <header className="dl-header">
        <div>
          <h1>Design Library</h1>
          <p className="dl-sub">{CATEGORY_META[category].label} · {filtered.length} shown</p>
        </div>
        <div className="dl-view-toggle">
          <button type="button" className={view === 'grid' ? 'is-on' : ''} onClick={() => setView('grid')}>Grid</button>
          <button type="button" className={view === 'list' ? 'is-on' : ''} onClick={() => setView('list')}>List</button>
        </div>
      </header>

      <div className="dl-toolbar hud-panel">
        <input
          type="search" className="dl-search" placeholder={`Search ${CATEGORY_META[category].label.toLowerCase()}…`}
          value={search} onChange={(e) => setSearch(e.target.value)} disabled={isImport}
        />
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">all statuses</option>
          {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      {error && <div className="media-error">{error}</div>}

      <div className="dl-body">
        <CategoryRail counts={counts} category={category} onPick={setCategory} />

        <div className="dl-main">
          {selected.size > 0 && (
            <div className="media-bulk">
              <span>{selected.size} selected</span>
              {statuses.map((s) => (
                <button key={s} type="button" disabled={busy} onClick={() => decide([...selected], s)}>
                  mark {s}
                </button>
              ))}
              <button type="button" className="btn-ghost" onClick={() => setSelected(new Set())}>Clear</button>
            </div>
          )}

          {loading && <div className="cc-empty">Loading…</div>}
          {!loading && filtered.length === 0 && (
            <div className="media-empty">
              Nothing here yet.
              {category !== 'local_import' && category !== 'design' && (
                <span> This category fills in as designs are cut, footage is shot, or a
                  mockup is generated — nothing is invented to fill the shelf.</span>
              )}
            </div>
          )}

          {!loading && filtered.length > 0 && view === 'grid' && (
            <div className="dl-grid">
              {filtered.map((it) => (
                <div key={it.id} className={`dl-card ${selected.has(it.id) ? 'is-selected' : ''}`}>
                  <div className="dl-card-thumb">
                    {it.previewUrl
                      ? <img className="dl-card-img" src={it.previewUrl} alt={it.title}
                             loading="lazy"
                             onError={(e) => { e.target.style.display = 'none' }} />
                      : <span className="dl-card-tag">{CATEGORY_META[category].tag}</span>}
                    <input type="checkbox" checked={selected.has(it.id)} onChange={() => toggle(it.id)} />
                    <span className={`dl-card-badge is-${it.status}`}>{it.status}</span>
                  </div>
                  <button type="button" className="dl-card-body" onClick={() => setSelectedId(it.id)}>
                    <span className="dl-card-title">{it.title}</span>
                    <span className="dl-card-meta">{it.meta}</span>
                  </button>
                </div>
              ))}
            </div>
          )}

          {!loading && filtered.length > 0 && view === 'list' && (
            <div className="media-table-wrap">
              <table className="media-table">
                <thead>
                  <tr>
                    <th className="col-check">
                      <input type="checkbox" checked={filtered.length > 0 && selected.size === filtered.length} onChange={toggleAll} />
                    </th>
                    <th>Title</th>
                    <th>Meta</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((it) => (
                    <tr key={it.id} className={selected.has(it.id) ? 'is-selected' : ''}>
                      <td className="col-check">
                        <input type="checkbox" checked={selected.has(it.id)} onChange={() => toggle(it.id)} />
                      </td>
                      <td>
                        <button type="button" className="dl-list-name" onClick={() => setSelectedId(it.id)}>{it.title}</button>
                      </td>
                      <td className="dl-list-meta">{it.meta}</td>
                      <td><span className={`dl-card-badge is-${it.status}`}>{it.status}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <Inspector item={selectedItem} category={category} busy={busy}
                   onDecide={decide} onGenerateMockup={generateMockup} />
      </div>
    </div>
  )
}
