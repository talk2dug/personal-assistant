import {
  LetterStreamClient,
  SendMailInput,
  SendMailResult,
  AuthorizeMailInput,
  AuthorizeMailResult,
  TrackMailInput,
  TrackMailResult,
} from './types';

/**
 * Real LetterStream binding.
 *
 * NOTE: I don't have visibility into how this environment actually exposes
 * letterstream_send_mail / letterstream_authorize_mail / letterstream_track_mail
 * (direct HTTP API vs. an internal SDK/tool bridge) -- please wire the three
 * TODOs below to whatever that mechanism turns out to be. The important
 * contract to preserve is: sendMail never mails anything, and authorizeMail
 * is the only call that spends real money -- callers in mailing.service.ts
 * already gate that behind the confirm step, so this class should stay a
 * thin, unopinionated pass-through and not add its own side effects.
 */
export class LetterStreamHttpClient implements LetterStreamClient {
  async sendMail(input: SendMailInput): Promise<SendMailResult> {
    // TODO: replace with the real letterstream_send_mail call.
    // const result = await letterstream_send_mail({ ...input });
    throw new Error(
      'LetterStreamHttpClient.sendMail is not wired to the real LetterStream API yet'
    );
  }

  async authorizeMail(input: AuthorizeMailInput): Promise<AuthorizeMailResult> {
    // TODO: replace with the real letterstream_authorize_mail call.
    // This is the ONLY method in this file that should ever cost money.
    // const result = await letterstream_authorize_mail({ ...input });
    throw new Error(
      'LetterStreamHttpClient.authorizeMail is not wired to the real LetterStream API yet'
    );
  }

  async trackMail(input: TrackMailInput): Promise<TrackMailResult> {
    // TODO: replace with the real letterstream_track_mail call.
    // const result = await letterstream_track_mail({ ...input });
    throw new Error(
      'LetterStreamHttpClient.trackMail is not wired to the real LetterStream API yet'
    );
  }
}

/**
 * In-memory mock for local dev and tests, so the confirm/authorize/track
 * flow (and its safety gate) can be exercised without hitting the real API
 * or spending real money. It still behaves like a real provider: sendMail
 * only quotes, authorizeMail is what "mails" it.
 */
export class MockLetterStreamClient implements LetterStreamClient {
  private mailIdCounter = 0;
  private mailed = new Set<string>();

  async sendMail(input: SendMailInput): Promise<SendMailResult> {
    this.mailIdCounter += 1;
    return {
      mailId: `mock-mail-${this.mailIdCounter}`,
      quotedCostCents: 512, // arbitrary flat mock quote ($5.12)
      currency: 'USD',
    };
  }

  async authorizeMail(input: AuthorizeMailInput): Promise<AuthorizeMailResult> {
    this.mailed.add(input.mailId);
    return {
      mailId: input.mailId,
      authorizedAt: new Date().toISOString(),
      trackingNumber: `MOCKTRACK-${input.mailId}`,
    };
  }

  async trackMail(input: TrackMailInput): Promise<TrackMailResult> {
    return {
      mailId: input.mailId,
      status: this.mailed.has(input.mailId) ? 'in_transit' : 'unknown',
      trackingNumber: this.mailed.has(input.mailId) ? `MOCKTRACK-${input.mailId}` : null,
      lastUpdatedAt: new Date().toISOString(),
    };
  }
}
