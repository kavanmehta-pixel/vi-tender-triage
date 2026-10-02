"""
Railway cron entrypoint: build and send the Friday tenders digest, then exit.

Deploy as a SECOND Railway service on the same repo:
  Start command:  python send_digest.py
  Cron schedule:  0 1 * * 5     (01:00 UTC Friday = 12:00pm AEDT / 11:00am AEST)
  Variables:      DATABASE_URL (reference the Postgres service), SMTP_USER,
                  SMTP_PASS, DIGEST_TO, DIGEST_FROM (optional), DASHBOARD_URL (optional)
"""
import sys
import db as dbx
import digest


def main():
    conn = dbx.connect()
    try:
        ok, detail = digest.send(conn)
    finally:
        conn.close()
    print(("OK: " if ok else "NOT SENT: ") + detail)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
