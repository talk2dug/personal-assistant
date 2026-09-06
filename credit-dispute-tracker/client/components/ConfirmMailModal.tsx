import React, { useState } from 'react';
import type { DisputeLetter } from '../types';

interface Props {
  letter: DisputeLetter;
  onConfirm: () => Promise<void>;
  onCancel: () => void;
}

/**
 * Hard confirmation step before any real postage is purchased.
 * Requires the user to tick two separate acknowledgements and type
 * the word MAIL before the button is even enabled -- deliberately
 * more friction than a normal "Are you sure?" dialog, because this is
 * the one action in the app that spends real money and puts a
 * physical letter with the user's dispute details in the mail.
 */
export default function ConfirmMailModal({ letter, onConfirm, onCancel }: Props) {
  const [ackRecipient, setAckRecipient] = useState(false);
  const [ackCost, setAckCost] = useState(false);
  const [typedConfirm, setTypedConfirm] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canConfirm = ackRecipient && ackCost && typedConfirm.trim().toUpperCase() === 'MAIL' && !sending;

  const handleConfirm = async () => {
    setError(null);
    setSending(true);
    try {
      await onConfirm();
    } catch (err: any) {
      setError(err.message || 'Failed to send');
    } finally {
      setSending(false);
    }
  };

  const dollars = ((letter.quotedCostCents ?? 0) / 100).toFixed(2);

  return (
    <div className="modal-backdrop">
      <div className="confirm-mail-modal" role="dialog" aria-modal="true">
        <h3>Confirm before mailing -- this costs real money</h3>

        <section>
          <h4>Recipient</h4>
          <p>
            {letter.recipientName}<br />
            {letter.recipientAddressLine1}{letter.recipientAddressLine2 ? <><br />{letter.recipientAddressLine2}</> : null}<br />
            {letter.recipientCity}, {letter.recipientState} {letter.recipientZip}
          </p>
        </section>

        <section>
          <h4>Letter text</h4>
          <pre className="letter-preview">{letter.letterText}</pre>
        </section>

        <section>
          <h4>Quoted cost</h4>
          <p className="quoted-cost">${dollars} {letter.quoteCurrency || 'USD'}</p>
        </section>

        <label>
          <input type="checkbox" checked={ackRecipient} onChange={e => setAckRecipient(e.target.checked)} />
          I've checked the recipient address above and it's correct.
        </label>
        <label>
          <input type="checkbox" checked={ackCost} onChange={e => setAckCost(e.target.checked)} />
          I accept the quoted cost of ${dollars}.
        </label>
        <label>
          Type MAIL to confirm
          <input type="text" value={typedConfirm} onChange={e => setTypedConfirm(e.target.value)} />
        </label>

        {error && <p className="error">{error}</p>}

        <div className="modal-actions">
          <button onClick={onCancel} disabled={sending}>Cancel</button>
          <button onClick={handleConfirm} disabled={!canConfirm} className="danger">
            {sending ? 'Sendingâ€¦' : 'Authorize & mail'}
          </button>
        </div>
      </div>
    </div>
  );
}
