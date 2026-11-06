"""Login checks. Run:  python3 -I tests/test_auth.py"""
import base64
import importlib
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def load_app(**env):
    for k in ("UI_USERNAME", "UI_PASSWORD"):
        os.environ.pop(k, None)
    os.environ.update(env, NO_SCHEDULER="1", DATA_DIR=tempfile.mkdtemp(prefix="ac-auth-"))
    for m in ("settings", "engine", "main"):
        sys.modules.pop(m, None)
    return importlib.import_module("main")


# --- login enabled
m = load_app(UI_USERNAME="Justin", UI_PASSWORD="p@ss w0rd:with/odd$chars")
c = m.app.test_client()
check("page redirects to /login", c.get("/").status_code == 302 and c.get("/").headers["Location"].endswith("/login"))
r = c.get("/api/status")
check("api returns 401 json", r.status_code == 401 and r.get_json().get("login") is True)
check("health is open", c.get("/health").status_code == 200)
check("login page is open", c.get("/login").status_code == 200)
r = c.post("/login", data={"username": "Justin", "password": "wrong"})
check("wrong password rejected", r.status_code == 302 and "e=bad" in r.headers["Location"])
r = c.post("/login", data={"username": "Justin", "password": "p@ss w0rd:with/odd$chars"})
check("right password accepted", r.status_code == 302 and r.headers["Location"].endswith("/") and c.get("/api/status").status_code == 200)
check("settings api works when logged in", c.get("/api/settings").get_json()["auth_enabled"] is True)
c.get("/logout")
check("logout locks it again", c.get("/api/status").status_code == 401)
r = c.post("/login", data={"username": "  jUSTIN ", "password": "p@ss w0rd:with/odd$chars"})
check("username is case-insensitive and trimmed", c.get("/api/status").status_code == 200)
h = {"Authorization": "Basic " + base64.b64encode(b"Justin:p@ss w0rd:with/odd$chars").decode()}
check("basic auth still works for scripts", m.app.test_client().get("/api/status", headers=h).status_code == 200)
c2 = m.app.test_client()
for _ in range(8):
    c2.post("/login", data={"username": "x", "password": "y"})
r = c2.post("/login", data={"username": "Justin", "password": "p@ss w0rd:with/odd$chars"})
check("lockout after repeated failures", "e=locked" in r.headers["Location"] and c2.get("/api/status").status_code == 401)


# --- basic auth shares the lockout counter and is logged
import io
from contextlib import redirect_stdout


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


m = load_app(UI_USERNAME="Justin", UI_PASSWORD="secret")
c3 = m.app.test_client()
buf = io.StringIO()
with redirect_stdout(buf):
    for _ in range(8):
        r = c3.get("/api/status", headers=basic("evil\nuser\x1b[31m", "nope"))
log = buf.getvalue()
check("basic auth failure answers 401", r.status_code == 401)
check("basic auth failures are logged", log.count("[auth] failed basic auth") == 8, log)
check("control characters never reach the log line", "\x1b" not in log and log.count("\n") == 8, repr(log))
r = c3.get("/api/status", headers=basic("Justin", "secret"))
check("basic auth is refused while locked, even with the right password", r.status_code == 429, r.status_code)
r = c3.post("/login", data={"username": "Justin", "password": "secret"})
check("the form login is locked by the same counter", "e=locked" in r.headers["Location"])
check("a request without credentials is not counted", len(m._fails.get("127.0.0.1", [])) == 8)
m._fails["127.0.0.1"] = [time.time() - 400] * 8       # the window has passed
r = c3.get("/api/status", headers=basic("Justin", "secret"))
check("lock ends after the window and old entries are pruned", r.status_code == 200 and "127.0.0.1" not in m._fails, m._fails)
m._fails.update({"10.0.0.1": [time.time() - 500], "10.0.0.2": []})
m._locked("10.0.0.3")
check("stale addresses are forgotten", not m._fails, m._fails)
m = load_app(UI_USERNAME="Justin", UI_PASSWORD="secret")
c4 = m.app.test_client()
c4.get("/api/status", headers=basic("Justin", "bad"))
c4.post("/login", data={"username": "Justin", "password": "bad"})
check("form and basic failures add up", len(m._fails["127.0.0.1"]) == 2)

# --- blank username falls back to admin
m = load_app(UI_USERNAME="", UI_PASSWORD="secret")
c = m.app.test_client()
c.post("/login", data={"username": "admin", "password": "secret"})
check("blank UI_USERNAME falls back to admin", c.get("/api/status").status_code == 200)

# --- a body that is not a JSON object is a 400, never a 500
m = load_app()
c = m.app.test_client()
for url in ("/api/settings", "/api/run", "/api/run-folder", "/api/cancel", "/api/ui"):
    bad = []
    for body in ("[1, 2]", "5", '"text"', "null", "true"):
        r = c.post(url, data=body, content_type="application/json")
        if r.status_code != 400 or not (r.get_json() or {}).get("error"):
            bad.append((body, r.status_code))
    check(f"{url}: non-object JSON gives 400 with an error", not bad, bad)
r = c.post("/api/cancel", json={"path": "/nothing"})
check("an object body still works", r.status_code == 409)
r = c.post("/api/ui", data="", content_type="application/json")
check("an empty body is still treated as {}", r.status_code == 200)

# --- no password = open
m = load_app()
c = m.app.test_client()
check("no password: UI open", c.get("/api/status").status_code == 200 and c.get("/login").status_code == 302)

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
