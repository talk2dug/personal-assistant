CREATE TABLE IF NOT EXISTS dispute_letters (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dispute_id            uuid NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
  letter_version        int NOT NULL,
  recipient_snapshot    jsonb NOT NULL,
  letter_body           text NOT NULL,
  letterstream_mail_id  text,
  quoted_cost_cents     int,
  quote_currency        text NOT NULL DEFAULT 'USD',
  content_hash          text,
  status                text NOT NULL DEFAULT 'draft' CHECK (status IN (
    'draft', 'quoted', 'confirmed', 'authorized', 'mailed',
    'in_transit', 'delivered', 'returned', 'failed', 'cancelled'
  )),
  confirmed_hash        text,
  confirmed_by_user_at  timestamptz,
  authorized_at         timestamptz,
  mailed_at             timestamptz,
  tracking_number       text,
  last_tracked_status   text,
  last_tracked_at       timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (dispute_id, letter_version)
);

CREATE INDEX IF NOT EXISTS idx_dispute_letters_dispute ON dispute_letters (dispute_id);
CREATE INDEX IF NOT EXISTS idx_dispute_letters_status ON dispute_letters (status);
