"""End-to-end checks for the compression engine, using the real rar program.
Run:  python3 -I tests/test_engine.py"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from engine import Engine, parse_sfv  # noqa: E402
import settings  # noqa: E402

FAILS = []
RAR = shutil.which("rar")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


if not RAR:
    print("rar is not installed - cannot run these tests")
    sys.exit(2)


def base_cfg(**kw):
    cfg = dict(settings.DEFAULTS)
    cfg.update(min_age_minutes=0, recovery_percent=15, level=5, source_action="delete")
    cfg.update(kw)
    clean, errors = settings.validate(cfg)
    assert not errors, errors
    return clean


def run(engine, cfg, dry=False, trigger="manual", verify=False, timeout=120):
    assert engine.start(cfg, trigger, dry, verify)
    engine.thread.join(timeout)
    assert not engine.running
    return engine.snapshot()


def fresh():
    work = tempfile.mkdtemp(prefix="ac-test-")
    return work, Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))


def tree(root):
    out = []
    for d, ds, fs in os.walk(root):
        for f in fs:
            out.append(os.path.relpath(os.path.join(d, f), root))
        for x in ds:
            if os.path.islink(os.path.join(d, x)):
                out.append(os.path.relpath(os.path.join(d, x), root) + "@")
    return sorted(out)


def make_folders(src):
    os.makedirs(f"{src}/My Show/sub", exist_ok=True)
    os.makedirs(f"{src}/My Show/.hid", exist_ok=True)
    os.makedirs(f"{src}/Plain", exist_ok=True)
    with open(f"{src}/My Show/big.bin", "wb") as f:
        f.write(os.urandom(1_500_000))
    for p, t in (("My Show/sub/a.txt", "hello"), ("My Show/.hid/h.txt", "hidden"), ("My Show/Thumbs.db", "t"),
                 ("My Show/sub/Thumbs.db", "t2"), ("Plain/p.txt", "plain" * 100), ("loose.txt", "not a folder")):
        with open(f"{src}/{p}", "w") as f:
            f.write(t)
    os.symlink("a.txt", f"{src}/My Show/sub/link")
    os.makedirs(f"{src}/Empty", exist_ok=True)


def rars(d):
    return sorted(f for f in os.listdir(d) if f.lower().endswith(".rar"))


def crc(path):
    return "%08X" % (zlib.crc32(open(path, "rb").read()) & 0xFFFFFFFF)


def extract_to(archive, dest, pw=None):
    os.makedirs(dest, exist_ok=True)
    r = subprocess.run([RAR, "x", "-idq", "-y"] + ([f"-hp{pw}"] if pw else []) + [archive, dest + "/"],
                       capture_output=True, text=True)
    return r.returncode == 0


def fake_rar(work, body):
    """A rar wrapper: runs the real rar, then `body` (shell) when the command is `a`."""
    p = os.path.join(work, "fakerar.sh")
    with open(p, "w") as f:
        f.write(f'#!/bin/bash\n"{RAR}" "$@"; rc=$?\nif [ "$1" = a ]; then\n{body}\nfi\nexit $rc\n')
    os.chmod(p, 0o755)
    return p


def test_basic_filename_crc():
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    orig = {n: tree(f"{src}/{n}") for n in ("My Show", "Plain")}
    snap = os.path.join(work, "orig")
    shutil.copytree(src, snap, symlinks=True)
    s = run(eng, base_cfg(sources=[src]))
    t = os.listdir(src)
    r = rars(src)
    check("basic: two archives named 'name [CRC].rar'",
          len(r) == 2 and all(re.fullmatch(r".+ \[[0-9A-F]{8}\]\.rar", x) for x in r), r)
    check("basic: CRC in the name is the CRC of the file",
          all(re.search(r"\[([0-9A-F]{8})\]", x).group(1) == crc(f"{src}/{x}") for x in r))
    check("basic: folders deleted, loose file and empty folder untouched",
          "My Show" not in t and "Plain" not in t and "loose.txt" in t and "Empty" in t, t)
    check("basic: counters", (s["found"], s["compressed"], s["failed"]) == (2, 2, 0) and s["result"] == "completed", s)
    check("basic: no work folder left", not [x for x in t if x.startswith(".autocompress-")], t)
    arc = [x for x in r if x.startswith("My Show")][0]
    ok = extract_to(f"{src}/{arc}", os.path.join(work, "x"))
    check("basic: archive extracts and matches the original folder (incl. hidden file and symlink)",
          ok and tree(os.path.join(work, "x", "My Show")) == orig["My Show"] == tree(f"{snap}/My Show"),
          (tree(os.path.join(work, "x")), orig["My Show"]))
    check("basic: archive has a recovery record",
          "recovery record" in subprocess.run([RAR, "lt", f"{src}/{arc}"], capture_output=True, text=True).stdout)
    check("basic: permissions 666", all(stat.S_IMODE(os.stat(f"{src}/{x}").st_mode) == 0o666 for x in r))
    st = eng.stats.snapshot()["totals"]
    check("basic: statistics", st["archives"] == 2 and st["crc_names"] == 2 and st["freed"] > 0 and st["failed"] == 0, st)
    shutil.rmtree(work)


def test_sfv_and_none():
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    run(eng, base_cfg(sources=[src], checksum_mode="sfv", source_action="keep"))
    check("sfv: name.rar + name.sfv", {"My Show.rar", "My Show.sfv", "Plain.rar", "Plain.sfv"} <= set(os.listdir(src)), os.listdir(src))
    got = dict(parse_sfv(f"{src}/My Show.sfv"))
    check("sfv: entry matches the archive CRC", got == {"My Show.rar": crc(f"{src}/My Show.rar")}, got)
    check("keep: folders still there", os.path.isdir(f"{src}/My Show") and os.path.isdir(f"{src}/Plain"))
    s = run(eng, base_cfg(sources=[src], checksum_mode="sfv", source_action="keep"))
    check("keep: second run skips folders that already have an archive", s["compressed"] == 0 and s["skipped"] == 2, s)
    shutil.rmtree(work)
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    run(eng, base_cfg(sources=[src], checksum_mode="none"))
    check("none: plain names, no sfv", sorted(x for x in os.listdir(src) if x != "loose.txt" and x != "Empty") == ["My Show.rar", "Plain.rar"], os.listdir(src))
    shutil.rmtree(work)


def test_move_output_mirror():
    work, eng = fresh()
    src, mv, out = (os.path.join(work, d) for d in ("src", "trash", "out"))
    make_folders(src)
    run(eng, base_cfg(sources=[src], source_action="move", move_dir=mv, output_dir=out, output_layout="mirror"))
    check("output mirror: archives in <out>/<source name>/", len(rars(f"{out}/src")) == 2 and not rars(src), (os.listdir(out), os.listdir(src)))
    check("move: folders moved with their contents", os.path.isfile(f"{mv}/src/My Show/sub/a.txt") and not os.path.exists(f"{src}/Plain"))
    make_folders(src)
    os.remove(f"{out}/src/" + [x for x in rars(f"{out}/src") if x.startswith("Plain")][0])
    run(eng, base_cfg(sources=[src], source_action="move", move_dir=mv, output_dir=out, output_layout="mirror"))
    check("move: a clash gets a .1 suffix", os.path.isdir(f"{mv}/src/Plain.1"), os.listdir(f"{mv}/src"))
    shutil.rmtree(work)
    work, eng = fresh()
    src, out = os.path.join(work, "src"), os.path.join(work, "out")
    make_folders(src)
    run(eng, base_cfg(sources=[src], output_dir=out))
    check("output flat: archives in the output folder", len(rars(out)) == 2 and not rars(src))
    shutil.rmtree(work)


def test_dry_run_age_and_skips():
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    before = tree(src)
    s = run(eng, base_cfg(sources=[src]), dry=True)
    check("dry run: nothing changed", tree(src) == before and not rars(src))
    check("dry run: reported", any("[dry run] would compress" in e["msg"] for e in eng.log_since(0, 1000)))
    s = run(eng, base_cfg(sources=[src], min_age_minutes=30))
    check("min age: fresh folders are skipped, not compressed", s["compressed"] == 0 and s["deferred"] == 2 and not rars(src), s)
    old = time.time() - 3 * 3600
    for d, ds, fs in os.walk(src):
        for x in ds + fs:
            os.utime(os.path.join(d, x), (old, old), follow_symlinks=False)
        os.utime(d, (old, old))
    s = run(eng, base_cfg(sources=[src], min_age_minutes=30))
    check("min age: old folders are compressed", s["compressed"] == 2, s)
    shutil.rmtree(work)


def test_volumes_password_excludes():
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    with open(f"{src}/My Show/big.bin", "wb") as f:
        f.write(os.urandom(3_000_000))
    s = run(eng, base_cfg(sources=[src], volume_mb=1, level=1))
    names = rars(src)
    check("volumes: split into name.partN.rar, SFV instead of CRC names",
          [x for x in names if x.startswith("My Show")] == ["My Show.part1.rar", "My Show.part2.rar", "My Show.part3.rar", "My Show.part4.rar"][:len([x for x in names if x.startswith("My Show")])]
          and len([x for x in names if x.startswith("My Show")]) >= 3 and os.path.exists(f"{src}/My Show.sfv"), names)
    got = dict(parse_sfv(f"{src}/My Show.sfv"))
    check("volumes: SFV lists every volume with the right CRC",
          set(got) == {x for x in names if x.startswith("My Show")} and all(crc(f"{src}/{k}") == v for k, v in got.items()), got)
    check("volumes: the set extracts", extract_to(f"{src}/My Show.part1.rar", os.path.join(work, "x")) and os.path.isfile(f"{work}/x/My Show/big.bin"))
    shutil.rmtree(work)

    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    s = run(eng, base_cfg(sources=[src], password="s3cr3t pw", encrypt_names=True, exclude_files=["Thumbs.db", "*.tmp"]))
    arc = [x for x in rars(src) if x.startswith("My Show")][0]
    check("password: compressed OK", s["compressed"] == 2 and s["failed"] == 0, s)
    check("password: wrong password fails, right one extracts",
          not extract_to(f"{src}/{arc}", os.path.join(work, "bad"), "nope") and extract_to(f"{src}/{arc}", os.path.join(work, "x"), "s3cr3t pw"))
    check("password: is not written to the log", not any("s3cr3t" in e["msg"] for e in eng.log_since(0, 2000)))
    x = tree(os.path.join(work, "x", "My Show"))
    check("excludes: Thumbs.db left out at every level, other files kept",
          not any("Thumbs.db" in p for p in x) and "sub/a.txt" in x, x)
    shutil.rmtree(work)


def test_safety_nets():
    # 1. archive is damaged after rar finished -> test fails -> folder untouched, nothing left behind
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    eng.tools["rar"] = fake_rar(work, 'truncate -s 100 "${@: -2:1}"')
    s = run(eng, base_cfg(sources=[src]))
    check("damaged archive: not removed, reported",
          s["failed"] == 2 and s["compressed"] == 0 and os.path.isdir(f"{src}/My Show") and os.path.isdir(f"{src}/Plain") and not rars(src)
          and not [x for x in os.listdir(src) if x.startswith(".autocompress-")], (s["failures"], os.listdir(src)))
    # failed folders are not retried by the scheduler until they change, but a manual run tries again
    n = len(eng.log_since(0, 5000))
    s2 = run(eng, base_cfg(sources=[src]), trigger="schedule")
    check("failed earlier: scheduled scan skips unchanged folders", s2["found"] == 0 and s2["skipped"] == 2, s2)
    s3 = run(eng, base_cfg(sources=[src]), trigger="manual")
    check("failed earlier: manual run retries", s3["found"] == 2, s3)
    shutil.rmtree(work)
    # 2. archive misses files -> count check fails

    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    wrapper = os.path.join(work, "skiptxt.sh")
    with open(wrapper, "w") as f:
        f.write(f'#!/bin/bash\nif [ "$1" = a ]; then exec "{RAR}" a -x"*.txt" -x"*/*.txt" "${{@:2}}"; fi\nexec "{RAR}" "$@"\n')
    os.chmod(wrapper, 0o755)
    eng.tools["rar"] = wrapper
    s = run(eng, base_cfg(sources=[src]))
    check("file count differs: folder kept", s["failed"] == 2 and os.path.isdir(f"{src}/My Show") and not rars(src)
          and "holds" in s["failures"][0]["error"], s["failures"])
    shutil.rmtree(work)

    # 3. folder changes while it is being compressed -> archive discarded, folder untouched
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    eng.tools["rar"] = fake_rar(work, f'echo late > "{src}/Plain/late.txt"; echo late > "{src}/My Show/late.txt"')
    s = run(eng, base_cfg(sources=[src]))
    check("changed while compressing: archive discarded, folder kept",
          s["compressed"] == 0 and os.path.isfile(f"{src}/Plain/late.txt") and not rars(src), (s["failures"], os.listdir(src)))
    shutil.rmtree(work)

    # 4. Stop kills rar, removes the work folder, leaves the folder alone
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    slow = os.path.join(work, "slow.sh")
    with open(slow, "w") as f:
        f.write(f'#!/bin/bash\nif [ "$1" = a ]; then sleep 60; fi\nexec "{RAR}" "$@"\n')
    os.chmod(slow, 0o755)
    eng.tools["rar"] = slow
    eng.start(base_cfg(sources=[src]), "manual", False)
    time.sleep(2)
    t0 = time.time()
    eng.stop()
    eng.thread.join(20)
    s = eng.snapshot()
    check("stop: ends quickly, result stopped, folders untouched",
          not eng.running and time.time() - t0 < 15 and s["result"] == "stopped" and os.path.isdir(f"{src}/My Show")
          and not [x for x in os.listdir(src) if x.startswith(".autocompress-")], (s["result"], os.listdir(src)))
    shutil.rmtree(work)

    # 5. odd names and no space
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(f"{src}/star*name")
    open(f"{src}/star*name/a", "w").write("x")
    os.makedirs(f"{src}/@list")
    open(f"{src}/@list/a", "w").write("x")
    os.makedirs(f"{src}/real_dir")
    open(f"{src}/real_dir/a", "w").write("x")
    os.symlink(f"{src}/real_dir", f"{src}/link_to_dir")
    s = run(eng, base_cfg(sources=[src]))
    check("odd names: * and @ names are refused with a reason, symlinked folders ignored",
          s["compressed"] == 1 and s["failed"] == 2 and os.path.isdir(f"{src}/star*name") and os.path.islink(f"{src}/link_to_dir"), s["failures"])
    shutil.rmtree(work)


def test_existing_archives_and_verify():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    for n, data in (("plain.rar", b"aaa"), ("has crc [DEADBEEF].rar", b"b"), ("sp ace.RAR", b"ccc"),
                    ("set.part1.rar", b"1"), ("set.part2.rar", b"2"), ("old.rar", b"o"), ("old.r00", b"o0"), (".hidden.rar", b"h")):
        open(f"{src}/{n}", "wb").write(data)
    s = run(eng, base_cfg(sources=[src], checksum_existing=True), dry=True)
    check("existing (dry run): nothing renamed", sorted(os.listdir(src))[0] == ".hidden.rar" and "plain.rar" in os.listdir(src))
    s = run(eng, base_cfg(sources=[src], checksum_existing=True))
    names = sorted(os.listdir(src))
    check("existing: CRC added to single archives only",
          f"plain [{'%08X' % (zlib.crc32(b'aaa') & 0xFFFFFFFF)}].rar" in names and "set.part1.rar" in names and "old.rar" in names
          and "has crc [DEADBEEF].rar" in names and ".hidden.rar" in names and s["crc_added"] == 2, (names, s["crc_added"]))
    s = run(eng, base_cfg(sources=[src], checksum_existing=True))
    check("existing: second run changes nothing", s["crc_added"] == 0)
    s = run(eng, base_cfg(sources=[src], checksum_existing=True, checksum_mode="sfv"))
    names = sorted(os.listdir(src))
    check("existing (sfv): an SFV for single archives and for volume sets",
          "set.sfv" in names and "old.sfv" in names and any(n.endswith(".sfv") and n.startswith("plain") for n in names), names)
    check("existing (sfv): set SFV lists both volumes", [f for f, _ in parse_sfv(f"{src}/set.sfv")] == ["set.part1.rar", "set.part2.rar"])
    # verify mode
    s = run(eng, base_cfg(sources=[src]), verify=True)
    v = s["verified"]
    check("verify: all good except the file with the made-up CRC", v["bad"] == 1 and v["missing"] == 0 and v["ok"] >= 5
          and "DEADBEEF" in s["failures"][0]["path"], (v, s["failures"]))
    open(f"{src}/set.part2.rar", "wb").write(b"changed")
    os.remove(f"{src}/old.rar")
    s = run(eng, base_cfg(sources=[src]), verify=True)
    v = s["verified"]
    check("verify: damaged and missing files are reported", v["bad"] >= 2 and v["missing"] >= 1 and s["failed"] >= 3, (v, s["failures"]))
    shutil.rmtree(work)


def test_parallel():
    work, eng = fresh()
    src = os.path.join(work, "src")
    for i in range(6):
        os.makedirs(f"{src}/folder{i}")
        open(f"{src}/folder{i}/f.bin", "wb").write(os.urandom(200_000))
    s = run(eng, base_cfg(sources=[src], parallel_jobs=3, rar_threads=1))
    check("parallel: six folders, three at a time", s["compressed"] == 6 and s["failed"] == 0 and len(rars(src)) == 6, s)
    shutil.rmtree(work)


def run_oneoff(eng, cfg, path, mode, dry=False):
    assert eng.start(cfg, "one-off", dry, False, oneoff={"path": path, "mode": mode})
    eng.thread.join(120)
    assert not eng.running
    return eng.snapshot()


def test_oneoff():
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    os.makedirs(f"{src}/Skipme/x", exist_ok=True)           # excluded by name in scheduled scans
    with open(f"{src}/Skipme/x/f.txt", "w") as f:
        f.write("data")
    # no configured sources at all: a one-off does not need them
    cfg = base_cfg(sources=[], exclude_dirs=["Skipme"])
    run_oneoff(eng, cfg, f"{src}/Plain", "folder", dry=True)
    check("one-off: dry run changes nothing", os.path.isdir(f"{src}/Plain") and not rars(src))
    run_oneoff(eng, cfg, f"{src}/Plain", "folder")
    check("one-off folder: only that folder is compressed and removed",
          len(rars(src)) == 1 and rars(src)[0].startswith("Plain [") and not os.path.exists(f"{src}/Plain")
          and os.path.isdir(f"{src}/My Show"), (rars(src), os.listdir(src)))
    run_oneoff(eng, cfg, f"{src}/Skipme", "folder")
    check("one-off folder: ignores the exclude list when picked on purpose",
          any(x.startswith("Skipme") for x in rars(src)), rars(src))
    check("one-off: recorded in history as one-off", eng.history[-1]["trigger"] == "one-off", eng.history[-1]["trigger"])
    mv = os.path.join(work, "trash")
    run_oneoff(eng, base_cfg(sources=[], source_action="move", move_dir=mv), src, "children")
    check("one-off children: every sub-folder gets its own archive",
          any(x.startswith("My Show") for x in rars(src)) and os.path.isfile(f"{mv}/src/My Show/sub/a.txt"), (rars(src), os.listdir(src)))
    shutil.rmtree(work)


def fake_mount(path, root, fstype="fuse.shfs", device="shfs"):
    return {"path": path, "fstype": fstype, "root": root, "device": device, "host": None, "config": False, "writable": True}


def test_recycle():
    import mounts
    # path resolution
    m_share = [fake_mount("/source", "/Media")]
    check("recycle path: mapped share root", mounts.recycle_target("/source/Show", m_share) == ("/source/.Recycle.Bin", None))
    check("recycle path: sub-folders keep their tree", mounts.recycle_target("/source/A/B/Show", m_share) == ("/source/.Recycle.Bin/A/B", None))
    check("recycle root: share root bin", mounts.recycle_root("/source/Show", m_share) == ("/source/.Recycle.Bin", None))
    check("recycle root: via all-shares mapping", mounts.recycle_root("/mnt/user/Media/A/Show", [fake_mount("/mnt/user", "/")]) == ("/mnt/user/Media/.Recycle.Bin", None))
    m_all = [fake_mount("/mnt/user", "/")]
    check("recycle path: all shares mapped", mounts.recycle_target("/mnt/user/Media/A/Show", m_all) == ("/mnt/user/Media/.Recycle.Bin/A", None))
    check("recycle path: top of all shares refused", mounts.recycle_target("/mnt/user", m_all)[1] is not None)
    m_sub = [fake_mount("/source", "/Media/Inbox"), fake_mount("/mnt/user", "/")]
    check("recycle path: share root found through the all-shares mapping",
          mounts.recycle_target("/source/Show", m_sub) == ("/mnt/user/Media/.Recycle.Bin/Inbox", None), mounts.recycle_target("/source/Show", m_sub))
    check("recycle path: unreachable share root gives a reason", mounts.recycle_target("/source/Show", m_sub[:1])[1] is not None)
    check("recycle path: unmapped path gives a reason", mounts.recycle_target("/elsewhere/x", m_sub)[1] is not None)

    # the engine: fake the mapping so a temp folder acts as a share root
    work, eng = fresh()
    share = os.path.join(work, "Media")
    make_folders(share)
    real = mounts.list_mounts
    mounts.list_mounts = lambda *a, **k: [fake_mount(share, "/Media")]
    try:
        run(eng, base_cfg(sources=[share], source_action="recycle"))
        check("recycle: folder moved into <share>/.Recycle.Bin", os.path.isfile(f"{share}/.Recycle.Bin/My Show/sub/a.txt")
              and os.path.isdir(f"{share}/.Recycle.Bin/Plain") and not os.path.exists(f"{share}/Plain"), os.listdir(share))
        check("recycle: archives created", len(rars(share)) == 2, rars(share))
        make_folders(share)
        os.remove(f"{share}/" + [x for x in rars(share) if x.startswith("Plain")][0])
        run(eng, base_cfg(sources=[share], source_action="recycle"))
        check("recycle: a clash in the bin gets a .1 suffix", os.path.isdir(f"{share}/.Recycle.Bin/Plain.1"), os.listdir(f"{share}/.Recycle.Bin"))
        log = " ".join(x["msg"] for x in eng.log_since(0)[0]) if isinstance(eng.log_since(0), tuple) else str(eng.log_since(0))
        check("recycle: log says where the folder went", "moved to the Recycle Bin: " + share in log and "go to the Recycle Bin" in log, log[-400:])
        check("recycle: .Recycle.Bin itself is never compressed", not any(x.startswith(".Recycle") for x in rars(share)))
    finally:
        mounts.list_mounts = real
    shutil.rmtree(work)

    # not reachable: the run fails up front and nothing is touched
    work, eng = fresh()
    src = os.path.join(work, "src")
    make_folders(src)
    mounts.list_mounts = lambda *a, **k: [fake_mount(src, "/Media/Inbox")]
    try:
        s = run(eng, base_cfg(sources=[src], source_action="recycle"))
    finally:
        mounts.list_mounts = real
    check("recycle: unreachable share root fails before touching anything",
          s["result"] == "error" and os.path.isdir(f"{src}/Plain") and not rars(src), (s["result"], s["message"]))
    shutil.rmtree(work)


def test_settings():
    cfg = settings.DEFAULTS
    c, e = settings.validate({**cfg, "sources": ["/a"], "output_dir": "/a/out"})
    check("settings: output folder inside a source is refused", any("must not be inside" in x for x in e), e)
    c, e = settings.validate({**cfg, "sources": ["/a"], "source_action": "move"})
    check("settings: move needs a folder", any("move folder is required" in x for x in e), e)
    c, e = settings.validate({**cfg, "sources": ["/a/b"], "source_action": "move", "move_dir": "/a"})
    check("settings: source inside the move folder is refused", any("must not be inside the move folder" in x for x in e), e)
    c, e = settings.validate({**cfg, "level": 9, "recovery_percent": 500, "checksum_mode": "md5"})
    check("settings: bad values are reported", len(e) == 3, e)
    d = tempfile.mkdtemp(prefix="ac-set-")
    settings.CONFIG_DIR, settings.SETTINGS_PATH = d, os.path.join(d, "settings.json")
    saved, err = settings.save({**cfg, "sources": ["/a"], "password": "hunter2"})
    again, err2 = settings.save({**cfg, "sources": ["/a"], "level": 3})
    check("settings: a blank password keeps the saved one", saved and again and again["password"] == "hunter2" and again["level"] == 3, (err, err2))
    cleared, _ = settings.save({**cfg, "sources": ["/a"], "clear_password": True})
    check("settings: password can be removed", cleared["password"] == "")
    saved, _ = settings.save({**cfg, "sources": ["/a"], "password": "hunter2"})
    check("settings: public view hides the password", "password" not in settings.public(saved) and settings.public(saved)["password_set"] is True)
    check("settings: default folder action is the Recycle Bin", settings.DEFAULTS["source_action"] == "recycle")
    check("settings: file is private (0600)", stat.S_IMODE(os.stat(settings.SETTINGS_PATH).st_mode) == 0o600)
    shutil.rmtree(d)


for t in (test_basic_filename_crc, test_sfv_and_none, test_move_output_mirror, test_dry_run_age_and_skips,
          test_volumes_password_excludes, test_safety_nets, test_existing_archives_and_verify, test_parallel,
          test_oneoff, test_recycle, test_settings):
    print(f"--- {t.__name__}")
    try:
        t()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{t.__name__} ran without exception", False, str(e))

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
