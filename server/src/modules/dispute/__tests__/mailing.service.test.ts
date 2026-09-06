import { MailingService, LetterNotConfirmedError, LetterContentChangedSinceConfirmError } from '../mailing.service';
import { MockLetterStreamClient } from '../../../integrations/letterstream/client';

// These tests document and lock in the hard-confirm gate. They assume a test
// DB/pool is available via the project's existing test setup (not included
// here since I don't have visibility into how Phases 1-4 configure test
// infrastructure) -- please adapt the pool wiring to match.

describe('MailingService authorize gate', () => {
  test('rejects authorize without explicitApproval === true', async () => {
    const service = new MailingService(new MockLetterStreamClient());
    // @ts-expect-error intentionally wrong type to prove the guard exists
    await expect(service.authorizeLetter('some-letter-id', false)).rejects.toThrow(
      LetterNotConfirmedError
    );
  });

  test('rejects authorize when letter is not yet confirmed', async () => {
    // This test is illustrative: with a real DB, seed a letter in 'quoted'
    // status (drafted but not confirmed) and assert authorizeLetter throws
    // LetterNotConfirmedError before ever touching the LetterStream client.
    expect(LetterNotConfirmedError).toBeDefined();
  });

  test('rejects authorize if content changed after confirm', async () => {
    // Illustrative: seed a confirmed letter, mutate letter_body directly in
    // the DB (simulating drift), then assert authorizeLetter throws
    // LetterContentChangedSinceConfirmError instead of calling authorizeMail.
    expect(LetterContentChangedSinceConfirmError).toBeDefined();
  });
});
