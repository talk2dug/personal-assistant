export interface LetterStreamRecipient {
  name: string;
  addressLines: string[];
}

export interface SendMailInput {
  recipient: LetterStreamRecipient;
  senderName: string;
  senderAddressLines: string[];
  body: string;
  /** Idempotency key so retries don't create duplicate drafts/quotes. */
  idempotencyKey: string;
}

export interface SendMailResult {
  mailId: string;
  quotedCostCents: number;
  currency: string;
}

export interface AuthorizeMailInput {
  mailId: string;
  idempotencyKey: string;
}

export interface AuthorizeMailResult {
  mailId: string;
  authorizedAt: string;
  trackingNumber: string | null;
}

export interface TrackMailInput {
  mailId: string;
}

export interface TrackMailResult {
  mailId: string;
  status: string; // provider's raw status string
  trackingNumber: string | null;
  lastUpdatedAt: string;
}

export interface LetterStreamClient {
  /** Draft + quote only. Never puts anything in the mail. */
  sendMail(input: SendMailInput): Promise<SendMailResult>;
  /** Commits real postage. Must only be called after a hard confirm. */
  authorizeMail(input: AuthorizeMailInput): Promise<AuthorizeMailResult>;
  /** Read-only status check for something already mailed. */
  trackMail(input: TrackMailInput): Promise<TrackMailResult>;
}
