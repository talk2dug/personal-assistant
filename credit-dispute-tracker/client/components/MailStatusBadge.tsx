import React from 'react';
import type { MailStatus } from '../types';

const LABELS: Record<MailStatus, string> = {
  not_sent: 'Not sent',
  quoted: 'Quoted',
  mailed: 'Mailed',
  in_transit: 'In transit',
  delivered: 'Delivered',
  returned: 'Returned',
  failed: 'Failed',
};

export default function MailStatusBadge({ status }: { status: MailStatus }) {
  return <span className={`mail-status-badge mail-status-${status}`}>{LABELS[status]}</span>;
}
