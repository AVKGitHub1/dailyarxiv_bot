"""LAN dashboard and the single scheduler process used by Docker."""

import argparse
import datetime
import hmac
import logging
import os
import secrets
import signal
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from waitress import serve
from werkzeug.security import check_password_hash

import bot_server
import slackbot_daily_arxiv as bot
from digest_service import DigestService, new_papers
from web_state import StateStore

BASE_DIR = Path(__file__).resolve().parent


class RateLimiter:
    def __init__(self):
        self.entries = {}
        self.lock = threading.Lock()

    def allow(self, key, limit, window):
        now = time.monotonic()
        with self.lock:
            self.entries = {key: value for key, value in self.entries.items() if value[0] > now}
            expiry, count = self.entries.get(key, (now + window, 0))
            if count >= limit or (key not in self.entries and len(self.entries) >= 4096):
                return False
            self.entries[key] = (expiry, count + 1)
            return True


def create_app(*, data_dir=None, config=None, seed_dir=None, start_scheduler=False):
    # Only re-read the file we loaded ourselves; an injected config stays fixed.
    config_loader = bot.load_config if config is None else None
    config = config if config is not None else bot.load_config()
    store = StateStore(data_dir or os.environ.get("ARXIV_DATA_DIR", BASE_DIR / "data"), seed_dir or BASE_DIR)
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=store.read()["session_secret"],
        MAX_CONTENT_LENGTH=16 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8),
    )
    service = DigestService(store, config, os.environ.get("TZ", "America/Los_Angeles"), config_loader=config_loader)
    app.extensions["store"] = store
    app.extensions["digest_service"] = service
    limiter = RateLimiter()

    if store.initial_password:
        print("\nDaily arXiv — first launch\nAdmin username: admin\n"
              f"Admin password: {store.initial_password}\n"
              "Save this password; it will not be printed on subsequent launches.\n", flush=True)
        store.initial_password = None

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("admin"):
                abort(403, "Sign in as admin to perform this action.")
            return view(*args, **kwargs)
        return wrapped

    @app.before_request
    def csrf_protection():
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            received = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected.encode(), received.encode()):
                abort(400, "Your form expired. Reload the page and try again.")

    @app.context_processor
    def shared_context():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return {
            "csrf_token": session["csrf_token"],
            "is_admin": bool(session.get("admin")),
            "schedule": ", ".join(bot_server.TIMES),
            "timezone_name": str(service.timezone),
            "scheduler_enabled": start_scheduler,
        }

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.template_filter("local_time")
    def local_time(value):
        return datetime.datetime.fromisoformat(value).astimezone(service.timezone).strftime("%b %d, %Y · %H:%M %Z")

    @app.template_filter("paper_authors")
    def paper_authors(authors):
        if len(authors) > bot.MAX_DISPLAY_AUTHORS:
            return "MANY AUTHORS"
        return ", ".join(authors)

    @app.get("/")
    def index():
        state = store.read()
        return render_template(
            "index.html",
            authors=sorted(state["authors"], key=str.casefold),
            keywords=sorted(state["keywords"], key=str.casefold),
            suggestions=state["suggestions"] if session.get("admin") else [],
            preview=state["preview"], last_message=state["last_message"],
            new_ids={paper["id"] for paper in new_papers(state)},
            job=state["job"], default_date=service.default_date(),
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if not limiter.allow(("login", request.remote_addr), 10, 15 * 60):
                abort(429, "Too many login attempts. Please wait 15 minutes.")
            valid_password = check_password_hash(store.read()["password_hash"], request.form.get("password", ""))
            if request.form.get("username") == "admin" and valid_password:
                session.clear()
                session["admin"] = True
                session.permanent = True
                return redirect(url_for("index"))
            flash("Incorrect username or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.post("/suggest")
    def suggest():
        if not limiter.allow(("suggest", request.remote_addr), 20, 60 * 60):
            abort(429, "Too many suggestions. Please try again in an hour.")
        try:
            store.suggest(request.form.get("kind"), request.form.get("value", ""))
            flash("Suggestion submitted. An admin will review it.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index"))

    @app.post("/admin/suggestions/<int:suggestion_id>")
    @admin_required
    def review(suggestion_id):
        decision = request.form.get("decision")
        if decision not in ("accept", "reject"):
            abort(400, "Choose accept or reject.")
        try:
            store.review(suggestion_id, decision == "accept")
            flash("Suggestion accepted. Regenerate to apply the updated watchlist." if decision == "accept" else "Suggestion dismissed.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index"))

    @app.post("/admin/regenerate")
    @admin_required
    def regenerate():
        try:
            service.start("regenerate", target_date=request.form.get("date"))
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index"))

    @app.post("/admin/send")
    @admin_required
    def send():
        try:
            service.start("send", preview_id=request.form.get("preview_id"))
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index"))

    @app.get("/status")
    def status():
        return jsonify(job=store.read()["job"] if session.get("admin") else None)

    @app.get("/healthz")
    def health():
        store.read()
        if start_scheduler and not service.scheduler_healthy():
            return jsonify(status="scheduler stopped"), 503
        return jsonify(status="ok")

    for code in (400, 403, 404, 413, 429, 500):
        def error_page(error):
            return render_template("error.html", error=error), error.code
        app.register_error_handler(code, error_page)

    if start_scheduler:
        service.start_scheduler()
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset-admin-password", action="store_true")
    args = parser.parse_args()
    if args.reset_admin_password:
        store = StateStore(os.environ.get("ARXIV_DATA_DIR", BASE_DIR / "data"), BASE_DIR)
        print(f"Admin username: admin\nNew admin password: {store.reset_password()}\nRestart the web service to invalidate existing sessions.", flush=True)
        return
    logging.basicConfig(level=logging.INFO)
    app = create_app(start_scheduler=os.environ.get("SCHEDULER_ENABLED", "1") == "1")

    def shutdown(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    try:
        serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8765")), threads=8)
    finally:
        app.extensions["digest_service"].close()


if __name__ == "__main__":
    main()
