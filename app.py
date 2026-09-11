import os, json, csv, io, re, sqlite3, hashlib, secrets
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, render_template, g
import db as dbx

app = Flask(__name__)
DB_PATH = dbx.location()

EPHEMERAL_DB = dbx.is_ephemeral()

# ─── Database ───────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = dbx.connect()
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = dbx.connect()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_hash TEXT UNIQUE,
        project_name TEXT,
        customer TEXT,
        location TEXT,
        asset_type TEXT,
        sector TEXT,
        stage TEXT,
        year TEXT,
        source_url TEXT,
        socI_relevance TEXT,
        opportunity_type TEXT,
        priority_score TEXT,
        signal_summary TEXT,
        notes TEXT,
        active TEXT,
        doability_score INTEGER,
        verdict TEXT,
        action_bucket TEXT,
        moats TEXT,
        moat_count INTEGER,
        risk_flags TEXT,
        commodity TEXT,
        first_seen_at TEXT,
        last_seen_at TEXT,
        run_id TEXT,
        closing_date TEXT,
        is_closed INTEGER DEFAULT 0,
        confidence TEXT,
        hidden INTEGER DEFAULT 0,
        merged_into TEXT,
        dup_key TEXT
    );
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_name TEXT,
        customer TEXT,
        location TEXT,
        asset_type TEXT,
        year TEXT,
        source_url TEXT,
        contact_name TEXT,
        company TEXT,
        title TEXT,
        email TEXT,
        phone TEXT,
        private_org TEXT,
        run_id TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        created_at TEXT,
        total_projects INTEGER,
        new_projects INTEGER,
        go_count INTEGER,
        maybe_count INTEGER,
        pass_count INTEGER
    );
    CREATE TABLE IF NOT EXISTS triage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_hash TEXT UNIQUE,
        decision TEXT,
        reason TEXT,
        decided_by TEXT,
        decided_at TEXT
    );
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_hash TEXT,
        author TEXT,
        body TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_triage_hash ON triage(project_hash);
    CREATE INDEX IF NOT EXISTS idx_comments_hash ON comments(project_hash);
    CREATE INDEX IF NOT EXISTS idx_project_hash ON projects(project_hash);
    CREATE INDEX IF NOT EXISTS idx_run_id ON projects(run_id);
    CREATE INDEX IF NOT EXISTS idx_verdict ON projects(verdict);
    """)
    # Migrations for existing DBs
    for _c, _t in (("confidence","TEXT"), ("hidden","INTEGER DEFAULT 0"),
                   ("merged_into","TEXT"), ("dup_key","TEXT"), ("why_fit","TEXT"),
                   ("ai_summary","TEXT"), ("location_flag","TEXT"),
                   ("closing_date","TEXT"), ("is_closed","INTEGER DEFAULT 0"),
                   ("research_status","TEXT"), ("research_notes","TEXT"),
                   ("verified_scope","TEXT"), ("portal_url","TEXT"),
                   ("contact_info","TEXT"), ("researched_at","TEXT")):
        dbx.add_column(db, "projects", _c, _t)
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_dup_key ON projects(dup_key)")
    except Exception:
        pass
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS triage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_hash TEXT UNIQUE,
            decision TEXT,
            reason TEXT,
            decided_by TEXT,
            decided_at TEXT
        )""")
    except Exception:
        pass
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_hash TEXT,
            author TEXT,
            body TEXT,
            created_at TEXT
        )""")
    except Exception:
        pass
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_triage_hash ON triage(project_hash)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_comments_hash ON comments(project_hash)")
    except Exception:
        pass
    # Pipeline tracking columns (Aug 14 meeting: status, owner, next steps, scope)
    for col, coltype in [("status","TEXT"),("owner","TEXT"),("next_steps","TEXT"),
                         ("scope","TEXT"),("status_updated_at","TEXT")]:
        dbx.add_column(db, "triage", col, coltype)
    db.commit()
    db.close()


# ─── Date Parser ────────────────────────────────────────────────────────

MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10,
    'november': 11, 'december': 12,
}

def extract_closing_date(stage_text):
    """Extract closing date from stage field text. Returns ISO date string or None."""
    if not stage_text:
        return None
    text = stage_text.lower()

    # Priority: look near close/closing/deadline keywords first
    patterns = [
        # "closes 14 Jul 2026", "closing 9 July 2026", "close 20 July 2026"
        r'clos(?:e[sd]?|ing)[^;\n]{0,40}?(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{4})',
        # "due 14 Jul 2026"
        r'due[^;\n]{0,20}?(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{4})',
        # Fallback: any "14 Jul 2026" style date
        r'(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{4})',
    ]

    for pat in patterns:
        matches = re.findall(pat, text)
        for m in matches:
            try:
                d, mon, yr = int(m[0]), MONTH_MAP.get(m[1][:3]), int(m[2])
                if mon and 2025 <= yr <= 2030 and 1 <= d <= 31:
                    return date(yr, mon, d).isoformat()
            except Exception:
                continue
    return None


def deadline_status(closing_date_str):
    """Returns: 'closed', 'urgent' (<=7d), 'soon' (8-30d), 'future', or None."""
    if not closing_date_str:
        return None
    try:
        cd = date.fromisoformat(closing_date_str)
        today = date.today()
        delta = (cd - today).days
        if delta < 0:
            return 'closed'
        elif delta <= 7:
            return 'urgent'
        elif delta <= 30:
            return 'soon'
        else:
            return 'future'
    except Exception:
        return None

# ─── Remote Location Detector ───────────────────────────────────────────

# Locations that are difficult/expensive for VI to service — flag and penalise
REMOTE_LOCATIONS = {
    # NT
    "katherine": ("Katherine NT — remote, limited VI branch coverage", -8),
    "darwin": ("Darwin NT — serviceable but logistics overhead", -4),
    "alice springs": ("Alice Springs NT — very remote, significant logistics cost", -12),
    "tennant creek": ("Tennant Creek NT — extremely remote", -14),
    "nhulunbuy": ("Nhulunbuy NT — remote, fly-in required", -14),
    # WA remote
    "kalgoorlie": ("Kalgoorlie WA — remote mining hub, logistics overhead", -6),
    "port hedland": ("Port Hedland WA — Pilbara, fly-in/drive logistics", -8),
    "karratha": ("Karratha WA — Pilbara, logistics overhead", -8),
    "broome": ("Broome WA — remote, limited coverage", -8),
    "geraldton": ("Geraldton WA — serviceable but distant from Perth", -4),
    "esperance": ("Esperance WA — remote southern WA", -6),
    "newman": ("Newman WA — Pilbara, fly-in", -10),
    "tom price": ("Tom Price WA — Pilbara, fly-in", -10),
    # QLD remote
    "mount isa": ("Mount Isa QLD — remote, significant logistics", -10),
    "longreach": ("Longreach QLD — remote outback QLD", -8),
    "cairns": ("Cairns QLD — serviceable, northern QLD", -3),
    "townsville": ("Townsville QLD — serviceable", -2),
    "rockhampton": ("Rockhampton QLD — serviceable", -2),
    # SA remote
    "whyalla": ("Whyalla SA — regional, serviceable", -3),
    "port augusta": ("Port Augusta SA — regional", -3),
    "coober pedy": ("Coober Pedy SA — very remote", -14),
    # TAS
    "tasmania": ("Tasmania — no VI branch, logistics/freight overhead", -8),
    "hobart": ("Hobart TAS — no VI branch", -8),
    "launceston": ("Launceston TAS — no VI branch", -8),
    # NZ
    "south island": ("NZ South Island — coverage thinner than North Island", -4),
    "christchurch": ("Christchurch NZ — serviceable", -2),
    "dunedin": ("Dunedin NZ — southern NZ, logistics overhead", -5),
    "invercargill": ("Invercargill NZ — remote southern NZ", -8),
}

def check_location_flag(location_text):
    """Returns (flag_description, score_penalty) or (None, 0)."""
    if not location_text:
        return None, 0
    text = location_text.lower()
    for key, (desc, penalty) in REMOTE_LOCATIONS.items():
        if key in text:
            return desc, penalty
    return None, 0


# ─── AI Summary (generated externally by Cowork, stored here) ────────────
# Summaries are NOT generated in this app. A weekly Cowork task reads the CSVs,
# generates plain-English summaries, and uploads an enriched CSV via /upload-enriched.
# This keeps all Anthropic API usage inside Cowork — no API key lives on Railway.

# ─── Moat Scorer v3 ────────────────────────────────────────────────────
# FIT and ACTIONABILITY are scored as two independent axes. Fit is inferred
# from project archetype -> asset class -> text (in that order of trust), so a
# terse one-line tender listing is no longer punished for being terse.

import re

# ── 1. PROJECT ARCHETYPE: what a project IS implies its conditions ──────────
# A wind farm is remote and construction-phase whether or not the text says so.
ARCHETYPES = {
    "renewable_generation": (
        r"wind farm|solar farm|solar project|windfarm|photovoltaic|renewable energy (project|zone|hub)|"
        r"energy hub|energy park|\bbess\b|battery energy storage|big battery|pumped hydro|hydropower|"
        r"offshore wind|solar \+ ?bess|energy storage system",
        {"M1": 1.0, "M5": 0.9, "M2": 0.35}),
    "transmission": (
        r"transmission (line|project|corridor|upgrade|development)|interconnector|substation|switching station|"
        r"\d{3}\s?kv|powerline|network operator|grid connection",
        {"M1": 1.0, "M5": 0.85}),
    "resources": (
        r"\bmine\b|mining|gold project|copper project|lithium|rare earth|mineral sands|iron ore|"
        r"processing plant|concentrator|tailings|quarry|exploration|drilling program|smelter|refinery",
        {"M1": 1.0, "M5": 0.8}),
    "linear_infra": (
        r"pipeline|highway|ring road|motorway|rail (link|line|corridor)|road upgrade|freight terminal|"
        r"level crossing|bridge|duplication",
        {"M1": 0.85, "M5": 0.85}),
    "water_waste": (
        r"water treatment|wastewater|wwtp|desalination|\bdam\b|reservoir|sewage|landfill|waste facility|"
        r"resource recovery|tailings storage",
        {"M1": 0.8, "M5": 0.6}),
    "construction_site": (
        r"construction (of|works|site|package|phase)|early works|enabling works|civil works|site establishment|"
        r"laydown|compound|redevelopment|main works|d&c|design and construct",
        {"M5": 1.0, "M1": 0.45}),
    "port_marine": (r"\bport\b|wharf|berth|marine (infrastructure|precinct)|terminal", {"M1": 0.55, "M5": 0.6}),
    "vertical_building": (
        r"\blibrary\b|community (centre|center|precinct|hub)|office (building|fit)|school building|classroom|"
        r"\bhospital\b|health (centre|facility)|aged care|childcare|museum|gallery|theatre|stadium roof|"
        r"apartment|residential building|\bclinic\b|prison|correctional (centre|facility)|courthouse",
        {}),
    "council_open": (
        r"council|shire|regional council|public space|park|reserve|sportsground|oval|foreshore|"
        r"illegal dumping|amenities",
        {"M1": 0.5, "M4": 0.55}),
}

# ── 2. ASSET TYPE: John's own classification is a strong, clean prior ────────
ASSET_PRIOR = {
    "off_grid_solar_security":        {"M1": 1.0, "M2": 1.0, "M5": 0.7},
    "remote_surveillance_monitoring": {"M1": 0.95, "M3": 0.9},
    "mobile_rapid_deploy_cctv":       {"M5": 1.0, "M1": 0.6},
    "unmanned_site_monitoring":       {"M1": 0.9, "M3": 0.95},
    "temporary_event_site_security":  {"M5": 1.0, "M3": 0.5},
    "license_plate_recognition":      {"M4": 1.0, "M6": 0.9},
    "critical_infrastructure_security": {"M1": 0.5},
    "security_operations_centres":    {"M3": 0.85},
    "access_control_perimeter_security": {},   # neutral — perimeter can be VI, access control isn't
    "integrated_security_platforms":  {},      # neutral — usually integrator scope
    "other":                          {},
}

# ── 3. TEXT EVIDENCE: supplements, never the sole driver ────────────────────
TEXT_MOATS = {
    "M1": r"off[- ]grid|no (mains|grid|fixed) power|unpowered|remote (site|area|location|asset)|isolated|"
          r"no (comms|connectivity)|starlink|satellite|greenfield|rural|regional|outback",
    "M2": r"solar[- ]powered|solar camera|solar panel|solar surveillance|photovoltaic",
    "M3": r"24/7|monitor(ing|ed)|alarm response|command centre|control room|virtual patrol|asial|"
          r"remote monitoring|live monitoring|surveillance service",
    "M4": r"\bai\b|artificial intelligence|analytics|anpr|number ?plate|licen[cs]e plate|illegal dumping|"
          r"dumping detection|fire detection|smoke detection|thermal|computer vision|object detection",
    "M5": r"temporary|relocatable|redeployable|short[- ]term|rapid[- ]deploy|mobile (cctv|camera|surveillance)|"
          r"trailer|construction (site|phase|period)|site security|hire\b|pop[- ]up",
    "M6": r"traffic (count|monitor|management|flow|survey)|vehicle (count|detection|movement)|weigh[- ]in[- ]motion|"
          r"axle count|loadsure|haulage|freight movement|pedestrian count",
}
MOAT_NAMES = {
    "M1": "off-grid/remote", "M2": "solar fleet", "M3": "monitored outcome",
    "M4": "AI analytics", "M5": "temp/rapid deploy", "M6": "traffic/transport",
}

# ── 4. DISQUALIFIERS: only fire on real evidence, weighted by specificity ────
HARD_KILLS = [
    (r"maintain(ing|enance)? of (the )?existing|existing (cctv|camera|security) (system|network)|legacy system|"
     r"renewals? and replacement", 34, "maintenance of existing system"),
    (r"verkada|genetec|milestone|integriti|inner range|c[·.]?cure|lenel|gallagher", 34, "third-party platform lock-in"),
    (r"nurse call|duress (alarm|system)|intercom|paging system", 30, "in-building electronics"),
    (r"cyber ?security|\bsiem\b|information security|\bit security\b|\bot security\b", 34, "IT/OT security"),
    (r"fit[- ]?out|refurbishment|landscaping|locksmith|master key", 28, "fit-out/refurb/locksmith"),
    (r"\blift\b|elevator|\bhvac\b|fire (alarm|panel|sprinkler)|cleaning services|building services", 26, "building services"),
    (r"screening (services|equipment)|x[- ]ray|weapons? detection|body[- ]?worn", 26, "screening/body-worn"),
]
SOFT_FLAGS = [
    (r"guard(s|ing)\b|manned (guard|security)|static security|concierge|patrol officer", 13, "guarding in scope"),
    (r"permanent (cctv|camera|installation)|fixed (cctv|camera|pole)|supply,? (and )?install", 11, "fixed-install component"),
    (r"access control", 9, "access-control works"),
    (r"cabling|fibre|fiber|conduit|trench(ing)?|structured network", 10, "cabling/civil works"),
    (r"indoor|in[- ]building|internal (cctv|camera)|corridor", 9, "indoor scope"),
    (r"panel|standing offer|preferred supplier|prequalification", 5, "panel dynamic"),
]

SECTOR_ADJ = {
    "energy": 7, "renewables": 9, "mining": 9, "mining/resources": 9, "resources": 8,
    "water": 5, "waste": 8, "construction": 7, "local government": 5, "councils": 5,
    "transport": 5, "roads": 6, "ports": 4, "agriculture": 7, "events": 6,
    "infrastructure": 5, "utilities": 6, "rail": 2, "defence": 0, "government": 1,
    "health": -9, "healthcare": -9, "education": -4, "corrections": -7, "justice": -5,
}

ACTIONABLE = {
    "EOI": "ACT NOW", "Open Tender": "ACT NOW", "Open Tender (RFP)": "ACT NOW",
    "Open Tender (RFT)": "ACT NOW", "RFQ": "ACT NOW", "RFT": "ACT NOW",
    "Panel/Standing Offer": "ACT NOW", "ROI": "ACT NOW", "Registration of Interest": "ACT NOW",
    "Pre-market/Upcoming": "MONITOR", "EPC package": "BD PLAY",
    "Compliance-driven uplift": "BD PLAY", "Open Grant": "MONITOR",
}

def _txt(row):
    return " ".join(str(row.get(f, "") or "") for f in
                    ("project_name","signal_summary","notes","stage","asset_type","sector","opportunity_type")).lower()

def score_project(row):
    text = _txt(row)

    # Is an actual security/surveillance scope stated, or are we inferring it
    # purely from what kind of project this is?
    scope_stated = bool(re.search(
        r"cctv|surveillance|security (system|service|package|work|camera|solution|uplift|upgrade)|"
        r"camera|monitoring|access control|alarm|perimeter", text))
    scope_detailed = bool(re.search(
        r"(scope|requirement|specification|deliverable|work package|package of work|services include|"
        r"comprising|covering)", text)) and scope_stated

    # --- accumulate moat strength 0..1 from three independent sources ---
    strength = {k: 0.0 for k in MOAT_NAMES}
    why = []

    arche_hits = []
    for aname, (pat, awards) in ARCHETYPES.items():
        if re.search(pat, text):
            arche_hits.append(aname)
            for m, w in awards.items():
                strength[m] = max(strength[m], w)
    if arche_hits:
        why.append("project type: " + ", ".join(a.replace("_", " ") for a in arche_hits[:2]))

    is_building = "vertical_building" in arche_hits
    at = (row.get("asset_type") or "").strip().lower()
    if not is_building:
        # asset_type is John's classifier and is sometimes wrong, so it needs
        # corroboration from the project archetype before it can carry a row alone
        corroborated = bool([a for a in arche_hits if a != "council_open"])
        for m, w in ASSET_PRIOR.get(at, {}).items():
            strength[m] = max(strength[m], w if corroborated else w * 0.55)
        if ASSET_PRIOR.get(at):
            why.append(f"asset class: {at.replace('_',' ')}"
                       + ("" if corroborated else " (unconfirmed)"))

    for m, pat in TEXT_MOATS.items():
        if re.search(pat, text):
            strength[m] = max(strength[m], 0.8)

    if is_building:
        # A library/hospital/office build is not an open-air site: the off-grid,
        # solar and relocatable angles cannot apply however the blurb is worded.
        for m in ("M1", "M2", "M5"):
            strength[m] = 0.0
        why = ["vertical building — open-air angles do not apply"]

    moats = [m for m, v in strength.items() if v >= 0.5]
    moat_count = len(moats)
    moat_mass = sum(strength[m] for m in moats)

    # --- base fit from moat mass (not raw count) ---
    fit = 22 + min(52, moat_mass * 15)

    sec = (row.get("sector") or "").strip().lower()
    sec_adj = SECTOR_ADJ.get(sec)
    if sec_adj is None:          # fuzzy: "energy (renewables)" -> best matching key
        cands = [v for k, v in SECTOR_ADJ.items() if k in sec]
        sec_adj = max(cands) if cands else 0
    fit += sec_adj

    # --- disqualifiers ---
    flags, hard_hit = [], False
    for pat, pen, label in HARD_KILLS:
        if re.search(pat, text):
            fit -= pen; flags.append("KILL: " + label); hard_hit = True
    for pat, pen, label in SOFT_FLAGS:
        if re.search(pat, text):
            # soft flags scale down when VI holds a genuinely broad moat set
            scale = 1.0 if moat_mass < 1.8 else (0.65 if moat_mass < 2.8 else 0.4)
            fit -= pen * scale
            flags.append(label)

    fit = max(0, min(100, round(fit)))

    # Confidence = do we know there is real security work here?
    if scope_detailed:   confidence = "HIGH"
    elif scope_stated:   confidence = "MEDIUM"
    else:                confidence = "LOW"

    # --- actionability is a SEPARATE axis ---
    opp = (row.get("opportunity_type") or "").strip()
    bucket = ACTIONABLE.get(opp)
    if bucket is None:
        bucket = "ACT NOW" if re.search(r"tender|eoi|rfp|rft|rfq|quotation|expression of interest", opp.lower()) \
                 else ("MONITOR" if re.search(r"pre-market|upcoming|planning|announced|future", (opp+text[:200]).lower())
                       else "REVIEW")

    # --- verdict: LOW confidence can't be a PASS, it's a NEEDS DOC ---
    if hard_hit:
        verdict = "PASS"
    elif fit >= 70 and confidence == "HIGH":
        verdict = "GO"
    elif fit >= 70 and confidence != "HIGH":
        # strong signals but we cannot see the actual scope — this is the
        # "3-line description" case the team keeps hitting. Pull the pack.
        verdict = "NEEDS DOC"
    elif fit >= 50:
        verdict = "MAYBE"
    elif bucket == "ACT NOW" and moat_count >= 2 and not is_building:
        # A live, open tender where VI holds two or more genuine angles is never
        # auto-killed. A false PASS here costs a real opportunity; a false MAYBE
        # costs one line of human review on Wednesday.
        verdict = "MAYBE"
    elif fit >= 38 and confidence != "HIGH" and bucket == "ACT NOW" and moat_count >= 2:
        verdict = "NEEDS DOC"
    else:
        verdict = "PASS"

    commodity = "yes" if (moat_count == 0 and confidence != "LOW" and not hard_hit) else "no"
    if commodity == "yes":
        fit = min(fit, 44)
        if verdict == "GO": verdict = "MAYBE"

    return {
        "doability_score": fit,
        "verdict": verdict,
        "action_bucket": bucket,
        "confidence": confidence,
        "moats": "; ".join(f"{m} {MOAT_NAMES[m]}" for m in sorted(moats)),
        "moat_count": moat_count,
        "risk_flags": "; ".join(flags),
        "commodity": commodity,
        "why": "; ".join(why),
    }


_DUP_NOISE = re.compile(
    r"\b(tender|rft|rfp|rfq|eoi|expression of interest|request for|open|contract|"
    r"no\.?\s*\w+|20\d\d|stage \d|work package|package|invitation|notice)\b")

def dup_key(row):
    """Collapse the same tender listed by many aggregators onto one key.
    Strips bracketed asides, procurement boilerplate and punctuation."""
    def norm(s):
        s = (s or "").lower()
        s = re.sub(r"\([^)]*\)", " ", s)
        s = _DUP_NOISE.sub(" ", s)
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        return " ".join(s.split())
    return (norm(row.get("project_name")) + "|" + norm(row.get("customer"))[:40]).strip()


def make_hash(row):
    key = (row.get("project_name", "") + "|" + row.get("customer", "") + "|" + row.get("source_url", "")).strip().lower()
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ─── CSV Ingest ─────────────────────────────────────────────────────────

PROJECT_FIELDS = [
    "project_name", "customer", "location", "asset_type", "sector",
    "stage", "year", "source_url", "socI_relevance", "opportunity_type",
    "priority_score", "signal_summary", "notes", "active",
]

CONTACT_FIELDS = [
    "project_name", "customer", "location", "asset_type", "year",
    "source_url", "contact_name", "company", "title", "email",
    "phone", "private_org",
]


def ingest_projects(csv_text, run_id, db):
    reader = csv.DictReader(io.StringIO(csv_text))
    now = datetime.utcnow().isoformat()
    new_count = 0
    total = 0
    new_for_summary = []  # collect new projects needing AI summary

    for row in reader:
        if row.get("active", "").lower() != "true":
            continue
        total += 1
        h = make_hash(row)
        dk = dup_key(row)
        scores = score_project(row)

        # Location flag + score adjustment
        loc_text = row.get("location", "") + " " + row.get("signal_summary", "")
        location_flag, loc_penalty = check_location_flag(loc_text)
        if loc_penalty:
            scores["doability_score"] = max(0, scores["doability_score"] + loc_penalty)
            if scores["doability_score"] < 70 and scores["verdict"] == "GO":
                scores["verdict"] = "MAYBE"
            if scores["doability_score"] < 50 and scores["verdict"] == "MAYBE":
                scores["verdict"] = "PASS"

        # Extract closing date
        stage_text = row.get("stage", "") + " " + row.get("notes", "")
        closing_date = extract_closing_date(stage_text)
        ds = deadline_status(closing_date)
        is_closed = 1 if ds == 'closed' else 0

        existing = db.execute("SELECT id, first_seen_at, ai_summary FROM projects WHERE project_hash = ?", (h,)).fetchone()

        proj_data = {**scores,
            "project_name": row.get("project_name",""),
            "customer": row.get("customer",""),
            "location": row.get("location",""),
            "opportunity_type": row.get("opportunity_type",""),
            "stage": row.get("stage",""),
            "closing_date": closing_date,
            "signal_summary": row.get("signal_summary",""),
            "location_flag": location_flag,
        }

        if existing:
            db.execute("""UPDATE projects SET
                doability_score=?, verdict=?, action_bucket=?, moats=?, moat_count=?,
                risk_flags=?, commodity=?, last_seen_at=?, run_id=?,
                stage=?, opportunity_type=?, priority_score=?, signal_summary=?, notes=?, active=?,
                closing_date=?, is_closed=?, location_flag=?, confidence=?, why_fit=?, dup_key=?
                WHERE project_hash=?""",
                (scores["doability_score"], scores["verdict"], scores["action_bucket"],
                 scores["moats"], scores["moat_count"], scores["risk_flags"], scores["commodity"],
                 now, run_id,
                 row.get("stage",""), row.get("opportunity_type",""),
                 row.get("priority_score",""), row.get("signal_summary",""),
                 row.get("notes",""), row.get("active",""),
                 closing_date, is_closed, location_flag,
                 scores.get("confidence"), scores.get("why"), dk, h))
            # Regenerate summary if score changed significantly or no summary yet
            if not existing["ai_summary"]:
                new_for_summary.append((h, proj_data))
        else:
            new_count += 1
            vals = {f: row.get(f, "") for f in PROJECT_FIELDS}
            db.execute("""INSERT INTO projects
                (project_hash, project_name, customer, location, asset_type, sector,
                 stage, year, source_url, socI_relevance, opportunity_type, priority_score,
                 signal_summary, notes, active,
                 doability_score, verdict, action_bucket, moats, moat_count,
                 risk_flags, commodity, first_seen_at, last_seen_at, run_id,
                 closing_date, is_closed, location_flag, confidence, why_fit, dup_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (h, vals["project_name"], vals["customer"], vals["location"],
                 vals["asset_type"], vals["sector"], vals["stage"], vals["year"],
                 vals["source_url"], vals["socI_relevance"], vals["opportunity_type"],
                 vals["priority_score"], vals["signal_summary"], vals["notes"], vals["active"],
                 scores["doability_score"], scores["verdict"], scores["action_bucket"],
                 scores["moats"], scores["moat_count"], scores["risk_flags"], scores["commodity"],
                 now, now, run_id, closing_date, is_closed, location_flag,
                 scores.get("confidence"), scores.get("why"), dk))
            new_for_summary.append((h, proj_data))

    db.commit()
    return total, new_count


def ingest_contacts(csv_text, run_id, db):
    reader = csv.DictReader(io.StringIO(csv_text))
    now = datetime.utcnow().isoformat()
    count = 0
    for row in reader:
        count += 1
        vals = {f: row.get(f, "") for f in CONTACT_FIELDS}
        db.execute("""INSERT INTO contacts
            (project_name, customer, location, asset_type, year, source_url,
             contact_name, company, title, email, phone, private_org,
             run_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vals["project_name"], vals["customer"], vals["location"],
             vals["asset_type"], vals["year"], vals["source_url"],
             vals["contact_name"], vals["company"], vals["title"],
             vals["email"], vals["phone"], vals["private_org"],
             run_id, now))
    return count


def ingest_enriched(csv_text, db):
    """Attach AI summaries from an enriched CSV (produced by the Tuesday Cowork run).
    Matches on project_name + customer (same basis as make_hash). Only updates ai_summary."""
    reader = csv.DictReader(io.StringIO(csv_text))
    updated = 0
    missing = 0
    for row in reader:
        summary = (row.get("ai_summary") or "").strip()
        if not summary:
            continue
        h = make_hash(row)
        res = db.execute("UPDATE projects SET ai_summary=? WHERE project_hash=?", (summary, h))
        if res.rowcount:
            updated += 1
        else:
            missing += 1
    db.commit()
    return updated, missing


# ─── Triage & Comments ──────────────────────────────────────────────────

@app.route("/api/triage/<project_hash>", methods=["GET"])
def get_triage(project_hash):
    db = get_db()
    t = db.execute("SELECT * FROM triage WHERE project_hash=?", (project_hash,)).fetchone()
    comments = db.execute(
        "SELECT * FROM comments WHERE project_hash=? ORDER BY created_at ASC",
        (project_hash,)
    ).fetchall()
    return jsonify({
        "triage": dict(t) if t else None,
        "comments": [dict(c) for c in comments],
    })

@app.route("/api/triage/<project_hash>", methods=["POST"])
def save_triage(project_hash):
    db = get_db()
    data = request.json
    decision = data.get("decision", "")
    reason = data.get("reason", "")
    decided_by = data.get("decided_by", "")
    status = data.get("status", "")
    owner = data.get("owner", "")
    next_steps = data.get("next_steps", "")
    scope = data.get("scope", "")
    now = datetime.utcnow().isoformat()
    db.execute("""INSERT INTO triage (project_hash, decision, reason, decided_by, decided_at, status, owner, next_steps, scope, status_updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(project_hash) DO UPDATE SET
        decision=excluded.decision, reason=excluded.reason,
        decided_by=excluded.decided_by, decided_at=excluded.decided_at,
        status=excluded.status, owner=excluded.owner,
        next_steps=excluded.next_steps, scope=excluded.scope,
        status_updated_at=excluded.status_updated_at""",
        (project_hash, decision, reason, decided_by, now, status, owner, next_steps, scope, now))
    db.commit()
    return jsonify({"ok": True, "decided_at": now})

@app.route("/api/comments/<project_hash>", methods=["POST"])
def add_comment(project_hash):
    db = get_db()
    data = request.json
    body = data.get("body", "").strip()
    author = data.get("author", "").strip() or "Kavan"
    if not body:
        return jsonify({"ok": False, "error": "empty"}), 400
    now = datetime.utcnow().isoformat()
    db.execute("INSERT INTO comments (project_hash, author, body, created_at) VALUES (?,?,?,?)",
               (project_hash, author, body, now))
    db.commit()
    return jsonify({"ok": True, "created_at": now})

@app.route("/api/comments/<project_hash>", methods=["DELETE"])
def delete_comment(project_hash):
    db = get_db()
    comment_id = request.json.get("id")
    db.execute("DELETE FROM comments WHERE id=? AND project_hash=?", (comment_id, project_hash))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/triage/summary", methods=["GET"])
def triage_summary():
    db = get_db()
    rows = db.execute("""
        SELECT t.decision, COUNT(*) as n FROM triage t
        GROUP BY t.decision
    """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/research-queue")
def api_research_queue():
    """Tenders worth researching: open, actionable, and either strong enough to
    pursue or too thinly described to judge. This is the weekly Cowork input."""
    db = get_db()
    limit = int(request.args.get("limit", 40))
    only_unresearched = request.args.get("unresearched", "1") == "1"
    where = """WHERE p.active='true' AND (p.hidden=0 OR p.hidden IS NULL) AND p.merged_into IS NULL
               AND (p.is_closed=0 OR p.is_closed IS NULL)
               AND p.verdict IN ('GO','NEEDS DOC','MAYBE')
               AND p.action_bucket IN ('ACT NOW','BD PLAY')"""
    if only_unresearched:
        where += " AND (p.research_status IS NULL OR p.research_status='')"
    rows = db.execute(f"""
        SELECT p.project_hash, p.project_name, p.customer, p.location, p.sector,
               p.source_url, p.opportunity_type, p.stage, p.closing_date,
               p.doability_score, p.verdict, p.confidence, p.moats, p.risk_flags,
               p.signal_summary, p.action_bucket
        FROM projects p {where}
        ORDER BY
            CASE p.verdict WHEN 'GO' THEN 0 WHEN 'NEEDS DOC' THEN 1 ELSE 2 END,
            p.doability_score DESC
        LIMIT {limit}
    """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/research", methods=["POST"])
def api_research():
    """Write back findings from the research run. Accepts a list of results keyed
    by project_hash. Only fills research fields — never touches triage decisions."""
    data = request.json or {}
    items = data.get("results") or ([data] if data.get("project_hash") else [])
    if not items:
        return jsonify({"ok": False, "error": "no results"}), 400
    db = get_db()
    now = datetime.utcnow().isoformat()
    updated = unmatched = 0
    for it in items:
        h = it.get("project_hash")
        if not h:
            unmatched += 1
            continue
        sets, vals = [], []
        for field, col in (("research_status","research_status"), ("research_notes","research_notes"),
                           ("verified_scope","verified_scope"), ("portal_url","portal_url"),
                           ("contact_info","contact_info"), ("ai_summary","ai_summary")):
            if it.get(field):
                sets.append(f"{col}=?"); vals.append(it[field])
        # a verified closing date also refreshes the open/closed state
        cd = it.get("verified_closing_date") or it.get("closing_date")
        if cd:
            sets.append("closing_date=?"); vals.append(cd)
            sets.append("is_closed=?"); vals.append(1 if deadline_status(cd) == "closed" else 0)
        if not sets:
            unmatched += 1
            continue
        sets.append("researched_at=?"); vals.append(now)
        vals.append(h)
        res = db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE project_hash=?", vals)
        if res.rowcount:
            updated += 1
        else:
            unmatched += 1
    db.commit()
    return jsonify({"ok": True, "updated": updated, "unmatched": unmatched})


@app.route("/api/duplicates")
def api_duplicates():
    """Groups of rows that are the same tender republished by several aggregators."""
    db = get_db()
    # Default to groups that actually affect the list you are looking at. A group
    # of long-closed tenders is noise: merging it changes nothing on screen.
    include_closed = request.args.get("include_closed", "0") == "1"
    rows = db.execute("""
        SELECT p.*, t.decision AS triage_decision,
               (SELECT COUNT(*) FROM comments c WHERE c.project_hash=p.project_hash) AS comment_count
        FROM projects p LEFT JOIN triage t ON t.project_hash = p.project_hash
        WHERE (p.hidden=0 OR p.hidden IS NULL) AND p.merged_into IS NULL
          AND p.dup_key IS NOT NULL AND p.dup_key != ''
        ORDER BY p.doability_score DESC
    """).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["dup_key"], []).append(dict(r))
    out = []
    for k, members in groups.items():
        if len(members) < 2:
            continue
        open_members = [m for m in members if not m.get("is_closed")]
        if not include_closed and len(open_members) < 2:
            continue
        if not include_closed:
            members = open_members
        for m in members:
            m["deadline_status"] = deadline_status(m.get("closing_date"))
            m["source_domain"] = re.sub(r"^https?://(www\.)?([^/]+).*$", r"\2", m.get("source_url") or "")
        # suggest the richest row as canonical: has a decision, then most detail
        members.sort(key=lambda m: (
            0 if m.get("triage_decision") else 1,
            -(m.get("comment_count") or 0),
            -len((m.get("signal_summary") or "") + (m.get("notes") or "")),
        ))
        out.append({"dup_key": k, "count": len(members),
                    "open_count": sum(1 for m in members if not m.get("is_closed")),
                    "suggested_canonical": members[0]["project_hash"], "members": members})
    out.sort(key=lambda g: -g["count"])
    return jsonify(out)


@app.route("/api/merge", methods=["POST"])
def api_merge():
    """Fold duplicates into one canonical row. Triage decisions and comments from
    every merged row are preserved and moved across — nothing is destroyed."""
    db = get_db()
    data = request.json or {}
    canonical = data.get("canonical")
    others = [h for h in (data.get("others") or []) if h != canonical]
    if not canonical or not others:
        return jsonify({"ok": False, "error": "need canonical and others"}), 400
    now = datetime.utcnow().isoformat()
    moved_comments = 0
    adopted = None
    existing = db.execute("SELECT decision FROM triage WHERE project_hash=?", (canonical,)).fetchone()
    for h in others:
        # carry comments over
        cur = db.execute("UPDATE comments SET project_hash=? WHERE project_hash=?", (canonical, h))
        moved_comments += cur.rowcount or 0
        # adopt a decision only if the canonical row does not already have one
        if not (existing and existing["decision"]):
            t = db.execute("SELECT * FROM triage WHERE project_hash=? AND decision IS NOT NULL AND decision!=''", (h,)).fetchone()
            if t and not adopted:
                db.execute("""INSERT INTO triage (project_hash,decision,reason,decided_by,decided_at,status,owner,next_steps,scope,status_updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(project_hash) DO UPDATE SET decision=excluded.decision, reason=excluded.reason,
                    decided_by=excluded.decided_by, decided_at=excluded.decided_at, status=excluded.status,
                    owner=excluded.owner, next_steps=excluded.next_steps, scope=excluded.scope""",
                    (canonical, t["decision"], t["reason"], t["decided_by"], t["decided_at"],
                     t["status"], t["owner"], t["next_steps"], t["scope"], now))
                adopted = t["decision"]
        db.execute("UPDATE projects SET merged_into=?, hidden=1 WHERE project_hash=?", (canonical, h))
    db.commit()
    return jsonify({"ok": True, "merged": len(others),
                    "comments_moved": moved_comments, "decision_adopted": adopted})


@app.route("/api/hide", methods=["POST"])
def api_hide():
    """Soft-hide (reversible). Never deletes a row or its history."""
    db = get_db()
    data = request.json or {}
    hashes = data.get("hashes") or ([data["hash"]] if data.get("hash") else [])
    hide = 1 if data.get("hidden", True) else 0
    if not hashes:
        return jsonify({"ok": False, "error": "no hashes"}), 400
    for h in hashes:
        db.execute("UPDATE projects SET hidden=?, merged_into=CASE WHEN ?=0 THEN NULL ELSE merged_into END WHERE project_hash=?", (hide, hide, h))
    db.commit()
    return jsonify({"ok": True, "updated": len(hashes), "hidden": bool(hide)})


@app.route("/api/archive")
def api_archive():
    """Everything set aside: passed, hidden, or merged away."""
    db = get_db()
    rows = db.execute("""
        SELECT p.*, t.decision AS triage_decision, t.reason AS triage_reason,
               t.owner AS triage_owner,
               (SELECT COUNT(*) FROM comments c WHERE c.project_hash=p.project_hash) AS comment_count
        FROM projects p LEFT JOIN triage t ON t.project_hash = p.project_hash
        WHERE p.hidden=1 OR p.merged_into IS NOT NULL OR t.decision='pass'
        ORDER BY p.doability_score DESC
    """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["deadline_status"] = deadline_status(d.get("closing_date"))
        d["archive_reason"] = ("merged" if d.get("merged_into") else
                               "passed" if d.get("triage_decision") == "pass" else "hidden")
        out.append(d)
    return jsonify(out)


@app.route("/api/pipeline")
def api_pipeline():
    """All tenders with an active triage decision or status — the working pipeline
    for the weekly report to Michael/Danny/Robin/John."""
    db = get_db()
    rows = db.execute("""
        SELECT p.project_name, p.customer, p.location, p.sector, p.source_url,
               p.doability_score, p.verdict, p.closing_date, p.is_closed,
               p.opportunity_type, p.project_hash,
               t.decision, t.reason, t.status, t.owner, t.next_steps, t.scope,
               t.decided_by, t.decided_at, t.status_updated_at
        FROM triage t
        JOIN projects p ON p.project_hash = t.project_hash
        WHERE t.decision IS NOT NULL AND t.decision != ''
        ORDER BY
            CASE t.decision WHEN 'full' THEN 0 WHEN 'philip' THEN 1 ELSE 2 END,
            p.doability_score DESC
    """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["deadline_status"] = deadline_status(d.get("closing_date"))
        result.append(d)
    return jsonify(result)


@app.route("/api/summary/<project_hash>")
def get_summary(project_hash):
    db = get_db()
    row = db.execute("SELECT ai_summary FROM projects WHERE project_hash=?", (project_hash,)).fetchone()
    if not row:
        return jsonify({"summary": None}), 404
    return jsonify({"summary": row["ai_summary"], "ready": bool(row["ai_summary"])})


@app.route("/api/upload-enriched", methods=["POST"])
def api_upload_enriched():
    db = get_db()
    f = request.files.get("enriched")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400
    text = f.read().decode("utf-8-sig")
    updated, missing = ingest_enriched(text, db)
    return jsonify({"ok": True, "summaries_attached": updated, "unmatched": missing})


# ─── Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/projects")
def api_projects():
    db = get_db()
    hide_closed = request.args.get("hide_closed", "1") == "1"
    where = "WHERE p.active = 'true' AND (p.hidden = 0 OR p.hidden IS NULL) AND p.merged_into IS NULL"
    if hide_closed:
        where += " AND (p.is_closed = 0 OR p.is_closed IS NULL)"
    rows = db.execute(f"""
        SELECT p.*,
            CASE WHEN p.first_seen_at = p.last_seen_at AND p.run_id = (
                SELECT id FROM runs ORDER BY created_at DESC LIMIT 1
            ) THEN 1 ELSE 0 END as is_new,
            t.decision as triage_decision,
            t.reason as triage_reason,
            t.decided_by as triage_decided_by,
            t.decided_at as triage_decided_at,
            t.status as triage_status,
            t.owner as triage_owner,
            t.next_steps as triage_next_steps,
            t.scope as triage_scope,
            p.research_status, p.research_notes, p.verified_scope,
            p.portal_url, p.contact_info, p.researched_at,
            (SELECT COUNT(*) FROM comments c WHERE c.project_hash = p.project_hash) as comment_count
        FROM projects p
        LEFT JOIN triage t ON t.project_hash = p.project_hash
        {where}
        ORDER BY p.doability_score DESC
    """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["deadline_status"] = deadline_status(d.get("closing_date"))
        result.append(d)
    return jsonify(result)

@app.route("/api/projects/new")
def api_new_projects():
    db = get_db()
    latest = db.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if not latest:
        return jsonify([])
    rows = db.execute("""
        SELECT * FROM projects
        WHERE run_id = ? AND first_seen_at = last_seen_at AND active = 'true'
        ORDER BY doability_score DESC
    """, (latest["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/contacts")
def api_contacts():
    db = get_db()
    latest = db.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if not latest:
        return jsonify([])
    rows = db.execute("SELECT * FROM contacts WHERE run_id = ?", (latest["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/runs")
def api_runs():
    db = get_db()
    rows = db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 20").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/health")
def api_health():
    db = get_db()
    def count(t):
        try:
            row = db.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            return row["n"] if row is not None else None
        except Exception:
            db.rollback()
            return None
    return jsonify({
        "backend": dbx.backend(),
        "db_path": dbx.location(),
        "ephemeral_warning": dbx.is_ephemeral(),
        "projects": count("projects"),
        "triage_decisions": count("triage"),
        "comments": count("comments"),
    })


@app.route("/api/stats")
def api_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true'").fetchone()["n"]
    go = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true' AND verdict='GO'").fetchone()["n"]
    maybe = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true' AND verdict='MAYBE'").fetchone()["n"]
    pas = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true' AND verdict='PASS'").fetchone()["n"]
    commodity = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true' AND commodity='yes'").fetchone()["n"]
    latest = db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    return jsonify({
        "total": total, "go": go, "maybe": maybe, "pass": pas,
        "commodity": commodity,
        "latest_run": dict(latest) if latest else None,
    })


@app.route("/upload", methods=["GET"])
def upload_page():
    return render_template("upload.html")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    db = get_db()
    run_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
    now = datetime.utcnow().isoformat()
    results = {"run_id": run_id}

    # Handle file uploads or raw CSV text
    for key, label in [("a_projects", "all_active_projects"), ("b_leads", "new_active_leads"), ("c_contacts", "new_leads_contacts")]:
        f = request.files.get(key)
        if f:
            text = f.read().decode("utf-8-sig")
            if key in ("a_projects", "b_leads"):
                total, new = ingest_projects(text, run_id, db)
                results[label] = {"total_rows": total, "new_projects": new}
            elif key == "c_contacts":
                count = ingest_contacts(text, run_id, db)
                results[label] = {"contacts_loaded": count}

    # Compute run stats
    go = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true' AND verdict='GO'").fetchone()["n"]
    maybe = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true' AND verdict='MAYBE'").fetchone()["n"]
    pas = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true' AND verdict='PASS'").fetchone()["n"]
    total_active = db.execute("SELECT COUNT(*) as n FROM projects WHERE active='true'").fetchone()["n"]
    new_this_run = sum(r.get("new_projects", 0) for r in results.values() if isinstance(r, dict))

    db.execute("INSERT INTO runs (id, created_at, total_projects, new_projects, go_count, maybe_count, pass_count) VALUES (?,?,?,?,?,?,?)",
               (run_id, now, total_active, new_this_run, go, maybe, pas))
    db.commit()
    results["summary"] = {
        "total_active": total_active, "new_this_run": new_this_run,
        "go": go, "maybe": maybe, "pass": pas,
    }
    return jsonify(results)


# ─── Init ───────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
