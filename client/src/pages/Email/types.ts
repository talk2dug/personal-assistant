export interface EmailSummary {
  id: string;
  threadId: string;
  from: string;
  to: string[];
  subject: string;
  snippet: string;
  date: string; // ISO 8601
  isRead: boolean;
  hasAttachments?: boolean;
}

export interface EmailMessage extends EmailSummary {
  body: string; // plain text (rendered as-is, one <p> per line)
  cc?: string[];
  bcc?: string[];
}

export interface ComposeDraft {
  to: string[];
  cc?: string[];
  bcc?: string[];
  subject: string;
  body: string;
}
