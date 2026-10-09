"""
Emails for the sales team, from the 9 Oct 2026 weekly tenders meeting.

  build_brief()       Monday email to all BDMs: what Kavan/Alister are bidding,
                      what Philip is chasing, then each state's major projects
                      (>= $10m) and new leads. Michael + Nick review it first.
  build_maybes()      monthly sweep: untouched MAYBEs by state, so nothing the
                      weekly meeting skipped is lost.
  build_allocation()  the email a BDM gets the moment a lead is allocated to them.

All HTML is email-safe (tables, inline styles) and every field is escaped.
"""
import html as _html
import re
from datetime import datetime, timedelta
from urllib.parse import quote

import routing
from digest import DASHBOARD_URL

ORDER = routing.STATES + ["OTHER"]


def esc(s):
    return _html.escape(str(s or ""), quote=True)


def link(name, url):
    if url and re.match(r"https?://", str(url), re.I):
        return f'<a href="{esc(url)}" style="color:#1a4f8b;text-decoration:none">{esc(name)}</a>'
    return esc(name)


def dash_link(p, label="Dashboard"):
    return (f'<a href="{DASHBOARD_URL}/?q={quote((p.get("project_name") or "")[:60])}" '
            f'style="color:#8a6d1f;font-size:11px">{esc(label)}</a>')


def _closes(p):
    cd = p.get("closing_date")
    if not cd:
        return ""
    st = p.get("deadline_status")
    color = "#b3434f" if st == "urgent" else "#c27c0e" if st == "soon" else "#555"
    return f'<span style="color:{color};font-weight:600">closes {esc(cd)}</span>'


def _row(p, extra=""):
    bits = [esc(p.get("customer") or ""), esc(p.get("location") or "")]
    if p.get("est_value"):
        bits.append(f'<b>{routing.fmt_money(p["est_value"])}</b>')
    c = _closes(p)
    if c:
        bits.append(c)
    new = ('<span style="background:#fff3d6;color:#8a5a00;font-size:10px;font-weight:700;'
           'padding:1px 5px;border-radius:3px;margin-left:6px">NEW</span>') if p.get("is_new") else ""
    return (f'<tr><td style="padding:7px 0;border-bottom:1px solid #eee;font-size:13px">'
            f'{link(p.get("project_name"), p.get("source_url"))}{new}'
            f'<div style="color:#666;font-size:12px;margin-top:2px">{" · ".join(b for b in bits if b)}</div>'
            f'{extra}</td></tr>')


def _table(rows, empty="Nothing this week."):
    if not rows:
        return f'<p style="color:#999;font-size:13px;margin:4px 0 0">{esc(empty)}</p>'
    return '<table width="100%" cellpadding="0" cellspacing="0">' + "".join(rows) + "</table>"


def _section(title, body, sub=""):
    sub_html = f'<div style="color:#777;font-size:12px;margin:2px 0 6px">{sub}</div>' if sub else ""
    return (f'<tr><td style="padding:18px 28px 4px"><div style="font-size:15px;font-weight:700;'
            f'color:#222">{title}</div>{sub_html}{body}</td></tr>')


def _wrap(title, intro, sections, footer):
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f4f4f4;font-family:-apple-system,Segoe UI,Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:20px 0"><tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;max-width:680px">
  <tr><td style="padding:24px 28px 8px;border-bottom:3px solid #f5a623">
    <div style="font-size:20px;font-weight:800;color:#111">{title}</div>
    <div style="color:#555;font-size:13px;margin-top:6px">{intro}</div></td></tr>
  {''.join(sections)}
  <tr><td style="padding:20px 28px 24px;color:#999;font-size:11px">{footer}
    <div style="margin-top:12px"><a href="{DASHBOARD_URL}" style="display:inline-block;background:#f5a623;color:#000;
    font-weight:700;font-size:13px;padding:9px 20px;border-radius:7px;text-decoration:none">Open the triage dashboard</a></div>
  </td></tr>
</table></td></tr></table></body></html>"""


def _dedupe(rows):
    """The same job often appears from several aggregators; show it once."""
    seen, out = set(), []
    for p in rows:
        k = p.get("dup_key") or p.get("project_hash")
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _is_tenderish(p):
    head = " ".join(str(p.get(k) or "") for k in ("opportunity_type", "stage", "project_name"))
    return bool(routing._TENDERISH.search(head))


def _bdm_label(region, bdms):
    b = routing.bdm_for(region, bdms)
    return f' · <span style="color:#1a4f8b">{esc(b["name"])}</span>' if b else ""


def build_brief(rows, bdms, today=None, per_state=8):
    """rows: load_projects(hide_closed=True) output. Returns (subject, html, stats)."""
    today = today or (datetime.utcnow() + timedelta(hours=11)).date()
    live = [p for p in rows if p.get("lane") != "pass"]

    # when the same job appears twice, keep the copy someone has made a decision on
    live = _dedupe(sorted(live, key=lambda p: 0 if p.get("triage_decision") else 1))
    tender = [p for p in live if p.get("triage_decision") == "full"
              and (p.get("triage_status") or "") not in ("Won", "Lost")]
    review = [p for p in live if p["lane"] == "tender" and p["lane_suggested"] and p.get("is_new")]
    philip = [p for p in live if p.get("triage_decision") == "philip"
              and (p.get("triage_status") or "") not in ("Won", "Lost")]
    tender.sort(key=lambda p: (p.get("closing_date") or "9999", -(p.get("doability_score") or 0)))
    review.sort(key=lambda p: (p.get("closing_date") or "9999", -(p.get("doability_score") or 0)))
    philip.sort(key=lambda p: (-(p.get("est_value") or 0), -(p.get("doability_score") or 0)))

    by_state = {s: {"allocated": [], "major": [], "new": []} for s in ORDER}
    for p in live:
        st = p.get("region") if p.get("region") in by_state else "OTHER"
        if p.get("triage_decision") == "bdm":
            by_state[st]["allocated"].append(p)
        elif (not _is_tenderish(p)) and (p.get("est_value") or 0) >= routing.MAJOR_PROJECT_MIN:
            by_state[st]["major"].append(p)
        elif p.get("is_new") and p["lane"] in ("bdm", "maybe") and not p.get("triage_decision"):
            by_state[st]["new"].append(p)

    sections = []
    sections.append(_section(
        f"Tenders &amp; EOIs we're responding to ({len(tender)})",
        _table([_row(p, f'<div style="font-size:12px;color:#1d7a4f">Owner: {esc(p.get("triage_owner") or "Kavan / Alister")}'
                        f'{" · " + esc(p.get("triage_status")) if p.get("triage_status") else ""}</div>')
                for p in tender[:20]], "No live tender responses this week."),
        "Security is in scope and it fits. Kavan and Alister prepare the response with the local BDM. "
        "If one is in your patch and you know the buyer, tell them."))
    if review:
        sections.append(_section(
            f"New security tenders being reviewed ({len(review)})",
            _table([_row(p) for p in review[:12]])
            + (f'<div style="font-size:12px;color:#888;margin-top:4px">+{len(review) - 12} more in the dashboard</div>'
               if len(review) > 12 else ""),
            "Came in this week with security in scope. Kavan and Alister decide this week whether to bid."))
    sections.append(_section(
        f"Philip - demand gen ({len(philip)})",
        _table([_row(p) for p in philip[:10]], "Nothing with Philip right now.")
        + (f'<div style="font-size:12px;color:#888;margin-top:4px">+{len(philip) - 10} more in the pipeline view</div>'
           if len(philip) > 10 else ""),
        "Big or critical-infrastructure jobs where security isn't in the scope yet. Philip digs in and loops in the BDM."))

    stats = {"tender": len(tender), "review": len(review), "philip": len(philip), "states": {}}
    for st in ORDER:
        b = by_state[st]
        if not (b["allocated"] or b["major"] or b["new"]):
            continue
        b["major"].sort(key=lambda p: (0 if p.get("is_new") else 1, -(p.get("est_value") or 0)))
        b["new"].sort(key=lambda p: -(p.get("doability_score") or 0))
        parts = []
        if b["allocated"]:
            parts.append('<div style="font-size:12px;font-weight:700;color:#1a4f8b;margin-top:8px">Allocated to you</div>'
                         + _table([_row(p, (f'<div style="font-size:12px;color:#555">{esc(p.get("triage_reason"))}</div>'
                                            if p.get("triage_reason") else "")) for p in b["allocated"]]))
        if b["major"]:
            more = len(b["major"]) - per_state
            parts.append('<div style="font-size:12px;font-weight:700;color:#444;margin-top:10px">'
                         f'Major projects $10m+ ({len(b["major"])})</div>'
                         + _table([_row(p) for p in b["major"][:per_state]])
                         + (f'<div style="font-size:12px;color:#888;margin-top:4px">+{more} more in the dashboard '
                            f'(filter by location)</div>' if more > 0 else ""))
        if b["new"]:
            more = len(b["new"]) - 8
            parts.append('<div style="font-size:12px;font-weight:700;color:#444;margin-top:10px">'
                         f'Other new leads worth a look ({len(b["new"])})</div>'
                         + _table([_row(p) for p in b["new"][:8]])
                         + (f'<div style="font-size:12px;color:#888;margin-top:4px">+{more} more in the dashboard</div>'
                            if more > 0 else ""))
        sections.append(_section(esc(routing.STATE_NAMES.get(st, st)) + _bdm_label(st, bdms), "".join(parts)))
        stats["states"][st] = {k: len(v) for k, v in b.items()}

    n_major = sum(len(by_state[s]["major"]) for s in ORDER)
    subject = (f"VI tenders & major projects - week of {today.strftime('%-d %b')} · "
               f"{len(tender)} tenders · {n_major} major projects by state")
    intro = ("Your state's section is below. Major projects are $10m+ where the value is published; "
             "most scraped projects don't state a value, so check <i>Other new leads</i> too. "
             "If a small one is in your patch, contact them directly.")
    footer = ("Sent each Monday from the VI tender triage tool. Lanes agreed at the 9 Oct tenders meeting: "
              "tenders/EOIs with security in scope go to Kavan and Alister; big jobs without a security scope go "
              "to Philip; major projects and smaller leads go to the local BDM. Untouched maybes come monthly.")
    return subject, _wrap("Tenders &amp; major projects this week", intro, sections, footer), stats


def build_maybes(rows, bdms, regions=None, min_score=50, per_state=15, today=None):
    """Untouched MAYBEs, open, by state. regions limits to one BDM's patch."""
    today = today or (datetime.utcnow() + timedelta(hours=11)).date()
    pick = [p for p in rows if (p.get("verdict") == "MAYBE" and not p.get("triage_decision")
                                and (p.get("doability_score") or 0) >= min_score)]
    if regions:
        pick = [p for p in pick if p.get("region") in regions]
    pick = _dedupe(pick)
    by = {}
    for p in pick:
        by.setdefault(p.get("region") if p.get("region") in ORDER else "OTHER", []).append(p)
    sections = []
    for st in ORDER:
        if st not in by:
            continue
        lst = sorted(by[st], key=lambda p: -(p.get("doability_score") or 0))
        more = len(lst) - per_state
        sections.append(_section(
            f"{esc(routing.STATE_NAMES.get(st, st))} ({len(lst)})" + _bdm_label(st, bdms),
            _table([_row(p) for p in lst[:per_state]])
            + (f'<div style="font-size:12px;color:#888;margin-top:4px">+{more} more in the dashboard</div>'
               if more > 0 else "")))
    subject = f"VI tenders - monthly maybes for your region · {len(pick)} to sift · {today.strftime('%b %Y')}"
    intro = ("Tenders the scorer rated MAYBE that nobody has looked at yet. Have a quick sift through your state: "
             "if one is worth a call, set it to <b>Send to BDM</b> in the dashboard with you as owner, or just reach out.")
    return subject, _wrap("Monthly maybes", intro, sections or [_section("Nothing to sift", "")],
                          "Sent monthly from the VI tender triage tool."), len(pick)


def build_allocation(p, bdm_name, allocated_by, note=""):
    """The email a BDM gets when a lead is allocated to them."""
    region = routing.STATE_NAMES.get(p.get("region") or "", p.get("location") or "")
    rows = [
        ("Buyer", esc(p.get("customer"))),
        ("Where", esc(p.get("location"))),
        ("Value", routing.fmt_money(p.get("est_value")) or "not stated"),
        ("Closes", esc(p.get("closing_date") or "not stated")),
        ("Stage", esc(p.get("stage"))),
        ("What it is", esc((p.get("ai_summary") or p.get("signal_summary") or "")[:700])),
    ]
    if note:
        rows.append(("Note from " + esc(allocated_by or "the team"), esc(note)))
    table = "".join(f'<tr><td style="padding:5px 12px 5px 0;color:#777;font-size:12px;vertical-align:top;'
                    f'white-space:nowrap">{k}</td><td style="padding:5px 0;font-size:13px">{v}</td></tr>'
                    for k, v in rows if v)
    body = (f'<tr><td style="padding:16px 28px 4px"><div style="font-size:16px;font-weight:700">'
            f'{link(p.get("project_name"), p.get("source_url"))}</div>'
            f'<table cellpadding="0" cellspacing="0" style="margin-top:10px">{table}</table>'
            f'<p style="font-size:13px;color:#333;margin:14px 0 4px">Please investigate: have a chat, speak to the '
            f'buyer and see whether there is a security or monitoring angle for us. It might not be a full tender, '
            f'but it&rsquo;s a reason to make contact.</p>'
            f'<div style="margin-top:6px">{dash_link(p, "Update it in the dashboard")}</div></td></tr>')
    subject = f"New lead in your region: {(p.get('project_name') or '')[:90]}"
    intro = f"{esc(allocated_by or 'The tenders team')} allocated this to you ({esc(region)})."
    return subject, _wrap(f"Hi {esc(bdm_name.split()[0] if bdm_name else '')}, a lead for you", intro, [body],
                          "Sent automatically when a lead is allocated in the VI tender triage tool.")
