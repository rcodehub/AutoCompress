"""Path / mount handling checks. Run:  python3 -I tests/test_paths.py"""
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


root = tempfile.mkdtemp(prefix="ac-paths-")
cfg, dl, proc, media = (os.path.join(root, d) for d in ("config", "source", "processed", "media"))
for d in (cfg, dl, proc, media):
    os.makedirs(d)
os.chmod(media, 0o555)   # read-only for non-root; root can still write, so only checked loosely


def mi(i, mp, fs, src="x"):
    return f"{i} 1 0:{i} /somewhere {mp} rw,noatime master:1 - {fs} {src} rw"


info = "\n".join([
    "20 1 0:20 / / rw - overlay overlay rw",
    "21 20 0:21 / /proc rw - proc proc rw",
    "22 20 0:22 / /dev rw - tmpfs tmpfs rw",
    "23 20 0:23 /etc/hosts /etc/hosts rw - ext4 /dev/sda1 rw",
    mi(30, cfg, "fuse.shfs", "shfs"),
    mi(31, dl, "fuse.shfs", "shfs"),
    mi(32, proc, "xfs", "/dev/md1"),
    mi(33, media.replace(" ", r"\040"), "btrfs", "/dev/sdb1"),
]) + "\n"
mi_path = os.path.join(root, "mountinfo")
open(mi_path, "w").write(info)

os.environ.update(MOUNTINFO_PATH=mi_path, DATA_DIR=cfg, SOURCE_DIR=dl, TRASH_DIR=proc, NO_SCHEDULER="1")
for k in ("UI_PASSWORD", "UI_USERNAME"):
    os.environ.pop(k, None)
for m in ("mounts", "settings", "engine", "main"):
    sys.modules.pop(m, None)
mounts = importlib.import_module("mounts")
settings = importlib.import_module("settings")
main = importlib.import_module("main")
check("settings.json is created at first start", os.path.isfile(os.path.join(cfg, "settings.json")))

ms = mounts.list_mounts(cfg)
paths = [m["path"] for m in ms]
check("system mounts are hidden", not any(p in ("/", "/proc", "/dev", "/etc/hosts") for p in paths), paths)
check("mapped folders are listed", {cfg, dl, proc, media} <= set(paths), paths)
check("config folder is flagged", [m["path"] for m in ms if m["config"]] == [cfg])
check("free space reported", all(m["free"] is not None for m in ms))

sug = mounts.suggested_sources(cfg)
check("first-run source = /source", sug == [dl], sug)
mounts.SOURCE_DIR = os.path.join(root, "not-mapped")
sug = mounts.suggested_sources(cfg)
check("no /source: other mapped folders, never data or trash", set(sug) == {dl, media}, sug)
mounts.SOURCE_DIR = dl
check("trash detected on the file system", mounts.suggested_move_dir() == proc)
mounts.TRASH_DIR = os.path.join(root, "not-mapped")
check("no trash mapped: delete", mounts.suggested_move_dir() == "")
mounts.TRASH_DIR = proc
s, e = settings.validate({"sources": [cfg]})
check("app data folder refused as a source", e, e)

pi = mounts.path_info(dl + "/sub/dir", ms)
check("path_info: missing folder inside a mount", not pi["exists"] and pi["mount"] == dl and not pi["host_path"], pi)
pi = mounts.path_info("/mnt/user/Downloads", ms)
check("path_info: Unraid host path is recognised", not pi["exists"] and pi["host_path"], pi)
pi = mounts.path_info(dl, ms)
check("path_info: good folder", pi["exists"] and pi["readable"] and pi["writable"] and pi["mount"] == dl, pi)

# validation, both directions
s, e = settings.validate({"sources": [dl], "source_action": "move", "move_dir": dl + "/done"})
check("move folder inside a source is refused", any("must not be inside the source" in x for x in e), e)
s, e = settings.validate({"sources": [proc + "/incoming"], "source_action": "move", "move_dir": proc})
check("source inside the move folder is refused", any("must not be inside the move folder" in x for x in e), e)
s, e = settings.validate({"sources": [dl], "source_action": "move", "move_dir": proc})
check("separate folders are accepted", not e, e)

# first-start settings come from the mapped folders
os.remove(os.path.join(cfg, "settings.json")) if os.path.exists(os.path.join(cfg, "settings.json")) else None
seed = settings.load()
check("first run: source is /source",
      seed["sources"] == [dl], seed["sources"])
check("first run: mapped trash folder becomes the move folder",
      seed["move_dir"] == proc and seed["source_action"] == "move", seed)
check("first run: scheduling stays off", seed["schedule_enabled"] is False)

# API
c = main.app.test_client()
d = c.get("/api/mounts").get_json()
check("api/mounts", {m["path"] for m in d["mounts"]} >= {dl, proc}, d)
d = c.get("/api/pathinfo?path=/mnt/user/foo").get_json()
check("api/pathinfo flags host paths", d["info"]["host_path"] is True, d)
r = c.post("/api/settings", json={"sources": [dl + "/nope", dl], "move_dir": proc, "source_action": "move"})
d = c.get("/api/settings").get_json()
check("api/settings reports per-source info",
      not d["sources_info"][dl + "/nope"]["exists"] and d["sources_info"][dl + "/nope"]["mount"] == dl and d["sources_info"][dl]["exists"] and d["move_info"]["exists"], d.get("sources_info"))

# --- persistence: a bad value must not wipe the good ones; problems and storage are reported
import json
import settings as S
open(S.SETTINGS_PATH, "w").write(json.dumps({"sources": [dl], "parallel_jobs": 7, "interval_minutes": "oops",
                                              "exclude_files": ["*.nfo"], "schedule_enabled": True}))
cfg_now, problems, source = S.load_with_report()
check("lenient load keeps valid values", cfg_now["parallel_jobs"] == 7 and cfg_now["sources"] == [dl]
      and cfg_now["exclude_files"] == ["*.nfo"] and cfg_now["schedule_enabled"] is True, cfg_now)
check("lenient load reports the bad one", source == "file" and any("interval" in x.lower() for x in problems), problems)
# one unmapped source must not take the valid ones with it
open(S.SETTINGS_PATH, "w").write(json.dumps({"sources": [dl, "/nowhere/unmapped"], "parallel_jobs": 5}))
cfg_now, problems, source = S.load_with_report()
check("lenient load drops only the invalid source", cfg_now["sources"] == [dl] and cfg_now["parallel_jobs"] == 5, cfg_now["sources"])
check("lenient load reports the dropped source", any("/nowhere/unmapped" in x for x in problems), problems)
d = c.get("/api/settings").get_json()
check("api/settings exposes problems + storage",
      d["problems"] and d["storage"]["source"] == "file" and d["storage"]["path"] == S.SETTINGS_PATH
      and d["storage"]["saved_at"] and d["storage"]["config_mapped"] is True, d.get("storage"))
open(S.SETTINGS_PATH, "w").write("{not json")
_, problems, source = S.load_with_report()
check("unreadable file reported", source == "unreadable", source)
os.remove(S.SETTINGS_PATH)
check("missing file = defaults", S.load_with_report()[2] == "defaults")
r = c.post("/api/settings", json={"sources": [dl], "parallel_jobs": 3})
check("save works and round-trips", r.status_code == 200 and S.load()["parallel_jobs"] == 3, r.get_data(as_text=True))
check("saved file is valid", c.get("/api/settings").get_json()["storage"]["source"] == "file")
# unwritable location -> clear error, not silent success
real = S.SETTINGS_PATH
S.SETTINGS_PATH = os.path.join(root, "no-such-dir", "x", "settings.json")
S.CONFIG_DIR = "/proc/nonexistent-ea"  # unwritable
res, errs = S.save({"sources": [dl]})
check("save failure is reported", res is None and errs and "Could not store" in errs[0], errs)
S.SETTINGS_PATH, S.CONFIG_DIR = real, cfg

# --- server (host) paths behind each mapping
check("host: user share", mounts.host_path("fuse.shfs", "shfs", "/source") == "/mnt/user/source")
check("host: array disk", mounts.host_path("xfs", "/dev/md1p1", "/trash") == "/mnt/disk1/trash")
check("host: zfs dataset", mounts.host_path("zfs", "cache/appdata", "/autoextract") == "/mnt/cache/appdata/autoextract")
check("host: unknown pool", mounts.host_path("btrfs", "/dev/nvme0n1p1", "/appdata") is None)
ms2 = mounts.list_mounts(cfg)
dlm = [m for m in ms2 if m["path"] == dl][0]
check("host: shown for a mapped folder", dlm["host"] == "/mnt/user/somewhere", dlm)
pi = mounts.path_info(dl + "/sub", ms2)
check("host: path inside a mapping", pi["host"] == "/mnt/user/somewhere/sub", pi)

# --- hidden folders are listed in the folder picker
os.makedirs(os.path.join(dl, ".hidden"), exist_ok=True)
os.makedirs(os.path.join(dl, ".autocompress-tmp1"), exist_ok=True)
os.makedirs(os.path.join(dl, "Visible"), exist_ok=True)
d = c.get("/api/browse?path=" + dl).get_json()
check("browse: hidden folders listed, work folders not", ".hidden" in d["dirs"] and "Visible" in d["dirs"]
      and not any(x.startswith(".autocompress-") for x in d["dirs"]), d)

# --- the picker only shows mapped folders
d = c.get("/api/browse").get_json()
check("browse: top level lists only the mapped folders (not app data)",
      d["top"] and d["path"] == "" and sorted(d["dirs"]) == sorted([dl, proc, media]) and d["error"] is None and cfg not in d["dirs"], d)
check("browse: an unmapped path gives the top level", c.get("/api/browse?path=/etc").get_json()["top"]
      and c.get("/api/browse?path=/").get_json()["top"] and c.get("/api/browse?path=" + cfg).get_json()["top"])
check("browse: a path that only starts like a mapped folder is not inside it", c.get("/api/browse?path=" + dl + "-other").get_json()["top"])
d = c.get("/api/browse?path=" + dl).get_json()
check("browse: a mapped folder's parent is the top level", d["parent"] == "" and not d.get("top"), d)
d = c.get("/api/browse?path=" + dl + "/Visible").get_json()
check("browse: inside a mapped folder goes up normally", d["parent"] == dl and not d.get("top"), d)
os.symlink("/etc", os.path.join(dl, "escape"))
os.symlink(os.path.join(dl, "Visible"), os.path.join(dl, "inside-link"))
d = c.get("/api/browse?path=" + dl).get_json()
check("browse: links that lead out of the mapped folders are hidden, links inside stay",
      "escape" not in d["dirs"] and "inside-link" in d["dirs"], d["dirs"])
check("browse: cannot walk out through a link", c.get("/api/browse?path=" + dl + "/escape").get_json()["top"])
r = c.post("/api/run-folder", json={"path": "/etc", "dry_run": True})
check("run-folder: refuses a folder outside the mapped folders", r.status_code == 400 and "mapped" in r.get_json()["error"], r.get_json())

# --- destination folders: app data, mapped folders, both directions
inner_cfg = os.path.join(cfg, "out")
s, e = settings.validate({"sources": [dl], "output_dir": inner_cfg})
check("output folder inside the app data folder is refused", any("app data folder" in x for x in e) and s["output_dir"] == "", e)
s, e = settings.validate({"sources": [dl], "source_action": "move", "move_dir": cfg})
check("move folder that is the app data folder is refused", any("app data folder" in x for x in e), e)
s, e = settings.validate({"sources": [dl], "output_dir": "/"})
check("output folder that contains the app data folder is refused", any("app data folder" in x for x in e), e)
s, e = settings.validate({"sources": [dl + "/in"], "output_dir": dl})
check("source inside the output folder is refused", any("must not be inside the output folder" in x for x in e), e)
s, e = settings.validate({"sources": [dl], "output_dir": dl})
check("output folder equal to the source stays allowed", not e, e)
roots = mounts.browse_roots(cfg)
s, e = settings.validate({"sources": ["/nowhere/x"], "output_dir": "/elsewhere", "source_action": "move",
                          "move_dir": "/else"}, roots)
check("unmapped folders are refused when roots are given", len(e) == 3 and all("mapped" in x for x in e), e)
s, e = settings.validate({"sources": ["/nowhere/x"]})
check("no roots given: not checked", not e, e)
s, e = settings.validate({"sources": [dl + "/sub/deeper"], "output_dir": media + "/o"}, roots)
check("folders inside mapped folders pass", not e, e)
r = c.post("/api/settings", json={"sources": ["/nowhere"]})
check("api/settings refuses an unmapped source", r.status_code == 400 and "mapped" in r.get_json()["errors"][0], r.get_json())
r = c.post("/api/settings", json={"sources": [dl], "output_dir": inner_cfg})
check("api/settings refuses output inside app data", r.status_code == 400, r.get_json())
r = c.post("/api/run-folder", json={"path": dl, "output_dir": "/nowhere", "dry_run": True})
check("run-folder: output folder must be mapped", r.status_code == 400 and "mapped" in r.get_json()["error"], r.get_json())
# lenient load resets exactly the bad path value, so the banner is true
open(S.SETTINGS_PATH, "w").write(json.dumps({"sources": [dl], "output_dir": "/nowhere", "move_dir": proc,
                                              "source_action": "move", "level": 3}))
cfg_now, problems, source = S.load_with_report()
check("lenient load: bad output folder is reset, the rest is kept",
      cfg_now["output_dir"] == "" and cfg_now["move_dir"] == proc and cfg_now["sources"] == [dl]
      and cfg_now["level"] == 3 and cfg_now["source_action"] == "move" and len(problems) == 1, (cfg_now, problems))
open(S.SETTINGS_PATH, "w").write(json.dumps({"sources": [dl, "/nowhere"], "source_action": "move",
                                              "move_dir": cfg + "/trash"}))
cfg_now, problems, source = S.load_with_report()
check("lenient load: the bad source is dropped, the bad move folder goes back to its default",
      cfg_now["sources"] == [dl] and cfg_now["move_dir"] == "" and cfg_now["source_action"] == S.DEFAULTS["source_action"], cfg_now)
d = c.get("/api/settings").get_json()
check("the banner problems match what was reset", len(d["problems"]) == 2 and d["settings"]["sources"] == [dl], d["problems"])
c.post("/api/settings", json={"sources": [dl], "schedule_enabled": False, "source_action": "keep"})

# --- nothing mapped: the picker says so
real_mi = mounts.MOUNTINFO
mounts.MOUNTINFO = os.path.join(root, "no-such-mountinfo")
d = c.get("/api/browse").get_json()
check("browse: nothing mapped gives an error", d["top"] and d["dirs"] == [] and "No folders are mapped" in d["error"], d)
d = c.get("/api/browse?path=" + dl).get_json()
check("browse: nothing mapped, any path gives the same error", d["top"] and "No folders are mapped" in d["error"], d)
mounts.MOUNTINFO = real_mi
check("browse: no error when folders are mapped", c.get("/api/browse").get_json()["error"] is None)

# --- clear the on-screen log
main.engine.log("info", "something")
check("log: has entries", c.get("/api/log?after=0").get_json()["entries"])
r = c.post("/api/log/clear", json={})
d = c.get("/api/log?after=0").get_json()
check("log: cleared", r.status_code == 200 and not d["entries"] and d["cleared"] == d["last"], d)

# --- scheduler: a scan that finishes between two ticks must not start the next one at once
empty = os.path.join(dl, "empty-src")
os.makedirs(os.path.join(empty, "leftover-folder"))
r = c.post("/api/settings", json={"sources": [empty], "schedule_enabled": True, "interval_minutes": 5,
                                   "run_on_start": False})
check("scheduler: settings saved", r.status_code == 200, r.get_data(as_text=True))
sch = main.Scheduler()
sch.started = time.time() - 3600             # container has been up longer than the interval
runs_before = len(main.engine.history)
sch.tick()                                   # overdue: first scan starts
if main.engine.thread:
    main.engine.thread.join(10)
for _ in range(5):                           # the scan finished between ticks
    sch.tick()
    time.sleep(0.05)
if main.engine.thread:
    main.engine.thread.join(10)
check("scheduler: no scan loop", len(main.engine.history) - runs_before == 1, len(main.engine.history) - runs_before)
check("scheduler: next scan one interval after the last one ended",
      abs(sch.next_run - (main.engine.status["finished_at"] + 300)) < 2, (sch.next_run, main.engine.status["finished_at"]))
main.engine.start(settings.load(), trigger="manual")   # a manual scan also resets the interval
main.engine.thread.join(10)
sch.tick()
check("scheduler: manual scan counts as the last scan", abs(sch.next_run - (main.engine.status["finished_at"] + 300)) < 2)
c.post("/api/settings", json={"sources": [dl], "schedule_enabled": False})

# --- appearance: saved on the server, validated
d = c.get("/api/ui").get_json()["ui"]
check("ui: defaults", d["theme"] == "default" and d["width"] == "wide", d)
r = c.post("/api/ui", json={"theme": "nord", "radius": 99, "density": "spacious", "width": "full", "font": "evil", "x": 1})
d = c.get("/api/ui").get_json()["ui"]
check("ui: saved and cleaned", d["theme"] == "nord" and d["radius"] == 24 and d["density"] == "spacious"
      and d["width"] == "full" and d["font"] == "" and "x" not in d, d)
check("ui: theme.js reachable", c.get("/static/theme.js").status_code == 200)

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
