import React from 'react';
import { EmailSummary } from '../types';

interface Props {
  messages: EmailSummary[];
  selectedId: string | null;
  loading: boolean;
  error: string | null;
  onSelect: (id: string) => void;
}

export function InboxList({ messages, selectedId, loading, error, onSelect }: Props) {
  if (loading && messages.length === 0) {
    return <div className="inbox-list inbox-list--status">Loading inboxâ€¦</div>;
  }
  if (error) {
    return <div className="inbox-list inbox-list--status inbox-list--error">Failed to load: {error}</div>;
  }
  if (messages.length === 0) {
    return <div className="inbox-list inbox-list--status">No messages.</div>;
  }
  return (
    <ul className="inbox-list" aria-label="Inbox">
      {messages.map((m) => (
        <li
          key={m.id}
          className={
            'inbox-list__item' +
            (m.id === selectedId ? ' inbox-list__item--selected' : '') +
            (!m.isRead ? ' inbox-list__item--unread' : '')
          }
          onClick={() => onSelect(m.id)}
        >
          <div className="inbox-list__row">
            <span className="inbox-list__from">{m.from}</span>
            <span className="inbox-list__date">{new Date(m.date).toLocaleString()}</span>
          </div>
          <div className="inbox-list__subject">{m.subject || '(no subject)'}</div>
          <div className="inbox-list__snippet">{m.snippet}</div>
        </li>
      ))}
    </ul>
  );
}
