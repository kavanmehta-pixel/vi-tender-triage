"""
Tender alert email parser.

Turns a portal alert email (QTenders, buy.nsw, Tenders WA, GETS, VendorPanel,
AusTender, ICN Gateway, Tenders VIC/SA/ACT/TAS/NT) into a list of tender items:

    {title, buyer, portal, portal_name, portal_ref, url, url_norm,
     closing_date, location, opportunity_type, snippet}

Pure functions only - no database access - so it can be unit-tested and reused
by the web endpoint, the inbox poller and the manual paste box.

Design notes
- Alerts arrive three ways: straight from the portal (once tenders@ is the
  registered email), redirected by an Outlook rule (original From kept), or
  forwarded by hand (portal sender only appears inside the body). Portal
  detection therefore looks at the sender, the subject, the body and the link
  domains, in that order.
- One alert often lists several tenders. Each portal detail link starts a new
  item; the text between that link and the next one is the item's block, and
  reference / buyer / closing date are read from that block.
- Microsoft 365 rewrites links through Safe Links; those are unwrapped so
  URL-based dedupe against the scrape still works.
- If nothing parses, a single fallback item is built from the subject line so
  an alert is never silently dropped (Alex: include ambiguity first).
"""
import email
import email.policy
import html as htmllib
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qs, parse_qsl, urlencode, unquote

# ── Portal registry ─────────────────────────────────────────────────────────
# domains: substrings matched against sender, link hosts and body text.
# detail:  regex a URL path+query must match to count as a tender detail link
#          (keeps login / unsubscribe / home-page links out).
# ref:     regexes for the portal's own tender reference, tried in order.
PORTALS = [
    {"key": "qtenders", "name": "QTenders", "location": "QLD-AU",
     "domains": ["qtenders.epw.qld.gov.au", "qtenders", "qld.gov.au/tenders"],
     "detail": r"(tender|search/opportunit|display|view|id=)",
     "ref": [r"\b([A-Z]{2,8}[-/ ]?\d{3,6}(?:[-/]\d{2,4})?)\b"]},
    {"key": "buynsw", "name": "buy.nsw", "location": "NSW-AU",
     "domains": ["buy.nsw.gov.au", "tenders.nsw.gov.au", "buy.nsw"],
     "detail": r"(tender|opportunit|rft|/notices?/|id=)",
     "ref": [r"\b(RF[TQPI]\s?[-#]?\s?[A-Z0-9][A-Z0-9.\-/]{3,})\b",
             r"\b([A-Za-z]{2,10}\.\d{4}\.\d{2,5})\b"]},
    {"key": "tenderswa", "name": "Tenders WA", "location": "WA-AU",
     "domains": ["tenders.wa.gov.au", "tenderswa"],
     "detail": r"(tender|display|id=|ref)",
     "ref": [r"\b([A-Z]{2,8}\d{2,6}/\d{2,4})\b", r"\b(DTMI\d+)\b"]},
    {"key": "gets", "name": "GETS (NZ)", "location": "NZ",
     "domains": ["gets.govt.nz"],
     "detail": r"(tenderdetail|externaltender|id=)",
     "ref": [r"(?:reference|ref(?:erence)?\s*(?:no|#)?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{3,})"]},
    {"key": "vendorpanel", "name": "VendorPanel", "location": "",
     "domains": ["vendorpanel.com.au", "myvendorpanel", "vendorpanel"],
     "detail": r"(opportunit|publicoffer|request|rfx|id=|vp\d)",
     "ref": [r"\b(VP\s?\d{5,7})\b"]},
    {"key": "austender", "name": "AusTender", "location": "AU",
     "domains": ["tenders.gov.au", "austender"],
     "detail": r"(/atm/show|/atm/|/cn/|/son/)",
     "ref": [r"ATM\s*ID\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9.\-/]{2,})"]},
    {"key": "icn", "name": "ICN Gateway", "location": "AU",
     "domains": ["gateway.icn.org.au", "icn.org.au"],
     "detail": r"(/project/|/work-?package/|/opportunit)",
     "ref": []},
    {"key": "tendersvic", "name": "Tenders VIC", "location": "VIC-AU",
     "domains": ["tenders.vic.gov.au", "buyingfor.vic.gov.au"],
     "detail": r"(tender|opportunit|id=)",
     "ref": [r"tender (?:number|no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-/]{3,})"]},
    {"key": "tenderssa", "name": "SA Tenders", "location": "SA-AU",
     "domains": ["tenders.sa.gov.au"], "detail": r"(tender|id=)", "ref": []},
    {"key": "tendersact", "name": "Tenders ACT", "location": "ACT-AU",
     "domains": ["tenders.act.gov.au"], "detail": r"(tender|id=)", "ref": []},
    {"key": "tenderstas", "name": "Tenders TAS", "location": "TAS-AU",
     "domains": ["tenders.tas.gov.au"], "detail": r"(tender|id=)", "ref": []},
    {"key": "tendersnt", "name": "NT Tenders", "location": "NT-AU",
     "domains": ["tendersonline.nt.gov.au", "nt.gov.au/tenders"], "detail": r"(tender|id=)", "ref": []},
    {"key": "localbuy", "name": "Local Buy", "location": "QLD-AU",
     "domains": ["localbuy.com.au"], "detail": r"(tender|opportunit|id=)", "ref": []},
    # SEN (Security & Electronics News) tender newsletters Michael/Alister get:
    # each article link becomes an item; there is no formal reference.
    {"key": "sen", "name": "SEN (news)", "location": "",
     "domains": ["sen.news"], "detail": r"/[a-z0-9]+(-[a-z0-9]+){2,}", "ref": []},
]
PORTAL_BY_KEY = {p["key"]: p for p in PORTALS}

# Link text / URLs that are never tenders.
_JUNK_LINK = re.compile(
    r"unsubscribe|preferences|manage (your )?alerts|privacy|log ?in|sign ?in|forgot|"
    r"help|contact us|terms|facebook|twitter|linkedin|youtube|mailto:|tel:|/home/?$|"
    r"view (this )?(email|in browser)|update (your )?profile", re.I)

_LABELS_BUYER = r"(?:agency|buyer|organi[sz]ation|department|issued by|purchaser|entity|council|client|tendering entity)"
_LABELS_REF = r"(?:reference(?: number| no\.?)?|ref(?:erence)?\s*(?:no|#|number)?|tender (?:number|no\.?|id)|rf[tqpx] (?:number|no\.?|id)|atm id|id)"
_LABELS_CLOSE = r"(?:closing (?:date(?: ?(?:&|and) ?time)?|time)|close[sd]? (?:date|on)?|closes|closing|(?:responses? )?due(?: date)?|submissions? close|deadline)"

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ── Email decoding ──────────────────────────────────────────────────────────
def message_from_raw(raw):
    """Parse an RFC822 message (str or bytes) into the dict shape parse() takes."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    text_parts, html_parts = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace")
        if ctype == "text/plain":
            text_parts.append(content)
        elif ctype == "text/html":
            html_parts.append(content)
    return {
        "sender": str(msg.get("From", "") or ""),
        "subject": str(msg.get("Subject", "") or ""),
        "date": str(msg.get("Date", "") or ""),
        "message_id": str(msg.get("Message-ID", "") or "").strip(),
        "text": "\n".join(text_parts),
        "html": "\n".join(html_parts),
    }


class _HTMLText(HTMLParser):
    """HTML -> text, with each <a href> replaced by a stable marker so items can
    be split by link position."""
    BLOCK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.links, self._href, self._atext, self._skip = [], [], None, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self._skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._atext = []

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self._skip = max(0, self._skip - 1)
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._atext).split())
            idx = len(self.links)
            self.links.append((text, self._href))
            self.out.append(f" ⟦L{idx}⟧ {text} ")
            self._href = None
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:
            self._atext.append(data)
        else:
            self.out.append(data)


def _html_to_text(html):
    p = _HTMLText()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    text = "".join(p.out)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip(), p.links


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _text_links(text):
    """Plain-text alerts: mark bare URLs the same way the HTML parser marks <a>.
    The title is usually the non-empty line just above the URL."""
    links, out_lines = [], []
    for line in (text or "").splitlines():
        def repl(m):
            idx = len(links)
            links.append(("", m.group(0).rstrip(".,;")))
            return f" ⟦L{idx}⟧ "
        out_lines.append(_URL_RE.sub(repl, line))
    return "\n".join(out_lines), links


# ── URL helpers ─────────────────────────────────────────────────────────────
def unwrap_url(url):
    """Undo Microsoft Safe Links / Google redirect wrapping."""
    url = htmllib.unescape((url or "").strip())
    for _ in range(3):
        parts = urlsplit(url)
        host = parts.netloc.lower()
        q = parse_qs(parts.query)
        if "safelinks.protection.outlook.com" in host and "url" in q:
            url = unquote(q["url"][0]); continue
        if host.endswith("google.com") and parts.path == "/url" and ("q" in q or "url" in q):
            url = unquote((q.get("q") or q.get("url"))[0]); continue
        break
    return url


def normalise_url(url):
    """Comparable form: lower-case host, no scheme/www/fragment/tracking params,
    no trailing slash. Used to match an alert to John's scraped source_url."""
    url = unwrap_url(url)
    if not url:
        return ""
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return url.lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query)
                             if not k.lower().startswith(("utm_", "mc_", "_hs", "trk"))))
    path = parts.path.rstrip("/")
    return urlunsplit(("", host, path, query, "")).lstrip("/")


# ── Field extraction ────────────────────────────────────────────────────────
def parse_date(text):
    """First recognisable date in text -> ISO string, else None. Handles
    '21 October 2026', 'Tue 21 Oct 2026', '21/10/2026', '2026-10-21'."""
    if not text:
        return None
    t = text.lower()
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})", t)
    if m:
        d, mo, y = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
    else:
        m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", t)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2}|\d{2})\b", t)  # AU/NZ: day first
            if not m:
                return None
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _labelled(block, labels, value=r"([^\n|]{2,120})"):
    m = re.search(rf"{labels}\s*[:\-–]\s*{value}", block, re.I)
    return m.group(1).strip(" .;,-") if m else ""


def closing_date_from(block):
    labelled = _labelled(block, _LABELS_CLOSE)
    return parse_date(labelled) or parse_date(
        (re.search(rf"{_LABELS_CLOSE}[^\n]{{0,60}}", block, re.I) or [""])[0])


def ref_from(block, url, portal):
    v = _labelled(block, _LABELS_REF, r"([A-Za-z0-9][A-Za-z0-9 .\-/#]{2,40})")
    if v:
        v = v.split("  ")[0]
        if re.search(r"\d", v):
            return _clean_ref(v)
    for pat in (portal or {}).get("ref", []):
        m = re.search(pat, block, re.I)
        if m:
            return _clean_ref(m.group(1))
    # fall back to an id embedded in the detail URL
    q = parse_qs(urlsplit(unwrap_url(url)).query)
    for k in ("id", "tenderid", "TenderID", "Id", "ref", "opportunityId", "rfxId"):
        if k in q and q[k][0]:
            return _clean_ref(q[k][0])
    m = re.search(r"/(\d{4,})(?:/|$|\?)", urlsplit(unwrap_url(url)).path or "")
    return _clean_ref(m.group(1)) if m else ""


def _clean_ref(v):
    return re.sub(r"\s+", "", (v or "").strip(" #:.")).upper()[:60]


def detect_portal(sender="", subject="", text="", urls=()):
    """Returns the portal dict or None. Sender first (most reliable), then the
    link hosts, then the body/subject (covers hand-forwarded alerts)."""
    hay = [(sender or "").lower()]
    hay += [urlsplit(unwrap_url(u)).netloc.lower() for u in urls]
    hay += [(subject or "").lower(), (text or "").lower()[:4000]]
    for h in hay:
        for p in PORTALS:
            if any(d in h for d in p["domains"]):
                return p
    return None


def portal_for_url(url):
    host = (urlsplit(unwrap_url(url)).netloc or "").lower() + urlsplit(unwrap_url(url)).path.lower()
    for p in PORTALS:
        if any(d in host for d in p["domains"]):
            return p
    return None


def _opp_type(title):
    t = (title or "").lower()
    if re.search(r"\beoi\b|expression of interest|\broi\b|registration of interest", t):
        return "EOI"
    if re.search(r"\brfq\b|request for quot", t):
        return "RFQ"
    if re.search(r"\brfp\b|request for proposal", t):
        return "Open Tender (RFP)"
    if re.search(r"\brfi\b|request for information|market (sounding|engagement)", t):
        return "Pre-market/Upcoming"
    if re.search(r"panel|standing offer|prequal", t):
        return "Panel/Standing Offer"
    return "Open Tender (RFT)"


_GENERIC_TITLE = re.compile(
    r"^\s*$|^(view|open|see|more|details?|click|read|go to|access)( here| the| this| full)?"
    r"( tender| opportunity| request| details| notice| more| here| now)*[\s.!>»]*$", re.I)

_SUBJECT_NOISE = re.compile(
    r"^(fw|fwd|re)\s*:\s*|new (tender|opportunit\w*)( alert)?\s*[:\-–]\s*|"
    r"tender (alert|notification)\s*[:\-–]\s*|\[external\]\s*", re.I)


def _clean_title(t):
    t = re.sub(r"⟦L\d+⟧", " ", t or "")
    t = " ".join(t.split()).strip(" :-–|")
    return t[:240]


# ── Main entry point ────────────────────────────────────────────────────────
def parse(msg):
    """msg: {sender, subject, text, html, message_id?, date?} -> dict with
    portal info and a list of items (never empty if the email had any content)."""
    sender, subject = msg.get("sender", ""), msg.get("subject", "")
    if msg.get("html"):
        body, links = _html_to_text(msg["html"])
    else:
        body, links = _text_links(msg.get("text", ""))
    links = [(t, unwrap_url(u)) for t, u in links]

    portal = detect_portal(sender, subject, body, [u for _, u in links])
    items = []

    # Positions of each link marker in the body
    marks = [(m.start(), int(m.group(1))) for m in re.finditer(r"⟦L(\d+)⟧", body)]
    detail = []
    for pos, idx in marks:
        text, url = links[idx]
        if not url.lower().startswith("http") or _JUNK_LINK.search(text or "") or _JUNK_LINK.search(url):
            continue
        lp = portal_for_url(url)
        if lp is None:
            continue
        path = urlsplit(url).path + "?" + urlsplit(url).query
        if not re.search(lp["detail"], path, re.I):
            continue
        detail.append((pos, idx, lp))

    # Layout: do the reference / closing-date lines sit AFTER each link (HTML
    # tables) or BEFORE it (plain-text alerts that end each entry with a URL)?
    fields_before = False
    if detail:
        lead = body[:detail[0][0]]
        fields_before = bool(re.search(rf"{_LABELS_REF}\s*[:\-–]|{_LABELS_CLOSE}\s*[:\-–]", lead, re.I))

    seen_urls = set()
    for n, (pos, idx, lp) in enumerate(detail):
        text, url = links[idx]
        un = normalise_url(url)
        if un in seen_urls:
            continue
        seen_urls.add(un)
        if fields_before:
            start = detail[n - 1][0] if n > 0 else 0
            block = body[start:pos]
            block = re.sub(r"^\s*\u27e6L\d+\u27e7[^\n]*", "", block)  # drop previous link line
        else:
            end = detail[n + 1][0] if n + 1 < len(detail) else len(body)
            block = body[pos:min(end, pos + 1500)]
        title = _clean_title(text)
        if _GENERIC_TITLE.match(title or ""):
            title = _labelled(re.sub(r"\u27e6L\d+\u27e7", " ", block),
                              r"(?:tender title|title|opportunity name|opportunity)") or ""
            if not title:
                lines = _clean_title_lines(block)
                title = (lines[-1] if fields_before else (lines[0] if lines else "")) if lines else ""
            title = _clean_title(title) or _SUBJECT_NOISE.sub("", subject or "").strip()
        items.append(_item(title, block, url, lp, portal))

    # Single-tender emails often print the buyer / reference / date outside the
    # link's own block (e.g. "X Council has invited you to respond to:"), so
    # top up missing fields from the whole body when there is only one item.
    if len(items) == 1:
        whole = _item(items[0]["title"], body, items[0]["url"], PORTAL_BY_KEY.get(items[0]["portal"]), portal)
        for k in ("buyer", "portal_ref", "closing_date"):
            if not items[0][k] and whole[k]:
                items[0][k] = whole[k]

    if not items:
        # Single-tender format with labels, or a format we don't know yet:
        # never drop the alert - build one item from the subject.
        url = next((u for t, u in links if u.lower().startswith("http")
                    and not _JUNK_LINK.search(u) and not _JUNK_LINK.search(t or "")), "")
        title = _labelled(body, r"(?:title|tender title|opportunity|description)") or \
            _SUBJECT_NOISE.sub("", subject or "").strip() or "Untitled alert"
        items.append(_item(_clean_title(title), body[:2500], url, portal_for_url(url) or portal, portal,
                           fallback=True))

    return {
        "portal": (portal or {}).get("key", "unknown"),
        "portal_name": (portal or {}).get("name", "Unknown portal"),
        "sender": sender, "subject": subject,
        "message_id": msg.get("message_id", ""), "date": msg.get("date", ""),
        "items": items,
    }


def _clean_title_lines(text):
    out = []
    for line in (text or "").split("\n"):
        line = _clean_title(line)
        if len(line) >= 6 and not re.match(rf"^({_LABELS_BUYER}|{_LABELS_REF}|{_LABELS_CLOSE})\s*[:\-–]", line, re.I):
            out.append(line)
    return out


def _item(title, block, url, link_portal, mail_portal, fallback=False):
    p = link_portal or mail_portal or {}
    block_clean = re.sub(r"⟦L\d+⟧", " ", block or "")
    buyer = _labelled(block_clean, _LABELS_BUYER)
    if not buyer:
        m = re.search(r"([A-Z][A-Za-z&'.\- ]{2,80}?) has invited you", block_clean)
        buyer = m.group(1).strip() if m else ""
    snippet = " ".join(block_clean.split())[:700]
    return {
        "title": title,
        "buyer": buyer[:160],
        "portal": p.get("key", "unknown"),
        "portal_name": p.get("name", "Unknown portal"),
        "portal_ref": ref_from(block_clean, url, p),
        "url": url,
        "url_norm": normalise_url(url),
        "closing_date": closing_date_from(block_clean),
        "location": p.get("location", ""),
        "opportunity_type": _opp_type(title),
        "snippet": snippet,
        "fallback": fallback,
    }
