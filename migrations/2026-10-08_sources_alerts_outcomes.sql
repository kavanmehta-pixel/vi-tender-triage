-- vi-tender-triage migration, 8 Oct 2026
-- Source tagging, email-alert ingest, outcome tracking.
--
-- The app applies exactly this on boot (app.migrate_sources_and_outcomes), so you
-- do NOT need to run it by hand. It is here so the change is reviewable and can be
-- applied ahead of the deploy if preferred:
--   psql "$DATABASE_URL" -f migrations/2026-10-08_sources_alerts_outcomes.sql
-- Every statement is idempotent and additive: no column is dropped or rewritten,
-- existing triage decisions, comments and scores are untouched.

BEGIN;

-- Where each tender came from.
--   source   = channel that FIRST reported it: 'scrape' | 'alert' | 'manual'
--   seen_via = every channel that has reported it, comma-separated, e.g. 'scrape,alert'
ALTER TABLE projects ADD COLUMN IF NOT EXISTS source           TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS seen_via         TEXT;
-- Portal identity, used to match an alert to a scraped row (and vice versa)
ALTER TABLE projects ADD COLUMN IF NOT EXISTS portal           TEXT;   -- e.g. qtenders, buynsw, vendorpanel
ALTER TABLE projects ADD COLUMN IF NOT EXISTS portal_ref       TEXT;   -- the portal's own tender reference
ALTER TABLE projects ADD COLUMN IF NOT EXISTS url_norm         TEXT;   -- normalised link (no tracking params / Safe Links)
ALTER TABLE projects ADD COLUMN IF NOT EXISTS alert_message_id TEXT;   -- Message-ID of the alert that created the row

-- Outcome tracking on the triage record
ALTER TABLE triage ADD COLUMN IF NOT EXISTS value          TEXT;  -- whole AUD, digits only ('1200000')
ALTER TABLE triage ADD COLUMN IF NOT EXISTS outcome_reason TEXT;  -- why we won / lost
ALTER TABLE triage ADD COLUMN IF NOT EXISTS submitted_at   TEXT;  -- first time status reached Submitted or later
ALTER TABLE triage ADD COLUMN IF NOT EXISTS outcome_at     TEXT;  -- when status became Won / Lost

-- One row per alert email received (idempotency on Message-ID + audit log)
CREATE TABLE IF NOT EXISTS alert_messages (
    id            SERIAL PRIMARY KEY,
    message_id    TEXT UNIQUE,
    received_at   TEXT,
    sender        TEXT,
    subject       TEXT,
    portal        TEXT,
    via           TEXT,          -- graph | imap | email | paste
    items_parsed  INTEGER,
    items_new     INTEGER,
    items_matched INTEGER,
    fallback      INTEGER DEFAULT 0   -- 1 = format not recognised, built from subject line
);

CREATE INDEX IF NOT EXISTS idx_projects_portal_ref ON projects(portal, portal_ref);
CREATE INDEX IF NOT EXISTS idx_projects_url_norm   ON projects(url_norm);
CREATE INDEX IF NOT EXISTS idx_projects_source     ON projects(source);

-- Everything that predates this change came from John's weekly scrape.
UPDATE projects SET source   = 'scrape' WHERE source   IS NULL OR source   = '';
UPDATE projects SET seen_via = 'scrape' WHERE seen_via IS NULL OR seen_via = '';

COMMIT;
