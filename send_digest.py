"""
Railway cron entrypoint for the scheduled emails. Builds, sends, exits.

  python send_digest.py            Friday pipeline digest (DIGEST_TO)
  python send_digest.py --brief    Monday BDM email. Sends a [REVIEW] copy to
                                   BRIEF_REVIEW_TO unless BRIEF_AUTO_SEND=1, in which
                                   case it goes to the BDMs (directory + BRIEF_TO).
  python send_digest.py --maybes   Monthly maybes, one email per BDM's regions.

Suggested Railway cron services on this repo (times UTC):
  digest   0 1 * * 5        Friday 12pm AEDT
  brief    0 20 * * 0       Monday 7am AEDT
  maybes   0 21 * * 1       every Monday 8am AEDT; only sends in the first week of the
                            month (cron ORs day-of-month with day-of-week). --force overrides.
Variables: DATABASE_URL, SMTP_USER, SMTP_PASS, DIGEST_FROM, DIGEST_TO,
           BRIEF_TO, BRIEF_CC, BRIEF_REVIEW_TO, BRIEF_AUTO_SEND, DASHBOARD_URL
"""
import os
import sys

import db as dbx
import digest


def main(argv):
    mode = argv[1] if len(argv) > 1 else "--digest"
    conn = dbx.connect()
    try:
        if mode == "--digest":
            ok, detail = digest.send(conn)
        else:
            import app   # the app's loaders, so the email matches the dashboard exactly
            if mode == "--brief":
                send_mode = "all" if os.environ.get("BRIEF_AUTO_SEND") == "1" else "review"
                subject, html, _ = app.brief_mod.build_brief(app.load_projects(conn), app.list_bdms(conn))
                to, cc = app.brief_recipients(conn, send_mode)
                ok, detail = digest.send_mail(("[REVIEW] " if send_mode == "review" else "") + subject,
                                              html, to, cc=cc)
            elif mode == "--maybes":
                from datetime import datetime, timedelta
                if (datetime.utcnow() + timedelta(hours=11)).day > 7 and "--force" not in argv:
                    print("SKIPPED: monthly maybes only go out in the first week of the month")
                    sys.exit(0)
                res = app.send_maybes(conn)
                ok = bool(res) and all(r[1] for r in res)
                detail = "; ".join(f"{r[0]}: {r[2]}" for r in res) or "nothing sent"
            else:
                print("unknown mode " + mode)
                sys.exit(2)
    finally:
        conn.close()
    print(("OK: " if ok else "NOT SENT: ") + detail)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv)
