import { describe, it, expect, beforeAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

// Point the module at a throwaway sqlite file for this test run.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dispute-test-'));
process.env.DASHBOARD_DATA_DIR = tmpDir;
process.env.DASHBOARD_DB_PATH = path.join(tmpDir, 'test.db');

import { createDisputeItem, transitionDisputeState, InvalidTransitionError } from '../models/dispute';

describe('dispute state machine', () => {
  it('only allows drafted -> mailed -> resolved, in order', () => {
    const item = createDisputeItem({
      bureau: 'experian',
      creditorName: 'Acme Card',
      itemDescription: 'Balance reported after payoff',
      disputeReason: 'Paid in full, still shows balance',
    });
    expect(item.state).toBe('drafted');

    expect(() => transitionDisputeState(item.id, 'resolved', { resolutionOutcome: 'removed' }))
      .toThrow(InvalidTransitionError);

    const mailed = transitionDisputeState(item.id, 'mailed');
    expect(mailed.state).toBe('mailed');

    expect(() => transitionDisputeState(item.id, 'drafted')).toThrow(InvalidTransitionError);

    const resolved = transitionDisputeState(item.id, 'resolved', { resolutionOutcome: 'removed' });
    expect(resolved.state).toBe('resolved');

    expect(() => transitionDisputeState(item.id, 'mailed')).toThrow(InvalidTransitionError);
  });

  it('requires a resolutionOutcome to resolve', () => {
    const item = createDisputeItem({
      bureau: 'transunion',
      creditorName: 'Beta Bank',
      itemDescription: 'Duplicate collection account',
      disputeReason: 'Same debt listed twice',
    });
    transitionDisputeState(item.id, 'mailed');
    expect(() => transitionDisputeState(item.id, 'resolved')).toThrow();
  });
});
