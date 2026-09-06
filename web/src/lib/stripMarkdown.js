/**
 * speechSynthesis reads markdown punctuation out loud — "**Chime Checking**" becomes
 * "asterisk asterisk Chime Checking asterisk asterisk". The system prompt asks Jarvis
 * not to use markdown, but this strips it anyway: a model slipping back into bullet
 * points shouldn't make the voice unusable, and the HUD caption shouldn't show raw
 * syntax either. Order matters — bold before italic, or the leftover single asterisks
 * from **x** get treated as emphasis.
 *
 * Lives here rather than in the Chat page because the Jarvis session moved above the
 * router and speaks from anywhere in the app.
 */
export function stripMarkdown(text) {
  if (!text) return ''
  return text
    .replace(/```[\s\S]*?```/g, ' ')          // fenced code blocks
    .replace(/`([^`]+)`/g, '$1')              // inline code
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1') // links/images -> their label
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')       // headings
    .replace(/^\s*>\s?/gm, '')                // blockquotes
    .replace(/^\s*[-*+]\s+/gm, '')            // bullet markers
    .replace(/^\s*\d+\.\s+/gm, '')            // numbered list markers
    .replace(/\*\*([^*]+)\*\*/g, '$1')        // bold
    .replace(/__([^_]+)__/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')            // italic
    .replace(/(^|\s)_([^_]+)_(?=\s|$)/g, '$1$2') // italic, but not snake_case mid-word
    .replace(/^\s*([-*_]\s*){3,}$/gm, '')     // horizontal rules
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
