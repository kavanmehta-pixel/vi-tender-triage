"""9 Oct 2026: sign-in, one data source for All vs New, closing dates, lanes,
BDM allocation emails, Monday brief, maybes, merge keeping decisions, repair."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if not os.environ.get("DATABASE_URL"):
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t9.db")
os.environ.setdefault("APP_PASSWORD", "test-pass")
os.environ.setdefault("INGEST_TOKEN", "test-ingest")
os.environ["COOKIE_INSECURE"] = "1"
import app as triage  # noqa: E402
import routing  # noqa: E402

HEADER = ("project_name,customer,location,asset_type,sector,stage,year,source_url,socI_relevance,"
          "opportunity_type,priority_score,signal_summary,notes,active\n")


def row(name, cust, loc, stage, url, summary="", notes="", opp="Open Tender (RFT)"):
    q = lambda v: '"' + v.replace('"', '""') + '"'
    return ",".join(q(x) for x in [name, cust, loc, "mobile_rapid_deploy_cctv", "energy", stage, "2026",
                                   url, "", opp, "60", summary, notes, "true"]) + "\n"


class Oct9(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = triage.app.test_client()
        cls.c.post("/login", data={"name": "Kavan", "password": os.environ["APP_PASSWORD"]})
        csv = HEADER + "".join([
            row("Lambruk Solar Farm - Security", "Venn Energy", "NSW-AU", "Open work package - closes 31 Dec 2030",
                "https://example.org/lambruk", "Security package for an off-grid solar farm, CCTV",
                notes="Comprehensive scrape 8 Oct 2026"),
            row("Big Stadium Build", "GIICA", "QLD-AU", "Builder selected 30 Sep 2026",
                "https://example.org/stadium", "A$3.6bn stadium, construction from 2027", opp="Major project"),
            row("Old approval only", "Someone", "VIC-AU", "Development consent approved (11 June 2025)",
                "https://example.org/old", "solar farm, perimeter security likely", opp="Major project"),
        ])
        r = cls.c.post("/api/upload", data={"a_projects": (__import__("io").BytesIO(csv.encode()), "a.csv")},
                       content_type="multipart/form-data")
        assert r.status_code == 200, r.data

    def test_a_signed_out_is_refused(self):
        anon = triage.app.test_client()
        self.assertEqual(anon.get("/api/projects").status_code, 401)
        self.assertEqual(anon.get("/").status_code, 302)
        self.assertEqual(anon.get("/api/contacts").status_code, 401)
        self.assertEqual(anon.post("/api/merge", json={}).status_code, 401)
        self.assertEqual(anon.get("/healthz").status_code, 200)
        bad = anon.post("/login", data={"name": "x", "password": "wrong"})
        self.assertIn(b"Wrong password", bad.data)

    def test_b_api_token(self):
        anon = triage.app.test_client()
        with mock.patch.dict(os.environ, {"API_TOKEN": "tok123"}):
            self.assertEqual(anon.get("/api/pipeline", headers={"X-API-Token": "tok123"}).status_code, 200)
            self.assertEqual(anon.get("/api/pipeline", headers={"X-API-Token": "nope"}).status_code, 401)

    def test_c_cross_site_write_refused(self):
        r = self.c.post("/api/comments/abc", json={"body": "x"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_d_new_matches_all(self):
        allp = {p["project_hash"]: p for p in self.c.get("/api/projects").get_json()}
        new = self.c.get("/api/projects/new").get_json()
        self.assertTrue(new)
        for p in new:   # same fields, same values, decisions included
            self.assertEqual(p, allp[p["project_hash"]])

    def test_e_closing_dates(self):
        rows = {p["project_name"]: p for p in self.c.get("/api/projects?hide_closed=0").get_json()}
        self.assertEqual(rows["Lambruk Solar Farm - Security"]["closing_date"], "2030-12-31")
        self.assertIsNone(rows["Old approval only"]["closing_date"])      # not "2025-06-11"
        self.assertEqual(rows["Old approval only"]["is_closed"], 0)
        self.assertIsNone(triage.extract_closing_date("ICN EOI open - close date login-gated | verified 8 Oct 2026"))

    def test_f_value_region_lane(self):
        rows = {p["project_name"]: p for p in self.c.get("/api/projects").get_json()}
        st = rows["Big Stadium Build"]
        self.assertEqual((st["est_value"], st["region"], st["lane"]), (3_600_000_000, "QLD", "bdm"))
        self.assertEqual(rows["Lambruk Solar Farm - Security"]["lane"], "tender")
        self.assertEqual(routing.est_value("NZ$800m pledge"), 728_000_000)

    def test_g_allocation_email(self):
        self.c.post("/api/bdms", json={"name": "Adam", "email": "adam@example.com", "regions": "NSW,ACT"})
        h = [p for p in self.c.get("/api/projects").get_json() if p["region"] == "NSW"][0]["project_hash"]
        sent = []
        with mock.patch.object(triage.digest_mod, "smtp_configured", return_value=True), \
             mock.patch.object(triage.digest_mod, "send_mail", side_effect=lambda *a, **k: sent.append((a, k)) or (True, "ok")), \
             mock.patch.object(triage.threading, "Thread") as T:
            T.side_effect = lambda target, daemon: mock.Mock(start=target)
            r = self.c.post(f"/api/triage/{h}", json={"decision": "bdm", "owner": "Adam", "reason": "call the council"}).get_json()
            self.assertEqual(r["notified"], "emailed Adam")
            self.assertEqual(sent[0][0][2], ["adam@example.com"])
            self.assertIn("call the council", sent[0][0][1])
            r2 = self.c.post(f"/api/triage/{h}", json={"next_steps": "x"}).get_json()   # same owner: no second email
            self.assertIsNone(r2["notified"])
            self.assertEqual(len(sent), 1)
        t = self.c.get(f"/api/triage/{h}").get_json()["triage"]
        self.assertEqual((t["decision"], t["owner"], t["decided_by"]), ("bdm", "Adam", "Kavan"))

    def test_h_decided_at_only_moves_on_decision_change(self):
        h = [p for p in self.c.get("/api/projects").get_json() if p["region"] == "QLD"][0]["project_hash"]
        a = self.c.post(f"/api/triage/{h}", json={"decision": "philip"}).get_json()["decided_at"]
        b = self.c.post(f"/api/triage/{h}", json={"next_steps": "ring them"}).get_json()["decided_at"]
        self.assertEqual(a, b)
        self.assertEqual(self.c.post(f"/api/triage/{h}", json={"decision": "nonsense"}).status_code, 400)

    def test_i_brief_and_maybes_render(self):
        r = self.c.get("/brief")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Queensland", r.data)
        self.assertIn(b"$3.6bn", r.data)
        self.assertEqual(self.c.get("/maybes").status_code, 200)
        self.assertEqual(self.c.get("/settings").status_code, 200)

    def test_j_merge_keeps_both_decisions(self):
        ps = self.c.get("/api/projects?hide_closed=0").get_json()
        a, b = ps[0]["project_hash"], ps[1]["project_hash"]
        self.c.post(f"/api/triage/{a}", json={"decision": "full"})
        self.c.post(f"/api/triage/{b}", json={"decision": "philip", "reason": "keep me"})
        r = self.c.post("/api/merge", json={"canonical": a, "others": [b]}).get_json()
        self.assertTrue(r["ok"])
        notes = self.c.get(f"/api/triage/{a}").get_json()["comments"]
        self.assertTrue(any("keep me" in n["body"] for n in notes))
        self.assertNotIn(b, [p["project_hash"] for p in self.c.get("/api/pipeline").get_json()])
        self.c.post("/api/hide", json={"hash": b, "hidden": False})   # undo for other tests

    def test_k_xss_inputs_rejected(self):
        r = self.c.post("/api/ingest/manual", json={"title": "x", "url": "javascript:alert(1)"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/ingest/manual", json={"title": "x", "closing_date": "<img src=x onerror=1>"})
        self.assertEqual(r.status_code, 400)

    def test_l_repair_preview_and_export(self):
        self.assertEqual(self.c.get("/admin/repair").status_code, 200)
        r = self.c.post("/admin/repair").get_json()
        self.assertTrue(r["ok"])
        exp = self.c.get("/api/export")
        self.assertEqual(exp.status_code, 200)
        self.assertIn("triage", exp.get_json())


class ReviewFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = triage.app.test_client()
        cls.c.post("/login", data={"name": "Kavan", "password": os.environ["APP_PASSWORD"]})

    def test_open_redirect_blocked(self):
        anon = triage.app.test_client()
        for bad in ["//evil.com", "/\t/evil.com", "/\\evil.com", "https://evil.com"]:
            r = anon.post("/login", data={"name": "a", "password": os.environ["APP_PASSWORD"], "next": bad})
            self.assertEqual(r.headers["Location"], "/", bad)

    def test_repair_is_safe_and_idempotent(self):
        db = triage.dbx.connect()
        now = "2026-10-01T00:00:00"
        for h, name in (("canon1", "Canon"), ("dupA", "Dup A"), ("dupB", "Dup B")):
            db.execute("INSERT INTO projects (project_hash, project_name, active, first_seen_at, source) VALUES (?,?,?,?,?)",
                       (h, name, "true", now, "scrape"))
        db.execute("UPDATE projects SET merged_into='canon1', hidden=1 WHERE project_hash IN ('dupA','dupB')")
        db.execute("INSERT INTO triage (project_hash, decision, owner, next_steps, reason) VALUES ('canon1','', 'Nick', 'call', 'keep')")
        db.execute("INSERT INTO triage (project_hash, decision, reason) VALUES ('dupA','full','A')")
        db.execute("INSERT INTO triage (project_hash, decision, reason) VALUES ('dupB','philip','B')")
        db.commit(); db.close()
        r1 = self.c.post("/admin/repair").get_json()
        self.assertEqual((r1["decisions_moved"], r1["decisions_noted"]), (1, 1))
        t = self.c.get("/api/triage/canon1").get_json()
        self.assertEqual((t["triage"]["decision"], t["triage"]["owner"], t["triage"]["next_steps"], t["triage"]["reason"]),
                         ("full", "Nick", "call", "keep"))
        self.assertTrue(any("philip" in c["body"] for c in t["comments"]))
        r2 = self.c.post("/admin/repair").get_json()
        self.assertEqual((r2["decisions_moved"], r2["decisions_noted"]), (0, 0))

    def test_adopted_alert_row_not_duplicated_on_reupload(self):
        self.c.post("/api/ingest/manual", json={"title": "Unique Dumping Camera Hire", "buyer": "Shire X",
                                                "url": "https://example.org/dump-cam"})
        csv = HEADER + row("Unique Dumping Camera Hire", "Shire X", "QLD-AU", "Open", "https://example.org/dump-cam")
        for _ in range(2):
            self.c.post("/api/upload", data={"a_projects": (__import__("io").BytesIO(csv.encode()), "a.csv")},
                        content_type="multipart/form-data")
        n = [p for p in self.c.get("/api/projects?hide_closed=0").get_json() if p["project_name"] == "Unique Dumping Camera Hire"]
        self.assertEqual(len(n), 1)

    def test_owner_must_be_string_safe(self):
        h = self.c.get("/api/projects").get_json()[0]["project_hash"]
        self.assertEqual(self.c.post(f"/api/triage/{h}", json={"owner": 5}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
