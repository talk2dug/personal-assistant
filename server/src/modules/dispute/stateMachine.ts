import { DisputeState } from './types';

const ALLOWED_TRANSITIONS: Record<DisputeState, DisputeState[]> = {
  drafted: ['mailed'],
  mailed: ['resolved'],
  // Allow reopening for a follow-up round if the bureau's response didn't
  // fully resolve the item.
  resolved: ['drafted'],
};

export function canTransitionDispute(from: DisputeState, to: DisputeState): boolean {
  return ALLOWED_TRANSITIONS[from]?.includes(to) ?? false;
}

export class InvalidDisputeTransitionError extends Error {
  constructor(from: DisputeState, to: DisputeState) {
    super(`Cannot move dispute from '${from}' to '${to}'`);
    this.name = 'InvalidDisputeTransitionError';
  }
}

export function assertCanTransitionDispute(from: DisputeState, to: DisputeState): void {
  if (!canTransitionDispute(from, to)) {
    throw new InvalidDisputeTransitionError(from, to);
  }
}
