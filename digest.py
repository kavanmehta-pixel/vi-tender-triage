"""
Weekly tenders digest — "what's new, what we decided, where the pipeline is up to".

Sent every Friday after the tenders meeting (Railway cron service runs
send_digest.py), and previewable any time at /digest/preview.

Env vars:
  SMTP_HOST      default smtp.gmail.com
  SMTP_PORT      default 587
  SMTP_USER      sending account (e.g. a visioni.com.au Gmail)
  SMTP_PASS      Gmail App Password (not the account password)
  DIGEST_FROM    default SMTP_USER
  DIGEST_TO      comma-separated recipients
  DASHBOARD_URL  default https://web-production-59089.up.railway.app
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://web-production-59089.up.railway.app")

DEC_LABEL = {"full": "Full tender response", "philip": "Philip reach-out", "pass": "Pass"}
DEC_COLOR = {"full": "#1d7a4f", "philip": "#6b4fa0", "pass": "#b3434f"}

ACTIVE_STATUSES = ("Screening", "Downloading docs", "Drafting response",
                   "With Michael/Alister", "Submitted", "Awaiting outcome")


def _week_ago():
    return (datetime.utcnow() - timedelta(days=7)).isoformat()


def collect(db):
    """Pull everything the digest needs from the DB."""
    week_ago = _week_ago()
    today = datetime.utcnow().date().isoformat()
    horizon = (datetime.utcnow() + timedelta(days=14)).date().isoformat()

    new_this_week = db.execute("""
        SELECT p.project_name, p.customer, p.location, p.doability_score, p.verdict,
               p.closing_date, p.source_url, t.decision AS triage_decision,
               p.seen_via, p.portal_ref
        FROM projects p LEFT JOIN triage t ON t.project_hash = p.project_hash
        WHERE p.active='true' AND (p.hidden=0 OR p.hidden IS NULL) AND p.merged_into IS NULL
          AND p.first_seen_at >= ?
          AND (t.decision IS NULL OR t.decision != 'pass')
        ORDER BY p.doability_score DESC
    """, (week_ago,)).fetchall()

    decided_this_week = db.execute("""
        SELECT p.project_name, p.customer, p.closing_date, p.source_url,
               t.decision, t.reason, t.decided_by, t.owner, t.next_steps, t.scope, t.status
        FROM triage t JOIN projects p ON p.project_hash = t.project_hash
        WHERE t.decided_at >= ? AND t.decision IN ('full','philip')
        ORDER BY CASE t.decision WHEN 'full' THEN 0 WHEN 'philip' THEN 1 ELSE 2 END
    """, (week_ago,)).fetchall()

    pipeline = db.execute("""
        SELECT p.project_name, p.customer, p.closing_date, p.source_url,
               t.decision, t.status, t.owner, t.next_steps, t.scope
        FROM triage t JOIN projects p ON p.project_hash = t.project_hash
        WHERE t.decision IN ('full','philip')
          AND (t.status IS NULL OR t.status NOT IN ('Won','Lost'))
        ORDER BY CASE WHEN p.closing_date IS NULL OR p.closing_date='' THEN 1 ELSE 0 END,
                 p.closing_date ASC
    """).fetchall()

    outcomes = db.execute("""
        SELECT p.project_name, p.customer, t.status, t.owner
        FROM triage t JOIN projects p ON p.project_hash = t.project_hash
        WHERE t.status IN ('Won','Lost') AND t.status_updated_at >= ?
    """, (week_ago,)).fetchall()

    deadlines = db.execute("""
        SELECT p.project_name, p.customer, p.closing_date, p.source_url,
               t.decision, t.owner, t.status
        FROM projects p LEFT JOIN triage t ON t.project_hash = p.project_hash
        WHERE p.active='true' AND (p.is_closed=0 OR p.is_closed IS NULL)
          AND p.closing_date >= ? AND p.closing_date <= ?
          AND (t.decision IN ('full','philip') OR p.verdict='GO')
        ORDER BY p.closing_date ASC
    """, (today, horizon)).fetchall()

    return {
        "new": [dict(r) for r in new_this_week],
        "decided": [dict(r) for r in decided_this_week],
        "pipeline": [dict(r) for r in pipeline],
        "outcomes": [dict(r) for r in outcomes],
        "deadlines": [dict(r) for r in deadlines],
    }


def _esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(name, url):
    name = _esc(name)
    if url:
        return f'<a href="{_esc(url)}" style="color:#1a4f8b;text-decoration:none">{name}</a>'
    return name


def _via_tag(r):
    """'· via email alert' when a tender reached us by alert/manual entry, not the scrape."""
    via = (r.get("seen_via") or "").split(",")
    if "alert" in via:
        return " · via email alert" + (f" ({_esc(r['portal_ref'])})" if r.get("portal_ref") else "")
    if "manual" in via:
        return " · added manually"
    return ""


def build_html(data):
    """Email-safe HTML (tables + inline styles, light background)."""
    today = datetime.utcnow() + timedelta(hours=10)  # AEST-ish for the header date
    n_new, n_pipe = len(data["new"]), len(data["pipeline"])

    def section(title, inner):
        return (f'<tr><td style="padding:22px 28px 6px"><div style="font-size:15px;font-weight:700;'
                f'color:#2a2829;border-bottom:2px solid #f5a623;padding-bottom:6px">{title}</div></td></tr>'
                f'<tr><td style="padding:4px 28px 6px">{inner}</td></tr>')

    def pill(text, color):
        return (f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;'
                f'font-weight:700;color:#fff;background:{color}">{_esc(text)}</span>')

    # New this week (cap the table; the dashboard has the rest)
    overflow = ""
    if len(data["new"]) > 40:
        overflow = (f'<div style="color:#777;font-size:12px;padding:6px 8px">… plus '
                    f'{len(data["new"]) - 40} more on the dashboard.</div>')
        data["new"] = data["new"][:40]
    if data["new"]:
        rows = ""
        for r in data["new"]:
            dec = r.get("triage_decision")
            rows += (f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:13px">'
                     f'{_link(r["project_name"], r.get("source_url"))}'
                     f'<div style="color:#777;font-size:11px">{_esc(r["customer"])} · {_esc(r.get("location",""))}'
                     f'{_via_tag(r)}</div></td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:13px;text-align:center">'
                     f'<b>{r["doability_score"]}</b> {_esc(r["verdict"])}</td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{_esc(r.get("closing_date") or "—")}</td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">'
                     + (pill(DEC_LABEL[dec], DEC_COLOR[dec]) if dec in DEC_LABEL
                        else '<span style="color:#999">not reviewed</span>')
                     + '</td></tr>')
        new_html = (f'<table width="100%" cellpadding="0" cellspacing="0">'
                    f'<tr><td style="font-size:11px;color:#999;padding:4px 8px">TENDER</td>'
                    f'<td style="font-size:11px;color:#999;padding:4px 8px;text-align:center">SCORE</td>'
                    f'<td style="font-size:11px;color:#999;padding:4px 8px">CLOSES</td>'
                    f'<td style="font-size:11px;color:#999;padding:4px 8px">DECISION</td></tr>{rows}</table>{overflow}')
    else:
        new_html = '<div style="color:#777;font-size:13px">No new tenders entered the database this week.</div>'

    # Decisions made this week
    if data["decided"]:
        rows = ""
        for r in data["decided"]:
            extra = " · ".join(x for x in [
                f'Owner: {_esc(r["owner"])}' if r.get("owner") else "",
                f'Next: {_esc(r["next_steps"])}' if r.get("next_steps") else "",
                _esc(r["scope"]) if r.get("scope") else "",
            ] if x)
            extra_html = f'<div style="color:#555;font-size:11px">{extra}</div>' if extra else ""
            reason_part = " — " + _esc(r["reason"]) if r.get("reason") else ""
            rows += (f'<tr><td style="padding:7px 8px;border-bottom:1px solid #eee;font-size:13px">'
                     f'{pill(DEC_LABEL.get(r["decision"], r["decision"]), DEC_COLOR.get(r["decision"], "#777"))} '
                     f'{_link(r["project_name"], r.get("source_url"))}'
                     f'<div style="color:#777;font-size:11px">{_esc(r["customer"])}'
                     f'{reason_part}</div>'
                     f'{extra_html}</td></tr>')
        decided_html = f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
    else:
        decided_html = '<div style="color:#777;font-size:13px">No triage decisions recorded this week.</div>'

    # Pipeline
    if data["pipeline"]:
        rows = ""
        for r in data["pipeline"]:
            rows += (f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:13px">'
                     f'{_link(r["project_name"], r.get("source_url"))}'
                     f'<div style="color:#777;font-size:11px">{_esc(r["customer"])}</div></td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{_esc(DEC_LABEL.get(r["decision"], r["decision"]))}</td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{_esc(r.get("status") or "not started")}</td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{_esc(r.get("owner") or "unassigned")}</td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{_esc(r.get("next_steps") or "—")}</td>'
                     f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{_esc(r.get("closing_date") or "—")}</td></tr>')
        pipe_html = (f'<table width="100%" cellpadding="0" cellspacing="0">'
                     f'<tr><td style="font-size:11px;color:#999;padding:4px 8px">TENDER</td>'
                     f'<td style="font-size:11px;color:#999;padding:4px 8px">DECISION</td>'
                     f'<td style="font-size:11px;color:#999;padding:4px 8px">STATUS</td>'
                     f'<td style="font-size:11px;color:#999;padding:4px 8px">OWNER</td>'
                     f'<td style="font-size:11px;color:#999;padding:4px 8px">NEXT STEP</td>'
                     f'<td style="font-size:11px;color:#999;padding:4px 8px">CLOSES</td></tr>{rows}</table>')
    else:
        pipe_html = '<div style="color:#777;font-size:13px">Pipeline is empty — nothing actively being worked.</div>'

    # Outcomes + deadlines
    outcomes_html = ""
    if data["outcomes"]:
        items = "".join(
            f'<div style="font-size:13px;padding:3px 0">{pill(r["status"], "#1d7a4f" if r["status"]=="Won" else "#b3434f")} '
            f'{_esc(r["project_name"])} <span style="color:#777">({_esc(r["customer"])})</span></div>'
            for r in data["outcomes"])
        outcomes_html = section("Outcomes this week", items)

    deadlines_html = ""
    if data["deadlines"]:
        items = "".join(
            f'<div style="font-size:13px;padding:3px 0"><b>{_esc(r["closing_date"])}</b> — '
            f'{_link(r["project_name"], r.get("source_url"))} '
            f'<span style="color:#777">({_esc(r.get("owner") or "unassigned")}{", " + _esc(r["status"]) if r.get("status") else ""})</span></div>'
            for r in data["deadlines"])
        deadlines_html = section("⏰ Closing in the next 14 days", items)

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f2f3f5;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f2f3f5;padding:20px 0">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e3e5e8">
  <tr><td style="background:#2a2829;padding:20px 28px">
    <div style="color:#fff;font-size:18px;font-weight:700">VI Tenders — Weekly Update</div>
    <div style="color:#f5a623;font-size:12px;margin-top:3px">Week ending {today.strftime('%A %d %B %Y')} ·
      {n_new} new this week · {n_pipe} in pipeline</div>
  </td></tr>
  {section('🆕 New tenders this week', new_html)}
  {section('✅ Decisions made this week', decided_html)}
  {section('📋 Pipeline — where everything is up to', pipe_html)}
  {outcomes_html}
  {deadlines_html}
  <tr><td style="padding:20px 28px 24px">
    <a href="{DASHBOARD_URL}" style="display:inline-block;background:#f5a623;color:#000;font-weight:700;
       font-size:13px;padding:10px 22px;border-radius:7px;text-decoration:none">Open the triage dashboard</a>
    <div style="color:#999;font-size:11px;margin-top:14px">Automated Friday digest from the VI Tender Doability
      Triage tool. Decisions and statuses are set in the dashboard — anything saved there lands in next week's email.</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def send(db):
    """Build and email the digest. Returns (ok, detail)."""
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    to = [t.strip() for t in os.environ.get("DIGEST_TO", "").split(",") if t.strip()]
    sender = os.environ.get("DIGEST_FROM", user)

    data = collect(db)
    html = build_html(data)

    if not (user and password and to):
        return False, "SMTP_USER / SMTP_PASS / DIGEST_TO not configured — digest built but not sent"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"VI Tenders — weekly update · {len(data['new'])} new · "
                      f"{len(data['pipeline'])} in pipeline")
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(sender, to, msg.as_string())
    return True, f"sent to {', '.join(to)}"
