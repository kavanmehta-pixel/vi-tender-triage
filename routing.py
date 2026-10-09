"""
Who should act on a tender, and where it is (9 Oct 2026 weekly tenders meeting).

Lanes, as Michael set them out:
  tender  - a real tender/EOI with security in scope that fits -> Kavan + Alister
            prepare the response, with the local BDM involved
  philip  - an EOI/tender for a big or critical-infrastructure job where security
            is NOT in the scope (or guarding already went elsewhere) -> Philip
            does demand gen
  bdm     - major projects that aren't tenders (>= $10m) and smaller
            opportunities -> the BDM for that state, in the Monday email
  maybe   - untouched MAYBEs -> monthly sweep by region for the BDMs

A human decision on the triage panel always wins over the suggested lane.
"""
import re

MAJOR_PROJECT_MIN = 10_000_000          # Michael/Nick, 9 Oct: down from $200m

STATES = ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "NT", "ACT", "NZ"]
STATE_NAMES = {
    "NSW": "New South Wales", "VIC": "Victoria", "QLD": "Queensland",
    "WA": "Western Australia", "SA": "South Australia", "TAS": "Tasmania",
    "NT": "Northern Territory", "ACT": "ACT", "NZ": "New Zealand",
    "OTHER": "National / unclear",
}
_STATE_WORDS = [
    ("NSW", r"\bnsw\b|new south wales|sydney|newcastle|wollongong|hunter"),
    ("VIC", r"\bvic\b|victoria(?!n? police)|melbourne|geelong|gippsland"),
    ("QLD", r"\bqld\b|queensland|brisbane|gold coast|townsville|cairns"),
    ("WA", r"\bwa\b|western australia|perth|pilbara|kimberley"),
    ("SA", r"\bsa\b|south australia|adelaide|eyre peninsula"),
    ("TAS", r"\btas\b|tasmania|hobart|launceston"),
    ("NT", r"\bnt\b|northern territory|darwin|alice springs"),
    ("ACT", r"\bact\b|canberra"),
    ("NZ", r"\bnz\b|new zealand|aotearoa|auckland|wellington|christchurch|waikato"),
]


def region_of(location, text=""):
    """'NSW-AU' -> 'NSW', 'NZ' -> 'NZ'. Falls back to place names in the text."""
    loc = (location or "").strip().upper()
    head = re.split(r"[-/,\s]", loc)[0] if loc else ""
    if head in STATES:
        return head
    blob = ((location or "") + " " + (text or "")).lower()
    for st, pat in _STATE_WORDS:
        if re.search(pat, (location or "").lower()):
            return st
    for st, pat in _STATE_WORDS:
        if re.search(pat, blob):
            return st
    return "OTHER"


# ─── Project value ──────────────────────────────────────────────────────
# John's CSV has no value column; the figure is in the text ("$3.6bn",
# "A$624m", "NZ$800m pledge", "$1.5 million per annum").
_MONEY = re.compile(
    r"(?<![a-z])(a\$|au\$|aud\s?|nz\$|nzd\s?|us\$|\$)\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"(?:\s?[–—-]\s?\$?\d+(?:\.\d+)?)?\s*"
    r"(bn|billion|b\b|m\b|mn\b|mil\b|million|k\b)?", re.I)
_FX = {"nz": 0.91, "us": 1.52}


def est_value(*texts):
    """Largest dollar figure mentioned, in AUD whole dollars, or None."""
    best = None
    for t in texts:
        for m in _MONEY.finditer(t or ""):
            cur, num, unit = m.group(1).lower(), m.group(2).replace(",", ""), (m.group(3) or "").lower()
            try:
                v = float(num)
            except ValueError:
                continue
            mult = {"bn": 1e9, "billion": 1e9, "b": 1e9, "m": 1e6, "mn": 1e6, "mil": 1e6,
                    "million": 1e6, "k": 1e3}.get(unit, 1)
            v *= mult
            if mult == 1 and v < 100_000:      # "$50 fee", "$2 coin": not a project value
                continue
            for k, fx in _FX.items():
                if cur.startswith(k):
                    v *= fx
            if 10_000 <= v <= 200e9 and (best is None or v > best):
                best = v
    return int(best) if best else None


def fmt_money(v):
    if not v:
        return ""
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:.1f}bn".replace(".0bn", "bn")
    if v >= 1e6:
        return f"${v / 1e6:.0f}m" if v >= 1e7 else f"${v / 1e6:.1f}m".replace(".0m", "m")
    return f"${v / 1e3:.0f}k"


# ─── Lanes ──────────────────────────────────────────────────────────────
_TENDERISH = re.compile(r"tender|\beoi\b|expression of interest|\brf[tqpi]\b|request for|work package|"
                        r"quot(e|ation)|panel|registration of interest|\broi\b|invitation to", re.I)
_SECURITY_SCOPE = re.compile(r"cctv|surveillance|security|camera|monitoring|access control|alarm|patrol|"
                             r"anpr|number plate|guard|perimeter|intrusion", re.I)
_PRE_MARKET = re.compile(r"pre-market|forward|advance notice|future|anticipat", re.I)

LANES = {
    "tender": "Tender / EOI response - Kavan & Alister",
    "philip": "Demand gen - Philip",
    "bdm": "BDM - local follow-up",
    "maybe": "Monthly maybes sweep",
    "pass": "Passed",
}
DECISION_LANE = {"full": "tender", "philip": "philip", "bdm": "bdm", "pass": "pass"}


def suggest_lane(p, value=None):
    """Suggested lane for an untriaged project row (dict from the projects table)."""
    verdict = (p.get("verdict") or "").upper()
    if verdict == "PASS":
        return ""
    head = " ".join(str(p.get(k) or "") for k in ("opportunity_type", "stage", "project_name"))
    tenderish = bool(_TENDERISH.search(head)) and not _PRE_MARKET.search(p.get("stage") or "")
    scope = bool(_SECURITY_SCOPE.search((p.get("project_name") or "") + " " + (p.get("verified_scope") or "")))
    value = value if value is not None else p.get("est_value")
    if tenderish and scope and verdict in ("GO", "NEEDS DOC", "MAYBE") and (p.get("doability_score") or 0) >= 55:
        return "tender"
    if tenderish and not scope:
        return "philip"
    if not tenderish and (value or 0) >= MAJOR_PROJECT_MIN:
        return "bdm"
    if verdict == "MAYBE":
        return "maybe"
    return "bdm" if verdict in ("GO", "NEEDS DOC") else ""


def lane_of(p):
    """Human decision first, then the suggestion. Returns (lane, suggested?)."""
    d = (p.get("triage_decision") or p.get("decision") or "").strip()
    if d in DECISION_LANE:
        return DECISION_LANE[d], False
    return suggest_lane(p), True


# ─── BDM directory ──────────────────────────────────────────────────────

def bdm_for(region, bdms):
    """First active BDM whose regions include this state (bdms: list of dicts)."""
    for b in bdms:
        regs = [r.strip().upper() for r in (b.get("regions") or "").split(",") if r.strip()]
        if b.get("active", 1) and region in regs:
            return b
    return None
