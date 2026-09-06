import React, { useEffect, useState, useCallback } from 'react';
import { DisputeForm } from './DisputeForm';
import { DisputeList } from './DisputeList';
import { LetterDraftPanel } from './LetterDraftPanel';
import { fetchDisputes, createDispute, resolveDispute } from './api';
import { Dispute, DisputeFormValues, DisputeLetter } from './types';

// ASSUMPTION: consumer identity (name/address used on outgoing letters)
// comes from wherever the dashboard already stores profile info in
// Phases 1-4. Hardcoded placeholder here pending that wiring -- please
// replace with the real profile source.
const PLACEHOLDER_CONSUMER = {
  fullName: 'Your Name',
  addressLines: ['123 Main St', 'Anytown, ST 00000'],
};

export function DisputeTrackerPage() {
  const [disputes, setDisputes] = useState<Dispute[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [letters, setLetters] = useState<DisputeLetter[]>([]);
  const [error, setError] = useState<string | null>(null);

  const loadDisputes = useCallback(async () => {
    try {
      const list = await fetchDisputes();
      setDisputes(list);
      if (!selectedId && list.length > 0) setSelectedId(list[0].id);
    } catch (err: any) {
      setError(err.message || 'Failed to load disputes.');
    }
  }, [selectedId]);

  const loadLetters = useCallback(async () => {
    if (!selectedId) {
      setLetters([]);
      return;
    }
    // NOTE: assumes a GET /api/disputes/:id/letters list endpoint exists
    // alongside the mailing routes; wire it up server-side if not already
    // exposed (mailing.routes.ts currently only has the action endpoints).
    try {
      const res = await fetch(`/api/disputes/${selectedId}/letters`);
      if (res.ok) {
        const body = await res.json();
        setLetters(body.letters ?? []);
      }
    } catch {
      // non-fatal for this view
    }
  }, [selectedId]);

  useEffect(() => {
    loadDisputes();
  }, [loadDisputes]);

  useEffect(() => {
    loadLetters();
  }, [loadLetters]);

  const handleCreate = async (values: DisputeFormValues) => {
    await createDispute(values);
    await loadDisputes();
  };

  const handleResolve = async (id: string) => {
    const resolution = window.prompt(
      'Resolution: removed / updated / verified_accurate / no_response'
    );
    if (!resolution) return;
    await resolveDispute(id, resolution);
    await loadDisputes();
  };

  const selected = disputes.find((d) => d.id === selectedId) ?? null;

  return (
    <section className="dispute-tracker-page">
      <h1>Dispute Tracker</h1>
      {error && <p className="form-error">{error}</p>}

      <DisputeForm onSubmit={handleCreate} />

      <div className="dispute-tracker-layout">
        <DisputeList disputes={disputes} selectedId={selectedId} onSelect={setSelectedId} />

        {selected && (
          <div className="dispute-detail">
            <h2>{selected.creditorName}</h2>
            <p>{selected.bureau} â€” {selected.state}</p>
            <p>{selected.itemDescription}</p>

            {selected.state === 'mailed' && (
              <button onClick={() => handleResolve(selected.id)}>Mark resolved</button>
            )}

            <LetterDraftPanel
              dispute={selected}
              consumer={PLACEHOLDER_CONSUMER}
              letters={letters}
              onLettersChanged={loadLetters}
            />
          </div>
        )}
      </div>
    </section>
  );
}
