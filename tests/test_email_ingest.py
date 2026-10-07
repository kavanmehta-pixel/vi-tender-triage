"""Parser tests against the alert fixtures in tests/fixtures.

The fixtures are SYNTHETIC - written from each portal's known alert layout, not
captured from real inboxes. Once tenders@ is live, drop real .eml files in here
(redact nothing sensitive is in them, they are public tender notices) and add a
case per portal; that is the real acceptance test for the parser.

Run:  python -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import email_ingest as ei  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def parse(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return ei.parse(ei.message_from_raw(f.read()))


class ParserTests(unittest.TestCase):
    def test_qtenders_multi_item_html(self):
        r = parse("qtenders_multi.eml")
        self.assertEqual(r["portal"], "qtenders")
        self.assertEqual(len(r["items"]), 2)          # unsubscribe/login links ignored
        a, b = r["items"]
        self.assertIn("Solar CCTV Trailers", a["title"])
        self.assertEqual(a["portal_ref"], "TMR-2026-1187")
        self.assertEqual(a["closing_date"], "2026-10-28")
        self.assertEqual(a["buyer"], "Department of Transport and Main Roads")
        self.assertEqual(b["portal_ref"], "MBRC-RFQ-0457")   # fields don't bleed across items
        self.assertEqual(b["buyer"], "Moreton Bay City Council")
        self.assertEqual(b["closing_date"], "2026-11-12")    # dd/mm/yyyy, AU order
        self.assertEqual(a["location"], "QLD-AU")

    def test_buynsw_generic_link_text_uses_labelled_title(self):
        it = parse("buynsw_single.eml")["items"][0]
        self.assertTrue(it["title"].startswith("Temporary site security"))
        self.assertEqual(it["portal_ref"], "WSA.2026.0142")
        self.assertEqual(it["closing_date"], "2026-11-03")
        self.assertNotIn("utm_source", it["url_norm"])     # tracking stripped for dedupe

    def test_plain_text_fields_before_url(self):
        r = parse("tenderswa_plain.eml")
        self.assertEqual([i["portal_ref"] for i in r["items"]], ["CTY12/2026", "DTMI0457/2026"])
        self.assertEqual(r["items"][1]["buyer"], "Department of Transport and Major Infrastructure")
        self.assertTrue(r["items"][1]["title"].startswith("Traffic Count"))

    def test_gets_nz(self):
        it = parse("gets_nz.eml")["items"][0]
        self.assertEqual((it["portal"], it["portal_ref"], it["location"]), ("gets", "WDC-26-088", "NZ"))

    def test_forwarded_vendorpanel_with_safelinks(self):
        r = parse("vendorpanel_forwarded.eml")
        it = r["items"][0]
        self.assertEqual(r["portal"], "vendorpanel")          # detected from body, not sender
        self.assertTrue(it["url"].startswith("https://www.vendorpanel.com.au/"))  # Safe Links unwrapped
        self.assertEqual(it["portal_ref"], "VP513223")
        self.assertEqual(it["buyer"], "Snowy Monaro Regional Council")
        self.assertEqual(it["closing_date"], "2026-08-05")

    def test_unknown_format_never_dropped(self):
        r = parse("unknown_format.eml")
        self.assertEqual(len(r["items"]), 1)
        self.assertTrue(r["items"][0]["fallback"])
        self.assertIn("Royal Adelaide Show", r["items"][0]["title"])
        self.assertEqual(r["items"][0]["closing_date"], "2026-12-02")

    def test_url_normalisation(self):
        a = ei.normalise_url("https://www.Tenders.WA.gov.au/x/display.do?id=5&utm_source=e#top")
        b = ei.normalise_url("http://tenders.wa.gov.au/x/display.do/?id=5")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
