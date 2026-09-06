import React from 'react';
import { Link } from 'react-router-dom';
import type { DisputeItem } from '../types';

const BUREAU_LABELS: Record<string, string> = {
  equifax: 'Equifax',
  experian: 'Experian',
  transunion: 'TransUnion',
};

export default function DisputeCard({ item }: { item: DisputeItem }) {
  return (
    <Link to={`/credit/disputes/${item.id}`} className="dispute-card">
      <span className="bureau-tag">{BUREAU_LABELS[item.bureau] || item.bureau}</span>
      <strong>{item.creditorName}</strong>
      <p>{item.itemDescription}</p>
      {item.state === 'resolved' && item.resolutionOutcome && (
        <span className={`outcome outcome-${item.resolutionOutcome}`}>{item.resolutionOutcome}</span>
      )}
    </Link>
  );
}
