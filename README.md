# VI Tender Doability Triage

Flask app that scores every tender VI could bid on against VI's moats (M1 off-grid/remote,
M2 solar, M3 monitored outcome, M4 AI analytics, M5 temp/rapid deploy, M6 traffic/transport),
tracks the triage decision through to outcome, and sends a Friday digest.

- **Web:** `gunicorn app:app` (Railway service `web`), Postgres via `DATABASE_URL`; SQLite (`DB_PATH`) for local dev only.
- **Railway builds on Python 3.11** (`nixpacks.toml`). Before pushing, check:
  `python3 -c "import ast,glob;[ast.parse(open(f).read(),feature_version=(3,11)) for f in glob.glob('*.py')]"`
- **Tests:** `python -m unittest discover tests` (SQLite), or set `DATABASE_URL` to run them on Postgres.

## Where tenders come from

Every tender row carries a **source tag**:

| Field | Meaning |
|---|---|
| `source` | the channel that **first** reported it — `scrape`, `alert` or `manual` |
| `seen_via` | every channel that has reported it, e.g. `scrape,alert` |
| `portal`, `portal_ref` | which portal and that portal's own tender reference (QTenders, buy.nsw, VendorPanel `VP513223`, …) |
| `url_norm` | link normalised for matching (Safe Links unwrapped, tracking params removed) |

| Channel | How it gets in |
|---|---|
| **Weekly scrape** | John's jarvis CSVs → `/upload` (unchanged) |
| **Email alert** | portal alerts → `tenders@visioni.com.au` → `poll_inbox.py` (hourly cron) → parser |
| **Paste** | `/upload` → *Paste an alert* (same parser, for alerts that landed in someone's own inbox) |
| **Manual** | `/upload` → *Add a tender manually* (SEN articles, tips) |

**Dedupe across channels.** An incoming tender is matched to an existing row by portal reference,
then normalised URL, then the name+buyer key. A match adds the channel to `seen_via` and keeps the
existing triage; it is never a second row. When the scrape later finds a tender that first arrived by
alert, the scrape **adopts** the alert row (richer description, same triage) instead of inserting.
Scrape rows are never auto-merged with each other — that stays on the Duplicates tab.

**Coverage.** `/report` counts, per ISO week, how many new tenders came from each channel and how many
were *missed by scrape* (reached us only by alert/manual). That number, broken down by portal, is the
weekly list for tuning John's pipeline.

### Email ingest

`email_ingest.py` parses alert emails from QTenders, buy.nsw, Tenders WA/VIC/SA/ACT/TAS/NT,
AusTender, ICN Gateway, VendorPanel, Local Buy, GETS (NZ) and the SEN newsletter: one item per
tender link (unsubscribe/login links ignored), with title, buyer, reference, closing date and link.
It handles HTML and plain-text alerts, alerts redirected by an Outlook rule, and hand-forwarded
alerts. If a format isn't recognised, one item is still created from the subject line and flagged
*check* in the alert log — alerts are never silently dropped.

Each email is ingested once (keyed on `Message-ID`, logged in `alert_messages`).

**Endpoints**

| Endpoint | Use |
|---|---|
| `POST /api/ingest/email` | raw RFC822 body, `.eml` upload (`eml`), or JSON `{subject, from, text, html, message_id}` / `{raw}`. Requires header `X-Ingest-Token: $INGEST_TOKEN` when that variable is set. |
| `POST /api/ingest/paste` | `{text}` — the upload page's paste box |
| `POST /api/ingest/manual` | `{title, buyer, url, closing_date, portal_ref, location, description, added_by}` |
| `GET /api/alerts` | last 50 alert emails with parsed / new / matched counts |

**Inbox poller** — second Railway cron service on this repo, like the digest:

```
Start command:  python poll_inbox.py
Cron schedule:  7 * * * *
Variables:      DATABASE_URL  (reference the Postgres service)
                GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET
                ALERT_MAILBOX=tenders@visioni.com.au
                ALERT_DONE_FOLDER=Ingested            (optional)
```

The Graph app registration (Entra, via Jon Maloney) needs application permission **Mail.ReadWrite**;
scope it to the one mailbox with an Exchange *application access policy* so it cannot read any other.
An IMAP fallback exists (`IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER`) if tenders@ ever
lives somewhere other than Microsoft 365.

Set `INGEST_TOKEN` on the web service as well, so only the poller (or a Power Automate flow) can call
`/api/ingest/email`. **No portal credentials, mailbox secrets or tokens are ever committed** — all of
them live in Railway variables.

How Michael and Alister route alerts to tenders@ (and the later cut-over of portal registrations):
[`docs/forwarding-rules-michael.md`](docs/forwarding-rules-michael.md).

## Scoring changes (Oct 2026)

- **Traffic / transport** is now a project archetype (road safety, traffic management/counts, speed
  and hooning, level crossings, intersections, parking, transport authorities) awarding M6 + M4, and
  the M6 text evidence is broader.
- **Ambiguity scores in** (Alex): a tender that isn't hard-killed, isn't a vertical building and isn't
  confirmed commodity, where we can see a VI angle or a security scope but not enough to be sure, is a
  **MAYBE** flagged *ambiguous — scored in for review*, not a PASS. Terse listings (alert titles,
  one-line portal entries) are treated as low-confidence, so they can't be written off as commodity.
  The remote-location penalty no longer pushes a scored-in MAYBE back to PASS.
- Effect on a recent jarvis CSV (861 active rows): PASS 298 → 263 (35 scored in), GO 123 → 143
  (mostly transport/level-crossing work picking up M6).

## Outcome tracking

The triage panel's status runs Screening → Downloading docs → Drafting response → With
Michael/Alister → **Submitted → Awaiting outcome → Won / Lost**, with two new fields:

- **Value (AUD)** — accepts `250k`, `1.2m`, `$1,200,000`; stored as whole dollars.
- **Outcome reason** — why we won or lost.

`submitted_at` is stamped the first time a tender reaches Submitted (or later), `outcome_at` when it
becomes Won/Lost. Values and reasons show on the Pipeline tab.

`/report` — decisions vs outcomes (Full tender / Philip reach-out / Pass × in progress / submitted /
won / lost, win rate, won value), the results list, what's awaiting a result, weekly coverage by
source, and the recent alert log. JSON at `/api/report`.

## Migration

`migrations/2026-10-08_sources_alerts_outcomes.sql` — additive and idempotent (new columns on
`projects` and `triage`, new `alert_messages` table, three indexes, existing rows tagged
`source='scrape'`). The app applies the same change on boot, so a normal deploy is enough; the SQL
is there for review or to apply ahead of the deploy.

## Held back

**New scrapers** (QTenders, Local Buy, LGP NSW, MAV Procurement, Procurement Australia, WALGA, LGA SA,
EstimateOne) are deliberately not built yet — per Alex (6 Oct), they wait until Brief 2's upstream
gates (vehicles → registered? → public/private) confirm which list is the right one. Until then those
channels are covered by email alerts into tenders@. When they are built: public pages only, respect
each site's terms of use, no logged-in scraping; EstimateOne in particular is login-only and should
stay on email alerts.

The Friday digest's **>$200m major projects scan** lives in the Weekly Tender Digest Cowork task,
which reads `/api/pipeline`; that endpoint only gained fields (`value`, `outcome_reason`, `source`,
`portal_ref`, …), so the task is unaffected.
