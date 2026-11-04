"""Web app + scheduler for the AutoCompress container."""
import hmac
import json
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import Flask, jsonify, redirect, request, send_from_directory, session

import mounts
import settings
from engine import Engine

CONFIG_DIR = settings.CONFIG_DIR
os.makedirs(CONFIG_DIR, exist_ok=True)

def install_rar_key():
    """A RAR license key (rarreg.key) in the app data folder is copied to where RAR looks for it."""
    src = os.path.join(CONFIG_DIR, "rarreg.key")
    if os.path.isfile(src):
        try:
            import shutil
            shutil.copyfile(src, "/etc/rarreg.key")
            print("[rar] license key found in the app data folder and installed", flush=True)
        except OSError as e:
            print(f"[rar] could not install {src}: {e}", flush=True)


install_rar_key()
engine = Engine(os.path.join(CONFIG_DIR, "logs"), os.path.join(CONFIG_DIR, "history.json"))
app = Flask(__name__, static_folder="static", static_url_path="/static")

# ---------------------------------------------------------------- login
# Set UI_PASSWORD to require a login. An empty UI_USERNAME falls back to "admin".
BUILD = {"number": os.environ.get("BUILD_NUMBER") or "dev", "sha": (os.environ.get("BUILD_SHA") or "")[:7],
         "date": os.environ.get("BUILD_DATE") or ""}
print(f"[build] AutoCompress build {BUILD['number']}" + (f" ({BUILD['sha']}, {BUILD['date']})" if BUILD["sha"] else ""),
      flush=True)
UI_USER = (os.environ.get("UI_USERNAME") or "admin").strip()
UI_PASS = os.environ.get("UI_PASSWORD", "")
AUTH_ON = bool(UI_PASS)


def _secret_key():
    """Session signing key, kept in the app data folder so logins survive a restart."""
    path = os.path.join(CONFIG_DIR, "secret.key")
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except OSError:
        pass
    return key


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_NAME="autocompress_session", SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=30))
print(f"[auth] login required, user '{UI_USER}'" if AUTH_ON
      else "[auth] UI_PASSWORD not set - the web UI is open to anyone who can reach it", flush=True)

_fails = {}   # ip -> timestamps of recent failed logins


def _locked(ip):
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 300]
    return len(_fails[ip]) >= 8


def creds_ok(user, password):
    return (hmac.compare_digest((user or "").strip().lower().encode(), UI_USER.lower().encode())
            and hmac.compare_digest((password or "").encode(), UI_PASS.encode()))


@app.before_request
def require_login():
    if not AUTH_ON or request.path in ("/health", "/login", "/static/theme.js"):
        return None
    if session.get("ok"):
        return None
    a = request.authorization                     # scripts may still send HTTP basic auth
    if a and creds_ok(a.username, a.password):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Login required", login=True), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ON:
        return redirect("/")
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _locked(ip):
            print(f"[auth] login blocked (too many failures) from {ip}", flush=True)
            return redirect("/login?e=locked")
        if creds_ok(request.form.get("username"), request.form.get("password")):
            session.clear()
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        _fails.setdefault(ip, []).append(time.time())
        print(f"[auth] failed login for user '{(request.form.get('username') or '')[:40]}' from {ip}", flush=True)
        return redirect("/login?e=bad")
    if session.get("ok"):
        return redirect("/")
    return send_from_directory(app.static_folder, "login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.after_request
def no_cache(resp):
    if request.path.startswith("/api/") or request.path in ("/", "/login"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- scheduler
class Scheduler(threading.Thread):
    """Starts a scan every N minutes (counted from the end of the previous one)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.started = time.time()
        self.last_start = None
        self.next_run = None
        self._start_run_done = False

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - never let the scheduler die
                engine.log("error", f"Scheduler error: {e}")
            time.sleep(2)

    def tick(self):
        if engine.running:
            self.next_run = None
            return
        cfg = settings.load()
        if not cfg["schedule_enabled"] or not cfg["sources"]:
            self.next_run = None
            return
        # The interval counts from the end of the last scan (scheduled or manual). Take that time
        # from the engine itself: a scan of empty folders can start and finish between two ticks,
        # so watching for "running" would miss it and start the next scan at once (a scan loop).
        finished = engine.status.get("finished_at") or 0
        last = max(self.started, finished, self.last_start or 0)
        if cfg["run_on_start"] and not self._start_run_done and not finished:
            self.next_run = self.started
        else:
            self.next_run = last + cfg["interval_minutes"] * 60
        if time.time() >= self.next_run:
            self._start_run_done = True
            self.last_start = time.time()
            engine.start(cfg, trigger="schedule")


scheduler = Scheduler()
if not os.environ.get("NO_SCHEDULER"):
    scheduler.start()


# ---------------------------------------------------------------- pages / API
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    return jsonify(ok=True, build=BUILD["number"])


@app.route("/api/status")
def api_status():
    cfg = settings.load()
    return jsonify(
        engine=engine.snapshot(),
        schedule={"enabled": cfg["schedule_enabled"], "interval": cfg["interval_minutes"],
                  "next_run": scheduler.next_run},
        build=BUILD,
        now=time.time(),
    )


@app.route("/api/log")
def api_log():
    after = request.args.get("after", 0, type=int)
    entries = engine.log_since(after)
    return jsonify(entries=entries, last=engine.log_seq, cleared=engine.log_cleared)


UI_PATH = os.path.join(CONFIG_DIR, "ui.json")
UI_DEFAULTS = {"theme": "default", "radius": 10, "density": "comfortable", "font": "", "shadow": "none",
               "width": "wide", "mode": "auto"}
UI_CHOICES = {"density": ("compact", "comfortable", "spacious"), "font": ("", "system", "rounded", "serif", "mono"),
              "shadow": ("none", "soft", "strong"), "width": ("standard", "wide", "full"),
              "mode": ("auto", "light", "dark")}
UI_THEMES = ("default", "unraid", "midnight", "nord", "dracula", "solarized", "forest", "rose", "glass",
             "contrast", "terminal")


def load_ui():
    try:
        with open(UI_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return clean_ui(data if isinstance(data, dict) else {})


def clean_ui(data):
    out = dict(UI_DEFAULTS)
    for k, choices in UI_CHOICES.items():
        if data.get(k) in choices:
            out[k] = data[k]
    if data.get("theme") in UI_THEMES:
        out["theme"] = data["theme"]
    try:
        out["radius"] = max(0, min(24, int(data.get("radius", out["radius"]))))
    except (TypeError, ValueError):
        pass
    return out


@app.route("/api/ui", methods=["GET", "POST"])
def api_ui():
    """Look and feel (theme, corners, density, font, shadows, page width, default light/dark mode)."""
    if request.method == "POST":
        ui = clean_ui({**load_ui(), **(request.get_json(silent=True) or {})})
        try:
            tmp = UI_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(ui, f, indent=2)
            os.replace(tmp, UI_PATH)
        except OSError as e:
            return jsonify(error=f"Could not save the look: {e}"), 500
        return jsonify(ok=True, ui=ui)
    return jsonify(ui=load_ui())


@app.route("/api/stats")
def api_stats():
    return jsonify(stats=engine.stats.snapshot(), now=time.time())


@app.route("/api/stats/reset", methods=["POST"])
def api_stats_reset():
    engine.stats.reset()
    engine.log("info", "Statistics were reset")
    return jsonify(ok=True)


@app.route("/api/log/clear", methods=["POST"])
def api_log_clear():
    engine.clear_log()
    return jsonify(ok=True)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        clean, errors = settings.save(data)
        if errors:
            return jsonify(ok=False, errors=errors), 400
        return jsonify(ok=True, settings=settings.public(clean))
    cfg, problems, source = settings.load_with_report()
    mnt = mounts.list_mounts(CONFIG_DIR)
    storage = dict(settings.storage_info(), source=source,
                   config_mapped=any(m["config"] for m in mnt))
    return jsonify(
        settings=settings.public(cfg),
        problems=problems,
        storage=storage,
        tools=engine.tools,
        choices={"levels": settings.LEVELS, "dictionaries": settings.DICTIONARIES},
        sources_info={p: mounts.path_info(p, mnt) for p in cfg["sources"]},
        move_info=mounts.path_info(cfg["move_dir"], mnt) if cfg["move_dir"] else None,
        output_info=mounts.path_info(cfg["output_dir"], mnt) if cfg["output_dir"] else None,
        auth_enabled=AUTH_ON,
    )


@app.route("/api/mounts")
def api_mounts():
    """Folders mapped into the container (container paths), for the Settings page."""
    return jsonify(mounts=mounts.list_mounts(CONFIG_DIR), config_dir=CONFIG_DIR)


@app.route("/api/pathinfo")
def api_pathinfo():
    """Live check of a typed path, so the page can warn before saving."""
    p = os.path.normpath(request.args.get("path") or "/")
    return jsonify(info=mounts.path_info(p, mounts.list_mounts(CONFIG_DIR)))


@app.route("/api/run", methods=["POST"])
def api_run():
    body = request.get_json(silent=True) or {}
    cfg = settings.load()
    if not cfg["sources"]:
        return jsonify(ok=False, error="Add at least one source folder in Settings first."), 400
    if engine.running:
        return jsonify(ok=False, error="A scan is already running."), 409
    engine.start(cfg, trigger="manual", dry_run=bool(body.get("dry_run")), verify_only=bool(body.get("verify")))
    return jsonify(ok=True)


@app.route("/api/run-folder", methods=["POST"])
def api_run_folder():
    """One-off job: compress a single folder (or each sub-folder of it) now, whatever the configured source
    folders and schedule say. Uses the saved compression settings; the folder action and the output folder
    can be overridden for this run only."""
    body = request.get_json(silent=True) or {}
    path = os.path.normpath(str(body.get("path") or ""))
    mode = body.get("mode") if body.get("mode") in ("folder", "children") else "folder"
    if not os.path.isabs(path) or not os.path.isdir(path):
        return jsonify(ok=False, error="Choose an existing folder (a path inside the container)."), 400
    if settings._within(path, settings.CONFIG_DIR):
        return jsonify(ok=False, error="That is the app data folder - choose a folder such as /source."), 400
    if engine.running:
        return jsonify(ok=False, error="A scan is already running. Wait for it to finish or stop it first."), 409
    cfg = settings.load()
    action = str(body.get("source_action") or "")
    if action:
        cfg["source_action"] = action
    if "output_dir" in body and str(body.get("output_dir") or "").strip():
        cfg["output_dir"] = str(body["output_dir"]).strip()
    if action == "move" and str(body.get("move_dir") or "").strip():
        cfg["move_dir"] = str(body["move_dir"]).strip()
    cfg["sources"] = [path] if mode == "children" else [os.path.dirname(path) or "/"]
    cfg["min_age_minutes"] = 0          # the user chose this folder on purpose
    cfg["checksum_existing"] = False
    cfg, errors = settings.validate(cfg)
    if errors:
        return jsonify(ok=False, error="; ".join(errors)), 400
    engine.start(cfg, trigger="one-off", dry_run=bool(body.get("dry_run")),
                 oneoff={"path": path, "mode": mode})
    return jsonify(ok=True)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    return jsonify(ok=engine.stop())


@app.route("/api/history")
def api_history():
    return jsonify(history=list(reversed(engine.history[-100:])))


@app.route("/api/browse")
def api_browse():
    """List sub-folders so the settings page can offer a folder picker."""
    path = os.path.normpath(request.args.get("path") or "/")
    if not os.path.isabs(path):
        path = "/"
    try:
        # hidden folders (.name) are listed too; only AutoCompress's own work folders are left out
        dirs = sorted((e.name for e in os.scandir(path)
                       if e.is_dir() and not e.name.startswith(".autocompress-")),
                      key=lambda n: (n.lstrip(".").lower(), n))[:2000]
    except OSError as e:
        return jsonify(path=path, parent=os.path.dirname(path), dirs=[], error=str(e))
    return jsonify(path=path, parent=os.path.dirname(path) if path != "/" else None, dirs=dirs)


_cfg, _problems, _src = settings.load_with_report()
if _src == "defaults":          # first start: write the settings file now so it always exists in the app data folder
    _saved, _errs = settings.save(_cfg)
    if _saved is None:
        print(f"[settings] could not create the settings file: {'; '.join(_errs)}", flush=True)
    else:
        _src = "file (created at first start)"
print(f"[settings] {settings.SETTINGS_PATH}: {_src}"
      + (f" - ignored invalid values: {'; '.join(_problems)}" if _problems else "")
      + f" | {CONFIG_DIR} is {'a mapped folder (settings persist)' if any(m['config'] for m in mounts.list_mounts(CONFIG_DIR)) else 'NOT a mapped folder - settings will be lost when the container is recreated!'}",
      flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
