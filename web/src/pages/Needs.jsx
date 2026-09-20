import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './needs.css'

/**
 * "Needs you" — the things the team is stopped on, grouped by pipeline.
 *
 * The Review queue answers "which of these do you prefer"; this answers "we cannot go
 * any further without something only you can get". They are kept apart deliberately:
 * a handful of items that halt a whole pipeline must not be buried under a stream of
 * mockups to approve.
 *
 * Everything is answerable in place. The point of the board is that supplying the thing
 * is the same gesture as reading about it — paste the token into the row that asked for
 * it and the agent that was blocked picks it up on its next run. Sending him elsewhere
 * to go paste a key into a config file is how the last store ended up with two empty
 * strings and five weeks of zero sales.
 */

const KIND_LABEL = {
  secret: 'API key / token',
  url: 'Link',
  text: 'Value',
  file: 'File to import',
  account: 'Account to create',
  purchase: 'Purchase',
}

// What the input should look like for each kind. A secret gets a password field so it
// isn't left legible on screen; a file/account ask is answered with a path or a note.
const PLACEHOLDER = {
  secret: 'Paste the key — stored, never shown back',
  url: 'https://…',
  text: 'The value they asked for',
  file: 'Full path to the file or folder',
  account: 'Account name / id once created',
  purchase: 'What you bought, or the account it is on',
}

function Prompts({ prompts }) {
  const [copied, setCopied] = useState(-1)
  if (!prompts || !prompts.length) return null

  async function copy(text, i) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(i)
      setTimeout(() => setCopied(-1), 1400)
    } catch {
      setCopied(-1)                       // clipboard blocked; the text is still selectable
    }
  }

  return (
    <div className="needs-prompts">
      <div className="needs-prompts-head">
        Paste into Leonardo — {prompts.length} prompt{prompts.length === 1 ? '' : 's'}
      </div>
      {prompts.map((p, i) => (
        <div className="needs-prompt" key={i}>
          <code>{p}</code>
          <button type="button" className="needs-copy" onClick={() => copy(p, i)}>
            {copied === i ? 'COPIED' : 'COPY'}
          </button>
        </div>
      ))}
    </div>
  )
}

function Row({ item, onAnswered }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const answered = item.status === 'provided' || item.status === 'resolved'

  async function submit(e) {
    e.preventDefault()
    if (!value.trim() || busy) return
    setBusy(true); setError(null)
    try {
      await api.provideNeed(item.id, value.trim())
      setValue('')
      onAnswered()
    } catch (err) {
      setError(err?.message || 'could not save that')
    } finally {
      setBusy(false)
    }
  }

  async function decline() {
    setBusy(true); setError(null)
    try {
      await api.setNeedStatus(item.id, 'rejected', 'declined from the board')
      onAnswered()
    } catch (err) {
      setError(err?.message || 'could not update that')
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className={`needs-row is-${item.status} p${item.priority}`}>
      <div className="needs-row-head">
        <span className="needs-kind">{KIND_LABEL[item.kind] || item.kind}</span>
        <span className="needs-title">{item.title}</span>
        {item.agent_key && <span className="needs-who">{item.agent_key.replace(/_/g, ' ')}</span>}
        <span className={`needs-status s-${item.status}`}>{item.status}</span>
      </div>

      {item.why && <p className="needs-why">{item.why}</p>}
      {item.blocks && (
        <p className="needs-blocks"><span>BLOCKS</span> {item.blocks}</p>
      )}
      {item.instructions && (
        <details className="needs-how">
          <summary>How to get it</summary>
          <p>{item.instructions}</p>
        </details>
      )}

      <Prompts prompts={item.prompts} />

      {answered ? (
        <div className="needs-answered">
          {item.is_secret ? 'Stored' : 'Provided'}
          {item.value ? <code>{item.value}</code> : null}
          {item.status === 'provided' && <em>waiting for the team to confirm it works</em>}
        </div>
      ) : item.status === 'rejected' ? (
        <div className="needs-answered">Declined{item.note ? ` — ${item.note}` : ''}</div>
      ) : (
        <form className="needs-give" onSubmit={submit}>
          <input
            type={item.kind === 'secret' ? 'password' : 'text'}
            value={value}
            autoComplete="off"
            placeholder={PLACEHOLDER[item.kind] || 'Their answer'}
            onChange={(e) => setValue(e.target.value)}
          />
          <button type="submit" className="needs-send" disabled={busy || !value.trim()}>
            {busy ? '…' : 'GIVE'}
          </button>
          <button type="button" className="needs-decline" onClick={decline} disabled={busy}>
            NOT DOING
          </button>
        </form>
      )}
      {error && <p className="needs-error">{error}</p>}
    </li>
  )
}

export default function Needs() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      setData(await api.needs())
      setError(null)
    } catch (err) {
      setError(err?.message || 'could not load the board')
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (error) return <p className="needs-error">{error}</p>
  if (!data) return <p className="needs-empty">Loading…</p>

  const { summary, groups } = data
  const anythingOpen = summary.open > 0

  return (
    <div className="needs">
      <div className="needs-summary">
        {anythingOpen ? (
          <>
            <strong>{summary.open}</strong> waiting on you
            {summary.top?.blocks && <span className="needs-top"> — top one blocks {summary.top.blocks}</span>}
          </>
        ) : (
          <>Nothing is waiting on you. {summary.provided > 0
            && <span className="needs-top">{summary.provided} answer{summary.provided === 1 ? '' : 's'} not yet confirmed by the team.</span>}</>
        )}
      </div>

      {groups.length === 0 && (
        <p className="needs-empty">
          The team has not asked for anything yet. Requests appear here the moment an
          agent hits something it cannot do without you.
        </p>
      )}

      {groups.map((group) => (
        <section className="needs-group" key={group.project_id ?? 'none'}>
          <h3>{group.name}</h3>
          <ul className="needs-list">
            {group.items.map((item) => (
              <Row key={item.id} item={item} onAnswered={load} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
