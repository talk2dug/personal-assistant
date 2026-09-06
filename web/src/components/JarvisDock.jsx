import { useEffect, useRef, useState } from 'react'
import { useJarvis } from '../context/JarvisContext'

/**
 * The always-present Jarvis surface: a status dock, the text modal, and the off-screen
 * media elements.
 *
 * Rendered in the app shell rather than on a page, so Jarvis is reachable from Finance
 * or the Office exactly as he is from the orb. The dock hides itself on the orb page —
 * there the whole screen is already the status display, and a second indicator would
 * just be noise.
 */

const STATE_LABEL = {
  idle: 'standing by',
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
}

export default function JarvisDock({ compact }) {
  const {
    mode, caption, messages, sending, mediaError, recording, transcribing,
    modalOpen, setModalOpen, sendToJarvis, toggleRecording,
    videoRef, snapshotCanvasRef,
  } = useJarvis()

  const [input, setInput] = useState('')
  const bottomRef = useRef(null)

  useEffect(() => {
    if (modalOpen) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, modalOpen, sending])

  function handleSend(e) {
    e.preventDefault()
    const text = input.trim()
    if (!text) return
    setInput('')
    sendToJarvis(text, { addToLog: true })
  }

  return (
    <>
      {/* Off-screen rather than display:none — browsers stop decoding hidden video, and
          a stalled <video> yields a blank camera frame. */}
      <video ref={videoRef} playsInline muted className="camera-sink" />
      <canvas ref={snapshotCanvasRef} className="camera-sink" />

      {!compact && (
        <div className={`jarvis-dock mode-${mode}`}>
          <button
            type="button"
            className={`jarvis-dock-orb ${recording ? 'recording' : ''}`}
            onClick={toggleRecording}
            disabled={transcribing}
            title="Talk to Jarvis (space)"
          >
            <span className="jarvis-dock-pulse" />
          </button>
          <div className="jarvis-dock-body">
            <span className="jarvis-dock-state">{STATE_LABEL[mode]}</span>
            <span className="jarvis-dock-caption">{mediaError || caption}</span>
          </div>
          <button type="button" className="jarvis-dock-chat" onClick={() => setModalOpen(true)}>
            chat
          </button>
        </div>
      )}

      {modalOpen && (
        <div className="chat-modal-backdrop" onClick={() => setModalOpen(false)}>
          <div className="chat-modal" onClick={(e) => e.stopPropagation()}>
            <div className="chat-modal-header">
              <span className="hud-label">TEXT LINK</span>
              <button type="button" className="chat-modal-close" onClick={() => setModalOpen(false)}>
                close
              </button>
            </div>
            <div className="chat-log">
              {messages.map((m, i) => (
                <div key={i} className={`chat-bubble ${m.role}`}>
                  {m.image && <img src={m.image} alt="captured" className="chat-bubble-image" />}
                  {m.content}
                </div>
              ))}
              {sending && <div className="chat-bubble assistant pending">…</div>}
              <div ref={bottomRef} />
            </div>
            <form className="chat-input-row" onSubmit={handleSend}>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Message Jarvis…"
                autoFocus
              />
              <button type="submit" disabled={sending}>Send</button>
            </form>
          </div>
        </div>
      )}
    </>
  )
}
