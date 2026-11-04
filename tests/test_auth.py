"""Login checks. Run:  python3 -I tests/test_auth.py"""
import importlib
import os
import sys
import tempfile

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
import base64
h = {"Authorization": "Basic " + base64.b64encode(b"Justin:p@ss w0rd:with/odd$chars").decode()}
check("basic auth still works for scripts", m.app.test_client().get("/api/status", headers=h).status_code == 200)
c2 = m.app.test_client()
for _ in range(8):
    c2.post("/login", data={"username": "x", "password": "y"})
r = c2.post("/login", data={"username": "Justin", "password": "p@ss w0rd:with/odd$chars"})
check("lockout after repeated failures", "e=locked" in r.headers["Location"] and c2.get("/api/status").status_code == 401)

# --- blank username falls back to admin
m = load_app(UI_USERNAME="", UI_PASSWORD="secret")
c = m.app.test_client()
c.post("/login", data={"username": "admin", "password": "secret"})
check("blank UI_USERNAME falls back to admin", c.get("/api/status").status_code == 200)

# --- no password = open
m = load_app()
c = m.app.test_client()
check("no password: UI open", c.get("/api/status").status_code == 200 and c.get("/login").status_code == 302)

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
