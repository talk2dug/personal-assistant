import React, { useState } from 'react';
import { DisputeFormValues, Bureau, DisputeReason } from './types';

const BUREAUS: Bureau[] = ['equifax', 'experian', 'transunion'];
const REASONS: { value: DisputeReason; label: string }[] = [
  { value: 'not_mine', label: "This isn't mine" },
  { value: 'incorrect_balance', label: 'Incorrect balance' },
  { value: 'paid_in_full', label: 'Paid in full' },
  { value: 'duplicate', label: 'Duplicate item' },
  { value: 'other', label: 'Other' },
];

const EMPTY: DisputeFormValues = {
  bureau: 'equifax',
  creditorName: '',
  accountReference: '',
  itemDescription: '',
  disputeReason: 'not_mine',
};

interface Props {
  onSubmit: (values: DisputeFormValues) => Promise<void>;
}

export function DisputeForm({ onSubmit }: Props) {
  const [values, setValues] = useState<DisputeFormValues>(EMPTY);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = <K extends keyof DisputeFormValues>(key: K, val: DisputeFormValues[K]) =>
    setValues((v) => ({ ...v, [key]: val }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await onSubmit(values);
      setValues(EMPTY);
    } catch (err: any) {
      setError(err.message || 'Failed to create dispute.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="dispute-form">
      <label>
        Bureau
        <select value={values.bureau} onChange={(e) => set('bureau', e.target.value as Bureau)}>
          {BUREAUS.map((b) => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
      </label>
      <label>
        Creditor / furnisher name
        <input
          type="text"
          value={values.creditorName}
          onChange={(e) => set('creditorName', e.target.value)}
          required
        />
      </label>
      <label>
        Account reference (optional)
        <input
          type="text"
          placeholder="last 4 digits, etc."
          value={values.accountReference}
          onChange={(e) => set('accountReference', e.target.value)}
        />
      </label>
      <label>
        Reason
        <select
          value={values.disputeReason}
          onChange={(e) => set('disputeReason', e.target.value as DisputeReason)}
        >
          {REASONS.map((r) => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </select>
      </label>
      <label>
        Description of the item
        <textarea
          value={values.itemDescription}
          onChange={(e) => set('itemDescription', e.target.value)}
          required
        />
      </label>
      {error && <p className="form-error">{error}</p>}
      <button type="submit" disabled={submitting}>
        {submitting ? 'Saving...' : 'Add dispute'}
      </button>
    </form>
  );
}
