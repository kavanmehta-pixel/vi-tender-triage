"""End-to-end: alert email -> dashboard row; scrape adopts the alert row instead
of duplicating it; a tender can be marked Won/Lost with value and reason.

Runs against SQLite by default. To run against Postgres set DATABASE_URL.
Run:  python -m unittest discover tests
"""
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if not os.environ.get("DATABASE_URL"):
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("APP_PASSWORD", "test-pass")
os.environ.setdefault("INGEST_TOKEN", "test-ingest")
os.environ["COOKIE_INSECURE"] = "1"
import app as triage  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
HEADER = ("project_name,customer,location,asset_type,sector,stage,year,source_url,socI_relevance,"
          "opportunity_type,priority_score,signal_summary,notes,active\n")


def csv_row(name, customer, url, summary="", stage="Open"):
    q = lambda v: '"' + v.replace('"', '""') + '"'
    return ",".join(q(x) for x in [name, customer, "QLD-AU", "mobile_rapid_deploy_cctv", "transport",
                                   stage, "2026", url, "", "Open Tender (RFT)", "60", summary, "", "true"]) + "\n"


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = triage.app.test_client()
        r = cls.c.post("/login", data={"name": "Tester", "password": os.environ["APP_PASSWORD"]})
        assert r.status_code == 302, r.status_code

    def post_eml(self, name):
        with open(os.path.join(FIX, name), "rb") as f:
            return self.c.post("/api/ingest/email", data=f.read(), content_type="message/rfc822",
                               headers={"X-Ingest-Token": os.environ["INGEST_TOKEN"]}).get_json()

    def test_1_alert_lands_on_dashboard(self):
        r = self.post_eml("qtenders_multi.eml")
        self.assertTrue(r["ok"])
        self.assertEqual((r["items"], r["new"]), (2, 2))
        rows = {p["project_name"]: p for p in self.c.get("/api/projects").get_json()}
        p = rows["Supply of Mobile Solar CCTV Trailers for Road Works Sites"]
        self.assertEqual((p["source"], p["seen_via"], p["portal_ref"]), ("alert", "alert", "TMR-2026-1187"))
        self.assertEqual(p["closing_date"], "2026-10-28")
        self.assertNotEqual(p["verdict"], "PASS")      # solar + mobile + traffic angle
        new = [x["project_name"] for x in self.c.get("/api/projects/new").get_json()]
        self.assertIn(p["project_name"], new)            # shows in New this week

    def test_2_same_email_twice_is_harmless(self):
        r = self.post_eml("qtenders_multi.eml")
        self.assertTrue(r.get("duplicate_email"))

    def test_3_scrape_adopts_alert_row(self):
        url = "https://qtenders.epw.qld.gov.au/qtenders/tender/display/tender-details.do?action=display&id=51240"
        body = HEADER + csv_row("Moreton Bay hooning & dumping cameras (RFQ)", "Moreton Bay City Council", url,
                                "Council seeks AI cameras to detect hooning and illegal dumping at remote reserves.")
        r = self.c.post("/api/upload", data={"a_projects": (io.BytesIO(body.encode()), "a.csv")},
                        content_type="multipart/form-data").get_json()
        self.assertEqual(r["all_active_projects"]["new_projects"], 0)   # adopted, not duplicated
        rows = [p for p in self.c.get("/api/projects").get_json() if p.get("portal_ref") == "MBRC-RFQ-0457"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["seen_via"]), ("alert", "scrape,alert"))
        self.assertIn("hooning", rows[0]["signal_summary"])           # richer scrape text kept

    def test_4_alert_after_scrape_is_matched_not_new(self):
        url = "https://www.tenders.wa.gov.au/watender/tender/display/tender-details.do?id=60019"
        body = HEADER + csv_row("Great Northern Hwy traffic count services", "DTMI", url)
        self.c.post("/api/upload", data={"a_projects": (io.BytesIO(body.encode()), "a.csv")},
                    content_type="multipart/form-data")
        r = self.post_eml("tenderswa_plain.eml")
        self.assertEqual((r["new"], r["matched"]), (1, 1))

    def test_5_paste_and_manual(self):
        with open(os.path.join(FIX, "gets_nz.eml")) as f:
            text = f.read().split("\n\n", 1)[1]
        r = self.c.post("/api/ingest/paste", json={"text": text}).get_json()
        self.assertEqual((r["ok"], r["new"], r["portal"]), (True, 1, "gets"))
        r = self.c.post("/api/ingest/manual", json={"title": "Laing O'Rourke SA site security partners",
                                                     "buyer": "Laing O'Rourke", "location": "SA-AU",
                                                     "description": "Remote construction compounds, solar CCTV"}).get_json()
        self.assertEqual(r["status"], "new")
        p = [x for x in self.c.get("/api/projects").get_json() if x["project_hash"] == r["project_hash"]][0]
        self.assertEqual(p["source"], "manual")

    def test_6_won_lost_with_value_and_reason(self):
        h = [p for p in self.c.get("/api/projects").get_json() if p.get("portal_ref") == "TMR-2026-1187"][0]["project_hash"]
        self.c.post(f"/api/triage/{h}", json={"decision": "full", "status": "Submitted", "value": "$1.2m"})
        r = self.c.post(f"/api/triage/{h}", json={"status": "Won", "outcome_reason": "Solar fleet + 24/7 monitoring"}).get_json()
        self.assertEqual(r["value"], "1200000")                       # value kept from earlier save
        self.assertTrue(r["submitted_at"] and r["outcome_at"])
        t = self.c.get(f"/api/triage/{h}").get_json()["triage"]
        self.assertEqual((t["decision"], t["status"], t["outcome_reason"]), ("full", "Won", "Solar fleet + 24/7 monitoring"))
        rep = self.c.get("/api/report").get_json()
        self.assertEqual(rep["matrix"]["full"]["Won"], 1)
        self.assertEqual(rep["matrix"]["full"]["won_value"], 1200000)
        self.assertEqual(rep["matrix"]["full"]["win_rate"], 100)
        self.assertEqual(self.c.get("/report").status_code, 200)
        pipe = [p for p in self.c.get("/api/pipeline").get_json() if p["project_hash"] == h][0]
        self.assertEqual((pipe["value"], pipe["source"]), ("1200000", "alert"))

    def test_7_coverage_counts_scrape_gaps(self):
        rep = self.c.get("/api/report").get_json()
        wk = rep["coverage"][0]
        self.assertGreaterEqual(wk["alert_only"], 1)   # e.g. the GETS + Joondalup tenders
        self.assertGreaterEqual(wk["both"], 2)

    def test_8_token_enforced_when_set(self):
        anon = triage.app.test_client()          # not signed in: the poller's path
        prev = os.environ.get("INGEST_TOKEN")
        os.environ["INGEST_TOKEN"] = "s3cret"
        try:
            r = anon.post("/api/ingest/email", json={"subject": "x", "text": "y"})
            self.assertEqual(r.status_code, 403)
            r = anon.post("/api/ingest/email", json={"subject": "x", "text": "y"}, query_string={"token": "s3cret"})
            self.assertEqual(r.status_code, 403)   # query-string tokens no longer accepted
            r = anon.post("/api/ingest/email", json={"subject": "Tender alert: CCTV hire", "text": "camera hire"},
                          headers={"X-Ingest-Token": "s3cret"})
            self.assertEqual(r.status_code, 200)
            del os.environ["INGEST_TOKEN"]
            r = anon.post("/api/ingest/email", json={"subject": "x", "text": "y"})
            self.assertEqual(r.status_code, 403)   # unset token fails closed
        finally:
            os.environ["INGEST_TOKEN"] = prev or "test-ingest"

    def test_9_digest_still_renders(self):
        self.assertEqual(self.c.get("/digest/preview").status_code, 200)


if __name__ == "__main__":
    unittest.main()
