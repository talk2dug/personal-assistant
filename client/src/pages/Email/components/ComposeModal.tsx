import React, { useState } from 'react';
import { ComposeDraft } from '../types';
import { sendEmailConfirmed } from '../api';
import { ConfirmSendDialog } from './ConfirmSendDialog';

interface Props {
  initial?: Partial<ComposeDraft>;
  onCancel: () => void;
  onSent: () => void;
}

type Step = 'edit' | 'confirm';

function parseRecipients(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

export function ComposeModal({ initial, onCancel, onSent }: Props) {
  const [step, setStep] = useState<Step>('edit');
  const [to, setTo] = useState((initial?.to || []).join(', '));
  const [cc, setCc] = useState((initial?.cc || []).join(', '));
  const [subject, setSubject] = useState(initial?.subject || '');
  const [body, setBody] = useState(initial?.body || '');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // Recomputed fresh from current field state on every render, so what the
  // confirm dialog shows and what gets sent can never drift apart.
  const draft: ComposeDraft = {
    to: parseRecipients(to),
    cc: parseRecipients(cc),
    subject: subject.trim(),
    body,
  };

  function handleReview(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (draft.to.length === 0) {
      setFormError('Add at least one recipient.');
      return;
    }
    if (!draft.subject) {
      setFormError('Subject is required.');
      return;
    }
    setStep('confirm');
  }

  async function handleConfirmSend() {
    setSending(true);
    setSendError(null);
    try {
      await sendEmailConfirmed(draft);
      setSending(false);
      onSent();
    } catch (err) {
      setSending(false);
      setSendError((err as Error).message);
    }
  }

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label="Compose email">
      <div className="modal">
        {step === 'edit' && (
          <form className="compose-form" onSubmit={handleReview}>
            <h2>New Message</h2>
            <label>
              To
              <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="name@example.com, ..." />
            </label>
            <label>
              Cc
              <input value={cc} onChange={(e) => setCc(e.target.value)} placeholder="optional" />
            </label>
            <label>
              Subject
              <input value={subject} onChange={(e) => setSubject(e.target.value)} />
            </label>
            <label>
              Body
              <textarea rows={10} value={body} onChange={(e) => setBody(e.target.value)} />
            </label>
            {formError && <div className="compose-form__error">{formError}</div>}
            <div className="compose-form__actions">
              <button type="button" onClick={onCancel}>
                Cancel
              </button>
              <button type="submit">Review &amp; Sendâ€¦</button>
            </div>
          </form>
        )}

        {step === 'confirm' && (
          <ConfirmSendDialog
            draft={draft}
            sending={sending}
            error={sendError}
            onBack={() => setStep('edit')}
            onConfirm={handleConfirmSend}
            onCancel={onCancel}
          />
        )}
      </div>
    </div>
  );
}
