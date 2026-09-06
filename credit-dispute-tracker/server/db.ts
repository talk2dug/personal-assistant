import Database from 'better-sqlite3';
import path from 'path';
import fs from 'fs';

const DATA_DIR = process.env.DASHBOARD_DATA_DIR || path.join(process.cwd(), 'data');
const DB_PATH = process.env.DASHBOARD_DB_PATH || path.join(DATA_DIR, 'dashboard.db');

let db: Database.Database | null = null;

/**
 * Returns a singleton connection to the dashboard's SQLite database.
 *
 * NOTE: If earlier phases of this dashboard already open a shared db
 * connection elsewhere (e.g. a central `server/db.ts`), prefer
 * importing that connection instead of opening a second one here.
 * This module is written defensively (CREATE TABLE IF NOT EXISTS) so
 * it's safe to point at an existing dashboard.db file via
 * DASHBOARD_DB_PATH.
 */
export function getDb(): Database.Database {
  if (db) return db;
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  db = new Database(DB_PATH);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  initSchema(db);
  return db;
}

function initSchema(db: Database.Database) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS credit_score_entries (
      id TEXT PRIMARY KEY,
      recorded_at TEXT NOT NULL,       -- ISO date the score is "as of"
      score INTEGER NOT NULL CHECK (score BETWEEN 300 AND 900),
      bureau TEXT NOT NULL CHECK (bureau IN ('equifax','experian','transunion','other')),
      source TEXT,                      -- e.g. "Chase Credit Journey", "manual pull"
      notes TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_credit_score_recorded_at
      ON credit_score_entries (recorded_at);

    CREATE TABLE IF NOT EXISTS dispute_items (
      id TEXT PRIMARY KEY,
      bureau TEXT NOT NULL CHECK (bureau IN ('equifax','experian','transunion')),
      creditor_name TEXT NOT NULL,
      account_number_last4 TEXT,
      item_description TEXT NOT NULL,
      dispute_reason TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'drafted'
        CHECK (state IN ('drafted','mailed','resolved')),
      resolution_outcome TEXT
        CHECK (resolution_outcome IN (NULL,'removed','updated','verified','no_change')),
      resolution_notes TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_dispute_items_bureau_state
      ON dispute_items (bureau, state);
  `);
}

export function nowIso(): string {
  return new Date().toISOString();
}
