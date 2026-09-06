CREATE TABLE IF NOT EXISTS disputes (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           text NOT NULL,
  bureau            text NOT NULL CHECK (bureau IN ('equifax', 'experian', 'transunion')),
  creditor_name     text NOT NULL,
  account_reference text,
  item_description  text NOT NULL,
  dispute_reason    text NOT NULL CHECK (
    dispute_reason IN ('not_mine', 'incorrect_balance', 'paid_in_full', 'duplicate', 'other')
  ),
  state             text NOT NULL DEFAULT 'drafted' CHECK (state IN ('drafted', 'mailed', 'resolved')),
  resolution        text CHECK (resolution IN ('removed', 'updated', 'verified_accurate', 'no_response')),
  resolution_notes  text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_disputes_user_state ON disputes (user_id, state);
CREATE INDEX IF NOT EXISTS idx_disputes_user_bureau ON disputes (user_id, bureau);
