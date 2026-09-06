import React from 'react';
import { EmailMessage } from '../types';

interface Props {
  message: EmailMessage | null;
  loading: boolean;
  error: string | null;
  /** Omit to hide the Reply button (e.g. before compose is wired up). */
  onReply?: (message: EmailMessage) => void;
}

export function ThreadView({ message, loading, error, onReply }: Props) {
  if (loading) {
    return <div className="thread-view thread-view--status">Loading messageâ€¦</div>;
  }
  if (error) {
    return <div className="thread-view thread-view--status thread-view--error">Failed to load: {error}</div>;
  }
  if (!message) {
    return <div className="thread-view thread-view--status">Select a message to read it.</div>;
  }
  return (
    <div className="thread-view">
      <div className="thread-view__header">
        <h2 className="thread-view__subject">{message.subject || '(no subject)'}</h2>
        <div className="thread-view__meta">
          <span>
            <strong>From:</strong> {message.from}
          </span>
          <span>
            <strong>To:</strong> {message.to?.join(', ')}
          </span>
          {message.cc && message.cc.length > 0 && (
            <span>
              <strong>Cc:</strong> {message.cc.join(', ')}
            </span>
          )}
          <span>
            <strong>Date:</strong> {new Date(message.date).toLocaleString()}
          </span>
        </div>
        {onReply && (
          <button className="thread-view__reply-btn" onClick={() => onReply(message)}>
            Reply
          </button>
        )}
      </div>
      <div className="thread-view__body">
        {message.body.split('\n').map((line, i) => (
          <p key={i}>{line}</p>
        ))}
      </div>
    </div>
  );
}
