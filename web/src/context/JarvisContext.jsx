import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { stripMarkdown } from '../lib/stripMarkdown'

/**
 * Jarvis's live session, hoisted above the router.
 *
 * This used to live inside the Chat page, which meant navigating to Finance or the
 * Office unmounted it: the mic stopped, a reply mid-flight was lost, and speech cut off
 * mid-sentence. An assistant that only exists on one screen isn't an assistant. The
 * provider sits in the app shell, so recording, sending, speaking and the camera all
 * survive route changes — the Chat page is now just the largest view onto it.
 *
 * The text modal lives here too, so the keyboard shortcut can open a conversation from
 * anywhere rather than bouncing you to /chat first.
 */

const JarvisContext = createContext(null)
export const useJarvis = () => useContext(JarvisContext)

const AUDIO_MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']
const JARVIS_VOICE_PREFERENCE = [/ryan/i, /george/i, /daniel/i, /google uk english male/i, /oliver/i, /arthur/i]
const FEMALE_NAME_HINTS = /female|zira|hazel|susan|libby|sonia|catherine|olivia|aria/i

// Below this, a spacebar press counts as a tap (mic latches open until you press
// again); above it, it counts as hold-to-talk and releasing sends. One key covers both
// habits without needing a preference.
const HOLD_TO_TALK_MS = 400

export const CAPTIONS = {
  idle: 'Standing by, sir.',
  listening: 'Listening…',
  thinking: 'One moment.',
}

function pickAudioMimeType() {
  if (typeof MediaRecorder === 'undefined') return null
  return AUDIO_MIME_CANDIDATES.find((t) => MediaRecorder.isTypeSupported(t)) || ''
}

function pickJarvisVoice(voices) {
  for (const pattern of JARVIS_VOICE_PREFERENCE) {
    const match = voices.find((v) => pattern.test(v.name))
    if (match) return match
  }
  const gbMale = voices.find((v) => /en-GB/i.test(v.lang) && !FEMALE_NAME_HINTS.test(v.name))
  if (gbMale) return gbMale
  return voices.find((v) => /en-GB/i.test(v.lang)) || voices.find((v) => /^en/i.test(v.lang)) || null
}

function voicesAsync() {
  return new Promise((resolve) => {
    const existing = speechSynthesis.getVoices()
    if (existing.length) return resolve(existing)
    speechSynthesis.onvoiceschanged = () => resolve(speechSynthesis.getVoices())
  })
}

/** Whether a keystroke belongs to whatever the user is typing in, rather than to us. */
function isTypingTarget(el) {
  if (!el) return false
  if (el.isContentEditable) return true
  const tag = el.tagName
  // Space activates a focused button; hijacking it there would break the UI's own controls.
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON'
}

export function JarvisProvider({ children }) {
  const [messages, setMessages] = useState([])
  const [modalOpen, setModalOpen] = useState(false)
  const [sending, setSending] = useState(false)
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [speaking, setSpeaking] = useState(false)
  const [cameraOn, setCameraOn] = useState(false)
  const [voiceOn, setVoiceOn] = useState(() => localStorage.getItem('jarvis-voice-out') !== 'off')
  const [mediaError, setMediaError] = useState('')
  const [voiceName, setVoiceName] = useState('')
  const [caption, setCaption] = useState(CAPTIONS.idle)

  const videoRef = useRef(null)
  const snapshotCanvasRef = useRef(null)
  const cameraStreamRef = useRef(null)
  const recorderRef = useRef(null)
  const chunksRef = useRef([])
  const jarvisVoiceRef = useRef(null)
  const cancelledRef = useRef(false)
  // Mirrors of state for use inside callbacks that outlive a render (the recorder's
  // onstop, the key handlers), where a captured value would be stale.
  const sendingRef = useRef(false)
  const recordingRef = useRef(false)
  const cameraOnRef = useRef(false)
  const voiceOnRef = useRef(voiceOn)

  // Exposed so the orb's canvas loop can read live mic levels without owning the stream.
  const analyserRef = useRef(null)
  const freqRef = useRef(null)
  const audioCtxRef = useRef(null)

  const mode = recording ? 'listening' : sending || transcribing ? 'thinking' : speaking ? 'speaking' : 'idle'
  const modeRef = useRef(mode)

  useEffect(() => { sendingRef.current = sending }, [sending])
  useEffect(() => { recordingRef.current = recording }, [recording])
  useEffect(() => { cameraOnRef.current = cameraOn }, [cameraOn])
  useEffect(() => { voiceOnRef.current = voiceOn }, [voiceOn])

  useEffect(() => {
    modeRef.current = mode
    if (mode === 'idle') setCaption(CAPTIONS.idle)
    else if (mode === 'listening') setCaption(CAPTIONS.listening)
    else if (mode === 'thinking') setCaption(CAPTIONS.thinking)
  }, [mode])

  useEffect(() => { api.chatHistory().then(setMessages).catch(() => {}) }, [])

  useEffect(() => {
    voicesAsync().then((voices) => {
      const picked = pickJarvisVoice(voices)
      jarvisVoiceRef.current = picked
      setVoiceName(picked ? picked.name : 'default')
    })
  }, [])

  // Only on unmount of the whole app — not on route changes, which is the entire point.
  useEffect(() => () => {
    cameraStreamRef.current?.getTracks().forEach((t) => t.stop())
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
    audioCtxRef.current?.close().catch(() => {})
    speechSynthesis.cancel()
  }, [])

  const speak = useCallback((text) => {
    const spoken = stripMarkdown(text)
    if (!spoken || !voiceOnRef.current) return
    speechSynthesis.cancel()
    const utterance = new SpeechSynthesisUtterance(spoken)
    if (jarvisVoiceRef.current) utterance.voice = jarvisVoiceRef.current
    utterance.pitch = 0.85
    utterance.rate = 0.97
    utterance.onstart = () => setSpeaking(true)
    utterance.onend = () => setSpeaking(false)
    utterance.onerror = () => setSpeaking(false)
    speechSynthesis.speak(utterance)
  }, [])

  const toggleVoice = useCallback(() => {
    setVoiceOn((prev) => {
      const next = !prev
      localStorage.setItem('jarvis-voice-out', next ? 'on' : 'off')
      if (!next) {
        speechSynthesis.cancel()
        setSpeaking(false)
      }
      return next
    })
  }, [])

  const captureFrame = useCallback(() => {
    const video = videoRef.current
    const canvas = snapshotCanvasRef.current
    if (!video || !canvas || video.readyState < 2) return null
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    canvas.getContext('2d').drawImage(video, 0, 0)
    return canvas.toDataURL('image/jpeg', 0.82)
  }, [])

  const toggleCamera = useCallback(async () => {
    setMediaError('')
    if (cameraOnRef.current) {
      cameraStreamRef.current?.getTracks().forEach((t) => t.stop())
      cameraStreamRef.current = null
      setCameraOn(false)
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
      cameraStreamRef.current = stream
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        await videoRef.current.play()
      }
      setCameraOn(true)
    } catch (err) {
      setMediaError(`Camera unavailable: ${err.message}`)
    }
  }, [])

  const sendToJarvis = useCallback(async (text, { addToLog }) => {
    if (!text || sendingRef.current) return
    const image = cameraOnRef.current ? captureFrame() : null
    if (addToLog) setMessages((prev) => [...prev, { role: 'user', content: text, image }])
    setSending(true)
    sendingRef.current = true
    try {
      const { reply } = await api.sendMessage(text, image)
      if (addToLog) setMessages((prev) => [...prev, { role: 'assistant', content: stripMarkdown(reply) }])
      if (reply) setCaption(stripMarkdown(reply))
      speak(reply)
    } catch (err) {
      const errText = `(error reaching Jarvis: ${err.message})`
      if (addToLog) setMessages((prev) => [...prev, { role: 'assistant', content: errText }])
      setCaption(errText)
    } finally {
      setSending(false)
      sendingRef.current = false
    }
  }, [captureFrame, speak])

  const stopRecording = useCallback(({ cancel = false } = {}) => {
    cancelledRef.current = cancel
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
  }, [])

  const startRecording = useCallback(async () => {
    setMediaError('')
    if (recordingRef.current) return
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })

      // Live mic levels for the orb, so "listening" reacts to actual speech.
      const AudioCtx = window.AudioContext || window.webkitAudioContext
      const audioCtx = new AudioCtx()
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 256
      analyser.smoothingTimeConstant = 0.72
      audioCtx.createMediaStreamSource(stream).connect(analyser)
      audioCtxRef.current = audioCtx
      analyserRef.current = analyser
      freqRef.current = new Uint8Array(analyser.frequencyBinCount)

      const mimeType = pickAudioMimeType()
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
      chunksRef.current = []
      cancelledRef.current = false
      recorder.ondataavailable = (e) => e.data.size > 0 && chunksRef.current.push(e.data)
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop())
        audioCtxRef.current?.close().catch(() => {})
        audioCtxRef.current = null
        analyserRef.current = null
        setRecording(false)
        recordingRef.current = false

        if (cancelledRef.current) {
          cancelledRef.current = false
          setCaption('Cancelled.')
          return
        }
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType })
        if (blob.size === 0) {
          setMediaError('No audio captured — check the mic permission and try again.')
          return
        }
        setTranscribing(true)
        try {
          const { text } = await api.transcribe(blob)
          if (text) await sendToJarvis(text, { addToLog: false })
          else setMediaError("Didn't catch any speech in that clip — try again, a bit longer and closer to the mic.")
        } catch (err) {
          setMediaError(`Transcription failed: ${err.message}`)
        } finally {
          setTranscribing(false)
        }
      }
      recorderRef.current = recorder
      recorder.start()
      setRecording(true)
      recordingRef.current = true
    } catch (err) {
      setMediaError(`Microphone unavailable: ${err.message}`)
    }
  }, [sendToJarvis])

  const toggleRecording = useCallback(() => {
    if (recordingRef.current) stopRecording()
    else startRecording()
  }, [startRecording, stopRecording])

  // ---- keyboard: space opens the mic, anywhere in the app --------------------
  useEffect(() => {
    let pressedAt = 0

    function onKeyDown(e) {
      if (e.code !== 'Space' || e.repeat || e.metaKey || e.ctrlKey || e.altKey) {
        if (e.key === 'Escape' && recordingRef.current) stopRecording({ cancel: true })
        return
      }
      if (isTypingTarget(e.target)) return
      // Stop the page scrolling out from under a press meant for the mic.
      e.preventDefault()
      if (recordingRef.current) {
        // Already latched open from an earlier tap — this press closes it and sends.
        stopRecording()
        pressedAt = 0
        return
      }
      pressedAt = Date.now()
      startRecording()
    }

    function onKeyUp(e) {
      if (e.code !== 'Space' || !pressedAt) return
      const held = Date.now() - pressedAt
      pressedAt = 0
      // Held down: treat it as push-to-talk and send on release. A quick tap leaves the
      // mic open so you can speak hands-free until you tap again.
      if (held >= HOLD_TO_TALK_MS && recordingRef.current) stopRecording()
    }

    // Losing focus with a hot mic is how you end up recording a phone call.
    function onBlur() {
      if (recordingRef.current) stopRecording({ cancel: true })
      pressedAt = 0
    }

    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
    }
  }, [startRecording, stopRecording])

  const value = {
    messages, setMessages, modalOpen, setModalOpen,
    sending, recording, transcribing, speaking, cameraOn, voiceOn, mediaError, caption, voiceName, mode,
    speak, toggleVoice, toggleCamera, toggleRecording, startRecording, stopRecording, sendToJarvis,
    analyserRef, freqRef, modeRef, videoRef, snapshotCanvasRef,
  }

  return <JarvisContext.Provider value={value}>{children}</JarvisContext.Provider>
}
