"""
Access control. Until 9 Oct 2026 every route was public: anyone with the Railway
URL could read every decision and contact, change triage, upload CSVs, merge or
hide rows, and trigger emails.

People sign in with the team password (APP_PASSWORD) and their name, so
decisions and comments are attributed. Scheduled jobs (the Cowork research and
digest tasks) send X-API-Token: $API_TOKEN instead. The inbox poller's
/api/ingest/email keeps its own INGEST_TOKEN.

Fails closed: with APP_PASSWORD unset nobody can sign in. Set it in Railway
before deploying.
"""
import hashlib
import hmac
import os
import time
from datetime import timedelta
from urllib.parse import urlparse

from flask import g, jsonify, redirect, render_template, request, session

PUBLIC_ENDPOINTS = {"login", "logout", "healthz", "static"}
# endpoints that do their own token check
SELF_AUTH_ENDPOINTS = {"api_ingest_email"}

_failures = {}          # ip -> (count, first_ts); slows down password guessing


def _env(name):
    return os.environ.get(name, "").strip()


def token_ok(supplied, env_name):
    """Constant-time compare; an unset token never matches (fail closed)."""
    expected = _env(env_name)
    return bool(expected) and bool(supplied) and hmac.compare_digest(str(supplied), expected)


def current_user():
    try:
        return getattr(g, "user", None) or session.get("user")
    except RuntimeError:
        return None


def _same_origin():
    """Cookie-authenticated writes must come from this site (CSRF guard)."""
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        return True     # same-origin fetch from older browsers / curl with a session
    return urlparse(origin).netloc == request.host


def init(app):
    secret = _env("SECRET_KEY") or hashlib.sha256(
        ("vi-triage|" + _env("APP_PASSWORD") + "|" + _env("DATABASE_URL")).encode()).hexdigest()
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env("COOKIE_INSECURE") != "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    )

    @app.before_request
    def _gate():
        ep = request.endpoint or ""
        if ep in PUBLIC_ENDPOINTS or ep in SELF_AUTH_ENDPOINTS:
            return None
        if token_ok(request.headers.get("X-API-Token"), "API_TOKEN"):
            g.user = "api"
            return None
        if session.get("user"):
            if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin():
                return jsonify({"ok": False, "error": "cross-site request refused"}), 403
            return None
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "sign in required"}), 401
        return redirect("/login?next=" + request.full_path.rstrip("?"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        configured = bool(_env("APP_PASSWORD"))
        error = None
        nxt = request.values.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if request.method == "POST":
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
            n, first = _failures.get(ip, (0, time.time()))
            if time.time() - first > 900:
                n, first = 0, time.time()
            name = (request.form.get("name") or "").strip()[:60]
            if n >= 10:
                error = "Too many attempts. Wait 15 minutes."
            elif not configured:
                error = "Sign-in is not set up yet (APP_PASSWORD is missing in Railway)."
            elif not name:
                error = "Enter your name so decisions and comments are attributed."
            elif token_ok(request.form.get("password"), "APP_PASSWORD"):
                _failures.pop(ip, None)
                session.clear()
                session.permanent = True
                session["user"] = name
                return redirect(nxt)
            else:
                _failures[ip] = (n + 1, first)
                time.sleep(1)
                error = "Wrong password."
        return render_template("login.html", error=error, next=nxt, configured=configured,
                               names=[x.strip() for x in _env("TEAM_NAMES").split(",") if x.strip()])

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.context_processor
    def _inject_user():
        return {"current_user": current_user()}
