"""
Local API for the live dashboard — exposes matches+jobs as JSON over HTTP,
and now streams scrapper.py's live log output over a WebSocket so the
dashboard can show it without terminal scrollback limits.

Setup (one-time):
    pip install flask flask-cors flask-socketio
    python3 -c "import keyring; keyring.set_password('linkedinbot', 'dashboard_token', 'pick-a-long-random-string')"

Run: python3 api.py
Then open dashboard.html in your browser (it fetches from 127.0.0.1:5050
and connects to the same address over WebSocket for live logs).
"""

import os
import sys
import signal
import subprocess
import threading

import keyring
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO

# The scraper/matcher live in scripts/, db.py in data/ — both siblings of dashboard/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

db.init_db()

# Keyring first (survives across shells, not visible in `ps`/env dumps), with
# LINKEDINBOT_DASHBOARD_TOKEN as a fallback for shell-scripted or headless runs.
RUN_TOKEN = (
    keyring.get_password("linkedinbot", "dashboard_token")
    or os.environ.get("LINKEDINBOT_DASHBOARD_TOKEN")
)
if not RUN_TOKEN:
    print("WARNING: no dashboard token found — run/trigger endpoints will refuse all requests.")
    print("  Set one via keyring: python3 -c \"import keyring; keyring.set_password('linkedinbot', 'dashboard_token', 'your-token')\"")
    print("  ...or via env:       export LINKEDINBOT_DASHBOARD_TOKEN='your-token'")

SCRAPER_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_run.log")
SCRAPER_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.lock")
scraper_lock = threading.Lock()
active_proc = {"proc": None}  # holds the running Popen so /api/stop-scraper can reach it

MATCHER_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "matcher_run.log")
MATCHER_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "matcher.lock")
matcher_lock = threading.Lock()
active_matcher_proc = {"proc": None}


def require_token(handler):
    def wrapper(*args, **kwargs):
        if not RUN_TOKEN or request.headers.get("X-API-Token") != RUN_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
        return handler(*args, **kwargs)
    wrapper.__name__ = handler.__name__
    return wrapper


def dry_run_enabled(var_name):
    """Reads a dry-run flag from api.py's own environment (set by
    start_api.sh/.bat before launching it). Defaults to True (dry-run) if
    unset, so running api.py directly without the wrapper script stays safe.
    """
    return os.environ.get(var_name, "1").lower() in ("1", "true", "yes")


def clear_stale_lock_file(lock_file_path):
    """If a previous run crashed without releasing the lock file, and the
    PID it recorded isn't actually running anymore, clean it up on startup
    so this run type isn't stuck permanently "locked"."""
    if not os.path.exists(lock_file_path):
        return
    try:
        with open(lock_file_path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # raises OSError if the process doesn't exist
    except (ValueError, ProcessLookupError, OSError):
        os.remove(lock_file_path)


clear_stale_lock_file(SCRAPER_LOCK_FILE)
clear_stale_lock_file(MATCHER_LOCK_FILE)


def run_scraper_background():
    """Runs scrapper.py as a subprocess, streaming each line to the log file
    and to any connected dashboard over the WebSocket in real time.

    Runs in its own process group (start_new_session=True) so a stop request
    can kill the whole group at once — scrapper.py spawns Chrome/chromedriver
    as children, and killing only the parent Python process would orphan
    those instead of actually stopping the browser.
    """
    with open(SCRAPER_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    env = dict(os.environ)
    env.setdefault("SCRAPER_DRY_RUN", "1")  # only fills in if start_api.sh didn't already set it

    with open(SCRAPER_LOG, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(SCRIPTS_DIR, "scrapper.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=PROJECT_ROOT,
            start_new_session=True,
            env=env,
        )
        active_proc["proc"] = proc
        for line in proc.stdout:
            log.write(line)
            log.flush()
            socketio.emit("scraper_log", {"line": line})
        proc.wait()

    active_proc["proc"] = None
    socketio.emit("scraper_done", {"returncode": proc.returncode})
    if os.path.exists(SCRAPER_LOCK_FILE):
        os.remove(SCRAPER_LOCK_FILE)
    scraper_lock.release()


def run_matcher_background():
    """Runs matcher.py as a subprocess with MATCHER_DRY_RUN=1 forced on —
    dashboard-triggered runs always score without writing to the DB. A plain
    `python3 matcher.py` from the terminal (no env var set) still runs the
    real, DB-writing "prod" behavior untouched.
    """
    with open(MATCHER_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    env = dict(os.environ)
    env.setdefault("MATCHER_DRY_RUN", "1")  # only fills in if start_api.sh didn't already set it

    with open(MATCHER_LOG, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(SCRIPTS_DIR, "matcher.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=PROJECT_ROOT,
            start_new_session=True,
            env=env,
        )
        active_matcher_proc["proc"] = proc
        for line in proc.stdout:
            log.write(line)
            log.flush()
            socketio.emit("matcher_log", {"line": line})
        proc.wait()

    active_matcher_proc["proc"] = None
    socketio.emit("matcher_done", {"returncode": proc.returncode})
    if os.path.exists(MATCHER_LOCK_FILE):
        os.remove(MATCHER_LOCK_FILE)
    matcher_lock.release()


@app.route("/api/matches")
def get_matches():
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT j.job_id, j.title, j.company, j.url, m.resume_version, m.score,
               m.missing_skills, m.reasoning, m.applied, m.scored_at
        FROM matches m
        JOIN jobs j ON j.job_id = m.job_id
        ORDER BY m.scored_at DESC
    """)
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]
    cur.close()
    conn.close()

    results = []
    for row in rows:
        record = dict(zip(columns, row))
        if record.get("scored_at"):
            record["scored_at"] = record["scored_at"].isoformat()
        results.append(record)

    return jsonify(results)


@app.route("/api/matches/<job_id>/applied", methods=["POST"])
def set_applied(job_id):
    db.mark_applied(job_id, applied=True)
    return jsonify({"job_id": job_id, "applied": True})


@app.route("/api/matches/<job_id>/applied", methods=["DELETE"])
def unset_applied(job_id):
    db.mark_applied(job_id, applied=False)
    return jsonify({"job_id": job_id, "applied": False})


@app.route("/api/run-scraper", methods=["POST"])
@require_token
def run_scraper():
    acquired = scraper_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({"error": "scraper already running"}), 409
    threading.Thread(target=run_scraper_background, daemon=True).start()
    return jsonify({"status": "started", "dry_run": dry_run_enabled("SCRAPER_DRY_RUN")})


@app.route("/api/stop-scraper", methods=["POST"])
@require_token
def stop_scraper():
    proc = active_proc["proc"]
    if proc is None or proc.poll() is not None:
        return jsonify({"error": "scraper not running"}), 409

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)  # asks Python + Chrome + chromedriver to exit
    except ProcessLookupError:
        pass  # already gone

    def force_kill_if_still_alive():
        if proc.poll() is None:  # SIGTERM didn't finish the job in time
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    threading.Timer(5.0, force_kill_if_still_alive).start()
    return jsonify({"status": "stopping"})


@app.route("/api/run-status")
def run_status():
    return jsonify({
        "scraper_running": scraper_lock.locked(),
        "matcher_running": matcher_lock.locked(),
        "scraper_dry_run": dry_run_enabled("SCRAPER_DRY_RUN"),
        "matcher_dry_run": dry_run_enabled("MATCHER_DRY_RUN"),
    })


@app.route("/api/run-matcher", methods=["POST"])
@require_token
def run_matcher():
    acquired = matcher_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({"error": "matcher already running"}), 409
    threading.Thread(target=run_matcher_background, daemon=True).start()
    return jsonify({"status": "started", "dry_run": dry_run_enabled("MATCHER_DRY_RUN")})


@app.route("/api/stop-matcher", methods=["POST"])
@require_token
def stop_matcher():
    proc = active_matcher_proc["proc"]
    if proc is None or proc.poll() is not None:
        return jsonify({"error": "matcher not running"}), 409

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    def force_kill_if_still_alive():
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    threading.Timer(5.0, force_kill_if_still_alive).start()
    return jsonify({"status": "stopping"})


@app.route("/api/logs/matcher")
def get_matcher_log():
    try:
        with open(MATCHER_LOG) as f:
            return f.read(), 200, {"Content-Type": "text/plain"}
    except FileNotFoundError:
        return "no log yet", 200, {"Content-Type": "text/plain"}


@app.route("/api/logs/scraper")
def get_scraper_log():
    try:
        with open(SCRAPER_LOG) as f:
            return f.read(), 200, {"Content-Type": "text/plain"}
    except FileNotFoundError:
        return "no log yet", 200, {"Content-Type": "text/plain"}


if __name__ == "__main__":
    # Not port 5000: macOS AirPlay Receiver binds *:5000 and shadows this
    # server on IPv6, which is what `localhost` resolves to in the browser.
    #
    # host=0.0.0.0 so other devices on the LAN can reach this at
    # josephs-macbook-pro.local:5050. Because the port is network-facing,
    # the Werkzeug debugger stays off — it's a remote-code-execution surface.
    socketio.run(
        app,
        host="0.0.0.0",
        port=5050,
        debug=True,
        use_reloader=False,  # reloader would spawn a second process and desync the lock
        allow_unsafe_werkzeug=True,
    )
