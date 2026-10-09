"""
Inbox poller: read new tender alert emails from the shared tenders@ mailbox and
ingest them into the triage DB. Runs as a Railway cron service, then exits.

Deploy (second cron service on this repo, like send_digest.py):
  Start command:  python poll_inbox.py
  Cron schedule:  7 * * * *          (hourly, a few minutes past the hour)
  Variables:      DATABASE_URL (reference the Postgres service) + ONE mailbox mode:

  Mode A - Microsoft 365 (tenders@visioni.com.au), recommended:
    GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET
    ALERT_MAILBOX=tenders@visioni.com.au
    ALERT_DONE_FOLDER=Ingested        (optional; mail is moved here after ingest)
    The Entra app needs application permission Mail.ReadWrite, ideally scoped to
    this one mailbox with an Exchange application access policy (Jon Maloney).

  Mode B - any IMAP mailbox (fallback, e.g. a Google Workspace inbox):
    IMAP_HOST, IMAP_USER, IMAP_PASSWORD, IMAP_FOLDER (default INBOX)

Each email is ingested once (de-duplicated on Message-ID), then marked read.
Credentials only ever come from environment variables - never commit them.
"""
import imaplib
import json
import os
import sys
import urllib.parse
import urllib.request

import db as dbx
import email_ingest
import app as triage_app   # runs migrations, provides ingest_alert_message

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_PER_RUN = int(os.environ.get("ALERT_MAX_PER_RUN", "100"))


def _http(method, url, token=None, data=None, headers=None, raw=False):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
        else:
            body = data if isinstance(data, bytes) else data.encode()
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = r.read()
        return payload if raw else (json.loads(payload) if payload else {})


def graph_token():
    tenant = os.environ["GRAPH_TENANT_ID"]
    form = urllib.parse.urlencode({
        "client_id": os.environ["GRAPH_CLIENT_ID"],
        "client_secret": os.environ["GRAPH_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    })
    res = _http("POST", f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
    return res["access_token"]


def _graph_folder_id(token, mbx, name):
    q = urllib.parse.quote(f"displayName eq '{name}'")
    res = _http("GET", f"{GRAPH}/users/{mbx}/mailFolders?$filter={q}", token)
    if res.get("value"):
        return res["value"][0]["id"]
    return _http("POST", f"{GRAPH}/users/{mbx}/mailFolders", token, {"displayName": name})["id"]


def poll_graph(conn):
    mbx = urllib.parse.quote(os.environ.get("ALERT_MAILBOX", "tenders@visioni.com.au"))
    token = graph_token()
    done_folder = os.environ.get("ALERT_DONE_FOLDER", "")
    done_id = _graph_folder_id(token, mbx, done_folder) if done_folder else None
    url = (f"{GRAPH}/users/{mbx}/mailFolders/inbox/messages"
           f"?$filter=isRead%20eq%20false&$top=25&$select=id,subject,internetMessageId")
    stats = {"emails": 0, "new": 0, "matched": 0, "duplicates": 0, "errors": 0}
    while url and stats["emails"] < MAX_PER_RUN:
        page = _http("GET", url, token)
        for m in page.get("value", []):
            try:
                mime = _http("GET", f"{GRAPH}/users/{mbx}/messages/{m['id']}/$value", token, raw=True)
                res = triage_app.ingest_alert_message(conn, email_ingest.message_from_raw(mime), via="graph")
                _tally(stats, res)
                _http("PATCH", f"{GRAPH}/users/{mbx}/messages/{m['id']}", token, {"isRead": True})
                if done_id:
                    _http("POST", f"{GRAPH}/users/{mbx}/messages/{m['id']}/move", token,
                          {"destinationId": done_id})
            except Exception as e:  # one bad email must not block the rest
                conn.rollback()
                stats["errors"] += 1
                print(f"ERROR on '{m.get('subject')}': {e}", file=sys.stderr)
        url = page.get("@odata.nextLink")
    return stats


def poll_imap(conn):
    stats = {"emails": 0, "new": 0, "matched": 0, "duplicates": 0, "errors": 0}
    box = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"])
    box.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
    box.select(os.environ.get("IMAP_FOLDER", "INBOX"))
    _, data = box.search(None, "UNSEEN")
    for num in (data[0].split() if data and data[0] else [])[:MAX_PER_RUN]:
        try:
            _, msgdata = box.fetch(num, "(RFC822)")
            res = triage_app.ingest_alert_message(conn, email_ingest.message_from_raw(msgdata[0][1]), via="imap")
            _tally(stats, res)
            box.store(num, "+FLAGS", "\\Seen")
        except Exception as e:
            conn.rollback()
            stats["errors"] += 1
            print(f"ERROR on message {num!r}: {e}", file=sys.stderr)
    box.logout()
    return stats


def _tally(stats, res):
    stats["emails"] += 1
    if res.get("duplicate_email"):
        stats["duplicates"] += 1
    stats["new"] += res.get("new", 0)
    stats["matched"] += res.get("matched", 0)


def main():
    conn = dbx.connect()
    try:
        if os.environ.get("GRAPH_CLIENT_ID"):
            stats = poll_graph(conn)
        elif os.environ.get("IMAP_HOST"):
            stats = poll_imap(conn)
        else:
            print("No mailbox configured: set GRAPH_* (Microsoft 365) or IMAP_* variables.")
            sys.exit(1)
    finally:
        conn.close()
    print("poll_inbox: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    sys.exit(1 if stats["errors"] and not stats["emails"] else 0)


if __name__ == "__main__":
    main()
