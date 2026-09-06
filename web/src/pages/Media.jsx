import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'

/**
 * The media catalogue review surface.
 *
 * The unit of decision here is the folder, not the file. There are hundreds of
 * thousands of files across these drives and no one is going to triage them
 * individually — but "bring /Designs/Vinyl Cuts over, skip /Downloads" is a decision a
 * person can actually make, and it's the one that determines what gets imported.
 *
 * So the table is sorted by size descending and supports bulk marking: the biggest
 * folders are the ones worth thinking about, and everything below the fold can be
 * swept in one action.
 */

const KIND_COLORS = {
  image: 'var(--cyan)',
  design: '#c88bff',
  vector: '#7dffb0',
  cut: 'var(--amber)',
  model3d: '#ff9166',
  video: '#5aa9ff',
  audio: '#8f9bb3',
  font: '#6fd7c8',
  doc: '#b0b8c4',
}

const KIND_ORDER = ['cut', 'design', 'vector', 'image', 'model3d', 'video', 'audio', 'font', 'doc']

function fmtBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`
}

function fmtNum(n) {
  return (n || 0).toLocaleString()
}

function fmtDate(iso) {
  if (!iso) return '—'
  return iso.slice(0, 7)
}

/** Proportional bar of what kinds of file live in a folder. */
function KindBar({ counts }) {
  const parsed = useMemo(() => {
    try { return JSON.parse(counts || '{}') } catch { return {} }
  }, [counts])
  const total = Object.values(parsed).reduce((a, b) => a + b, 0)
  if (!total) return null
  const ordered = KIND_ORDER.filter((k) => parsed[k])
  return (
    <div className="kindbar" title={ordered.map((k) => `${k}: ${parsed[k]}`).join('\n')}>
      {ordered.map((k) => (
        <span
          key={k}
          className="kindbar-seg"
          style={{ width: `${(parsed[k] / total) * 100}%`, background: KIND_COLORS[k] || 'var(--text-faint)' }}
        />
      ))}
    </div>
  )
}

/** How much of each source drive has made it into D:\Collected Art. */
function CollectionPanel() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)

  const load = useCallback(async () => {
    try { setData(await api.mediaCollection()); setErr(null) } catch (e) { setErr(e.message) }
  }, [])

  useEffect(() => { load() }, [load])
  // A copy of this size runs for a long while; the page should show it moving.
  useEffect(() => {
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [load])

  if (err) return <div className="media-error">{err}</div>
  if (!data) return <div className="media-empty">Loading…</div>

  const copied = data.by_status.find((s) => s.status === 'copied')
  const dupes = data.by_status.find((s) => s.status === 'duplicate')
  const failed = data.by_status.find((s) => s.status === 'failed')

  return (
    <div>
      <section className="media-kinds">
        <div className="media-kind" style={{ '--kind-color': 'var(--cyan)' }}>
          <span className="media-kind-name">collected</span>
          <span className="media-kind-count">{fmtNum(copied?.files || 0)}</span>
          <span className="media-kind-size">{fmtBytes(copied?.bytes || 0)}</span>
        </div>
        <div className="media-kind" style={{ '--kind-color': 'var(--text-dim)' }}>
          <span className="media-kind-name">duplicates</span>
          <span className="media-kind-count">{fmtNum(dupes?.files || 0)}</span>
          <span className="media-kind-size">skipped</span>
        </div>
        <div className="media-kind" style={{ '--kind-color': failed?.files ? 'var(--red)' : 'var(--text-faint)' }}>
          <span className="media-kind-name">failed</span>
          <span className="media-kind-count">{fmtNum(failed?.files || 0)}</span>
          <span className="media-kind-size">{failed?.files ? 'see log' : 'none'}</span>
        </div>
      </section>

      <div className="media-table-wrap">
        <table className="media-table">
          <thead>
            <tr>
              <th>Source drive</th>
              <th className="col-num">Copied</th>
              <th className="col-num">Size</th>
              <th className="col-num">Duplicates</th>
              <th className="col-num">Failed</th>
            </tr>
          </thead>
          <tbody>
            {data.by_volume.length === 0 && (
              <tr><td colSpan="5" className="media-empty">Nothing collected yet.</td></tr>
            )}
            {data.by_volume.map((v, i) => (
              <tr key={i}>
                <td className="col-path">
                  <span className="media-path">{v.label || v.mountpoint}</span>
                  <span className="media-origin">{v.hostname || v.address} · {v.mountpoint}</span>
                </td>
                <td className="col-num">{fmtNum(v.copied)}</td>
                <td className="col-num">{fmtBytes(v.bytes)}</td>
                <td className="col-num">{fmtNum(v.duplicate)}</td>
                <td className={`col-num ${v.failed ? 'media-warn' : ''}`}>{fmtNum(v.failed)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Unopened bundles, ranked by what's inside them. */
function ArchivePanel() {
  const [rows, setRows] = useState([])
  const [unreadable, setUnreadable] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    let live = true
    api.mediaArchives(unreadable)
      .then((r) => { if (live) { setRows(r.archives); setErr(null) } })
      .catch((e) => live && setErr(e.message))
    return () => { live = false }
  }, [unreadable])

  const totalMedia = rows.reduce((a, r) => a + (r.inner_media || 0), 0)

  return (
    <div className="media-archives">
      <div className="media-controls">
        <label className="media-toggle">
          <input type="checkbox" checked={unreadable} onChange={(e) => setUnreadable(e.target.checked)} />
          show only unreadable (rar / 7z)
        </label>
        <span className="media-count">
          {rows.length} archives · {fmtNum(totalMedia)} media files inside
        </span>
      </div>
      {err && <div className="media-error">{err}</div>}
      <div className="media-table-wrap">
        <table className="media-table">
          <thead>
            <tr>
              <th>Archive</th>
              <th className="col-num">On disk</th>
              <th className="col-num">Unpacked</th>
              <th className="col-num">Media inside</th>
              <th>Contents</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan="5" className="media-empty">
                {unreadable ? 'Nothing unreadable — every archive was indexed.' : 'No archives indexed yet.'}
              </td></tr>
            )}
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="col-path">
                  <span className="media-path">{r.filename}</span>
                  <span className="media-origin">
                    {r.hostname || r.address} · {r.label || r.mountpoint} · {r.dir_path}
                  </span>
                </td>
                <td className="col-num">{fmtBytes(r.size_bytes)}</td>
                <td className="col-num">{r.error ? '—' : fmtBytes(r.inner_bytes)}</td>
                <td className="col-num">{r.error ? '—' : fmtNum(r.inner_media)}</td>
                <td className="col-kinds">
                  {r.error
                    ? <span className="media-exts media-warn">{r.error.replace(/^\w+Error: /, '')}</span>
                    : <>
                        <KindBar counts={r.kind_counts} />
                        <span className="media-exts">{r.top_exts}</span>
                      </>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function Media() {
  const [tab, setTab] = useState('folders')
  const [summary, setSummary] = useState(null)
  const [folders, setFolders] = useState([])
  const [selected, setSelected] = useState(() => new Set())
  const [kind, setKind] = useState('')
  const [decision, setDecision] = useState('')
  const [minFiles, setMinFiles] = useState(5)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const loadSummary = useCallback(async () => {
    try {
      setSummary(await api.mediaSummary())
    } catch (e) {
      setError(e.message)
    }
  }, [])

  const loadFolders = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.mediaFolders({ kind, decision, minFiles, limit: 800 })
      setFolders(res.folders)
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [kind, decision, minFiles])

  useEffect(() => { loadSummary() }, [loadSummary])
  useEffect(() => { loadFolders() }, [loadFolders])

  // Polling exists because the scan runs in a separate process — the page has no other
  // way to notice that another 90,000 files just landed.
  useEffect(() => {
    const t = setInterval(loadSummary, 15000)
    return () => clearInterval(t)
  }, [loadSummary])

  const toggle = (id) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    setSelected((prev) => (prev.size === folders.length ? new Set() : new Set(folders.map((f) => f.id))))
  }

  const decide = async (ids, value) => {
    try {
      if (ids.length === 1) await api.decideMediaFolder(ids[0], value)
      else await api.decideMediaFoldersBulk(ids, value)
      setFolders((prev) => prev.map((f) => (ids.includes(f.id) ? { ...f, decision: value } : f)))
      setSelected(new Set())
      loadSummary()
    } catch (e) {
      setError(e.message)
    }
  }

  const selectedBytes = folders
    .filter((f) => selected.has(f.id))
    .reduce((a, f) => a + (f.total_bytes || 0), 0)

  const importTotal = summary?.decisions?.find((d) => d.decision === 'import')

  return (
    <div className="media-page">
      <header className="media-header">
        <div>
          <h1>Media Catalogue</h1>
          <p className="media-sub">
            {summary
              ? `${fmtNum(summary.total_files)} files · ${fmtBytes(summary.total_bytes)} across ${summary.by_volume?.filter((v) => v.files).length || 0} volumes`
              : 'Loading…'}
          </p>
        </div>
        {importTotal && (
          <div className="media-marked">
            <span className="media-marked-num">{fmtBytes(importTotal.bytes)}</span>
            <span className="media-marked-label">marked to import</span>
          </div>
        )}
      </header>

      {error && <div className="media-error">{error}</div>}

      {summary && (
        <section className="media-kinds">
          {summary.by_kind.map((k) => (
            <button
              key={k.kind}
              className={`media-kind ${kind === k.kind ? 'is-active' : ''}`}
              onClick={() => setKind(kind === k.kind ? '' : k.kind)}
              style={{ '--kind-color': KIND_COLORS[k.kind] || 'var(--text-dim)' }}
            >
              <span className="media-kind-name">{k.kind}</span>
              <span className="media-kind-count">{fmtNum(k.files)}</span>
              <span className="media-kind-size">{fmtBytes(k.bytes)}</span>
            </button>
          ))}
        </section>
      )}

      {summary && (
        <section className="media-volumes">
          {summary.by_volume.filter((v) => v.files > 0).map((v) => (
            <div key={v.id} className="media-volume">
              <span className="media-volume-host">{v.hostname || v.address}</span>
              <span className="media-volume-mount">{v.label || v.mountpoint}</span>
              <span className="media-volume-stat">{fmtNum(v.files)} files · {fmtBytes(v.bytes)}</span>
            </div>
          ))}
        </section>
      )}

      <nav className="media-tabs">
        <button className={tab === 'folders' ? 'is-on' : ''} onClick={() => setTab('folders')}>
          Folders
        </button>
        <button className={tab === 'archives' ? 'is-on' : ''} onClick={() => setTab('archives')}>
          Archives
        </button>
        <button className={tab === 'collection' ? 'is-on' : ''} onClick={() => setTab('collection')}>
          Collection
        </button>
      </nav>

      {tab === 'archives' && <ArchivePanel />}
      {tab === 'collection' && <CollectionPanel />}

      {tab === 'folders' && (
      <>
      <div className="media-controls">
        <label>
          Decision
          <select value={decision} onChange={(e) => setDecision(e.target.value)}>
            <option value="">all</option>
            <option value="undecided">undecided</option>
            <option value="import">import</option>
            <option value="skip">skip</option>
            <option value="imported">imported</option>
          </select>
        </label>
        <label>
          Min files
          <input
            type="number"
            min="1"
            value={minFiles}
            onChange={(e) => setMinFiles(Math.max(1, Number(e.target.value) || 1))}
          />
        </label>
        {kind && (
          <button className="media-clear" onClick={() => setKind('')}>clear “{kind}” filter</button>
        )}
        <span className="media-count">{folders.length} folders</span>
      </div>

      {selected.size > 0 && (
        <div className="media-bulk">
          <span>{selected.size} selected · {fmtBytes(selectedBytes)}</span>
          <button className="btn-import" onClick={() => decide([...selected], 'import')}>Import these</button>
          <button className="btn-skip" onClick={() => decide([...selected], 'skip')}>Skip these</button>
          <button className="btn-ghost" onClick={() => setSelected(new Set())}>Clear</button>
        </div>
      )}

      <div className="media-table-wrap">
        <table className="media-table">
          <thead>
            <tr>
              <th className="col-check">
                <input
                  type="checkbox"
                  checked={folders.length > 0 && selected.size === folders.length}
                  onChange={toggleAll}
                />
              </th>
              <th>Folder</th>
              <th className="col-num">Files</th>
              <th className="col-num">Size</th>
              <th className="col-kinds">Contents</th>
              <th className="col-dates">Modified</th>
              <th className="col-decision">Decision</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan="7" className="media-empty">Scanning…</td></tr>
            )}
            {!loading && folders.length === 0 && (
              <tr><td colSpan="7" className="media-empty">No folders match those filters.</td></tr>
            )}
            {folders.map((f) => (
              <tr key={f.id} className={`decision-${f.decision} ${selected.has(f.id) ? 'is-selected' : ''}`}>
                <td className="col-check">
                  <input type="checkbox" checked={selected.has(f.id)} onChange={() => toggle(f.id)} />
                </td>
                <td className="col-path">
                  <span className="media-path">{f.dir_path === '.' ? '(root of drive)' : f.dir_path}</span>
                  <span className="media-origin">
                    {f.hostname || f.address} · {f.label || f.mountpoint}
                  </span>
                </td>
                <td className="col-num">{fmtNum(f.file_count)}</td>
                <td className="col-num">{fmtBytes(f.total_bytes)}</td>
                <td className="col-kinds">
                  <KindBar counts={f.kind_counts} />
                  <span className="media-exts">{f.top_exts}</span>
                </td>
                <td className="col-dates">
                  {fmtDate(f.oldest_mtime)} → {fmtDate(f.newest_mtime)}
                </td>
                <td className="col-decision">
                  <div className="media-decide">
                    <button
                      className={`chip chip-import ${f.decision === 'import' ? 'is-on' : ''}`}
                      onClick={() => decide([f.id], f.decision === 'import' ? 'undecided' : 'import')}
                    >import</button>
                    <button
                      className={`chip chip-skip ${f.decision === 'skip' ? 'is-on' : ''}`}
                      onClick={() => decide([f.id], f.decision === 'skip' ? 'undecided' : 'skip')}
                    >skip</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      </>
      )}
    </div>
  )
}
