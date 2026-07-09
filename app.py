import os, json, csv, io, re, sqlite3, hashlib, secrets
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, render_template, g

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "vi_triage.db")

# ─── Database ───────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
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
        is_closed INTEGER DEFAULT 0
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
    try:
        db.execute("ALTER TABLE projects ADD COLUMN closing_date TEXT")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE projects ADD COLUMN is_closed INTEGER DEFAULT 0")
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

# ─── Moat Scorer v2.2 ──────────────────────────────────────────────────

MOATS = [
    ("M1 off-grid/remote", r"\boff[- ]grid\b|no (mains|fixed|grid) power|unpowered|remote (site|area|location|asset)s?|isolated|no (comms|connectivity|network)|starlink|satellite|transmission (line|project|corridor)|wind farm|solar farm|\bbess\b|battery energy|pipeline|quarry|\bmine\b|mining|landfill|\bdam\b|greenfield|renewable energy zone|\brez\b|exploration|remote area|rural area|off-site|off site"),
    ("M2 solar", r"solar.powered|solar power|solar camera|solar panel|solar energy|solar surveillance|\bpv\b|photovoltaic"),
    ("M3 monitored outcome", r"24/7|monitor(ing|ed)|alarm response|command centre|control room|monitoring[- ]as[- ]a[- ]service|asial|virtual patrol|surveillance service|remote monitoring|security monitoring|live monitoring|continuous monitoring"),
    ("M4 AI analytics", r"\bai\b|artificial intelligence|analytics|anpr|number ?plate|licen[cs]e plate|illegal dumping|dumping detection|fire detection|smoke detection|machine learning|computer vision|smart camera|object detection|behavioural detection|heat detection|heatguard|enviroguard|envirosense"),
    ("M5 temp/rapid deploy", r"temporary|relocatable|redeployable|short[- ]term|rapid[- ]deploy|quick[- ]deploy|mobile (cctv|camera|surveillance)|trailer|construction (site|phase|period|work)|site security|laydown|compound|early works|enabling works|hire\b|event security|pop[- ]up"),
    ("M6 traffic/transport analytics", r"traffic count|traffic monitor|traffic management|vehicle count|traffic flow|traffic survey|traffic camera|vehicle detection|axle count|weigh[- ]in[- ]motion|wim|loadSure|load monitoring|freight|haulage|road safety camera|intersection|road count|pedestrian count|transport monitoring"),
]

ASSET_MOAT = {
    "off_grid_solar_security": ["M1 off-grid/remote", "M2 solar"],
    "mobile_rapid_deploy_cctv": ["M5 temp/rapid deploy"],
    "unmanned_site_monitoring": ["M3 monitored outcome", "M1 off-grid/remote"],
    "temporary_event_site_security": ["M5 temp/rapid deploy"],
    "remote_surveillance_monitoring": ["M3 monitored outcome"],
    "license_plate_recognition": ["M4 AI analytics"],
    "traffic_monitoring": ["M6 traffic/transport analytics", "M4 AI analytics"],
    "environmental_monitoring": ["M4 AI analytics", "M1 off-grid/remote"],
}

SECTOR_SCORE = {
    "construction": 6, "mining": 8, "mining/resources": 8, "energy": 6,
    "renewables": 8, "waste": 8, "local government": 5, "water": 5,
    "water/wastewater": 5, "transport": 6, "rail": 4, "roads": 6,
    "roads/tunnels/bridges": 5, "ports": 4, "infrastructure": 4,
    "government": 2, "defence": 1, "agriculture": 6, "events": 7,
    "health": -6, "healthcare": -6, "education": -3, "corrections": -5,
}

HARD_KILLS = [
    (r"maintain(ing|enance)? (of )?(the )?existing|existing (cctv|camera|security) (system|network|infrastructure)|legacy system", -25, "maintenance of existing systems"),
    (r"verkada|genetec|milestone|integriti|inner range|ccure|lenel|third[- ]party (system|camera)", -25, "third-party platform lock-in"),
    (r"nurse call|duress|intercom|paging system", -22, "in-building electronic security"),
    (r"\b(ems|scada|cyber ?security|siem|information security|it security)\b|software (platform|tools)", -25, "IT/OT/software security"),
    (r"fit[- ]?out|refurbish|landscap", -20, "fit-out/refurb works"),
    (r"locksmith|keying|master key", -20, "locksmith"),
    (r"lift|elevator|hvac|fire (alarm|panel|system maintenance)", -18, "building services"),
    (r"upgrade (of )?(the )?existing|replacement of (the )?existing", -16, "upgrade/replace installed systems"),
]

SOFT_FLAGS = [
    (r"guard(s|ing)?\b|manned|static security|patrol(s|ling)?|concierge", -14, "guarding in scope"),
    (r"permanent (cctv|camera|installation)|fixed (cctv|camera|pole)|supply,? (install(ation)?|and install)", -12, "fixed-install component"),
    (r"access control (system|upgrade|installation|and)", -10, "access-control works"),
    (r"cabling|fibre|fiber|structured network|network infrastructure|conduit|trench", -12, "cabling/network works"),
    (r"indoor|in[- ]building|internal (cctv|camera)", -10, "indoor scope"),
    (r"body[- ]?worn", -12, "body-worn"),
    (r"weapons? detection|x[- ]ray|screening", -12, "screening tech"),
    (r"panel|standing offer|preferred supplier", -5, "panel — price-comparison dynamic"),
    (r"repairs", -8, "repair scope"),
]

MOAT_SCALE = {0: 0, 1: 14, 2: 30, 3: 42, 4: 50, 5: 56, 6: 60}

def score_project(row):
    text = " ".join([
        row.get("project_name", ""), row.get("signal_summary", ""),
        row.get("notes", ""), row.get("stage", ""),
        row.get("opportunity_type", ""),
        row.get("asset_type", "").replace("_", " "),
        row.get("sector", ""),
    ]).lower()

    moats = []
    for name, pat in MOATS:
        if re.search(pat, text):
            moats.append(name)
    for m in ASSET_MOAT.get(row.get("asset_type", ""), []):
        if m not in moats:
            moats.append(m)
    moats.sort()
    nm = len(moats)

    score = 25 + MOAT_SCALE.get(nm, 56)
    score += SECTOR_SCORE.get(row.get("sector", "").strip().lower(), 0)

    flags = []
    for pat, w, label in HARD_KILLS:
        if re.search(pat, text):
            score += w
            flags.append("HARD: " + label)

    softmult = 1.0 if nm <= 1 else (0.6 if nm == 2 else 0.35)
    for pat, w, label in SOFT_FLAGS:
        if re.search(pat, text):
            score += w * softmult
            flags.append(label)

    loc = row.get("location", "").upper()
    if not ("AU" in loc or "NZ" in loc):
        score -= 25
        flags.append("HARD: outside AU/NZ")

    score = int(round(max(0, min(100, score))))
    commodity = nm == 0
    if commodity:
        score = min(score, 45)

    verdict = "GO" if score >= 70 else ("MAYBE" if score >= 50 else "PASS")

    ot = row.get("opportunity_type", "")
    stage = row.get("stage", "")
    if ot == "Pre-market/Upcoming":
        action = "MONITOR (pre-market)"
    elif ot in ("EPC package", "Compliance-driven uplift"):
        action = "BD PLAY (sell direct)"
    elif re.search(r"open|eoi|rfq|rft|rfp|tender|panel|grant", (ot + " " + stage).lower()):
        action = "ACT NOW (open)"
    else:
        action = "REVIEW"

    return {
        "doability_score": score,
        "verdict": verdict,
        "action_bucket": action,
        "moats": "; ".join(moats),
        "moat_count": nm,
        "risk_flags": "; ".join(flags[:6]),
        "commodity": "yes" if commodity else "",
    }


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

    for row in reader:
        if row.get("active", "").lower() != "true":
            continue
        total += 1
        h = make_hash(row)
        scores = score_project(row)

        # Extract closing date from stage field
        stage_text = row.get("stage", "") + " " + row.get("notes", "")
        closing_date = extract_closing_date(stage_text)
        ds = deadline_status(closing_date)
        is_closed = 1 if ds == 'closed' else 0

        existing = db.execute("SELECT id, first_seen_at FROM projects WHERE project_hash = ?", (h,)).fetchone()

        if existing:
            db.execute("""UPDATE projects SET
                doability_score=?, verdict=?, action_bucket=?, moats=?, moat_count=?,
                risk_flags=?, commodity=?, last_seen_at=?, run_id=?,
                stage=?, opportunity_type=?, priority_score=?, signal_summary=?, notes=?, active=?,
                closing_date=?, is_closed=?
                WHERE project_hash=?""",
                (scores["doability_score"], scores["verdict"], scores["action_bucket"],
                 scores["moats"], scores["moat_count"], scores["risk_flags"], scores["commodity"],
                 now, run_id,
                 row.get("stage", ""), row.get("opportunity_type", ""),
                 row.get("priority_score", ""), row.get("signal_summary", ""),
                 row.get("notes", ""), row.get("active", ""),
                 closing_date, is_closed,
                 h))
        else:
            new_count += 1
            vals = {f: row.get(f, "") for f in PROJECT_FIELDS}
            db.execute("""INSERT INTO projects
                (project_hash, project_name, customer, location, asset_type, sector,
                 stage, year, source_url, socI_relevance, opportunity_type, priority_score,
                 signal_summary, notes, active,
                 doability_score, verdict, action_bucket, moats, moat_count,
                 risk_flags, commodity, first_seen_at, last_seen_at, run_id,
                 closing_date, is_closed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (h, vals["project_name"], vals["customer"], vals["location"],
                 vals["asset_type"], vals["sector"], vals["stage"], vals["year"],
                 vals["source_url"], vals["socI_relevance"], vals["opportunity_type"],
                 vals["priority_score"], vals["signal_summary"], vals["notes"], vals["active"],
                 scores["doability_score"], scores["verdict"], scores["action_bucket"],
                 scores["moats"], scores["moat_count"], scores["risk_flags"], scores["commodity"],
                 now, now, run_id, closing_date, is_closed))

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
    now = datetime.utcnow().isoformat()
    db.execute("""INSERT INTO triage (project_hash, decision, reason, decided_by, decided_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(project_hash) DO UPDATE SET
        decision=excluded.decision, reason=excluded.reason,
        decided_by=excluded.decided_by, decided_at=excluded.decided_at""",
        (project_hash, decision, reason, decided_by, now))
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


# ─── Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/projects")
def api_projects():
    db = get_db()
    hide_closed = request.args.get("hide_closed", "1") == "1"
    where = "WHERE p.active = 'true'"
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
