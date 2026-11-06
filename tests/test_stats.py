"""Lifetime statistics checks. Run:  python3 -I tests/test_stats.py"""
import glob
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
from stats import Stats  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


work = tempfile.mkdtemp(prefix="ac-stats-")
path = os.path.join(work, "stats.json")
st = Stats(path)
st.archive("/src/a", 10, 4000, 1000, 2, 5, "delete", "filename")
st.archive("/src/b", 3, 2_000_000, 500_000, 1, 3, "move", "sfv", volumes=3)
st.archive("/src/c", 7, 100_000_000, 40_000_000, 4, 5, "keep", "none")
st.checksum("filename")
st.failed()
st.run_finished()
snap = st.snapshot()
t = snap["totals"]
check("archives/files/bytes counted", t["archives"] == 3 and t["files"] == 20 and t["bytes_in"] == 102_004_000
      and t["bytes_out"] == 40_501_000, t)
check("what happened to the folders", t["freed"] == 4000 and t["moved"] == 2_000_000 and t["kept"] == 100_000_000, t)
check("checksums, volumes, failures, runs", t["crc_names"] == 2 and t["sfv"] == 1 and t["volumes"] == 3
      and t["failed"] == 1 and t["runs"] == 1 and t["seconds"] == 7, t)
day = snap["days"][time.strftime("%Y-%m-%d")]
check("per day", day["archives"] == 3 and day["files"] == 20 and day["failed"] == 1, day)
check("per level", snap["levels"]["5"]["n"] == 2 and snap["levels"]["3"]["in"] == 2_000_000, snap["levels"])
r = snap["records"]
check("records", r["largest"]["value"] == 100_000_000 and r["largest"]["path"] == "/src/c"
      and r["most_files"]["value"] == 10 and r["best_ratio"]["path"] == "/src/b" and r["fastest"]["path"] == "/src/c", r)
check("snapshot is a copy", snap is not st.data and snap["totals"] is not st.data["totals"])

st2 = Stats(path)
check("persisted across a reload", st2.snapshot()["totals"] == t and st2.snapshot()["records"]["largest"]["value"] == 100_000_000)

# concurrent saves: one file, always valid, no temp files left behind
errors = []


def hammer(n):
    try:
        for i in range(40):
            st2.archive(f"/src/t{n}-{i}", 1, 10, 5, 0, 3, "delete", "none")
            st2.save()
    except Exception as e:  # noqa: BLE001
        errors.append(e)


ths = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
for th in ths:
    th.start()
for th in ths:
    th.join()
st2.save()
try:
    with open(path) as f:
        saved = json.load(f)
    valid = saved["totals"]["archives"] == 3 + 8 * 40
except (OSError, ValueError, KeyError) as e:
    valid, saved = False, e
check("concurrent saves leave a complete, valid file", not errors and valid, (errors, saved if not valid else ""))
check("no temp files left behind", sorted(os.listdir(work)) == ["stats.json"], os.listdir(work))

# a damaged or odd file never stops the app
for name, content in (("not json", "{nope"), ("list", "[1, 2]"), ("empty", "")):
    open(path, "w").write(content)
    check(f"unreadable file ({name}) starts blank", Stats(path).snapshot()["totals"]["archives"] == 0)
open(path, "w").write(json.dumps({"since": "x", "totals": [1], "levels": 5, "days": {"2026-01-01": "bad", "2026-01-02": {"archives": "n"}},
                                  "records": {"largest": 7, "most_files": {"value": 3, "path": "/p", "unit": "files", "at": 1}}}))
odd = Stats(path)
odd.archive("/x", 1, 1, 1, 0, 3, "delete", "none")
odd.failed()
s = odd.snapshot()
check("wrong-typed keys are replaced, valid parts kept", s["totals"]["archives"] == 1 and isinstance(s["since"], float)
      and "2026-01-01" not in s["days"] and s["days"]["2026-01-02"]["archives"] == 0 and s["records"]["largest"]["value"] == 1
      and s["records"]["most_files"]["value"] == 3, s)
open(path, "w").write(json.dumps({"totals": {"archives": "many", "files": 4}}))
check("wrong-typed counters become 0", Stats(path).snapshot()["totals"]["archives"] == 0
      and Stats(path).snapshot()["totals"]["files"] == 4)

st2.reset()
check("reset", st2.snapshot()["totals"]["archives"] == 0 and not st2.snapshot()["records"]
      and Stats(path).snapshot()["totals"]["archives"] == 0)
shutil.rmtree(work)

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
