"""
VI Tender Triage — Weekly Summary Generator (Cowork Task)
=========================================================
Runs every TUESDAY. Produces plain-English summaries for open GO/MAYBE tenders
so Robin and the team can read WHY each scored what it did.

Two ways to run:
1. In Cowork with Claude: paste the exported GO/MAYBE JSON, Claude writes the
   summaries into the ai_summary field, exports enriched CSV.
2. Standalone (this script): builds a structured summary from the moat/flag data
   as a fallback when Claude isn't in the loop.

Output: enriched CSV with an `ai_summary` column, uploaded to the triage app
at /upload (Step 2 — Attach Summaries).
"""
import json, csv, sys

VERDICT_FRAME = {
    "GO": "Strong fit — worth a full assessment.",
    "MAYBE": "Partial fit — needs the tender doc to confirm before committing.",
    "PASS": "Weak fit — likely skip.",
}

MOAT_PLAIN = {
    "M1 off-grid/remote": "remote/off-grid site where VI's solar cameras avoid the cost of running power",
    "M2 solar": "solar-powered deployment plays directly to VI's core product",
    "M3 monitored outcome": "24/7 monitoring requirement suits VI's ASIAL command centre",
    "M4 AI analytics": "AI analytics needs (fire/dumping/ANPR) match VI's SENSE stack",
    "M5 temp/rapid deploy": "temporary/construction-phase need fits VI's relocatable fleet",
    "M6 traffic/transport analytics": "traffic/transport monitoring fits VI's ANPR/LoadSure line",
}

FLAG_PLAIN = {
    "guarding in scope": "mentions guarding (VI can't service — may need a partner)",
    "fixed-install component": "has a fixed-install element outside VI's model",
    "access-control works": "includes access control (outside VI's scope)",
    "cabling/network works": "needs cabling/network infrastructure VI doesn't provide",
    "panel — price-comparison dynamic": "panel arrangement — risk of price-driven competition",
    "indoor scope": "indoor scope where solar/wireless offers no edge",
    "maintenance of existing systems": "maintenance of existing kit — a hard no for VI",
    "third-party platform lock-in": "locks to a third-party platform VI can't match",
}

def build_summary(p):
    name = p["project_name"]
    cust = p.get("customer","")
    score = p.get("doability_score","")
    verdict = p.get("verdict","")
    opp = p.get("opportunity_type","") or "opportunity"
    loc = p.get("location","")
    close = p.get("closing_date")
    moats = [m for m in (p.get("moats") or "").split("; ") if m]
    flags = [f for f in (p.get("risk_flags") or "").split("; ") if f]
    locflag = p.get("location_flag")

    # Sentence 1 — what it is
    s1 = f"{name} ({cust}) is a {opp.lower()} in {loc}."

    # Sentence 2 — why this score
    moat_phrases = [MOAT_PLAIN.get(m, m) for m in moats[:3]]
    if moat_phrases:
        why = "Scored {} because {}".format(score, "; ".join(moat_phrases))
    else:
        why = f"Scored {score} with no clear VI moat — likely commodity CCTV"
    if flags:
        flag_phrases = [FLAG_PLAIN.get(f, f) for f in flags[:2]]
        why += f". Pulled down by: {'; '.join(flag_phrases)}"
    s2 = why + "."

    # Sentence 3 — action + concern
    action_bits = []
    if close:
        action_bits.append(f"closes {close}")
    if locflag:
        action_bits.append(f"note location risk — {locflag}")
    concern = (" (" + "; ".join(action_bits) + ")") if action_bits else ""
    if verdict == "GO":
        s3 = f"Next: download the tender doc and pre-vet with Philip{concern}."
    elif verdict == "MAYBE":
        s3 = f"Next: pull the requirements pack to resolve the flags before deciding{concern}."
    else:
        s3 = f"Next: skip unless scope changes{concern}."

    return f"{s1} {s2} {s3}"

def main():
    data = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/to_summarise.json"))
    for p in data:
        p["ai_summary"] = build_summary(p)
    # Write enriched CSV — must include project_name + customer + source_url so app can hash-match
    fields = ["project_name", "customer", "source_url", "location", "asset_type", "year", "ai_summary"]
    out = sys.argv[2] if len(sys.argv) > 2 else "/home/claude/enriched.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in data:
            w.writerow({k: p.get(k, "") for k in fields})
    print(f"Wrote {len(data)} summaries to {out}")

if __name__ == "__main__":
    main()
