import React, { useState } from 'react';
import { Dispute, DisputeLetter } from './types';
import { draftLetter, confirmLetter, authorizeLetter, cancelLetter, trackLetter } from './api';
import { MailConfirmDialog } from './MailConfirmDialog';
import { MailStatusBadge } from './MailStatusBadge';

interface Props {
  dispute: Dispute;
  consumer: { fullName: string; addressLines: string[] };
  letters: DisputeLetter[];
  onLettersChanged: () => Promise<void>;
}

export function LetterDraftPanel({ dispute, consumer, letters, onLettersChanged }: Props) {
  const [activeLetter, setActiveLetter] = useState<DisputeLetter | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDraft = async () => {
    setDrafting(true);
    setError(null);
    try {
      const letter = await draftLetter(dispute.id, consumer);
      await onLettersChanged();
      setActiveLetter(letter);
    } catch (err: any) {
      setError(err.message || 'Failed to draft letter.');
    } finally {
      setDrafting(false);
    }
  };

  const refreshActive = async (updated: DisputeLetter) => {
    setActiveLetter(updated);
    await onLettersChanged();
  };

  return (
    <div className="letter-draft-panel">
      <button onClick={handleDraft} disabled={drafting}>
        {drafting ? 'Drafting & quoting...' : 'Draft & quote a dispute letter'}
      </button>
      {error && <p className="form-error">{error}</p>}

      <ul className="letter-list">
        {letters.map((l) => (
          <li key={l.id}>
            v{l.letterVersion} â€” <MailStatusBadge status={l.status} />{' '}
            <button onClick={() => setActiveLetter(l)}>Open</button>
            {['mailed', 'in_transit'].includes(l.status) && (
              <button
                onClick={async () => {
                  const updated = await trackLetter(l.id);
                  await refreshActive(updated);
                }}
              >
                Check tracking
              </button>
            )}
          </li>
        ))}
      </ul>

      {activeLetter && (
        <MailConfirmDialog
          letter={activeLetter}
          onConfirm={async () => refreshActive(await confirmLetter(activeLetter.id))}
          onAuthorize={async () => refreshActive(await authorizeLetter(activeLetter.id))}
          onCancel={async () => refreshActive(await cancelLetter(activeLetter.id))}
          onClose={() => setActiveLetter(null)}
        />
      )}
    </div>
  );
}
