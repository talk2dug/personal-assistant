import { canTransitionDispute, assertCanTransitionDispute, InvalidDisputeTransitionError } from '../stateMachine';

describe('dispute state machine', () => {
  test('drafted -> mailed is allowed', () => {
    expect(canTransitionDispute('drafted', 'mailed')).toBe(true);
  });

  test('mailed -> resolved is allowed', () => {
    expect(canTransitionDispute('mailed', 'resolved')).toBe(true);
  });

  test('resolved -> drafted (re-dispute round) is allowed', () => {
    expect(canTransitionDispute('resolved', 'drafted')).toBe(true);
  });

  test('drafted -> resolved (skipping mailed) is not allowed', () => {
    expect(canTransitionDispute('drafted', 'resolved')).toBe(false);
  });

  test('mailed -> drafted is not allowed', () => {
    expect(canTransitionDispute('mailed', 'drafted')).toBe(false);
  });

  test('assertCanTransitionDispute throws on invalid transition', () => {
    expect(() => assertCanTransitionDispute('drafted', 'resolved')).toThrow(
      InvalidDisputeTransitionError
    );
  });
});
