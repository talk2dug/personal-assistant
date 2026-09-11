import { useEffect, useState } from 'react'
import { api } from '../api'
import ConfirmSendModal from '../components/email/ConfirmSendModal'
import './email.css'

/** A message's full body is a separate, heavier IMAP fetch than the list -- lazily
 *  loaded the first time a row is opened, same idiom as DisputeItemRow's letters. */
function MessageDetail({ uid, folder, onClose }) {
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    setMessage(null)
    setError(null)
    api.readEmail(uid, folder).then(setMessage).catch((e) => setError(e.message))
  }, [uid, folder])

  return (
    <div className="email-detail">
      <div className="email-detail-header">
        <h4>{message ? message.subject || '(no subject)' : 'Loading…'}</h4>
        <button className="task-drop" onClick={onClose} title="Close">✕</button>
      </div>
      {error && <p className="empty-hint">Couldn't load that message ({error}).</p>}
      {message && (
        <>
          <div className="email-detail-meta">
            <div><span className="hud-label">From</span> {message.from}</div>
            <div><span className="hud-label">Date</span> {message.date}</div>
          </div>
          <pre className="email-detail-body">{message.body}</pre>
        </>
      )}
    </div>
  )
}

function InboxPanel() {
  const [messages, setMessages] = useState(null)
  const [query, setQuery] = useState('')
  const [openUid, setOpenUid] = useState(null)
  const [error, setError] = useState(null)

  async function load() {
    try {
      const result = await api.listEmails('INBOX', 20, query || undefined)
      setMessages(result.emails || [])
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [query])

  if (messages === null) return <p className="empty-hint">Loading…</p>

  return (
    <section>
      <div className="tasks-header">
        <h3>Inbox</h3>
        <input
          className="email-search"
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {error && <p className="empty-hint">{error}</p>}
      {messages.length === 0 && <p className="empty-hint">Nothing here.</p>}
      <ul className="task-list">
        {messages.map((m) => (
          <li
            key={m.uid}
            className={`task-row email-row ${m.unread ? 'is-unread' : ''}`}
            onClick={() => setOpenUid(m.uid)}
          >
            <div className="task-body">
              <div className="task-text">{m.subject || '(no subject)'}</div>
              <div className="task-meta">
                <span>{m.from}</span>
                <span>{m.date}</span>
              </div>
            </div>
          </li>
        ))}
      </ul>
      {openUid && <MessageDetail uid={openUid} folder="INBOX" onClose={() => setOpenUid(null)} />}
    </section>
  )
}

function ComposePanel() {
  const [to, setTo] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [pending, setPending] = useState(null)
  const [sent, setSent] = useState(false)

  function review(e) {
    e.preventDefault()
    if (!to.trim() || !subject.trim() || !body.trim()) return
    setSent(false)
    setPending({ to: to.trim(), subject: subject.trim(), body })
  }

  function afterSent() {
    setPending(null)
    setSent(true)
    setTo('')
    setSubject('')
    setBody('')
  }

  return (
    <section>
      <h3>Compose</h3>
      <form className="task-form email-compose-form" onSubmit={review}>
        <input placeholder="To" type="email" value={to} onChange={(e) => setTo(e.target.value)} />
        <input placeholder="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} />
        <textarea
          className="email-compose-body"
          placeholder="Write your message…"
          rows={10}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <div className="task-form-actions">
          <button type="submit">Review &amp; send…</button>
        </div>
      </form>
      {sent && <p className="empty-hint">Sent.</p>}
      {pending && (
        <ConfirmSendModal draft={pending} onClose={() => setPending(null)} onSent={afterSent} />
      )}
    </section>
  )
}

export default function Email() {
  const [tab, setTab] = useState('inbox')

  return (
    <div className="email-page">
      <div className="kitchen-tabs">
        <button className={`kitchen-tab ${tab === 'inbox' ? 'active' : ''}`} onClick={() => setTab('inbox')}>
          Inbox
        </button>
        <button className={`kitchen-tab ${tab === 'compose' ? 'active' : ''}`} onClick={() => setTab('compose')}>
          Compose
        </button>
      </div>
      {tab === 'inbox' && <InboxPanel />}
      {tab === 'compose' && <ComposePanel />}
    </div>
  )
}
