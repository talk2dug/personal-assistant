import React from 'react';
import { LetterStatus } from './types';

const LABELS: Record<LetterStatus, string> = {
  draft: 'Draft',
  quoted: 'Quoted â€” awaiting review',
  confirmed: 'Confirmed â€” not yet mailed',
  authorized: 'Authorized',
  mailed: 'Mailed',
  in_transit: 'In transit',
  delivered: 'Delivered',
  returned: 'Returned to sender',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const COLORS: Record<LetterStatus, string> = {
  draft: '#95a5a6',
  quoted: '#f39c12',
  confirmed: '#f39c12',
  authorized: '#2980b9',
  mailed: '#2980b9',
  in_transit: '#2980b9',
  delivered: '#27ae60',
  returned: '#c0392b',
  failed: '#c0392b',
  cancelled: '#7f8c8d',
};

export function MailStatusBadge({ status }: { status: LetterStatus }) {
  return (
    <span
      className="mail-status-badge"
      style={{ backgroundColor: COLORS[status], color: 'white', padding: '2px 8px', borderRadius: 8 }}
    >
      {LABELS[status]}
    </span>
  );
}
