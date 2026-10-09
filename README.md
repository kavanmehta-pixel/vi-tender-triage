# VI Tender Doability Triage

Flask app that scores every tender VI could bid on against VI's moats (M1 off-grid/remote,
M2 solar, M3 monitored outcome, M4 AI analytics, M5 temp/rapid deploy, M6 traffic/transport),
tracks the triage decision through to outcome, and sends a Friday digest.

- **Web:** `gunicorn app:app` (Railway service `web`), Postgres via `DATABASE_URL`; SQLite (`DB_PATH`) for local dev only.
- **Railway builds on Python 3.11** (`nixpacks.toml`). Before pushing, check:
  `python3 -c "import ast,glob;[ast.parse(open(f).read(),feature_version=(3,11)) for f in glob.glob('*.py')]"`
- **Tests:** `python -m unittest discover tests` (SQLite), or set `DATABASE_URL` to run them on Postgres.

## Access (Oct 2026) - set these BEFORE deploying

Every page and API now needs sign-in. Before 9 Oct 2026 everything was public.

| Railway variable (web service) | What it does |
|---|---|
| `APP_PASSWORD` | team password. **Required**: with it unset nobody can sign in |
| `SECRET_KEY` | long random string; signs the login cookie |
| `API_TOKEN` | for scheduled jobs (Cowork digest/research): send header `X-API-Token` |
| `INGEST_TOKEN` | the inbox poller's header `X-Ingest-Token`; unset = ingest refused |
| `TEAM_NAMES` | optional, comma-separated names offered on the sign-in page |

People sign in with their name and the team password, so decisions and comments are attributed.
Links shared with someone new (e.g. Nick) need the password too.

## Lanes, BDM allocation and the sales emails (9 Oct 2026 meeting)

| Lane | Who | How it gets there |
|---|---|---|
| Tender / EOI | Kavan + Alister (with the BDM) | decision **Full tender**, or suggested when a tender/EOI has security in scope |
| Demand gen | Philip | decision **Philip reach-out**, or suggested for a tender/EOI with no security scope |
| BDM | local BDM | decision **Send to BDM** with the BDM as owner; major projects $10m+ by state |
| Maybes | BDMs, monthly | untouched MAYBEs, by state |

- **BDM directory:** `/settings`. Name, email and states for each BDM.
- **Allocation email:** setting a lead's owner to someone in the directory emails them once, straight away
  (cc `ALLOCATION_CC`). Logged at `/api/notifications`.
- **Monday email:** preview at `/brief`; send a review copy (`BRIEF_REVIEW_TO`, e.g. Michael and Nick) or
  send to all BDMs from `/settings`. Cron: `python send_digest.py --brief`.
- **Monthly maybes:** preview at `/maybes`; one email per BDM's states. Cron: `python send_digest.py --maybes`.
- **Project value** is read from the listing text (`$3.6bn`, `A$624m`, `NZ$800m`); most scraped rows don't state
  one, so the $10m filter only sees projects whose value is published.

## Data fixes (9 Oct 2026)

- **New this week = All, filtered.** Both views come from one loader (`load_projects`), so decisions,
  deadlines and counts can't disagree. Previously New used a separate query with no triage join, so a
  decision made in All didn't show in New (and autosave from New could blank its reason).
- **Closing dates.** The parser used to take *any* date in the text as the closing date (approval dates,
  award dates, "verified 8 Oct 2026"), which hid ~450 live rows as closed. Dates now count only next to a
  closing keyword, and open/closed is worked out when the page loads, not frozen at upload.
  `/admin/repair` previews and saves the corrected dates (old value kept in `closing_date_prev`).
- **Merges keep every decision.** If both rows had a decision, the dropped one is kept as a comment.
  `/admin/repair` also fixes decisions already stranded on merged rows.
- **Backups:** `/api/export` downloads every decision, comment, BDM and email log as JSON. Take one weekly
  until Railway Postgres backups are confirmed on.

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

Set `INGEST_TOKEN` on the web service as well; without it `/api/ingest/email` refuses everything. **No portal credentials, mailbox secrets or tokens are ever committed** — all of
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
