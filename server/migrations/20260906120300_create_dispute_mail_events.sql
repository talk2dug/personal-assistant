-- Append-only audit trail for everything that happens to a dispute letter,
-- especially the confirm/authorize steps around real postage.
CREATE TABLE IF NOT EXISTS dispute_mail_events (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dispute_letter_id  uuid NOT NULL REFERENCES dispute_letters(id) ON DELETE CASCADE,
  event_type         text NOT NULL CHECK (event_type IN (
    'draft_created', 'quote_received', 'user_confirmed', 'authorize_requested',
    'authorized', 'authorize_failed', 'tracking_update', 'cancelled'
  )),
  event_payload      jsonb,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dispute_mail_events_letter ON dispute_mail_events (dispute_letter_id, created_at);
