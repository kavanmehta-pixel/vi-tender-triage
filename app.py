import os, json, csv, io, re, sqlite3, hashlib, secrets
from datetime import datetime, timedelta
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
        run_id TEXT
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
    CREATE INDEX IF NOT EXISTS idx_project_hash ON projects(project_hash);
    CREATE INDEX IF NOT EXISTS idx_run_id ON projects(run_id);
    CREATE INDEX IF NOT EXISTS idx_verdict ON projects(verdict);
    """)
    db.close()

# ─── Moat Scorer v2.2 ──────────────────────────────────────────────────

MOATS = [
    ("M1 off-grid/remote", r"\boff[- ]grid\b|no (mains|fixed|grid) power|unpowered|remote (site|area|location|asset)s?|isolated|no (comms|connectivity|network)|starlink|satellite|transmission (line|project|corridor)|wind farm|solar farm|\bbess\b|battery energy|pipeline|quarry|\bmine\b|mining|landfill|\bdam\b|greenfield|renewable energy zone|\brez\b|exploration"),
    ("M2 solar", r"solar"),
    ("M3 monitored outcome", r"24/7|monitor(ing|ed)|alarm response|command centre|control room|monitoring[- ]as[- ]a[- ]service|asial|virtual patrol|surveillance service"),
    ("M4 AI analytics", r"\bai\b|analytics|anpr|number ?plate|licen[cs]e plate|illegal dumping|dumping detection|fire detection|smoke detection|traffic count|machine learning|computer vision|smart camera"),
    ("M5 temp/rapid deploy", r"temporary|relocatable|redeployable|short[- ]term|rapid[- ]deploy|quick[- ]deploy|mobile (cctv|camera|surveillance)|trailer|construction (site|phase|period|work)|site security|laydown|compound|early works|enabling works|hire\b|event"),
]

ASSET_MOAT = {
    "off_grid_solar_security": ["M1 off-grid/remote", "M2 solar"],
    "mobile_rapid_deploy_cctv": ["M5 temp/rapid deploy"],
    "unmanned_site_monitoring": ["M3 monitored outcome", "M1 off-grid/remote"],
    "temporary_event_site_security": ["M5 temp/rapid deploy"],
    "remote_surveillance_monitoring": ["M3 monitored outcome"],
    "license_plate_recognition": ["M4 AI analytics"],
}

SECTOR_SCORE = {
    "construction": 6, "mining": 8, "mining/resources": 8, "energy": 6,
    "renewables": 8, "waste": 8, "local government": 5, "water": 5,
    "water/wastewater": 5, "transport": 4, "rail": 4, "roads": 4,
    "roads/tunnels/bridges": 4, "ports": 4, "infrastructure": 4,
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

MOAT_SCALE = {0: 0, 1: 14, 2: 30, 3: 42, 4: 50, 5: 56}

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
        existing = db.execute("SELECT id, first_seen_at FROM projects WHERE project_hash = ?", (h,)).fetchone()

        if existing:
            db.execute("""UPDATE projects SET
                doability_score=?, verdict=?, action_bucket=?, moats=?, moat_count=?,
                risk_flags=?, commodity=?, last_seen_at=?, run_id=?,
                stage=?, opportunity_type=?, priority_score=?, signal_summary=?, notes=?, active=?
                WHERE project_hash=?""",
                (scores["doability_score"], scores["verdict"], scores["action_bucket"],
                 scores["moats"], scores["moat_count"], scores["risk_flags"], scores["commodity"],
                 now, run_id,
                 row.get("stage", ""), row.get("opportunity_type", ""),
                 row.get("priority_score", ""), row.get("signal_summary", ""),
                 row.get("notes", ""), row.get("active", ""),
                 h))
        else:
            new_count += 1
            vals = {f: row.get(f, "") for f in PROJECT_FIELDS}
            db.execute("""INSERT INTO projects
                (project_hash, project_name, customer, location, asset_type, sector,
                 stage, year, source_url, socI_relevance, opportunity_type, priority_score,
                 signal_summary, notes, active,
                 doability_score, verdict, action_bucket, moats, moat_count,
                 risk_flags, commodity, first_seen_at, last_seen_at, run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (h, vals["project_name"], vals["customer"], vals["location"],
                 vals["asset_type"], vals["sector"], vals["stage"], vals["year"],
                 vals["source_url"], vals["socI_relevance"], vals["opportunity_type"],
                 vals["priority_score"], vals["signal_summary"], vals["notes"], vals["active"],
                 scores["doability_score"], scores["verdict"], scores["action_bucket"],
                 scores["moats"], scores["moat_count"], scores["risk_flags"], scores["commodity"],
                 now, now, run_id))

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


# ─── Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/projects")
def api_projects():
    db = get_db()
    rows = db.execute("""
        SELECT *, CASE WHEN first_seen_at = last_seen_at AND run_id = (
            SELECT id FROM runs ORDER BY created_at DESC LIMIT 1
        ) THEN 1 ELSE 0 END as is_new
        FROM projects WHERE active = 'true'
        ORDER BY doability_score DESC
    """).fetchall()
    return jsonify([dict(r) for r in rows])


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
