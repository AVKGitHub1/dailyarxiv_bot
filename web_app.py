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
from itsdangerous import BadSignature, URLSafeTimedSerializer
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import schedule_config as schedule
import slackbot_daily_arxiv as bot
from digest_service import DigestService, new_papers
from web_state import StateStore

BASE_DIR = Path(__file__).resolve().parent

LOGIN_LIMIT = 10  # Failed attempts per 15 minutes, per known browser or per address.
LOGIN_WINDOW = 15 * 60
SUGGEST_LIMIT = 20  # Suggestions per hour, per browser.
SUGGEST_ADDRESS_LIMIT = 60  # Suggestions per hour, per address: the bound a scripted client cannot reset.
DEVICE_COOKIE = "arxiv_admin_device"
DEVICE_MAX_AGE = 180 * 24 * 60 * 60


class RateLimiter:
    """In-memory fixed-window counters, bounded to MAX_KEYS live keys."""

    MAX_KEYS = 4096

    def __init__(self):
        self.entries = {}
        self.lock = threading.Lock()

    def allow(self, *rules, bounded=True):
        """Count one attempt against every (key, limit, window) rule, or against none if any is used up.

        bounded=False admits a new key even when the table is full. Use it only for keys a
        client cannot mint at will, so a flood of other keys cannot crowd them out.
        """
        now = time.monotonic()
        with self.lock:
            self.entries = {key: value for key, value in self.entries.items() if value[0] > now}
            new_keys = sum(1 for key, _limit, _window in rules if key not in self.entries)
            if bounded and new_keys and len(self.entries) + new_keys > self.MAX_KEYS:
                return False
            if any(self.entries.get(key, (0, 0))[1] >= limit for key, limit, _window in rules):
                return False
            for key, _limit, window in rules:
                expiry, count = self.entries.get(key, (now + window, 0))
                self.entries[key] = (expiry, count + 1)
            return True

    def refund(self, key):
        """Give back an attempt that turned out to be legitimate."""
        with self.lock:
            if key in self.entries:
                expiry, count = self.entries[key]
                if count <= 1:
                    del self.entries[key]
                else:
                    self.entries[key] = (expiry, count - 1)


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
    proxy_hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))
    if proxy_hops > 0:
        # Only when the proxy is the sole route to this port (see ARXIV_BIND in
        # compose.yaml); a client reaching the app directly could forge X-Forwarded-For.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops)
    service = DigestService(store, config, os.environ.get("TZ", "America/Los_Angeles"), config_loader=config_loader)
    app.extensions["store"] = store
    app.extensions["digest_service"] = service
    # Separate tables, so a suggestion flood can never fill the one logins use.
    login_limiter, suggest_limiter = RateLimiter(), RateLimiter()
    app.extensions["rate_limiters"] = {"login": login_limiter, "suggest": suggest_limiter}
    # Device cookies grant no privilege, only a login allowance, so they use their own
    # secret: a password reset must not drop every known browser into the shared bucket.
    device_signer = URLSafeTimedSerializer(store.read()["device_secret"], salt="daily-arxiv-admin-device")

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

    def trusted_device():
        """The id of a browser that has signed in as admin before, if its cookie is genuine."""
        token = request.cookies.get(DEVICE_COOKIE)
        if not token:
            return None
        try:
            return device_signer.loads(token, max_age=DEVICE_MAX_AGE)
        except BadSignature:
            return None

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
            "schedule": ", ".join(schedule.TIMES),
            "timezone_name": str(service.timezone),
            "scheduler_enabled": start_scheduler,
        }

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.endpoint not in ("static", "favicon"):
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
            # Behind Docker Desktop's port forwarding or a proxy, every client can
            # share one address. A browser that has signed in before keeps its own
            # allowance, so failed guesses from elsewhere cannot lock the admin out.
            # A first sign-in from a new browser still shares the per-address allowance.
            device = trusted_device()
            key = ("login-device", device) if device else ("login", request.remote_addr)
            if not login_limiter.allow((key, LOGIN_LIMIT, LOGIN_WINDOW), bounded=device is None):
                abort(429, "Too many login attempts. Please wait 15 minutes.")
            valid_password = check_password_hash(store.read()["password_hash"], request.form.get("password", ""))
            if request.form.get("username") == "admin" and valid_password:
                login_limiter.refund(key)  # Only failed attempts count.
                session.clear()
                session["admin"] = True
                session.permanent = True
                response = redirect(url_for("index"))
                response.set_cookie(
                    DEVICE_COOKIE, device_signer.dumps(device or secrets.token_urlsafe(16)),
                    max_age=DEVICE_MAX_AGE, path=url_for("login"), httponly=True, samesite="Lax",
                    secure=app.config["SESSION_COOKIE_SECURE"],
                )
                return response
            flash("Incorrect username or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.post("/suggest")
    def suggest():
        # Per browser, so people sharing one address do not use up each other's
        # allowance; the looser per-address cap still bounds a flood.
        if not suggest_limiter.allow((("suggest", session["csrf_token"]), SUGGEST_LIMIT, 60 * 60),
                                     (("suggest-address", request.remote_addr), SUGGEST_ADDRESS_LIMIT, 60 * 60)):
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

    @app.get("/favicon.ico")
    def favicon():
        # Browsers probe the site root regardless of the <link> tags.
        return app.send_static_file("favicon.ico")

    @app.get("/status")
    def status():
        return jsonify(job=store.read()["job"] if session.get("admin") else None)

    @app.get("/healthz")
    def health():
        store.read()
        if start_scheduler and not service.scheduler_healthy():
            return jsonify(status="scheduler stopped"), 503
        return jsonify(status="ok")

    def error_page(error):
        return render_template("error.html", error=error), error.code

    for code in (400, 403, 404, 413, 429, 500):
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
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
