-- Phase 5: manual credit score entries (no live bureau feed)
CREATE TABLE IF NOT EXISTS credit_scores (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       text NOT NULL,
  bureau        text NOT NULL CHECK (bureau IN ('equifax', 'experian', 'transunion', 'other')),
  score         int  NOT NULL CHECK (score BETWEEN 300 AND 850),
  score_model   text,
  recorded_on   date NOT NULL,
  source        text NOT NULL DEFAULT 'manual',
  notes         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_scores_user_bureau_date
  ON credit_scores (user_id, bureau, recorded_on DESC);
