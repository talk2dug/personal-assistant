import React, { useEffect, useState, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { fetchDisputeItem } from '../api/disputeApi';
import {
  fetchLetterForDispute, draftLetter as draftLetterApi, quoteLetter as quoteLetterApi,
  confirmAndMailLetter, refreshMailStatus,
} from '../api/letterApi';
import ConfirmMailModal from '../components/ConfirmMailModal';
import MailStatusBadge from '../components/MailStatusBadge';
import type { DisputeItem, DisputeLetter } from '../types';

export default function DisputeDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [item, setItem] = useState<DisputeItem | null>(null);
  const [letter, setLetter] = useState<DisputeLetter | null>(null);
  const [letterText, setLetterText] = useState('');
  const [recipient, setRecipient] = useState({ name: '', addressLine1: '', addressLine2: '', city: '', state: '', zip: '' });
  const [showConfirm, setShowConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!id) return;
    const [i, l] = await Promise.all([fetchDisputeItem(id), fetchLetterForDispute(id)]);
    setItem(i);
    setLetter(l);
    if (l) setLetterText(l.letterText);
  }, [id]);

  useEffect(() => { load(); }, [load]);

  if (!item) return <p>Loadingâ€¦</p>;

  const handleDraft = async () => {
    if (!id) return;
    setBusy(true); setError(null);
    try {
      setLetter(await draftLetterApi(id, letterText, recipient));
    } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };

  const handleQuote = async () => {
    if (!letter) return;
    setBusy(true); setError(null);
    try {
      setLetter(await quoteLetterApi(letter.id));
    } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };

  const handleConfirmed = async () => {
    if (!letter) return;
    const updated = await confirmAndMailLetter(letter.id, letter.recipientName, letter.quotedCostCents ?? -1);
    setLetter(updated);
    setShowConfirm(false);
    await load();
  };

  const handleRefreshStatus = async () => {
    if (!letter) return;
    setBusy(true); setError(null);
    try {
      setLetter(await refreshMailStatus(letter.id));
    } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };

  return (
    <div className="dispute-detail-page">
      <h2>{item.creditorName} â€” {item.bureau}</h2>
      <p>{item.itemDescription}</p>
      <p><strong>Reason:</strong> {item.disputeReason}</p>
      <p><strong>State:</strong> {item.state}</p>

      {error && <p className="error">{error}</p>}

      {!letter && (
        <section>
          <h3>Draft a dispute letter</h3>
          <textarea rows={10} value={letterText} onChange={e => setLetterText(e.target.value)} placeholder="Letter text..." />
          <fieldset>
            <legend>Bureau mailing address</legend>
            <input placeholder="Name" value={recipient.name} onChange={e => setRecipient({ ...recipient, name: e.target.value })} />
            <input placeholder="Address line 1" value={recipient.addressLine1} onChange={e => setRecipient({ ...recipient, addressLine1: e.target.value })} />
            <input placeholder="Address line 2 (optional)" value={recipient.addressLine2} onChange={e => setRecipient({ ...recipient, addressLine2: e.target.value })} />
            <input placeholder="City" value={recipient.city} onChange={e => setRecipient({ ...recipient, city: e.target.value })} />
            <input placeholder="State" value={recipient.state} onChange={e => setRecipient({ ...recipient, state: e.target.value })} />
            <input placeholder="ZIP" value={recipient.zip} onChange={e => setRecipient({ ...recipient, zip: e.target.value })} />
          </fieldset>
          <button onClick={handleDraft} disabled={busy || !letterText}>Save draft</button>
        </section>
      )}

      {letter && letter.mailStatus === 'not_sent' && (
        <section>
          <h3>Draft saved</h3>
          <pre>{letter.letterText}</pre>
          <button onClick={handleQuote} disabled={busy}>Get quote from LetterStream</button>
        </section>
      )}

      {letter && letter.mailStatus === 'quoted' && (
        <section>
          <h3>Ready to mail</h3>
          <p>Quoted cost: ${((letter.quotedCostCents ?? 0) / 100).toFixed(2)} {letter.quoteCurrency}</p>
          <button onClick={() => setShowConfirm(true)} className="danger">Review & mailâ€¦</button>
        </section>
      )}

      {letter && letter.authorized && (
        <section>
          <h3>Mailed <MailStatusBadge status={letter.mailStatus} /></h3>
          <p>Tracking ID: {letter.letterstreamTrackingId}</p>
          <p>Last updated: {letter.mailStatusUpdatedAt}</p>
          <button onClick={handleRefreshStatus} disabled={busy}>Refresh status</button>
        </section>
      )}

      {showConfirm && letter && (
        <ConfirmMailModal letter={letter} onConfirm={handleConfirmed} onCancel={() => setShowConfirm(false)} />
      )}
    </div>
  );
}
