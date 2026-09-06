import React from 'react';
import { ComposeDraft } from '../types';

interface Props {
  draft: ComposeDraft;
  sending: boolean;
  error: string | null;
  onBack: () => void;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * The single gate before send_email is ever invoked. Renders the exact
 * recipient(s), subject, and body that will be sent, verbatim, with no
 * further transformation. The parent (ComposeModal) only calls the send
 * API after the user clicks "Send" here.
 */
export function ConfirmSendDialog({ draft, sending, error, onBack, onConfirm, onCancel }: Props) {
  return (
    <div className="confirm-send">
      <h2>Confirm before sending</h2>
      <p className="confirm-send__warning">This will send an email immediately. Please check every field below.</p>
      <dl className="confirm-send__fields">
        <dt>To</dt>
        <dd>{draft.to.join(', ') || '(none)'}</dd>
        {draft.cc && draft.cc.length > 0 && (
          <>
            <dt>Cc</dt>
            <dd>{draft.cc.join(', ')}</dd>
          </>
        )}
        <dt>Subject</dt>
        <dd>{draft.subject}</dd>
        <dt>Body</dt>
        <dd className="confirm-send__body">{draft.body}</dd>
      </dl>
      {error && <div className="confirm-send__error">Send failed: {error}</div>}
      <div className="confirm-send__actions">
        <button type="button" onClick={onBack} disabled={sending}>
          Back to edit
        </button>
        <button type="button" onClick={onCancel} disabled={sending}>
          Cancel
        </button>
        <button type="button" className="confirm-send__send-btn" onClick={onConfirm} disabled={sending}>
          {sending ? 'Sendingâ€¦' : 'Send'}
        </button>
      </div>
    </div>
  );
}
