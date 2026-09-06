import React, { useState } from 'react';
import { DisputeLetter } from './types';

interface Props {
  letter: DisputeLetter;
  onConfirm: () => Promise<void>; // calls the confirm API
  onAuthorize: () => Promise<void>; // calls the authorize API (real postage)
  onCancel: () => Promise<void>;
  onClose: () => void;
}

/**
 * The hard-confirm gate, in UI form. Two distinct, deliberately separated
 * actions:
 *   1. "I've reviewed this" checkbox + Confirm button -> POST .../confirm
 *   2. A separately-styled, disabled-until-confirmed "Authorize real
 *      postage" button -> POST .../authorize (explicitApproval: true)
 * There is no single click that can trigger real postage.
 */
export function MailConfirmDialog({ letter, onConfirm, onAuthorize, onCancel, onClose }: Props) {
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const costDisplay =
    letter.quotedCostCents != null
      ? `${(letter.quotedCostCents / 100).toFixed(2)} ${letter.quoteCurrency}`
      : 'unknown';

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err: any) {
      setError(err.message || 'Action failed.');
    } finally {
      setBusy(false);
    }
  };

  const isConfirmed = letter.status === 'confirmed' || ['authorized', 'mailed', 'in_transit', 'delivered'].includes(letter.status);
  const isMailed = ['authorized', 'mailed', 'in_transit', 'delivered', 'returned'].includes(letter.status);

  return (
    <div className="mail-confirm-dialog" role="dialog" aria-modal="true">
      <h2>Review before mailing</h2>

      <section>
        <h3>Recipient</h3>
        <p>{letter.recipientSnapshot.bureauName}</p>
        {letter.recipientSnapshot.addressLines.map((line) => (
          <p key={line}>{line}</p>
        ))}
        <p className="muted">
          Bureau addresses change periodically â€” double-check this is current before authorizing.
        </p>
      </section>

      <section>
        <h3>Letter text</h3>
        <pre className="letter-body">{letter.letterBody}</pre>
      </section>

      <section>
        <h3>Quoted cost</h3>
        <p>{costDisplay}</p>
      </section>

      {error && <p className="form-error">{error}</p>}

      {!isMailed && (
        <>
          <label className="review-checkbox">
            <input
              type="checkbox"
              checked={reviewed || isConfirmed}
              disabled={isConfirmed}
              onChange={(e) => setReviewed(e.target.checked)}
            />
            I have reviewed the recipient, the full letter text, and the quoted cost above.
          </label>

          {!isConfirmed && (
            <button disabled={!reviewed || busy} onClick={() => run(onConfirm)}>
              Confirm review
            </button>
          )}

          {isConfirmed && (
            <button
              className="danger authorize-button"
              disabled={busy}
              onClick={() => {
                if (!window.confirm(`This will mail this letter and spend ${costDisplay}. Continue?`)) return;
                run(onAuthorize);
              }}
            >
              Authorize real postage ({costDisplay})
            </button>
          )}

          <button disabled={busy} onClick={() => run(onCancel)}>
            Cancel this draft
          </button>
        </>
      )}

      <button onClick={onClose}>Close</button>
    </div>
  );
}
