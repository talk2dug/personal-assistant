import { useEffect, useRef, useState } from 'react'
import { useJarvis } from '../../context/JarvisContext'
import { stripMarkdown } from '../../lib/stripMarkdown'
import Orb, { MOODS, moodKeyFor } from './Orb'

const QUICK = [
  { label: 'Morning brief', prompt: 'Give me my morning brief.' },
  { label: 'What needs me', prompt: 'What needs my decision right now?' },
  { label: 'Plan my day', prompt: 'Plan my day around what is due and what I have scheduled.' },
  { label: 'Close the desk', prompt: 'Wrap up for the day — what did we finish and what carries over?' },
]

/**
 * The console: the orb, the last few turns of conversation, and the input.
 *
 * This is the same JarvisContext session the rest of the app uses — the mic, the
 * speech output and the history all already live above the router, so this is a view
 * onto it rather than a second chat. Typing here and speaking into the terminal in the
 * kitchen land in the same conversation.
 *
 * The quick commands are real prompts, not filters: each one sends its sentence as if
 * it had been typed, so whatever Jarvis can do in conversation he can do from a button.
 */
export default function Console({ pendingReview = 0, orbSize = 212 }) {
  const {
    messages, mode, recording, transcribing, sending, caption, mediaError,
    sendToJarvis, toggleRecording,
  } = useJarvis()
  const [draft, setDraft] = useState('')
  const scrollRef = useRef(null)

  const mood = MOODS[moodKeyFor({ mode, recording, pendingReview })] || MOODS.idle
  const recent = messages.slice(-4)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, caption])

  function submit(e) {
    e?.preventDefault()
    const text = draft.trim()
    if (!text || sending) return
    setDraft('')
    sendToJarvis(text, { addToLog: true })
  }

  return (
    <section className="blueprint cc-console">
      <i className="corner tl" /><i className="corner tr" />
      <i className="corner bl" /><i className="corner br" />

      <div className="cc-console-orb">
        <Orb size={orbSize} pendingReview={pendingReview} />
        <div className="cc-console-mood">
          <span className="cc-mood-label" style={{ color: mood.core }}>{mood.label}</span>
          <span className="cc-mood-sub">{mood.sub}</span>
        </div>
      </div>

      <div className="cc-console-body">
        <header className="cc-panel-head">
          <span className="cc-panel-title">Command console</span>
          <span className="cc-panel-meta">
            {pendingReview > 0 ? `${pendingReview} awaiting you` : 'nothing pending'}
          </span>
        </header>

        <div className="cc-chat" ref={scrollRef}>
          {recent.length === 0 && (
            <div className="cc-chat-row">
              <span className="cc-chat-who">JARVIS</span>
              <span className="cc-chat-text">Standing by. Ask me anything, or hold space to talk.</span>
            </div>
          )}
          {recent.map((m, i) => (
            <div className="cc-chat-row" key={i}>
              <span className="cc-chat-who">{m.role === 'user' ? 'YOU' : 'JARVIS'}</span>
              <span className={`cc-chat-text ${m.role === 'user' ? 'is-you' : ''}`}>
                {stripMarkdown(m.content)}
              </span>
            </div>
          ))}
          {(sending || transcribing) && (
            <div className="cc-chat-row">
              <span className="cc-chat-who">JARVIS</span>
              <span className="cc-chat-text">
                {transcribing ? 'Transcribing' : 'Thinking'}<span className="cc-caret">▌</span>
              </span>
            </div>
          )}
          {mediaError && <div className="cc-chat-err">{mediaError}</div>}
        </div>

        <div className="cc-quick">
          {QUICK.map((q) => (
            <button
              key={q.label}
              type="button"
              className="cc-chip"
              disabled={sending}
              onClick={() => sendToJarvis(q.prompt, { addToLog: true })}
            >
              {q.label}
            </button>
          ))}
        </div>

        <form className="cc-console-input" onSubmit={submit}>
          <span className="cc-prompt">&gt;</span>
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Speak or type a directive…"
            aria-label="Message Jarvis"
          />
          <button type="submit" className="blueprint cc-btn is-primary" disabled={!draft.trim() || sending}>
            <i className="corner tl" /><i className="corner tr" />
            <i className="corner bl" /><i className="corner br" />
            TRANSMIT
          </button>
          <button
            type="button"
            className={`cc-btn ${recording ? 'is-hot' : ''}`}
            onClick={toggleRecording}
            disabled={transcribing}
          >
            <span className="cc-pip is-pulse" />
            {recording ? 'LISTENING…' : transcribing ? 'TRANSCRIBING' : 'HOLD TO TALK'}
          </button>
        </form>
      </div>
    </section>
  )
}
