/**
 * Types for the LetterStream integration.
 *
 * ASSUMPTION / TODO: LetterStream's exact request/response field names
 * depend on the account's API version (their docs sit behind a
 * logged-in "My Account" area, not general documentation, and I don't
 * have account credentials). Field names below are a best-effort
 * mapping based on LetterStream's published API description (send
 * mail -> get a job/quote, authorize mail -> finalize + charge +
 * produce, track mail -> job status / USPS tracking). Verify against
 * the actual account docs/credentials before go-live and adjust this
 * file + client.ts -- the rest of the app only depends on this file's
 * exported types, so a mismatch stays contained to these two files.
 */

export interface LetterStreamAddress {
  name: string;
  addressLine1: string;
  addressLine2?: string;
  city: string;
  state: string;
  zip: string;
}

export interface LetterStreamSendMailRequest {
  /** Our own id for the dispute letter, echoed back for correlation. */
  externalId: string;
  to: LetterStreamAddress;
  from: LetterStreamAddress;
  /** Plain text or simple HTML body LetterStream will render to a letter. */
  documentText: string;
  mailClass: 'first_class' | 'certified' | 'certified_return_receipt';
  color: boolean;
  doubleSided: boolean;
}

/**
 * Result of letterstream_send_mail. In LetterStream's model this both
 * creates a mail job/order AND returns pricing -- it does NOT put a
 * stamp on anything. Nothing is produced or mailed until
 * letterstream_authorize_mail is called separately.
 */
export interface LetterStreamSendMailResult {
  orderId: string;
  quotedCostCents: number;
  currency: string;
  quotedAt: string;
}

export interface LetterStreamAuthorizeMailRequest {
  orderId: string;
}

/**
 * Result of letterstream_authorize_mail -- this is the step that
 * actually spends money and puts the letter into production.
 */
export interface LetterStreamAuthorizeMailResult {
  orderId: string;
  authorizedAt: string;
  trackingId: string;
}

export interface LetterStreamTrackMailResult {
  orderId: string;
  trackingId: string;
  status: 'queued' | 'in_production' | 'mailed' | 'in_transit' | 'delivered' | 'returned' | 'failed';
  lastEventAt: string;
  lastEventDescription?: string;
}
